# CURRENT_ARCHITECTURE.md

> 审计时间：2026-09-02 19:00–19:35 (UTC+8)
> 审计方式：**只读**。未修改任何线上文件，未重启任何服务。
> 目的：把「手机服务器 + 监控」的既有运行环境固化成事实基线，作为后续改造的依据。

---

## 1. 分层架构总览

```
 ┌──────────────────────────────────────────────────────────────┐
 │ Android 13 / MIUI V816  ·  Xiaomi Mi 10 Pro (cmi)            │  设备层
 │ 无 Root · 不刷机 · FBE 未改动 · 长期插电                        │
 └───────────────────────────┬──────────────────────────────────┘
                             │
 ┌───────────────────────────▼──────────────────────────────────┐
 │ Termux 0.118.3 (uid u0_a264)                                 │  运行时层
 │  · sshd              监听 8022                                │
 │  · watchdog-sshd.sh  采集调度 + sshd 守护（PID 28706）          │
 │  · collect.sh        单次采集，写 current.json + history/*.jsonl│
 │  · ~/.termux/boot/*  开机自启                                  │
 │  · ~/.termux/tasker/ensure_sshd.sh  外部自动化保活入口           │
 └───────────────────────────┬──────────────────────────────────┘
                             │ 127.0.0.1:8765
 ┌───────────────────────────▼──────────────────────────────────┐
 │ ZONIRA Monitor Bridge (Android APK, uid u0_a265 之外的独立应用) │  补充数据源
 │  · 前台服务 + 极简 HTTP 服务                                    │
 │  · 提供 BatteryManager 电量/电压/温度、PowerManager 屏幕状态     │
 │  · 只有它和 collect.sh 两个进程参与，无第三方重量级监控 App        │
 └──────────────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────────────┐
 │ 网络：Tailscale (100.x)  +  LAN (192.168.x.x)                  │  传输层
 │ 云端 → 手机走 Tailscale SSH :8022（已验证，非 LAN）              │
 └───────────────────────────┬──────────────────────────────────┘
                             │ ssh cat current.json
 ┌───────────────────────────▼──────────────────────────────────┐
 │ 云端 zonira (阿里云 ECS，地址见私有配置，不入库)                 │  判定/展示层
 │  /opt/mi10pro-monitor/                                        │
 │   · dashboard/fetch.py     拉取 → 告警判定 → 渲染              │
 │   · dashboard/data/        history.jsonl / current.json / …    │
 │   · dashboard/out/         dashboard.html（静态页）             │
 │   · mi10pro-web.service    python3 -m http.server 8080         │
 │   · mi10pro-fetch.timer    每 300s 触发一次 fetch              │
 │   · mi10pro-prune.timer    每天 03:30 裁剪历史（保留 30 天）      │
 └──────────────────────────────────────────────────────────────┘
```

**设计原则（既有的，本次保留）**：手机只采集、不判定；判定与展示全在云端。
手机端脚本一律 **只读、幂等、可重复执行**。

---

## 2. 各层当前状态（审计实测）

| 层 | 组件 | 状态 | 实测证据 |
|---|---|---|---|
| Android | Mi 10 Pro / Android 13 / MIUI V816 | ✅ 在线 | `uptime` 3 天 22 小时；boot `2026-08-29 20:27:44` |
| Android | 内核 / 补丁 | — | `4.19.157-perf-g00f8d3a28662`，安全补丁 `2024-03-01` |
| Termux | 应用进程 `com.termux` | ✅ | `ps` PID 28643 |
| Termux | sshd | ✅ | PID 28695，`pgrep -x sshd` 命中 |
| Termux | 8022 可达 | ✅ | 云端每 5 分钟经 Tailscale 成功拉取 |
| Termux | watchdog-sshd.sh | ✅ 运行中 | PID 28706（**它才是真正的采集调度器**） |
| Termux | run.sh（60s 采样器） | ⚠️ **未运行** | pidfile 残留 PID 12588，`kill -0` 失败 |
| Monitor Bridge | HTTP 127.0.0.1:8765 | ✅ | 返回 `zonira-monitor-bridge/v1`，电池 88% / 32.1°C / 屏幕 OFF |
| Tailscale | 手机节点 | ✅ | `tailscale status` 显示 `mi-10-pro … idle` |
| 云端 | mi10pro-web.service | ✅ | `curl 127.0.0.1:8080` → 200 |
| 云端 | mi10pro-fetch.timer | ✅ | 每 300s，最近一次 19:10:46 |
| 云端 | mi10pro-prune.timer | ✅ | 每天 03:30，保留 30 天 |
| 云端 | history.jsonl | ✅ | 2592 条，4.0 MB，首条 `2026-08-29T20:12:32` |
| 手机 | history/*.jsonl | ✅ | 5 个文件（08-29 ~ 09-02），共 4.8 MB，保留 7 天 |
| 手机 | termux-api | ⚠️ 已装但无响应 | `termux-battery-status` / `termux-wifi-connectioninfo` 均 8s 超时无输出 → **不可用，不得依赖** |

---

## 3. 目录与文件清单

### 手机 `~/zonira-monitor/`

| 文件 | 作用 | 备注 |
|---|---|---|
| `collect.sh` | 单次采集，输出 `current.json` + 追加 `history/<date>.jsonl` | 耗时约 1.9s |
| `monitor.conf` | 采样周期、探测超时、阈值说明、Tailscale IP 声明 | 真实配置不入库，用 `monitor.example.conf` |
| `watchdog-sshd.sh` | **调度器**：每 298s 跑一次 collect.sh；sshd 掉了就拉起 | 当前活跃进程 |
| `watchdog.sh` | 早期版本（30s 轮询 + ss/netstat 判定） | 已停用，仅保留 |
| `run.sh` | 60s 采样循环（pidfile 单实例） | 当前未运行 |
| `start-monitor.sh` | Termux 交互式启动引导（由 `~/.bashrc` 调用） | |
| `start-zonira-monitor` | Termux:Boot 钩子，拉起 run.sh | |
| `current.json` | 实时状态快照（schema `mi10pro-monitor/v1`） | |
| `battery_adb.json` | 可选的 ADB 侧电量补充（有 600s 时效校验） | |
| `history/<YYYY-MM-DD>.jsonl` | 本机历史，每天一个文件 | |
| `logs/monitor.log` | 采集日志，256KB 轮转 | |
| `watchdog.log` | 守护日志 | 88 KB，未轮转（见风险项） |

### 手机 `~/.termux/boot/`

`start-sshd`（sshd + 拉起 watchdog）、`watchdog-sshd`（sshd 守护循环）、
`connect-tailscale`、`start-monitor.sh`、`start-zonira-monitor`、
`ensure_sshd.log`（12 KB，外部自动化写入）。

### 手机 `~/.termux/tasker/ensure_sshd.sh`

由外部自动化（MacroDroid / Termux:Tasker 的 RUN_COMMAND）约每 8 分钟调用一次：
持唤醒锁 → sshd 不在就拉起 → 顺带确保 `watchdog-sshd.sh` 活着。
这是 MIUI 后台杀进程之后的主要自愈入口（日志中 `MONITOR restarted` 可查）。

### 云端 `/opt/mi10pro-monitor/`

```
dashboard/fetch.py          拉取 → judge() 告警判定 → render_dashboard()
dashboard/run_loop.py       无特权循环驱动器（备用，非当前主力）
dashboard/prune_history.py  历史裁剪
dashboard/rules.json        阈值（battery temp / storage / offline / bridge …）
dashboard/template.html     页面模板，纯原生 JS + 内联 SVG，零外部依赖
dashboard/data/             history.jsonl · current.json · series.json · state.json · alerts.json
dashboard/out/              dashboard.html（http.server 8080 对外）
tests/                      regress_rules.py（19 例）、test_prune.py
logs/                       fetch.log · web.log · prune.log
```

---

## 4. 周期与节奏（实测）

| 项 | 值 |
|---|---|
| 手机采集周期 | **约 300s**（watchdog-sshd.sh：298s 睡眠 + ~1.9s 采集） |
| 单次采集耗时 | 1.9 s（thermal 表已做内建 `read` 优化，原为 9.25s） |
| 云端拉取周期 | **300s**（`mi10pro-fetch.timer` OnUnitActiveSec=300） |
| 页面刷新 | `<meta http-equiv="refresh" content="60">`，数据每 5 分钟变一次 |
| 本地历史保留 | 手机 7 天 / 云端 30 天 |

---

## 5. 权限边界（Android 13 + Termux uid，无 Root）

能读：`/proc/meminfo`、`df`、`/sys/class/thermal/*`（92 个 zone）、
`uptime`（走 `sysinfo(2)`）、`top -bn1`、`curl %{local_ip}`、同 uid 进程（`pgrep -x`）。

读不到（一律写 `null` 并在 `_unsupported` 中注明原因，绝不伪造）：

| 指标 | 原因 |
|---|---|
| `/proc/uptime` `/proc/stat` `/proc/net/*` | Permission denied |
| netlink（`ip` / `ss` / `netstat`） | `Cannot bind netlink socket` |
| `dumpsys battery/display/power` | 缺 `android.permission.DUMP` |
| `/sys/class/power_supply/*` | Permission denied |
| 电池 level / status / health | 改由 **Monitor Bridge**（BatteryManager 官方 API）提供 |
| 屏幕状态 | 改由 **Monitor Bridge**（PowerManager）提供 |
| 电池电流 | UNRELIABLE：thermal `ibat` 符号与充放电状态矛盾，直接放弃 |
| `android.boot_reason` | 该 ROM 未暴露 |
| Tailscale 进程 | 跨 uid（u0_a265），`/proc/<pid>` 不可见 → 由云端真实 SSH 连通性判定 |
| 已安装包数量 | Android 13 包可见性限制，`pm` 只见自身 |
| Wi-Fi SSID / 信号 | termux-api 无响应 → **UNSUPPORTED**（本次实测确认，不硬做） |

---

## 6. 历史数据现状（决定 V1.1 能算出什么）

| 项 | 值 |
|---|---|
| 云端最早样本 | `2026-08-29T20:12:32` |
| 云端样本数 | 2592 条（含早期更高频采样的混合节奏） |
| 覆盖时长 | 约 3 天 23 小时（**不足 7 天**） |
| 手机本机历史 | 2026-08-29 ~ 2026-09-02 共 5 个 jsonl |

**结论**：V1.1 的稳定性统计必须建立在「时间加权」而不是「样本计数」之上——
早期采样节奏与现在不同（2592 条 / 约 71 小时 ≠ 5 分钟一条），
用样本数直接相除会得出错误的在线率。详见 `server/stats.py` 的实现说明。

---

## 7. 已知风险 / 待办（本次审计发现，未处理）

1. `watchdog.log` 88 KB 且无轮转 → 长期运行会持续增长（低危，暂不动）。
2. `run.sh` pidfile 残留（PID 12588 已死）→ 若 Termux:Boot 再次触发，单实例检查会读到死 PID 并正常重启，无实际危害，但状态容易误读。
3. 采样节奏在历史中发生过变化 → 统计必须时间加权（已在上文说明）。
4. `rules.json` 中 `poll_interval_seconds=300` 必须与 timer 的 300s 保持一致，否则离线判定窗口会被静默拉偏。
5. `termux-api` 装了但不可用 → 任何依赖它的指标都不得进入采集脚本。
6. 云端 `fetch.py` 的 ADB 补充路径在 Linux 上恒为不可用（adb 二进制不存在），
   电量字段实际由 Monitor Bridge 提供。
