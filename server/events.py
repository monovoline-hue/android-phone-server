#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Event log for Android Phone Server Monitoring V1.1.

Appends one JSON object per line to dashboard/data/events.jsonl, and ONLY on a
state CHANGE. A device that stays up for three days produces no events - that
is the point. Nothing here is emitted "every minute" to look busy.

Incremental by design: dashboard/data/events_state.json remembers the last
processed sample timestamp plus the per-signal state machine, so re-running
never re-emits an event and never skips one.

EVENT TYPES ACTUALLY EMITTED
----------------------------
  monitor_started        first run, or after a collector gap > 6h
  android_seen           heartbeat: device continuously observable, max 1/h
  android_reboot         uptime regressed (device restarted)
  network_down           device unreachable from the cloud (debounced)
  network_recovered      device reachable again (+duration_sec)
  ssh_down               device up, but its own sshd / 8022 reported down
  ssh_recovered          sshd healthy again (+duration_sec)
  sshd_restart           sshd PID changed without a reboot (cause unknown)
  watchdog_restart       MERGED from the phone: watchdog-sshd.sh restarted sshd
  termux_down / termux_recovered
  bridge_down / bridge_recovered
  charging_started / charging_stopped
  high_battery_temp      hysteresis: enter >= 43C, exit < 41C
  high_cpu_temp          hysteresis: enter >= 60C, exit < 55C

DELIBERATELY NOT EMITTED
------------------------
  tailscale_down / tailscale_recovered
      The cloud's only path to the phone IS Tailscale SSH, so a Tailscale event
      would be byte-identical to network_down. Emitting both would present one
      measurement as two independent signals. See stats.py: tailscale_pct is
      reported explicitly as the same measurement as overall_pct.
  wifi_changed
      Requires the Wi-Fi SSID, which Termux cannot read: termux-api is
      installed but its calls hang and time out with no output (verified
      2026-09-02, 8s timeout, empty). Marked UNSUPPORTED, not faked.

RECOVERY TYPING
---------------
  automatic   a restart marker (watchdog_restart / sshd_restart) occurred in the
              outage window -> something on the device recovered it by itself
  unknown     recovered, but no restart marker was observed
  manual      NEVER assigned automatically. There is no reliable signal that
              distinguishes "a human touched it" from "it came back on its own",
              and inventing one would corrupt the auto-recovery rate.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
EVENTS = DATA / "events.jsonl"
STATE = DATA / "events_state.json"

# Consecutive same-direction observations required before a transition fires.
# One dropped poll is a blip, not an outage.
DEBOUNCE = 2

# A heartbeat is emitted at most this often while the device stays observable.
# Kept deliberately sparse: it is liveness evidence for the log, not news. The
# dashboard filters it out of the visible feed (see template.html) so that a
# stable week reads as "nothing happened" instead of a wall of heartbeats.
HEARTBEAT_SECONDS = 6 * 3600

# If the collector was silent for longer than this, log a fresh start.
MONITOR_GAP_SECONDS = 6 * 3600

# Hysteresis for temperature events: enter high, leave lower (avoids flapping).
TEMP_RULES = {
    "high_battery_temp": {"enter": 43.0, "exit": 41.0, "unit": "C"},
    "high_cpu_temp": {"enter": 60.0, "exit": 55.0, "unit": "C"},
}
# Minimum spacing between two temperature events of the same kind.
TEMP_COOLDOWN_SECONDS = 900

BINARY_SIGNALS = ("network", "ssh", "termux", "bridge")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(s):
    """Parse a timestamp and normalise it to a NAIVE LOCAL datetime.

    Normalisation is not cosmetic: the phone stamps samples with '+08:00'
    while event times written by this module use bare local time. Comparing
    the two directly (last_ts vs sample ts) raises TypeError on a mix of aware
    and naive values, which would silently kill event detection.
    """
    dt = None
    if s:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            dt = None
    if dt is None and s:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


class Signal:
    """Debounced binary state for one signal."""

    def __init__(self, d=None):
        d = d or {}
        self.state = d.get("state")            # True / False / None (unknown)
        self.pending = d.get("pending")
        self.count = d.get("count", 0)
        self.pending_since = _parse_ts(d.get("pending_since"))
        self.down_since = _parse_ts(d.get("down_since"))

    def to_dict(self):
        return {
            "state": self.state,
            "pending": self.pending,
            "count": self.count,
            "pending_since": self.pending_since.isoformat(timespec="seconds")
            if self.pending_since else None,
            "down_since": self.down_since.isoformat(timespec="seconds")
            if self.down_since else None,
        }


class EventEngine:
    def __init__(self, events_path=EVENTS, state_path=STATE):
        self.events_path = Path(events_path)
        self.state_path = Path(state_path)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.emitted = []

    # ---- persistence ----------------------------------------------------
    def _load_state(self):
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_ts": None, "signals": {}, "temps": {}, "heartbeat": None,
                "restart_markers": [], "sshd_pid": None, "uptime": None,
                "initialized": False}

    def save(self):
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sig(self, name):
        if name not in self.state["signals"]:
            self.state["signals"][name] = Signal().to_dict()
        return Signal(self.state["signals"][name])

    def _put_sig(self, name, sig):
        self.state["signals"][name] = sig.to_dict()

    # ---- emitting -------------------------------------------------------
    def emit(self, type_, ts=None, **fields):
        ev = {"time": (ts or datetime.now()).isoformat(timespec="seconds"),
              "type": type_}
        ev.update({k: v for k, v in fields.items() if v is not None})
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.emitted.append(ev)
        return ev

    def _restart_in_window(self, start, end):
        """Was a device-side restart observed during this outage window?"""
        for m in self.state.get("restart_markers", []):
            mt = _parse_ts(m)
            if mt is None:
                continue
            if start - timedelta(seconds=60) <= mt <= end:
                return True
        return False

    def _prune_markers(self, before):
        keep = []
        for m in self.state.get("restart_markers", []):
            mt = _parse_ts(m)
            if mt and mt >= before - timedelta(hours=24):
                keep.append(m)
        self.state["restart_markers"] = keep

    # ---- per-signal handling -------------------------------------------
    def observe_binary(self, name, value, ts):
        """value: True/False/None. Returns the emitted event or None."""
        if value is None:
            return None
        sig = self._sig(name)
        if sig.state is None:
            sig.state = value
            self._put_sig(name, sig)
            return None
        if value == sig.state:
            sig.pending = None
            sig.count = 0
            sig.pending_since = None
            self._put_sig(name, sig)
            return None

        if sig.pending != value:
            sig.pending = value
            sig.count = 1
            sig.pending_since = ts
        else:
            sig.count += 1

        if sig.count < DEBOUNCE:
            self._put_sig(name, sig)
            return None

        started = sig.pending_since or ts
        old = sig.state
        sig.state = value
        sig.pending = None
        sig.count = 0
        sig.pending_since = None

        if value is False:
            sig.down_since = started
            self._put_sig(name, sig)
            # 'time' = when the transition was CONFIRMED (debounce satisfied),
            # 'since' = when it actually started (first failing observation).
            # Both matter: alerting wants the confirmation moment, reporting
            # wants the true onset.
            return self.emit(f"{name}_down", ts,
                             since=started.isoformat(timespec="seconds"))
        else:
            down_since = sig.down_since or started
            sig.down_since = None
            self._put_sig(name, sig)
            dur = int((ts - down_since).total_seconds())
            rtype = "automatic" if self._restart_in_window(down_since, ts) else "unknown"
            return self.emit(f"{name}_recovered", ts,
                             duration_sec=max(dur, 0), recovery_type=rtype)

    def observe_temp(self, name, value, ts):
        """Hysteresis + cooldown, so a value hovering at the threshold does not
        emit an event every single poll."""
        rule = TEMP_RULES[name]
        st = self.state["temps"].get(name, {})
        active = st.get("active", False)
        last = _parse_ts(st.get("last_emit"))
        if value is None:
            return None
        try:
            value = float(value)
        except Exception:
            return None

        cooldown_ok = (last is None or
                       (ts - last).total_seconds() >= TEMP_COOLDOWN_SECONDS)

        if not active and value >= rule["enter"] and cooldown_ok:
            st.update({"active": True, "last_emit": ts.isoformat(timespec="seconds")})
            self.state["temps"][name] = st
            return self.emit(name, ts, value=value, threshold=rule["enter"])
        if active and value < rule["exit"]:
            st["active"] = False
            self.state["temps"][name] = st
        return None

    # ---- main entry -----------------------------------------------------
    def process(self, samples):
        """samples: iterable of history entries, ascending by time."""
        last_ts = _parse_ts(self.state.get("last_ts"))
        newest = None

        for entry in samples:
            raw = entry.get("raw") or {}
            ts = _parse_ts(raw.get("timestamp")) or _parse_ts(entry.get("ts"))
            if ts is None:
                continue
            if last_ts and ts <= last_ts:
                continue

            # monitor_started: first run, or the collector was silent a long time
            if not self.state.get("initialized"):
                self.emit("monitor_started", ts,
                          note="event log initialised; earlier history has no "
                               "event trail")
                self.state["initialized"] = True
            elif last_ts and (ts - last_ts).total_seconds() > MONITOR_GAP_SECONDS:
                gap = int((ts - last_ts).total_seconds())
                self.emit("monitor_started", ts,
                          note=f"collector silent for {gap}s before this sample")

            reachable = bool(raw) and not entry.get("error")

            # --- network (device reachable at all) ---
            self.observe_binary("network", reachable, ts)

            if reachable:
                svc = raw.get("services") or {}
                batt = raw.get("battery") or {}
                android = raw.get("android") or {}
                cpu = raw.get("cpu") or {}

                # --- ssh layer: only meaningful while we can see the device ---
                if svc.get("sshd") is not None and svc.get("tcp_8022") is not None:
                    self.observe_binary("ssh",
                                        bool(svc.get("sshd")) and bool(svc.get("tcp_8022")),
                                        ts)

                # --- sshd restart: pid changed without an Android reboot ---
                pid = svc.get("sshd_pid")
                up = android.get("uptime_seconds")
                prev_pid = self.state.get("sshd_pid")
                prev_up = self.state.get("uptime")
                rebooted = (prev_up is not None and up is not None
                            and up + 120 < prev_up)
                if (pid is not None and prev_pid is not None and pid != prev_pid
                        and not rebooted):
                    self.emit("sshd_restart", ts, old_pid=prev_pid, new_pid=pid,
                              note="sshd PID changed without an Android reboot; "
                                   "cause not attributable (watchdog or manual)")
                    self.state.setdefault("restart_markers", []).append(
                        ts.isoformat(timespec="seconds"))
                if pid is not None:
                    self.state["sshd_pid"] = pid
                if up is not None:
                    self.state["uptime"] = up

                # --- reboot ---
                if rebooted:
                    self.emit("android_reboot", ts,
                              note=f"uptime {prev_up}s -> {up}s")

                # --- termux / bridge ---
                self.observe_binary("termux", svc.get("termux"), ts)
                self.observe_binary("bridge", svc.get("monitor_bridge"), ts)

                # --- charging ---
                charging = batt.get("charging")
                if charging is not None:
                    prev_chg = self.state.get("charging")
                    if prev_chg is None:
                        self.state["charging"] = charging
                    elif charging != prev_chg:
                        self.state["charging"] = charging
                        self.emit("charging_started" if charging else "charging_stopped",
                                  ts, level=batt.get("level"),
                                  plugged=batt.get("plugged"))

                # --- temperatures ---
                self.observe_temp("high_battery_temp", batt.get("temperature_c"), ts)
                self.observe_temp("high_cpu_temp", cpu.get("temperature_c"), ts)

                # --- heartbeat ---
                hb = _parse_ts(self.state.get("heartbeat"))
                if hb is None or (ts - hb).total_seconds() >= HEARTBEAT_SECONDS:
                    self.emit("android_seen", ts,
                              note="heartbeat: device continuously observable",
                              uptime_seconds=android.get("uptime_seconds"))
                    self.state["heartbeat"] = ts.isoformat(timespec="seconds")

            newest = ts
            last_ts = ts

        if newest:
            self.state["last_ts"] = newest.isoformat(timespec="seconds")
            self._prune_markers(newest - timedelta(hours=24))
        self.save()
        return self.emitted

    # ---- device-side events ---------------------------------------------
    def merge_device_events(self, lines):
        """Merge events produced on the phone itself (watchdog_restart).

        The phone's watchdog is the only component that can truthfully say
        "I restarted sshd"; the cloud can only infer it. Duplicates are
        suppressed by (time, type) so a re-read never double-counts.
        """
        merged = 0
        for line in (lines or []):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(ev.get("time") or ev.get("ts"))
            etype = ev.get("type")
            if not ts or not etype:
                continue
            if _event_exists(self.events_path, ts.isoformat(timespec="seconds"), etype):
                continue
            fields = {k: v for k, v in ev.items() if k not in ("time", "ts", "type")}
            self.emit(etype, ts, **fields)
            if etype in ("watchdog_restart", "sshd_restart"):
                self.state.setdefault("restart_markers", []).append(
                    ts.isoformat(timespec="seconds"))
            merged += 1
        if merged:
            self.save()
        return merged


def _event_exists(path, ts_iso, etype):
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == etype and (ev.get("time") or "").startswith(ts_iso[:19]):
                    return True
    except Exception:
        return False
    return False


def load_events(limit=200, path=EVENTS):
    """Most recent events first."""
    if not Path(path).exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    out.reverse()
    return out[:limit]


def summarize_events(events):
    """Fault / recovery counters derived from the event log itself."""
    downs = [e for e in events if e["type"].endswith("_down")]
    recs = [e for e in events if e["type"].endswith("_recovered")]
    auto = [e for e in recs if e.get("recovery_type") == "automatic"]
    unknown = [e for e in recs if e.get("recovery_type") == "unknown"]
    manual = [e for e in recs if e.get("recovery_type") == "manual"]
    durs = [e["duration_sec"] for e in recs if isinstance(e.get("duration_sec"), int)]
    total_rec = len(recs)
    return {
        "fault_count": len(downs),
        "recovered_count": total_rec,
        "automatic_count": len(auto),
        "manual_count": len(manual),
        "unknown_count": len(unknown),
        "auto_recovery_pct": round(len(auto) * 100.0 / total_rec, 1) if total_rec else None,
        "avg_recovery_seconds": int(sum(durs) / len(durs)) if durs else None,
        "max_recovery_seconds": max(durs) if durs else None,
        "open_fault_count": len(downs) - total_rec if len(downs) > total_rec else 0,
    }


if __name__ == "__main__":
    import sys
    eng = EventEngine()
    if "--summary" in sys.argv:
        evs = load_events()
        print(json.dumps(summarize_events(evs), ensure_ascii=False, indent=2))
        print(f"events total: {len(evs)}")
    else:
        print(f"state: {eng.state_path}")
        print(f"events: {eng.events_path}")
