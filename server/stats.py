#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stability statistics for Android Phone Server Monitoring V1.1.

Everything here is derived from REAL samples in dashboard/data/history.jsonl.
There is no estimation, no interpolation, no fabrication. When a number cannot
be derived honestly it is reported as null with a note in "caveats".

WHY TIME-WEIGHTED AND NOT SAMPLE-COUNTED
----------------------------------------
The sampling cadence has NOT been constant over the life of this project
(early runs polled more often than the current 300s). Dividing "good samples"
by "total samples" would therefore report a rate that reflects *when* we
polled rather than *how long* the device was actually up.

So every rate below is computed over TIME:

    each sample governs the interval that follows it, up to the next sample.

    sample_i  ---- (t[i+1] - t[i]) ----  sample_i+1

The last sample governs one nominal poll interval.

GAP CLIPPING (important, and honest):
    A segment longer than GAP_CLIP_FACTOR x poll_interval is clipped.
    Reason: when the gap is that large we cannot tell whether the PHONE was
    down or the COLLECTOR was down (deploy, cloud restart, network partition
    on our side). Counting such a gap as verified device downtime would be
    inventing data. The un-clipped maximum is still reported as
    `max_data_gap_seconds` so the anomaly stays visible.

DEFINITIONS (kept deliberately narrow)
--------------------------------------
reachable / 可观测
    The cloud successfully read current.json over Tailscale SSH.
    -> "the device answered"
ssh_ok / SSH 层正常
    The device itself reports services.sshd == true AND services.tcp_8022 == true.
    -> "SSH was actually serving" (device can be up while sshd is dead)
normal sample / 正常样本
    reachable AND status == "NORMAL" (no alert fired)
abnormal sample / 异常样本
    reachable but WARNING/CRITICAL, or not reachable at all
"""

import json
import os
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
HISTORY = DATA / "history.jsonl"

# A segment longer than this many poll-intervals is clipped (see docstring).
GAP_CLIP_FACTOR = 2.0
DEFAULT_POLL_INTERVAL = 300


def _parse_ts(s):
    """Accept both '2026-09-02T19:10:53' and '2026-09-02T19:10:53+08:00'.

    Always returns a NAIVE LOCAL datetime. This matters: the phone stamps its
    samples with an offset ('+08:00') while the cloud stamps its own entries
    with bare local time. Sorting or subtracting a mix of aware and naive
    values raises TypeError, so every timestamp is normalised to the local wall
    clock exactly once, here.
    """
    dt = None
    if s:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            dt = None
    if dt is None and s:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except Exception:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def human_seconds(sec):
    """123456 -> '1天 10小时'. Never rounds up, never pretends precision."""
    if sec is None:
        return None
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}天 {h}小时"
    if h:
        return f"{h}小时 {m}分"
    if m:
        return f"{m}分 {s}秒"
    return f"{s}秒"


def iter_samples(path=HISTORY):
    """Stream samples in file order. Yields dicts; skips unparseable lines."""
    if not Path(path).exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _sample_time(entry):
    """Prefer the sample's own timestamp; fall back to the fetch timestamp."""
    raw = entry.get("raw") or {}
    t = _parse_ts(raw.get("timestamp")) or _parse_ts(entry.get("ts"))
    return t


def _is_reachable(entry):
    """True when the cloud actually managed to read the device."""
    if entry.get("error"):
        return False
    raw = entry.get("raw") or {}
    return bool(raw)


def _svc(entry, key):
    return ((entry.get("raw") or {}).get("services") or {}).get(key)


def compute_stats(poll_interval=DEFAULT_POLL_INTERVAL,
                  since=None, until=None, path=HISTORY):
    """Compute all stability statistics over the samples in [since, until].

    since/until are datetimes or None (open ended). Returns a dict that is
    safe to serialize straight into stats.json.
    """
    clip = max(poll_interval, 60) * GAP_CLIP_FACTOR

    points = []
    for e in iter_samples(path):
        t = _sample_time(e)
        if t is None:
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        reachable = _is_reachable(e)
        raw = e.get("raw") or {}
        points.append({
            "t": t,
            "reachable": reachable,
            "status": e.get("status"),
            "sshd": _svc(e, "sshd"),
            "tcp": _svc(e, "tcp_8022"),
            "termux": _svc(e, "termux"),
            "bridge": _svc(e, "monitor_bridge"),
            "batt_temp": (raw.get("battery") or {}).get("temperature_c"),
            "cpu_temp": (raw.get("cpu") or {}).get("temperature_c"),
            "mem_used": (raw.get("memory") or {}).get("used_percent"),
            "sto_free": (raw.get("storage") or {}).get("free_gb"),
        })

    stats = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "poll_interval_seconds": poll_interval,
        "gap_clip_seconds": int(clip),
        "samples": {"total": len(points), "reachable": 0, "failed": 0,
                    "normal": 0, "abnormal": 0},
        "window": {"first_sample": None, "last_sample": None,
                   "observed_seconds": 0, "observed_human": None},
        "online": {"overall_pct": None, "ssh_pct": None, "termux_pct": None,
                   "bridge_pct": None, "tailscale_pct": None,
                   "tailscale_note": None},
        "streaks": {"longest_uptime_seconds": 0, "longest_outage_seconds": 0,
                    "max_data_gap_seconds": 0, "outage_count": 0},
        "env": {"batt_temp_max": None, "batt_temp_avg": None,
                "cpu_temp_max": None, "ram_used_avg": None,
                "storage_free_min": None, "storage_free_max": None},
        "caveats": [],
    }

    if len(points) < 2:
        stats["caveats"].append(
            "样本不足（<2 条），无法计算任何在线率；这不是 0%，是「尚不可知」")
        return stats

    points.sort(key=lambda p: p["t"])
    first, last = points[0]["t"], points[-1]["t"]
    stats["window"]["first_sample"] = first.isoformat(timespec="seconds")
    stats["window"]["last_sample"] = last.isoformat(timespec="seconds")

    # ---- time-weighted accumulation -------------------------------------
    total_t = 0.0
    up_t = 0.0                     # device reachable
    ssh_t = 0.0                    # reachable AND sshd+8022 reported healthy
    termux_t = 0.0
    bridge_t = 0.0

    # denominators for the "device answered" subset: SSH/Termux/Bridge rates
    # are only meaningful over samples where we could actually see the device.
    ssh_seen_t = 0.0
    termux_seen_t = 0.0
    bridge_seen_t = 0.0

    cur_up = 0.0
    cur_down = 0.0
    longest_up = 0.0
    longest_down = 0.0
    max_gap = 0.0
    outage_count = 0
    in_outage = False

    batt_temps = []
    cpu_temps = []
    mem_used = []
    sto_free = []

    for i, p in enumerate(points):
        if i + 1 < len(points):
            gap = (points[i + 1]["t"] - p["t"]).total_seconds()
        else:
            gap = float(poll_interval)
        if gap <= 0:
            gap = 0.0
        max_gap = max(max_gap, gap)
        seg = min(gap, clip)

        total_t += seg

        if p["reachable"]:
            up_t += seg
            cur_up += seg
            if cur_down > 0:
                longest_down = max(longest_down, cur_down)
                cur_down = 0.0
                in_outage = False

            if p["sshd"] is True and p["tcp"] is True:
                ssh_t += seg
                ssh_seen_t += seg
            elif p["sshd"] is not None and p["tcp"] is not None:
                ssh_seen_t += seg

            if p["termux"] is not None:
                termux_seen_t += seg
                if p["termux"] is True:
                    termux_t += seg

            if p["bridge"] is not None:
                bridge_seen_t += seg
                if p["bridge"] is True:
                    bridge_t += seg

            if p["batt_temp"] is not None:
                batt_temps.append(p["batt_temp"])
            if p["cpu_temp"] is not None:
                cpu_temps.append(p["cpu_temp"])
            if p["mem_used"] is not None:
                mem_used.append(p["mem_used"])
            if p["sto_free"] is not None:
                sto_free.append(p["sto_free"])
        else:
            cur_down += seg
            if cur_up > 0:
                longest_up = max(longest_up, cur_up)
                cur_up = 0.0
            if not in_outage:
                outage_count += 1
                in_outage = True

    longest_up = max(longest_up, cur_up)
    longest_down = max(longest_down, cur_down)

    # ---- sample counters -------------------------------------------------
    for p in points:
        if p["reachable"]:
            stats["samples"]["reachable"] += 1
            if p["status"] == "NORMAL":
                stats["samples"]["normal"] += 1
            else:
                stats["samples"]["abnormal"] += 1
        else:
            stats["samples"]["failed"] += 1
            stats["samples"]["abnormal"] += 1

    # ---- rates -----------------------------------------------------------
    def pct(num, den):
        if not den:
            return None
        return round(num * 100.0 / den, 2)

    stats["window"]["observed_seconds"] = int((last - first).total_seconds())
    stats["window"]["observed_human"] = human_seconds(
        stats["window"]["observed_seconds"])

    stats["online"]["overall_pct"] = pct(up_t, total_t)
    stats["online"]["ssh_pct"] = pct(ssh_t, ssh_seen_t)
    stats["online"]["termux_pct"] = pct(termux_t, termux_seen_t)
    stats["online"]["bridge_pct"] = pct(bridge_t, bridge_seen_t)

    # The cloud reaches the phone ONLY over Tailscale SSH, so Tailscale
    # reachability and overall reachability are the same measurement. Reporting
    # them as two independent numbers would be double counting.
    stats["online"]["tailscale_pct"] = stats["online"]["overall_pct"]
    stats["online"]["tailscale_note"] = (
        "与整体在线率同源：云端唯一路径就是 Tailscale SSH，"
        "因此这是同一测量的两种表述，不是独立证据")

    stats["streaks"]["longest_uptime_seconds"] = int(longest_up)
    stats["streaks"]["longest_outage_seconds"] = int(longest_down)
    stats["streaks"]["max_data_gap_seconds"] = int(max_gap)
    stats["streaks"]["outage_count"] = outage_count

    # ---- environment aggregates -----------------------------------------
    if batt_temps:
        stats["env"]["batt_temp_max"] = round(max(batt_temps), 1)
        stats["env"]["batt_temp_avg"] = round(sum(batt_temps) / len(batt_temps), 1)
    if cpu_temps:
        stats["env"]["cpu_temp_max"] = round(max(cpu_temps), 1)
    if mem_used:
        stats["env"]["ram_used_avg"] = round(sum(mem_used) / len(mem_used), 1)
    if sto_free:
        stats["env"]["storage_free_min"] = round(min(sto_free), 1)
        stats["env"]["storage_free_max"] = round(max(sto_free), 1)

    # ---- caveats ---------------------------------------------------------
    if max_gap > clip:
        stats["caveats"].append(
            f"存在 {human_seconds(int(max_gap))} 的数据空缺，已按 "
            f"{int(clip)}s 截断计入（无法区分『手机离线』与『采集器未运行』）")
    if stats["window"]["observed_seconds"] < 86400:
        stats["caveats"].append("观察时长不足 24 小时，在线率尚不具备统计意义")
    stats["caveats"].append(
        "SSH / Termux / Bridge 在线率的分母是『设备可观测的时间』，"
        "设备整体离线的时间不计入这些分项")

    return stats


def write_stats(stats, out_path=None):
    out_path = Path(out_path) if out_path else DATA / "stats.json"
    out_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return out_path


def main():
    poll = int(os.environ.get("MI10PRO_POLL_SECONDS", DEFAULT_POLL_INTERVAL))
    s = compute_stats(poll_interval=poll)
    p = write_stats(s)
    w = s["window"]
    o = s["online"]
    print(f"window  : {w['observed_human']}  ({w['first_sample']} -> {w['last_sample']})")
    print(f"samples : total={s['samples']['total']} "
          f"normal={s['samples']['normal']} abnormal={s['samples']['abnormal']}")
    print(f"online  : overall={o['overall_pct']}% ssh={o['ssh_pct']}% "
          f"termux={o['termux_pct']}% bridge={o['bridge_pct']}%")
    print(f"streaks : up={human_seconds(s['streaks']['longest_uptime_seconds'])} "
          f"down={human_seconds(s['streaks']['longest_outage_seconds'])} "
          f"outages={s['streaks']['outage_count']}")
    for c in s["caveats"]:
        print(f"note    : {c}")
    print(f"written : {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
