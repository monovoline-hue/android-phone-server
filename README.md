# Android Phone Server

**版本：v0.1 Experimental** · **状态：长期无人值守稳定性验证中**

把一台退役的旧 Android 手机变成一台低功耗、永远在线的家庭小服务器，并配上一套诚实的远程监控系统。

> **诚实声明**：本项目处于实验阶段。所有在线率、事件、恢复统计均由真实历史数据计算，无法可靠获取的指标一律标注 `UNSUPPORTED`/`UNRELIABLE`，前端绝不编造数据。

---

## 目录

1. [项目简介](#项目简介)
2. [为什么用旧 Android 手机](#为什么用旧-android-手机)
3. [测试设备](#测试设备)
4. [系统架构](#系统架构)
5. [功能总览](#功能总览)
6. [安装部署](#安装部署)
7. [SSH 通道](#ssh-通道)
8. [Tailscale 组网](#tailscale-组网)
9. [Termux Boot 自启](#termux-boot-自启)
10. [看门狗](#看门狗)
11. [Monitor Bridge](#monitor-bridge)
12. [Dashboard](#dashboard)
13. [日志与数据](#日志与数据)
14. [稳定性统计口径](#稳定性统计口径)
15. [常见故障与处理](#常见故障与处理)
16. [MIUI 后台查杀问题](#miui-后台查杀问题)
17. [安全](#安全)
18. [已知限制](#已知限制)
19. [Roadmap](#roadmap)
20. [长期测试进度](#长期测试进度)

---

## 项目简介

这是一台**小米 10 Pro**（也可推广到其他 Android 手机），跑 Termux + sshd + 一个自研看门狗 + 一个电池/亮屏数据 Bridge APP，通过 Tailscale 组网接受云服务器定时拉取数据。云端负责判断、告警、统计并渲染一个静态 Dashboard。

核心原则：

- **手机只上报，云端来判断**——手机端采集器是无状态的诚实记录员
- **拿不到的数据就是 null**——不猜、不编、不用近似值冒充
- **事件只在状态变化时记录**——长期无事件 = 长期稳定，不是"没在工作"

## 为什么用旧 Android 手机

- 零成本复用退役设备，整机功耗远低于任何 x86 小主机
- 自带 UPS（电池），断电自动切换，天然适合无人值守
- Termux 提供完整的 Linux 用户态（bash / python / ssh / cron）
- 缺点也很诚实：MIUI 查杀、无 Root 权限边界、跨 uid 不可见（见[已知限制](#已知限制)）

## 测试设备

| 项 | 值 |
|---|---|
| 机型 | Xiaomi Mi 10 Pro (cmi) |
| 系统 | Android 13 / MIUI |
| Root | 否 |
| 网络 | Tailscale（WireGuard）VPN 内可达 |

> 本项目代码是通用的（Termux + 任意 Android），但阈值、温区文件名、Bridge 细节在 Mi 10 Pro 上验证。换机型需重新核对 `/sys/class/thermal/` 温区表。

## 系统架构

```
┌─────────────────────────── 手机（无人值守）───────────────────────────┐
│  Android 13                                                          │
│   ├─ Termux (uid u0_a264)                                            │
│   │    ├─ collect.sh        每 ~300s 采集一次 → current.json + history│
│   │    ├─ watchdog-sshd.sh  看门狗：sshd 保活 + 调度采集 + 事件落盘    │
│   │    ├─ start-monitor.sh  自举：唤醒锁 + sshd + 拉起看门狗           │
│   │    └─ ensure_sshd.sh    Tasker/MacroDroid 每 ~8min 兜底           │
│   ├─ Monitor Bridge APP (foreground service, 127.0.0.1:8765)         │
│   │    └─ BatteryManager / PowerManager 官方 API 数据源               │
│   └─ Tailscale (uid u0_a265，跨 uid 不可见，只能由云端实测)           │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ Tailscale SSH :8022（唯一通道）
┌──────────────────────────────┴───────────────────────────────────────┐
│  云服务器（systemd timer 每 5 分钟）                                   │
│   ├─ fetch.py      拉取 current.json/events.jsonl → 判断 → 告警       │
│   ├─ events.py     事件引擎：去抖/滞回/恢复分类（automatic|unknown）  │
│   ├─ stats.py      时间加权在线率/最长在线/最长故障                    │
│   ├─ report.py     7 天 / 30 天报告生成器                             │
│   ├─ prune_history.py  原子修剪滚动窗口                               │
│   └─ template.html → out/dashboard.html（mi10pro-web.service 静态服务）│
└──────────────────────────────────────────────────────────────────────┘
```

## 功能总览

- 每 ~5 分钟采集：运行时长、CPU、内存、存储、电池温度/电压、服务存活
- Bridge 在线时补充：电量百分比、充电状态、亮屏状态（官方 API，非猜测）
- 云端判断 + 告警（阈值在 `rules.json` / `monitor.conf`）
- 结构化事件日志（`events.jsonl`）：仅在状态变化时记录，去抖 + 温度滞回
- 时间加权稳定性统计：整体/分项在线率、最长连续在线、最长故障
- 7 天 / 30 天报告（数据不足时拒绝生成或明确标注 PARTIAL）
- 离线可用的单文件 Dashboard（数据内联，file:// 直接打开）

## 安装部署

### 手机端（Termux）

```bash
# 1. 安装 Termux（F-Droid 版，勿用 Play 商店旧版）
# 2. 开启 sshd 并配置公钥登录
# 3. 部署脚本
mkdir -p ~/zonira-monitor
# 把 device/ 下的 collect.sh watchdog-sshd.sh start-monitor.sh 复制进去并 chmod 700
cp device/monitor.example.conf ~/zonira-monitor/monitor.conf  # 填真实值
# 4. 配置 ~/.termux/boot/ 与 Termux:Boot 自启，Tasker/MacroDroid 兜底
```

### 云端

```bash
sudo mkdir -p /opt/mi10pro-monitor/dashboard
# 复制 server/ 下的脚本；配置 ~/.ssh/config 的 mi10pro 别名
cp .env.example /opt/mi10pro-monitor/.env   # 填真实值
# systemd 单元：
#   mi10pro-fetch.timer/service  每 5 分钟跑 fetch.py
#   mi10pro-prune.timer/service  每日跑 prune_history.py
#   mi10pro-web.service          http.server 8080 静态服务 out/
```

## SSH 通道

- 手机跑 Termux `sshd`，监听 8022，仅公钥认证
- 云端通过 `~/.ssh/config` 别名 `mi10pro` 连接（真实 IP 只存云端，不入库）
- `ss/netstat` 在 Termux 下不可用（netlink 权限拒绝），端口判定用 `pgrep -x sshd` + 云端真实连接验证

## Tailscale 组网

- 手机与云服务器同一 tailnet，云端唯一通道是 Tailscale SSH
- **诚实说明**：Termux 流量绕过 Tailscale tun（Android VpnService），手机自身无法发现/验证 Tailscale 状态（跨 uid 也不可见）。因此 `tailscale_up` 恒为 null，可达性由云端实测——这使"Tailscale 可达率"与"整体在线率"**同源**，不是独立证据，统计里明确标注

## Termux Boot 自启

- `~/.termux/boot/` 放置自启脚本；配合 Termux:Boot 应用
- `start-monitor.sh` 幂等：唤醒锁 → 确保 sshd → 立即采集一次 → 拉起看门狗（pgrep 防重）
- 另有 Tasker/MacroDroid 每 ~8 分钟调用 `ensure_sshd.sh` 兜底（RUN_COMMAND 不触发 Boot 钩子，所以兜底自己保证监控复活）

## 看门狗

`watchdog-sshd.sh` 是一个死循环（298s 睡眠 + ~1.5s 采集 ≈ 300s 节奏）：

1. `pgrep -x sshd` 判定 sshd 存活；不存活 → 记 `watchdog_restart` 事件到 `events.jsonl` → 重启 sshd
2. 无条件跑一次 `collect.sh`（失败自愈于下一循环）
3. 事件行是手机侧权威标记：云端据此把恢复分类为 `automatic`（否则诚实记 `unknown`）

## Monitor Bridge

一个独立小 APK（前台服务，`127.0.0.1:8765`，schema `zonira-monitor-bridge/v1`）：

- Termux 无 Root 读不到 `/sys/class/power_supply`，Bridge 用 **BatteryManager/PowerManager 官方 API** 提供电量百分比、充电状态、电压、亮屏状态
- 健康判定 = HTTP 成功 + schema 匹配；Bridge 挂了最多损失 2s，采集照常降级
- 源码在 `bridge/`（`build.sh` 一键构建）

## Dashboard

单文件 `dashboard.html`（数据内联 JSON，离线可用）：

- 顶部：当前状态 + 告警
- 趋势图：电池/温度/CPU/内存/存储
- **【稳定性】**：在线率卡片矩阵 + 最长连续在线/最长故障 + 口径备注
- **【最近事件】**：最近 30 条状态变化事件（心跳心跳行折叠显示）
- 刻意克制的配色，无花哨动画

## 日志与数据

| 文件 | 位置 | 说明 |
|---|---|---|
| `current.json` | 手机 `~/zonira-monitor/` | 最新一帧采集（原子写） |
| `history/YYYY-MM-DD.jsonl` | 手机 | 每日一文件，滚动保留 7 天 |
| `events.jsonl` | 手机 + 云端 | 结构化事件（一行一 JSON） |
| `watchdog.log` / `logs/monitor.log` | 手机 | 轮转日志（256KB × 5） |
| `history.jsonl` | 云端 `dashboard/data/` | 云端聚合历史（30 天滚动窗口） |
| `stats.json` | 云端 | 最新统计快照 |
| `reports/7day-report.md` 等 | 云端/本地 | 长期报告 |

## 稳定性统计口径

- **时间加权，不是样本数平均**：每个样本管辖到下一个样本为止的区间（历史上有 60s/300s 两种节奏，数样本会算错）
- **空缺截断**：样本间隔超过 2× 轮询间隔时按比例截断——无法区分"手机离线"和"采集器没跑"，宁可少算故障
- **整体在线率** = 可达时间占比；**SSH/Termux/Bridge 在线率** = 在"设备可观测时间"内的占比（设备整体离线不计入分母）
- **恢复分类**：`automatic` 仅当故障窗口内存在手机侧 `watchdog_restart` 标记；其余一律 `unknown`；`manual` 永不自动赋值（没有任何可靠信号能区分"人动了"和"自己好了"）
- **事件去抖**：连续 2 次观测才确认状态翻转；温度事件带滞回 + 冷却

## 常见故障与处理

| 现象 | 首查 |
|---|---|
| 手机整夜离线 | MIUI 查杀（见下节）、是否充电/息屏策略 |
| SSH 连不上但手机在线 | `pgrep -x sshd`、看门狗日志、Termux 是否被杀 |
| Bridge 字段全 null | Bridge APP 是否存活、`127.0.0.1:8765/status` schema |
| 电量/电压为估算值 | 看 `voltage_source` 字段：`thermal_vbat_estimated` ≈ ±1% |
| 在线率突然变差 | 先看 `max_data_gap_seconds`，空缺截断口径是否变化 |

## MIUI 后台查杀问题

这是本项目最大的敌人，已验证的缓解组合：

1. **termux-wake-lock**（部分唤醒锁）——第一道防线
2. **Termux:Boot** 自启 + `.bashrc` 自举
3. **Tasker/MacroDroid 兜底**：RUN_COMMAND 每 ~8 分钟 `ensure_sshd.sh`（不依赖 Boot/bachrc 钩子）
4. MIUI 设置里对 Termux/Tailscale/Bridge：自启动、省电策略无限制、锁定最近任务
5. 仍然会被杀：观察期已确认多次整段小时级的 Termux 冻结（这正是本项目要长期量化的东西）

## 安全

- 手机端仅公钥 SSH，监听 Tailscale 网内（不暴露公网）
- 仓库**不含**任何真实 IP / 密钥 / 设备 ID：`.gitignore` 排除全部运行数据，只有 `*.example` 模板入库
- 真实 Tailscale IP 只存在于手机 `monitor.conf` 与云端 `~/.ssh/config`
- 上传前跑一遍密钥/私网段扫描（见 `CURRENT_ARCHITECTURE.md` 的安全审计章节）

## 已知限制

- **无 Root 权限边界**：`/proc/uptime`、`/proc/stat`、`/proc/net/*`、netlink、`dumpsys`、`/sys/class/power_supply` 全部 `PERMISSION_DENIED`（采集器头部注释有完整清单与绕行方案）
- **跨 uid 不可见**：Tailscale(u0_a265) 对 Termux(u0_a264) 完全不可见
- `termux-api` 挂起无输出（实测 8s 无响应）→ `wifi_changed` 事件不支持
- 电压为热敏温区估算（±1%）或 Bridge 官方值，`current_ma` 符号与充电状态矛盾 → `UNRELIABLE`
- 电池电量在 Bridge 挂掉且无 ADB 补充时不可得（诚实 null）
- Bridge `boot_reason` 该构建不暴露 → `UNSUPPORTED`

## Roadmap

- [x] V1.0 采集 + 判断 + Dashboard
- [x] V1.1 事件日志 + 时间加权统计 + 报告生成器
- [ ] 30 天完整稳定版报告（数据积累中）
- [ ] 事件恢复分类细分（区分 ensure_sshd 快路径与看门狗慢路径）
- [ ] 更换机型验证（温区表、Bridge 兼容性）
- [ ] 桥接 APP 独立仓库 + 签名发布

## 长期测试进度

- **开始时间**：2026-08-29（历史数据起点）
- **当前覆盖**：约 4 天（截至 2026-09-02，2600+ 样本）
- **初步观察**：整体在线率 ~73%（两次 >9 小时的 Termux 冻结窗口，手机自身在该时段也停止采集，确认为 MIUI 查杀而非采集失真）；SSH/Termux/Bridge 在可观测时间内均 100%
- **下一个检查点**：数据满 7 天后生成第一份完整 7 天报告
- 报告将随数据积累持续追加于 `reports/`（数据不足时生成器会拒绝，绝不硬凑）

---

*本项目由 AI 辅助构建与维护，配合 ZONIRA 项目体系（P004）管理。*
