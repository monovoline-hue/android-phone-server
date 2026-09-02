#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Mi10Pro Server Monitoring V1 - watchdog (resilient supervisor, no root)
#
# Purpose: keep sshd (8022) and collect.sh alive under MIUI battery/lock-screen
#          kills. Runs forever; safe to launch repeatedly (start-monitor.sh
#          guards against double-launch via pgrep).
#
# Design contract (same as collect.sh):
#   * READ-ONLY   - never writes outside ~/zonira-monitor/
#   * IDEMPOTENT  - restart actions are no-ops when already healthy
#   * ISOLATED    - a failing probe yields a log line, never aborts the loop
# ============================================================================

# Minimal PATH: under cron/Termux:Boot the env may be stripped; every probe
# (pgrep, sshd, stat, date) lives in the Termux prefix.
PATH="/data/data/com.termux/files/usr/bin:/system/bin:/system/xbin:$PATH"
export PATH

MON_DIR="${MON_DIR:-$HOME/zonira-monitor}"
LOG="$MON_DIR/watchdog.log"
EVENTS="$MON_DIR/events.jsonl"
# collect.sh is SINGLE-SHOT (no internal loop), so this watchdog IS the
# scheduler. 298s sleep + ~1.5s sampling keeps current.json on a ~300s cadence (5-min poll, lower battery drain).
INTERVAL=298

mkdir -p "$MON_DIR" 2>/dev/null
touch "$LOG" 2>/dev/null

log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# Emit a structured event line. One JSON per line; the cloud merge layer
# dedupes by (time,type), so a crash between append and log is harmless.
# V1.1 event contract: watchdog_restart is the phone-authoritative marker that
# lets the server classify a recovery as "automatic" instead of "unknown".
emit_event(){
  printf '{"time":"%s","type":"%s"}\n' "$(date -Iseconds)" "$1" >> "$EVENTS" 2>/dev/null
}

# Termux 的 ss/netstat 均不可用（netlink Permission denied / AF_INET 不支持），
# 改用 sshd 进程存活判定 8022（sshd 固定监听该端口；pgrep -x 精确匹配进程名，
# 不会匹配到本脚本自身）。云端能否 SSH 拉取才是最终验证。
port_listening(){
  pgrep -x sshd >/dev/null 2>&1
}

while true; do
  # 1) sshd must listen on 8022 so the cloud server can pull current.json.
  if ! port_listening; then
    log "WARN sshd not listening on 8022, restarting"
    emit_event "watchdog_restart"
    sshd 2>>"$LOG"
    sleep 2
    if port_listening; then
      log "OK sshd restarted and listening"
    else
      log "ERR sshd still down after restart attempt"
    fi
  fi

  # 2) Sample every cycle. Running collect.sh unconditionally (rather than
  #    waiting for a stale-timeout) keeps current.json on a ~300s cadence AND
  #    makes any failed sample self-heal on the very next cycle.
  bash "$MON_DIR/collect.sh" >>"$LOG" 2>&1
  rc=$?
  [ "$rc" -ne 0 ] && log "WARN collect.sh exited rc=$rc"

  sleep "$INTERVAL"
done
