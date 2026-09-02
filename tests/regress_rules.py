#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Non-destructive regression suite for the monitoring judgement rules.

Everything here uses CONSTRUCTED data. The phone is never touched, never
disconnected, and no network call is made. That is deliberate: outage
behaviour must be provable without causing one.

Run:  python3 regress_rules.py
Exit: 0 = all pass, 1 = at least one failure.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASH = HERE.parent / "dashboard"
sys.path.insert(0, str(DASH))

import fetch  # noqa: E402

RULES = fetch.load_rules()
POLL = int(RULES.get("poll_interval_seconds", fetch.POLL_INTERVAL))
NEED = max(1, RULES["offline_critical_minutes"] * 60 // POLL)
BRIDGE_NEED = max(1, RULES["bridge_fail_warning_minutes"] * 60 // POLL)


def sample(**over):
    """A healthy baseline snapshot; override any nested field via kwargs."""
    d = {
        "schema": "mi10pro-monitor/v1",
        "timestamp": "2026-08-29T22:00:00+08:00",
        "epoch": int(time.time()),
        "android": {"uptime_seconds": 5000, "boot_time": "2026-08-29 20:27:44"},
        "battery": {"level": 80, "status": "charging", "charging": True,
                    "temperature_c": 31.0, "voltage_mv": 4264},
        "cpu": {"usage_percent": 1.0, "temperature_c": 35.0, "cores": 8},
        "memory": {"used_percent": 25.0},
        "storage": {"free_gb": 400.0, "used_percent": 3.3},
        "services": {"termux": True, "sshd": True, "tcp_8022": True,
                     "watchdog": True},
    }
    for k, v in over.items():
        top, _, sub = k.partition("__")
        if sub:
            d.setdefault(top, {})[sub] = v
        else:
            d[top] = v
    return d


# (name, data, prev, state, expected_status, must_contain)
CASES = [
    ("健康样本 -> NORMAL",
     sample(), sample(), {}, "NORMAL", None),

    ("uptime 回退 -> 重启告警 CRITICAL",
     sample(android__uptime_seconds=100),
     sample(android__uptime_seconds=9000), {},
     "CRITICAL", "重启"),

    ("uptime 正常增长 -> 不告警",
     sample(android__uptime_seconds=9100),
     sample(android__uptime_seconds=9040), {},
     "NORMAL", None),

    ("断联 1 次(未达阈值) -> WARNING 不误判离线",
     None, {}, {"ssh_fail_streak": 1},
     "WARNING", "SSH 连接失败"),

    (f"断联 {NEED - 1} 次(临界前) -> 仍为 WARNING",
     None, {}, {"ssh_fail_streak": NEED - 1},
     "WARNING", "SSH 连接失败"),

    (f"断联 {NEED} 次(满 {RULES['offline_critical_minutes']} 分钟) -> OFFLINE",
     None, {}, {"ssh_fail_streak": NEED},
     "OFFLINE", "OFFLINE"),

    ("断联超过阈值 -> 依然 OFFLINE",
     None, {}, {"ssh_fail_streak": NEED + 10},
     "OFFLINE", "OFFLINE"),

    (f"数据陈旧 >{RULES['stale_sample_seconds']}s -> WARNING",
     sample(epoch=int(time.time()) - RULES["stale_sample_seconds"] - 100),
     sample(), {},
     "WARNING", "陈旧"),

    (f"数据新鲜(age < {RULES['stale_sample_seconds']}s) -> 不告警",
     sample(epoch=int(time.time()) - RULES["stale_sample_seconds"] + 100),
     sample(), {},
     "NORMAL", None),

    ("sshd 停 -> CRITICAL",
     sample(services__sshd=False), sample(), {},
     "CRITICAL", "sshd"),

    ("8022 未监听 -> CRITICAL",
     sample(services__tcp_8022=False), sample(), {},
     "CRITICAL", "8022"),

    ("watchdog 缺失 -> WARNING",
     sample(services__watchdog=False), sample(), {},
     "WARNING", "watchdog"),

    ("电池温度 >=48 -> CRITICAL",
     sample(battery__temperature_c=49.0), sample(), {},
     "CRITICAL", "电池温度"),

    ("电池温度 >=43 -> WARNING",
     sample(battery__temperature_c=44.0), sample(), {},
     "WARNING", "电池温度"),

    ("存储剩余 <10GB -> CRITICAL",
     sample(storage__free_gb=5.0), sample(), {},
     "CRITICAL", "剩余"),

    ("电量 <=20% 且未充电 -> WARNING",
     sample(battery__level=15, battery__charging=False,
            battery__status="discharging"), sample(), {},
     "WARNING", "电量"),

    ("电量 <=20% 但充电中 -> 不告警",
     sample(battery__level=15, battery__charging=True), sample(), {},
     "NORMAL", None),

    ("内存 >=90% -> WARNING",
     sample(memory__used_percent=95.0), sample(), {},
     "WARNING", "内存"),

    # ---- V1.1: battery tiering + monitor bridge + screen ----
    ("电量 50% 充电中 -> NORMAL",
     sample(battery__level=50, battery__charging=True), sample(), {},
     "NORMAL", None),

    ("电量 15% 充电中 -> 不触发低电 WARNING",
     sample(battery__level=15, battery__charging=True), sample(), {},
     "NORMAL", None),

    ("电量 15% 未充电 -> WARNING",
     sample(battery__level=15, battery__charging=False), sample(), {},
     "WARNING", "电量"),

    ("电量 8% 未充电 -> CRITICAL",
     sample(battery__level=8, battery__charging=False), sample(), {},
     "CRITICAL", "电量"),

    ("电量 8% 充电中 -> 不告警",
     sample(battery__level=8, battery__charging=True), sample(), {},
     "NORMAL", None),

    ("电量 null(充电状态未知) -> 不误报",
     sample(battery__level=None), sample(), {},
     "NORMAL", None),

    ("电量 15% 但 charging=null -> 不误报(状态未知) ",
     sample(battery__level=15, battery__charging=None), sample(), {},
     "NORMAL", None),

    ("Bridge 单次失败 -> 不告警",
     sample(), sample(), {"bridge_fail_streak": 1},
     "NORMAL", None),

    (f"Bridge 连续 {BRIDGE_NEED - 1} 次失败(阈值前) -> 不告警",
     sample(), sample(), {"bridge_fail_streak": BRIDGE_NEED - 1},
     "NORMAL", None),

    (f"Bridge 连续满 {RULES['bridge_fail_warning_minutes']} 分钟 -> WARNING",
     sample(), sample(), {"bridge_fail_streak": BRIDGE_NEED},
     "WARNING", "Monitor Bridge"),

    ("Bridge 健康(services.monitor_bridge=true) -> 不告警",
     sample(services__monitor_bridge=True), sample(), {},
     "NORMAL", None),

    ("屏幕 OFF -> 不告警",
     sample(display__state="OFF", display__interactive=False), sample(), {},
     "NORMAL", None),

    ("屏幕 ON -> 不告警",
     sample(display__state="ON", display__interactive=True), sample(), {},
     "NORMAL", None),

    ("屏幕 null -> 不告警",
     sample(display__state=None), sample(), {},
     "NORMAL", None),

    ("无历史(prev 为空) -> 不误判重启",
     sample(), {}, {},
     "NORMAL", None),

    # Recovery path: main() resets ssh_fail_streak on a successful fetch, and
    # judge() must not re-read that stale counter once real data is present -
    # otherwise the device would stay pinned at OFFLINE after coming back.
    ("断联后恢复: 数据已回来(state 仍残留 streak) -> 回到 NORMAL",
     sample(), sample(), {"ssh_fail_streak": 5},
     "NORMAL", None),
]


def main():
    print(f"poll={POLL}s  offline 阈值={RULES['offline_critical_minutes']}分钟"
          f" (= 连续 {NEED} 次失败)\n")
    passed = failed = 0
    for name, data, prev, state, want_status, want_sub in CASES:
        status, alerts = fetch.judge(data, RULES, prev, dict(state))
        ok = status == want_status
        if ok and want_sub:
            ok = any(want_sub in a["msg"] for a in alerts)
        tag = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        msgs = " | ".join(a["msg"] for a in alerts) or "-"
        print(f"[{tag}] {name}")
        print(f"       期望={want_status} 实际={status}  告警: {msgs}")

    print(f"\n合计 {passed + failed} 项: 通过 {passed}, 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
