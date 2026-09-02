#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for server/events.py - constructed data, touches nothing.

Event detection fails the same way the old reboot rule did: no exception, no
crash, it just quietly never fires. Each test below pins one transition.

    python3 tests/test_events.py
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import events  # noqa: E402

T0 = datetime(2026, 9, 1, 0, 0, 0)


def sample(ts, reachable=True, sshd=True, tcp=True, termux=True, bridge=True,
           charging=True, level=80, batt_temp=32.0, cpu_temp=40.0,
           pid=4242, uptime=10000):
    raw = {}
    if reachable:
        raw = {
            "timestamp": ts.isoformat(),
            "android": {"uptime_seconds": uptime},
            "services": {"sshd": sshd, "tcp_8022": tcp, "termux": termux,
                         "monitor_bridge": bridge, "sshd_pid": pid},
            "battery": {"charging": charging, "level": level,
                        "temperature_c": batt_temp},
            "cpu": {"temperature_c": cpu_temp},
        }
    e = {"ts": ts.isoformat(), "status": "NORMAL", "alerts": [], "raw": raw}
    if not reachable:
        e["error"] = "ssh rc=255"
    return e


def series(specs):
    """specs: list of (offset_seconds, kwargs)."""
    return [sample(T0 + timedelta(seconds=off), **kw) for off, kw in specs]


class EventTest(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="events-test-"))
        self.engine = events.EventEngine(events_path=self.dir / "events.jsonl",
                                         state_path=self.dir / "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def types(self):
        return [e["type"] for e in self.engine.emitted]

    # --- debounce ---------------------------------------------------------
    def test_single_dropped_poll_is_not_an_outage(self):
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {}), (900, {}),
        ]))
        self.assertNotIn("network_down", self.types())

    def test_two_consecutive_failures_are_an_outage(self):
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}), (1200, {}),
        ]))
        self.assertIn("network_down", self.types())
        self.assertIn("network_recovered", self.types())
        down = [e for e in self.engine.emitted if e["type"] == "network_down"][0]
        rec = [e for e in self.engine.emitted if e["type"] == "network_recovered"][0]
        # 'time' = confirmation moment (2nd failure, debounce satisfied)
        # 'since' = true onset (first failing observation)
        self.assertEqual(down["time"], (T0 + timedelta(seconds=600)).isoformat())
        self.assertEqual(down["since"], (T0 + timedelta(seconds=300)).isoformat())
        # recovery confirmed at 1200s (2nd good sample), measured from true onset 300s
        self.assertEqual(rec["duration_sec"], 900)

    def test_recovery_needs_two_good_samples(self):
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}),
        ]))
        self.assertIn("network_down", self.types())
        self.assertNotIn("network_recovered", self.types())

    # --- ssh layer vs network layer ---------------------------------------
    def test_ssh_layer_fault_is_distinct_from_device_unreachable(self):
        """Device reachable but its own sshd reports down -> ssh_down only."""
        self.engine.process(series([
            (0, {}), (300, {"sshd": False, "tcp": False}),
            (600, {"sshd": False, "tcp": False}), (900, {}), (1200, {}),
        ]))
        self.assertIn("ssh_down", self.types())
        self.assertNotIn("network_down", self.types())
        self.assertIn("ssh_recovered", self.types())

    # --- restarts ---------------------------------------------------------
    def test_sshd_pid_change_without_reboot_emits_restart(self):
        self.engine.process(series([(0, {}), (300, {"pid": 9999})]))
        self.assertIn("sshd_restart", self.types())

    def test_reboot_suppresses_sshd_restart_and_emits_android_reboot(self):
        self.engine.process(series([
            (0, {"uptime": 30000}),
            (300, {"uptime": 150, "pid": 9999}),   # uptime regressed -> reboot
        ]))
        self.assertIn("android_reboot", self.types())
        self.assertNotIn("sshd_restart", self.types())

    def test_watchdog_marker_makes_recovery_automatic(self):
        """A device-side restart marker inside the outage window => automatic."""
        self.engine.state["restart_markers"] = [
            (T0 + timedelta(seconds=400)).isoformat()]
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}), (1200, {}),
        ]))
        rec = [e for e in self.engine.emitted if e["type"] == "network_recovered"][0]
        self.assertEqual(rec["recovery_type"], "automatic")

    def test_recovery_without_marker_is_unknown_never_manual(self):
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}), (1200, {}),
        ]))
        rec = [e for e in self.engine.emitted if e["type"] == "network_recovered"][0]
        self.assertEqual(rec["recovery_type"], "unknown")

    # --- charging ---------------------------------------------------------
    def test_charging_transitions(self):
        self.engine.process(series([
            (0, {"charging": True}), (300, {"charging": False}),
            (600, {"charging": True}),
        ]))
        self.assertEqual(self.types().count("charging_stopped"), 1)
        self.assertEqual(self.types().count("charging_started"), 1)

    def test_steady_charging_emits_nothing(self):
        self.engine.process(series([(0, {"charging": True}),
                                    (300, {"charging": True}),
                                    (600, {"charging": True})]))
        self.assertEqual(self.types(), ["monitor_started", "android_seen"])

    # --- temperature hysteresis ------------------------------------------
    def test_battery_temp_hysteresis_and_cooldown(self):
        """43.0 enters; 42.5 must NOT re-enter (still hot); 40 exits; re-entry
        inside the 15 min cooldown is suppressed."""
        self.engine.process(series([
            (0, {"batt_temp": 35.0}),
            (300, {"batt_temp": 43.0}),      # enter
            (600, {"batt_temp": 42.5}),      # still hot -> no new event
            (900, {"batt_temp": 40.0}),      # exit (no event)
            (1100, {"batt_temp": 44.0}),     # inside cooldown -> suppressed
        ]))
        self.assertEqual(self.types().count("high_battery_temp"), 1)

    def test_battery_temp_reemits_after_cooldown(self):
        self.engine.process(series([
            (0, {"batt_temp": 35.0}),
            (300, {"batt_temp": 43.0}),      # enter
            (900, {"batt_temp": 40.0}),      # exit
            (3000, {"batt_temp": 45.0}),     # > 15 min later -> new event
        ]))
        self.assertEqual(self.types().count("high_battery_temp"), 2)

    def test_cpu_temp_threshold_is_independent(self):
        self.engine.process(series([(0, {"cpu_temp": 61.0}),
                                    (300, {"cpu_temp": 61.0})]))
        self.assertEqual(self.types().count("high_cpu_temp"), 1)
        self.assertEqual(self.types().count("high_battery_temp"), 0)

    # --- bridge / termux --------------------------------------------------
    def test_bridge_down_requires_two_samples(self):
        self.engine.process(series([
            (0, {}), (300, {"bridge": False}), (600, {"bridge": False}),
            (900, {}), (1200, {}),
        ]))
        self.assertIn("bridge_down", self.types())
        self.assertIn("bridge_recovered", self.types())

    def test_termux_down(self):
        self.engine.process(series([
            (0, {}), (300, {"termux": False}), (600, {"termux": False}),
            (900, {}), (1200, {}),
        ]))
        self.assertIn("termux_down", self.types())

    # --- idempotency ------------------------------------------------------
    def test_reprocessing_emits_nothing_new(self):
        data = series([(0, {}), (300, {"reachable": False}),
                       (600, {"reachable": False}), (900, {}), (1200, {})])
        self.engine.process(data)
        first = len(self.engine.emitted)
        self.engine.process(data)                     # same samples again
        self.assertEqual(len(self.engine.emitted), first)

    def test_processing_is_incremental_across_restarts(self):
        self.engine.process(series([(0, {}), (300, {})]))
        n1 = len(self.engine.emitted)
        eng2 = events.EventEngine(events_path=self.dir / "events.jsonl",
                                  state_path=self.dir / "state.json")
        eng2.process(series([(0, {}), (300, {}), (600, {})]))
        self.assertEqual(len(eng2.emitted), 0)        # nothing new to say
        self.assertEqual(n1, 2)

    # --- device-side merge ------------------------------------------------
    def test_device_watchdog_event_is_merged_once(self):
        dev = [json.dumps({"time": (T0 + timedelta(seconds=150)).isoformat(),
                           "type": "watchdog_restart", "pid": 777})]
        self.assertEqual(self.engine.merge_device_events(dev), 1)
        self.assertIn("watchdog_restart", self.types())
        self.assertEqual(self.engine.merge_device_events(dev), 0)   # dedup

    def test_merged_marker_upgrades_recovery_to_automatic(self):
        dev = [json.dumps({"time": (T0 + timedelta(seconds=400)).isoformat(),
                           "type": "watchdog_restart", "pid": 777})]
        self.engine.merge_device_events(dev)
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}), (1200, {}),
        ]))
        rec = [e for e in self.engine.emitted if e["type"] == "network_recovered"][0]
        self.assertEqual(rec["recovery_type"], "automatic")

    # --- summary ----------------------------------------------------------
    def test_summary_counters(self):
        self.engine.process(series([
            (0, {}), (300, {"reachable": False}), (600, {"reachable": False}),
            (900, {}), (1200, {}),                       # outage 1
            (1500, {"reachable": False}), (1800, {"reachable": False}),
            (2100, {}), (2400, {}),                      # outage 2
        ]))
        s = events.summarize_events(events.load_events(
            limit=10 ** 9, path=self.dir / "events.jsonl"))
        self.assertEqual(s["fault_count"], 2)
        self.assertEqual(s["recovered_count"], 2)
        self.assertEqual(s["manual_count"], 0)
        self.assertEqual(s["unknown_count"], 2)
        self.assertEqual(s["auto_recovery_pct"], 0.0)
        self.assertEqual(s["avg_recovery_seconds"], 900)
        self.assertEqual(s["max_recovery_seconds"], 900)

    def test_heartbeat_is_rate_limited(self):
        """24h of steady samples must not emit 288 heartbeats."""
        self.engine.process(series([(i * 300, {}) for i in range(288)]))
        self.assertEqual(self.types().count("android_seen"), 4)   # 24h / 6h


if __name__ == "__main__":
    unittest.main(verbosity=2)
