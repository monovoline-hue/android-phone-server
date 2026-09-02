#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mi10Pro Server Monitoring V1 - server/workstation side.

Architecture (deliberate):
    phone:  collect  -> current.json          (lightweight, read-only)
    server: fetch    -> history + alerts + web dashboard
The phone never runs a database or a web stack; it only reports.

Primary transport is Tailscale SSH (no ADB). ADB is an OPTIONAL supplement
used solely to fill battery fields that Android denies to the Termux uid.
If ADB is unavailable the dashboard degrades honestly - it never invents data.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = BASE / "out"
RULES_FILE = BASE / "rules.json"
for d in (DATA, OUT):
    d.mkdir(exist_ok=True)

SSH_HOST = "mi10pro"                       # ~/.ssh/config alias
REMOTE_JSON = "~/zonira-monitor/current.json"
REMOTE_ADB_BATT = "~/zonira-monitor/battery_adb.json"
# V1.1: the phone's watchdog writes its own restart events here. The device is
# the only party that can truthfully say "I restarted sshd" - the cloud can
# only infer it from a PID change. A missing file just means an older
# deployment on the device; it is never treated as an error.
REMOTE_EVENTS = "~/zonira-monitor/events.jsonl"
# Windows default kept for the workstation; on Linux the binary simply does
# not exist and the supplement is skipped. Override with MI10PRO_ADB.
ADB = os.environ.get("MI10PRO_ADB", r"C:\platform-tools\adb.exe")
SSH_TIMEOUT = int(os.environ.get("MI10PRO_SSH_TIMEOUT", "20"))
# Sampling cadence of this process. It MUST match the systemd timer / cron
# period, because the "N minutes offline" rule is enforced by counting
# consecutive failures - a wrong value silently skews the 5-minute window.
POLL_INTERVAL = int(os.environ.get("MI10PRO_POLL_SECONDS", "300"))

CST = timedelta(hours=8)

DEFAULT_RULES = {
    "batt_temp_critical": 48,
    "batt_temp_warning": 43,
    "storage_free_critical_gb": 10,
    "storage_free_warning_gb": 30,
    "offline_critical_minutes": 5,
    "ssh_fail_warning_count": 3,
    "mem_used_warning_percent": 90,
    "stale_sample_seconds": 300,
    "battery_low_warning_percent": 20,
    "battery_low_critical_percent": 10,
    "bridge_fail_warning_minutes": 5,
}


def load_rules():
    rules = dict(DEFAULT_RULES)
    if RULES_FILE.exists():
        try:
            rules.update(json.loads(RULES_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[warn] rules.json unreadable ({e}); using defaults")
    return rules


def ssh(cmd, timeout=SSH_TIMEOUT):
    """Run a command on the phone over Tailscale SSH. Returns (rc, out, err)."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
             SSH_HOST, cmd],
            capture_output=True, text=True, timeout=timeout + 5,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 127, "", str(e)


def fetch_current():
    """Read current.json from the phone over SSH. Returns (data, error)."""
    rc, out, err = ssh(f"cat {REMOTE_JSON}")
    if rc != 0:
        return None, f"ssh rc={rc}: {err.strip()[:200]}"
    try:
        return json.loads(out), None
    except Exception as e:
        return None, f"json parse: {e}"


# ---------------------------------------------------------------------------
# Optional ADB supplement
# ---------------------------------------------------------------------------
def adb_available():
    if not os.path.exists(ADB):
        return False
    try:
        p = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace")
        return "\tdevice" in p.stdout
    except Exception:
        return False


def adb_battery():
    """Battery facts Android denies to Termux. Returns dict or None."""
    try:
        p = subprocess.run([ADB, "shell", "dumpsys", "battery"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        raw = p.stdout
    except Exception:
        return None
    if "level" not in raw:
        return None

    def grab(key, cast=int):
        m = re.search(rf"^\s*{key}:\s*(\S+)", raw, re.M)
        if not m:
            return None
        try:
            return cast(m.group(1))
        except Exception:
            return None

    level = grab("level")
    if level is None:
        return None
    status = grab("status")
    health = grab("health")
    voltage = grab("voltage")
    temp = grab("temperature")
    ac = grab("AC powered", str) == "true"
    usb = grab("USB powered", str) == "true"
    wireless = grab("Wireless powered", str) == "true"

    # status: 2=charging, 3=discharging, 4=not charging, 5=full
    status_map = {1: "unknown", 2: "charging", 3: "discharging",
                  4: "not-charging", 5: "full"}
    return {
        "level": level,
        "status": status_map.get(status, "unknown"),
        "charging": bool(status == 2 or ac or usb or wireless),
        "plugged_ac": ac,
        "plugged_usb": usb,
        "plugged_wireless": wireless,
        "voltage_mv": voltage,
        "temperature_c": round(temp / 10.0, 1) if temp is not None else None,
        "health": health,
        "source": "adb",
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def push_adb_supplement(batt):
    """Write the supplement onto the phone so collect.sh can merge it."""
    if batt is None:
        return False
    payload = json.dumps(batt, ensure_ascii=False)
    # single-quote safe: payload has no single quotes
    rc, _o, _e = ssh(f"cat > {REMOTE_ADB_BATT} <<'ZM_EOF'\n{payload}\nZM_EOF")
    return rc == 0


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def append_history(entry):
    hf = DATA / "history.jsonl"
    with hf.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history(max_points=720):
    """Return recent history points (default ~12h at 1/min)."""
    hf = DATA / "history.jsonl"
    if not hf.exists():
        return []
    lines = hf.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = [l for l in lines if l.strip()][-max_points:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------
def judge(data, rules, prev, state):
    """Return (status, [alerts]). Kept separate from collection by design."""
    alerts = []
    status = "NORMAL"

    def crit(msg):
        nonlocal status
        status = "CRITICAL"
        alerts.append({"level": "CRITICAL", "msg": msg})

    def warn(msg):
        nonlocal status
        if status != "CRITICAL":
            status = "WARNING"
        alerts.append({"level": "WARNING", "msg": msg})

    poll = int(rules.get("poll_interval_seconds", POLL_INTERVAL))

    if data is None:
        # Offline rule. The window is declared in MINUTES (rules.json) and
        # converted to a consecutive-failure count with the real polling
        # interval, so it stays honest if the cadence is ever changed.
        # One dropped poll is a blip, not an outage - only escalate once the
        # full window has elapsed, otherwise every transient timeout pages us.
        fails = state.get("ssh_fail_streak", 0)
        need = max(1, rules["offline_critical_minutes"] * 60 // poll)
        if fails >= need:
            status = "OFFLINE"
            alerts.append({
                "level": "CRITICAL",
                "msg": (f"设备离线 OFFLINE：连续 {fails} 次（约 {fails * poll // 60} 分钟）"
                        f"无法通过 Tailscale/SSH 读取 current.json"),
            })
        else:
            warn(f"SSH 连接失败 {fails}/{need} 次"
                 f"（未达 {rules['offline_critical_minutes']} 分钟离线阈值）")
        return status, alerts

    # reboot detection: uptime must not regress.
    # `prev` is the raw sample, where uptime lives under "android" - reading it
    # from the top level silently yields None and disables this rule entirely.
    up = (data.get("android") or {}).get("uptime_seconds")
    last_up = ((prev.get("android") or {}).get("uptime_seconds")) if prev else None
    if up is not None and last_up is not None and up + 120 < last_up:
        crit(f"检测到 Android 重启（uptime {last_up}s -> {up}s）")

    # storage
    free = (data.get("storage") or {}).get("free_gb")
    if free is not None:
        if free < rules["storage_free_critical_gb"]:
            crit(f"/data 剩余 {free} GB < {rules['storage_free_critical_gb']} GB")
        elif free < rules["storage_free_warning_gb"]:
            warn(f"/data 剩余 {free} GB < {rules['storage_free_warning_gb']} GB")

    # battery temperature
    bt = (data.get("battery") or {}).get("temperature_c")
    if bt is not None:
        if bt >= rules["batt_temp_critical"]:
            crit(f"电池温度 {bt}°C >= {rules['batt_temp_critical']}°C")
        elif bt >= rules["batt_temp_warning"]:
            warn(f"电池温度 {bt}°C >= {rules['batt_temp_warning']}°C")

    # battery level (bridge since V1.1; ADB supplement as legacy fallback).
    # Guarded by `charging is not None`: a null charging flag means we cannot
    # know, and warning on unknown state would be inventing a problem.
    lvl = (data.get("battery") or {}).get("level")
    charging = (data.get("battery") or {}).get("charging")
    if lvl is not None and charging is False:
        if lvl <= rules["battery_low_critical_percent"]:
            crit(f"电量 {lvl}% 且未在充电")
        elif lvl <= rules["battery_low_warning_percent"]:
            warn(f"电量 {lvl}% 且未在充电")

    # monitor bridge health: HTTP/schema based on the phone side, streak here.
    # WARNING only, never CRITICAL - a dead bridge degrades battery/display to
    # null; the device itself is still alive and the offline rule owns that.
    bfails = state.get("bridge_fail_streak", 0)
    bneed = max(1, rules["bridge_fail_warning_minutes"] * 60 // poll)
    if bfails >= bneed:
        warn(f"Monitor Bridge 连续 {bfails} 次无响应（约 {bfails * poll // 60} 分钟），"
             f"电池/屏幕数据已退化")

    # memory
    mu = (data.get("memory") or {}).get("used_percent")
    if mu is not None and mu >= rules["mem_used_warning_percent"]:
        warn(f"内存使用率 {mu}% >= {rules['mem_used_warning_percent']}%")

    # services
    svc = data.get("services") or {}
    if svc.get("sshd") is False:
        crit("sshd 未运行")
    if svc.get("tcp_8022") is False:
        crit("TCP 8022 未监听")
    if svc.get("termux") is False:
        warn("Termux 异常")
    if svc.get("watchdog") is False:
        warn("watchdog-sshd 不存在")

    # NOTE: consecutive-failure escalation lives in the `data is None` branch
    # above. It cannot be evaluated here: reaching this point means the fetch
    # succeeded, so main() has already reset ssh_fail_streak to 0.

    # stale data
    epoch = data.get("epoch")
    if epoch:
        age = int(time.time()) - int(epoch)
        if age > rules["stale_sample_seconds"]:
            warn(f"数据陈旧（{age}s 未更新）")

    return status, alerts


def backfill(days=2):
    """Pull the phone's own jsonl history so charts have real data immediately.

    The device is the primary collector; this simply mirrors what it already
    recorded, so the dashboard is useful from the first minute instead of
    showing an empty chart until enough local samples accumulate.
    """
    got = 0
    for offset in range(days, -1, -1):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        rc, out, err = ssh(f"cat ~/zonira-monitor/history/{day}.jsonl 2>/dev/null")
        if rc != 0 or not out.strip():
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            # normalise into the same shape fetch produces
            append_history({
                "ts": raw.get("timestamp", datetime.now().isoformat(timespec="seconds")),
                "status": "NORMAL",
                "alerts": [],
                "adb_supplement": False,
                "raw": raw,
                "backfilled": True,
            })
            got += 1
    print(f"backfilled {got} samples")
    return got


def load_history_series(max_points=720):
    """Flatten stored history into the compact shape the dashboard plots."""
    series = []
    for h in load_history(max_points):
        r = h.get("raw") or {}
        ts = h.get("ts") or r.get("timestamp") or ""
        series.append({
            "t": ts[11:16] if len(ts) > 16 else ts,
            "batt_lvl": (r.get("battery") or {}).get("level"),
            "batt_temp": (r.get("battery") or {}).get("temperature_c"),
            "cpu": (r.get("cpu") or {}).get("usage_percent"),
            "cpu_temp": (r.get("cpu") or {}).get("temperature_c"),
            "mem": (r.get("memory") or {}).get("used_percent"),
            "sto": (r.get("storage") or {}).get("used_percent"),
        })
    return series


# ---------------------------------------------------------------------------
# V1.1 additions: structured event log + stability statistics
# ---------------------------------------------------------------------------
def update_v11():
    """Refresh events.jsonl and stats.json from real history.

    Deliberately wrapped: a failure here must never take the collector down.
    Worst case the dashboard loses its stability panel for one cycle; the
    status/alerts pipeline above stays intact.
    """
    payload = {"stats": None, "recent_events": [], "events_emitted": 0,
               "device_events_merged": 0, "error": None}
    try:
        sys.path.insert(0, str(BASE))
        import events as events_mod
        import stats as stats_mod
    except Exception as e:                                  # pragma: no cover
        payload["error"] = f"import: {e}"
        print(f"[warn] V1.1 modules unavailable ({e}); stability panel skipped")
        return payload

    try:
        # 1) merge events written by the phone itself (watchdog restarts)
        dev_lines = []
        rc, out, _err = ssh(f"cat {REMOTE_EVENTS} 2>/dev/null")
        if rc == 0 and out.strip():
            dev_lines = out.splitlines()

        eng = events_mod.EventEngine()
        payload["device_events_merged"] = eng.merge_device_events(dev_lines)

        # 2) derive server-side events from every sample not yet processed
        payload["events_emitted"] = len(eng.process(
            stats_mod.iter_samples(DATA / "history.jsonl")))

        # 3) stability statistics, always recomputed from the full history
        st = stats_mod.compute_stats(poll_interval=POLL_INTERVAL)
        st["events"] = events_mod.summarize_events(
            events_mod.load_events(limit=10 ** 9))
        stats_mod.write_stats(st)

        payload["stats"] = st
        payload["recent_events"] = events_mod.load_events(limit=30)
    except Exception as e:
        payload["error"] = str(e)
        print(f"[warn] V1.1 update failed: {e}")

    return payload


def main():
    do_backfill = "--backfill" in sys.argv
    if do_backfill:
        # mirror the device's own log, then fall through to a normal fetch so
        # the page still renders with a live status and current alerts
        backfill()

    rules = load_rules()
    state_file = DATA / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}

    data, err = fetch_current()
    if err:
        state["ssh_fail_streak"] = state.get("ssh_fail_streak", 0) + 1
        print(f"[error] fetch failed: {err}")
    else:
        state["ssh_fail_streak"] = 0

    # bridge health streak - only meaningful when the phone itself is
    # reachable; when SSH is down the offline rule owns the state machine.
    if data is not None:
        if (data.get("services") or {}).get("monitor_bridge") is False:
            state["bridge_fail_streak"] = state.get("bridge_fail_streak", 0) + 1
        else:
            state["bridge_fail_streak"] = 0

    # optional ADB supplement (fills fields Termux cannot read)
    adb_used = False
    if adb_available():
        batt = adb_battery()
        if batt:
            adb_used = push_adb_supplement(batt)
            if data is not None:
                data.setdefault("battery", {}).update({
                    "level": batt["level"],
                    "status": batt["status"],
                    "charging": batt["charging"],
                    "voltage_mv": batt.get("voltage_mv"),
                    "temperature_c": batt.get("temperature_c"),
                })
                data["battery"]["source"] = "adb-supplement"

    history = load_history()
    prev = history[-1].get("raw", {}) if history else {}

    status, alerts = judge(data, rules, prev, state)

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "alerts": alerts,
        "adb_supplement": adb_used,
        "raw": data or {},
    }
    if data is None:
        entry["error"] = err

    append_history(entry)
    state["last_status"] = status
    state["last_ok"] = data is not None
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    (DATA / "current.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "alerts.json").write_text(
        json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- V1.1: event log + stability statistics --------------------------
    v11 = update_v11()

    # dashboard series (trimmed for a light page)
    series = load_history_series()
    r = entry.get("raw") or {}
    series.append({
        "t": entry["ts"][11:16],
        "batt_lvl": (r.get("battery") or {}).get("level"),
        "batt_temp": (r.get("battery") or {}).get("temperature_c"),
        "cpu": (r.get("cpu") or {}).get("usage_percent"),
        "cpu_temp": (r.get("cpu") or {}).get("temperature_c"),
        "mem": (r.get("memory") or {}).get("used_percent"),
        "sto": (r.get("storage") or {}).get("used_percent"),
    })
    (DATA / "series.json").write_text(
        json.dumps(series, ensure_ascii=False), encoding="utf-8")

    render_dashboard(entry, series, v11)

    print(f"status={status} alerts={len(alerts)} adb={adb_used} ts={entry['ts']}")
    for a in alerts:
        print(f"  [{a['level']}] {a['msg']}")
    print(f"v1.1    : emitted={v11['events_emitted']} "
          f"device_events={v11['device_events_merged']} "
          f"online={((v11['stats'] or {}).get('online') or {}).get('overall_pct')}%"
          f"{' ERR=' + v11['error'] if v11['error'] else ''}")
    print(f"dashboard -> {OUT / 'dashboard.html'}")
    return 0


def render_dashboard(entry, series, v11=None):
    """Inline data into the template so the page works offline (file:// too)."""
    tpl_file = BASE / "template.html"
    if not tpl_file.exists():
        print("[warn] template.html missing; skipping render")
        return
    tpl = tpl_file.read_text(encoding="utf-8")
    payload = json.dumps({"current": entry, "series": series,
                          "v11": v11 or {}}, ensure_ascii=False)
    html = tpl.replace("__DATA__", payload)
    target = OUT / "dashboard.html"
    target.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
