#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for server/stats.py - constructed data, touches nothing.

These exist because the failure mode of statistics code is silent: it returns a
plausible-looking wrong number instead of raising. Every expectation below is
computed by hand from a synthetic history, so a regression in the weighting
logic cannot pass unnoticed.

    python3 tests/test_stats.py
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import stats  # noqa: E402

T0 = datetime(2026, 9, 1, 0, 0, 0)


def entry(ts, reachable=True, status="NORMAL", sshd=True, tcp=True,
          termux=True, bridge=True, batt_temp=None, cpu_temp=None,
          mem=None, sto_free=None):
    raw = {}
    if reachable:
        raw = {
            "timestamp": ts.isoformat(),
            "services": {"sshd": sshd, "tcp_8022": tcp, "termux": termux,
                         "monitor_bridge": bridge, "sshd_pid": 4242},
            "battery": {"temperature_c": batt_temp},
            "cpu": {"temperature_c": cpu_temp},
            "memory": {"used_percent": mem},
            "storage": {"free_gb": sto_free},
        }
    e = {"ts": ts.isoformat(), "status": status, "alerts": [], "raw": raw}
    if not reachable:
        e["error"] = "ssh rc=255: connection refused"
    return json.dumps(e)


def write_history(lines):
    d = tempfile.mkdtemp(prefix="stats-test-")
    p = Path(d) / "history.jsonl"
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


def walk(start, count, step=300, **kw):
    return [entry(start + timedelta(seconds=i * step), **kw)
            for i in range(count)]


class StatsTest(unittest.TestCase):

    def test_all_up_is_hundred_percent(self):
        p = write_history(walk(T0, 13))
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["online"]["overall_pct"], 100.0)
        self.assertEqual(s["online"]["ssh_pct"], 100.0)
        self.assertEqual(s["online"]["bridge_pct"], 100.0)
        self.assertEqual(s["samples"]["total"], 13)
        self.assertEqual(s["samples"]["normal"], 13)
        self.assertEqual(s["samples"]["abnormal"], 0)

    def test_known_outage_matches_hand_computed_rate(self):
        """24 samples @300s, samples 5..10 unreachable -> 6*300 = 1800s down.

        total = 24 * 300 = 7200s (last sample governs one poll interval)
        up    = 7200 - 1800 = 5400s  ->  75.0%
        """
        up = walk(T0, 5)                                   # idx 0..4
        down = walk(T0 + timedelta(seconds=5 * 300), 6, reachable=False)
        rest = walk(T0 + timedelta(seconds=11 * 300), 13)
        p = write_history(up + down + rest)
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["window"]["observed_seconds"], 23 * 300)
        self.assertAlmostEqual(s["online"]["overall_pct"], 75.0, places=2)
        self.assertEqual(s["streaks"]["outage_count"], 1)
        self.assertEqual(s["streaks"]["longest_outage_seconds"], 1800)

    def test_weighting_is_time_based_not_sample_based(self):
        """One long up segment must outweigh many short down samples.

        Layout: up@0s, then 10 down samples 60s apart (60..600), then up@1200.
          up segments  : 60s (0->60) + 300s (last sample's poll) = 360s
          down segments: 9*60 + min(600, 600) = 1140s
          total        : 1500s -> 24.0%
        A sample-count implementation would say 2/12 = 16.7% and fail here.
        """
        lines = [entry(T0)]
        lines += walk(T0 + timedelta(seconds=60), 10, step=60, reachable=False)
        lines += [entry(T0 + timedelta(seconds=1200))]
        p = write_history(lines)
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertAlmostEqual(s["online"]["overall_pct"], 360 * 100.0 / 1500,
                               places=2)
        self.assertNotAlmostEqual(s["online"]["overall_pct"],
                                  2 * 100.0 / 12, places=1)

    def test_long_gap_is_clipped_not_counted_as_downtime(self):
        """Two up samples 10h apart: the gap is unknowable, so it is clipped.

        total = clip(600) + one poll (300) = 900s, all up -> 100%.
        The raw 10h gap must still be reported and flagged.
        """
        lines = [entry(T0), entry(T0 + timedelta(hours=10))]
        p = write_history(lines)
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["online"]["overall_pct"], 100.0)
        self.assertEqual(s["streaks"]["max_data_gap_seconds"], 36000)
        self.assertTrue(any("截断" in c for c in s["caveats"]))

    def test_ssh_rate_uses_only_observable_time(self):
        """Device up but sshd dead for half the time -> ssh_pct ~50%, overall 100%."""
        ok = walk(T0, 6, sshd=True, tcp=True)
        bad = walk(T0 + timedelta(seconds=6 * 300), 6, sshd=False, tcp=False)
        p = write_history(ok + bad)
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["online"]["overall_pct"], 100.0)
        self.assertAlmostEqual(s["online"]["ssh_pct"], 50.0, places=2)

    def test_single_sample_yields_none_not_zero(self):
        """'Not enough data' must never masquerade as '0% online'."""
        p = write_history([entry(T0)])
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertIsNone(s["online"]["overall_pct"])
        self.assertTrue(any("样本不足" in c for c in s["caveats"]))

    def test_empty_history_is_safe(self):
        d = tempfile.mkdtemp(prefix="stats-test-")
        p = Path(d) / "history.jsonl"
        p.write_text("", encoding="utf-8")
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["samples"]["total"], 0)
        self.assertIsNone(s["online"]["overall_pct"])

    def test_tailscale_is_declared_same_measurement(self):
        p = write_history(walk(T0, 5))
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["online"]["tailscale_pct"], s["online"]["overall_pct"])
        self.assertIn("同源", s["online"]["tailscale_note"])

    def test_env_aggregates(self):
        lines = [entry(T0, batt_temp=30.0, cpu_temp=40.0, mem=50.0, sto_free=100.0),
                 entry(T0 + timedelta(seconds=300), batt_temp=40.0, cpu_temp=60.0,
                       mem=70.0, sto_free=90.0)]
        p = write_history(lines)
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["env"]["batt_temp_max"], 40.0)
        self.assertEqual(s["env"]["batt_temp_avg"], 35.0)
        self.assertEqual(s["env"]["cpu_temp_max"], 60.0)
        self.assertEqual(s["env"]["ram_used_avg"], 60.0)
        self.assertEqual(s["env"]["storage_free_min"], 90.0)

    def test_human_seconds(self):
        self.assertEqual(stats.human_seconds(339889), "3天 22小时")
        self.assertEqual(stats.human_seconds(3725), "1小时 2分")
        self.assertEqual(stats.human_seconds(45), "45秒")

    def test_aware_and_naive_timestamps_can_mix(self):
        """Phone stamps carry +08:00, cloud stamps do not. Sorting must survive."""
        a = json.loads(entry(T0))
        b = json.loads(entry(T0 + timedelta(seconds=300)))
        b["raw"]["timestamp"] += "+08:00"
        p = write_history([json.dumps(a), json.dumps(b)])
        s = stats.compute_stats(poll_interval=300, path=p)
        self.assertEqual(s["samples"]["total"], 2)
        self.assertEqual(s["online"]["overall_pct"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
