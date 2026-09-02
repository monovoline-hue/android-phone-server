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
# (ss, netstat, sshd, stat, date) lives in the Termux prefix.
PATH="/data/data/com.termux/files/usr/bin:/system/bin:/system/xbin:$PATH"
export PATH

MON_DIR="${MON_DIR:-$HOME/zonira-monitor}"
LOG="$MON_DIR/watchdog.log"
INTERVAL=30

mkdir -p "$MON_DIR" 2>/dev/null
touch "$LOG" 2>/dev/null

log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

port_listening(){
  ss -ltn 2>/dev/null | grep -q ':8022' || netstat -ltn 2>/dev/null | grep -q ':8022'
}

while true; do
  # 1) sshd must listen on 8022 so the cloud server can pull current.json.
  if ! port_listening; then
    log "WARN sshd not listening on 8022, restarting"
    sshd 2>>"$LOG"
    sleep 2
    if port_listening; then
      log "OK sshd restarted and listening"
    else
      log "ERR sshd still down after restart attempt"
    fi
  fi

  # 2) current.json must be fresh (< 120s). If stale/missing, run collect.sh.
  if [ -f "$MON_DIR/current.json" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$MON_DIR/current.json" 2>/dev/null || echo 0) ))
    if [ "$age" -gt 120 ]; then
      log "WARN current.json stale (${age}s), running collect.sh"
      bash "$MON_DIR/collect.sh" >>"$LOG" 2>&1
    fi
  else
    log "WARN current.json missing, running collect.sh"
    bash "$MON_DIR/collect.sh" >>"$LOG" 2>&1
  fi

  sleep "$INTERVAL"
done
