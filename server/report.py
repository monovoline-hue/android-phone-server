#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Long-term report generator (7 / 30 day).

HARD RULE: if the collected history does not actually cover the requested
period, NO report is produced. The generator exits non-zero and says how much
data is still missing. A short-window report dressed up as a "30 day report"
would be exactly the kind of fabricated number this project exists to avoid.

Usage:
    python3 report.py --days 7
    python3 report.py --days 30 --out reports/30day-report.md
    python3 report.py --days 7 --force      # allow a short window, clearly
                                            # labelled as PARTIAL in the report
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stats as stats_mod          # noqa: E402
import events as events_mod        # noqa: E402

BASE = Path(__file__).resolve().parent.parent
REPORTS = BASE / "reports"
DATA = Path(__file__).resolve().parent / "data"

# Allow 2% slack so a report is not blocked by a few missing minutes.
COVERAGE_TOLERANCE = 0.98


def _load_events_in_window(start, end):
    path = DATA / "events.jsonl"
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = stats_mod._parse_ts(ev.get("time"))
            if t and start <= t <= end:
                out.append(ev)
    return out


def build_report(days, partial=False):
    poll = int(os.environ.get("MI10PRO_POLL_SECONDS", 300))
    end = datetime.now()
    start = end - timedelta(days=days)

    st = stats_mod.compute_stats(poll_interval=poll, since=start, until=end)
    first = stats_mod._parse_ts(st["window"]["first_sample"])
    last = stats_mod._parse_ts(st["window"]["last_sample"])
    if not first or not last:
        return None, "历史中没有任何样本，无法生成报告"

    span = (last - first).total_seconds()
    required = days * 86400.0
    coverage = span / required
    if coverage < COVERAGE_TOLERANCE and not partial:
        have = stats_mod.human_seconds(int(span))
        missing = stats_mod.human_seconds(int(required - span))
        return None, (f"数据不足：请求 {days} 天报告，实际只覆盖 {have}（缺 {missing}）。"
                      f"\n不生成报告。等数据足够后重跑，或加 --force 生成明确标注 PARTIAL 的报告。")

    evs = _load_events_in_window(start, end)
    summ = events_mod.summarize_events(evs)

    w = st["window"]
    o = st["online"]
    e = st["env"]
    s = st["streaks"]
    sm = st["samples"]

    def pct(v):
        return "—" if v is None else f"{v}%"

    def dur(v):
        return "—" if v is None else stats_mod.human_seconds(v)

    lines = []
    add = lines.append
    add(f"# Android Phone Server · {days} 天稳定性报告")
    add("")
    add(f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}")
    if partial and coverage < COVERAGE_TOLERANCE:
        add(f"- ⚠️ **PARTIAL**：数据仅覆盖 {stats_mod.human_seconds(int(span))} / "
            f"{days} 天，本报告**不代表** {days} 天完整结论")
    add("")
    add("## 设备与系统")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add("| 设备 | Xiaomi Mi 10 Pro (cmi) |")
    add("| 系统 | Android 13 / MIUI V816 |")
    add(f"| 内核 | 4.19.157-perf |")
    add("| Root | 否 |")
    add("| 远程通道 | Tailscale SSH :8022 |")
    add("")
    add("## 观察周期")
    add("")
    add(f"- 起始样本：{w['first_sample']}")
    add(f"- 结束样本：{w['last_sample']}")
    add(f"- 观察时长：{w['observed_human']}（{w['observed_seconds']} 秒）")
    add(f"- 样本总数：{sm['total']}（正常 {sm['normal']} · 异常 {sm['abnormal']} · "
        f"拉取失败 {sm['failed']}）")
    add("")
    add("## 在线率")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 整体在线率 | {pct(o['overall_pct'])} |")
    add(f"| SSH 在线率 | {pct(o['ssh_pct'])} |")
    add(f"| Termux 在线率 | {pct(o['termux_pct'])} |")
    add(f"| Bridge 在线率 | {pct(o['bridge_pct'])} |")
    add(f"| Tailscale 可达率 | {pct(o['tailscale_pct'])}（{o['tailscale_note']}） |")
    add("")
    add("## 故障与恢复")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 故障次数 | {summ['fault_count']} |")
    add(f"| 已恢复 | {summ['recovered_count']} |")
    add(f"| 自动恢复 | {summ['automatic_count']} |")
    add(f"| 人工干预 | {summ['manual_count']}（不可自动识别，见下） |")
    add(f"| 恢复方式未知 | {summ['unknown_count']} |")
    add(f"| 自动恢复率 | {pct(summ['auto_recovery_pct'])} |")
    add(f"| 平均恢复时间 | {dur(summ['avg_recovery_seconds'])} |")
    add(f"| 最长恢复时间 | {dur(summ['max_recovery_seconds'])} |")
    add(f"| 最长连续在线 | {dur(s['longest_uptime_seconds'])} |")
    add(f"| 最长故障时间 | {dur(s['longest_outage_seconds'])} |")
    add(f"| 最长数据空缺 | {dur(s['max_data_gap_seconds'])} |")
    add("")
    add("> 「人工干预」默认永远为 0：没有任何可靠信号能区分『人动过』和『它自己好了』。"
        "凡是缺少 watchdog/sshd 重启标记的恢复，一律记为 unknown，绝不伪装成自动或人工。")
    add("")
    add("## 环境与资源")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 最高电池温度 | {e['batt_temp_max']} °C |")
    add(f"| 平均电池温度 | {e['batt_temp_avg']} °C |")
    add(f"| 最高 CPU 温度 | {e['cpu_temp_max']} °C |")
    add(f"| RAM 平均占用 | {e['ram_used_avg']} % |")
    add(f"| 存储剩余 最小 / 最大 | {e['storage_free_min']} / {e['storage_free_max']} GB |")
    add("")
    storage_delta = None
    if e["storage_free_min"] is not None and e["storage_free_max"] is not None:
        storage_delta = round(e["storage_free_max"] - e["storage_free_min"], 1)
    add(f"- 存储使用变化：{storage_delta if storage_delta is not None else '—'} GB"
        f"（按剩余空间极差计算）")
    add("")
    add("## 计算口径与限制")
    add("")
    for c in st["caveats"]:
        add(f"- {c}")
    add(f"- 采样节奏：约 {poll}s；超过 {st['gap_clip_seconds']}s 的空缺按比例截断计入，"
        f"避免把『采集器自己没跑』算成『手机宕机』")
    add("- 事件仅在状态发生变化时记录，长期无事件 = 长期稳定，不是「没在工作」")
    add("")
    return "\n".join(lines) + "\n", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, choices=(7, 14, 30))
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true",
                    help="generate even when data is short, marked PARTIAL")
    args = ap.parse_args()

    text, err = build_report(args.days, partial=args.force)
    if err:
        print(err)
        return 1

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS / f"{args.days}day-report.md"
    out.write_text(text, encoding="utf-8")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
