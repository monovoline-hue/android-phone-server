#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Mi10Pro Server Monitoring V1 - sampler loop
#
# Runs collect.sh every SAMPLE_INTERVAL seconds.
# Single-instance: re-running while alive is a no-op, so it is safe to call
# from Termux:Boot repeatedly (boot scripts can fire more than once).
#
# Modeled on the existing watchdog-sshd, which has proven stable on this
# device: pidfile + `kill -0` liveness + cmdline verification, so a recycled
# PID can never be mistaken for our own.
# ============================================================================

MON_DIR="${MON_DIR:-$HOME/zonira-monitor}"
CONF="$MON_DIR/monitor.conf"

[ -f "$CONF" ] && . "$CONF"
: "${SAMPLE_INTERVAL:=60}"
: "${HISTORY_RETENTION_DAYS:=7}"
: "${LOG_MAX_BYTES:=262144}"
: "${MAX_LOG_KEEP:=5}"

HISTORY_DIR="$MON_DIR/history"
LOG_DIR="$MON_DIR/logs"
LOG="$LOG_DIR/monitor.log"
RUNNER_LOG="$LOG_DIR/runner.log"
PIDFILE="$MON_DIR/zonira-monitor.pid"

mkdir -p "$MON_DIR" "$HISTORY_DIR" "$LOG_DIR" 2>/dev/null

now()  { date '+%Y-%m-%d %H:%M:%S%z'; }

rotate() {
  [ -f "$RUNNER_LOG" ] || return 0
  local sz i
  sz=$(wc -c < "$RUNNER_LOG" 2>/dev/null || echo 0)
  [ "${sz:-0}" -lt "$LOG_MAX_BYTES" ] && return 0
  for ((i = MAX_LOG_KEEP; i >= 1; i--)); do
    [ -f "$RUNNER_LOG.$i" ] && mv -f "$RUNNER_LOG.$i" "$RUNNER_LOG.$((i + 1))" 2>/dev/null
  done
  mv -f "$RUNNER_LOG" "$RUNNER_LOG.1" 2>/dev/null
  : > "$RUNNER_LOG" 2>/dev/null
}

log() {
  rotate
  printf '[%s] %s\n' "$(now)" "$*" >> "$RUNNER_LOG" 2>/dev/null
}

# --- single-instance guard -------------------------------------------------
if [ -f "$PIDFILE" ]; then
  old_pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [ -n "$old_pid" ] && [ "$old_pid" != "$$" ] && kill -0 "$old_pid" 2>/dev/null; then
    old_cmd=$(tr '\000' ' ' < "/proc/$old_pid/cmdline" 2>/dev/null || true)
    case "$old_cmd" in
      *zonira-monitor*|*run.sh*)
        log "already running pid=$old_pid; exiting"
        exit 0 ;;
    esac
  fi
fi

printf '%s\n' "$$" > "$PIDFILE" 2>/dev/null
cleanup() { rm -f "$PIDFILE" 2>/dev/null; }
trap cleanup EXIT INT TERM

# --- history pruning -------------------------------------------------------
# Bounded work: delete jsonl files whose date is older than retention.
prune_history() {
  local cutoff keep
  cutoff=$(date -d "-${HISTORY_RETENTION_DAYS} days" '+%Y-%m-%d' 2>/dev/null) || return 0
  [ -z "$cutoff" ] && return 0
  for f in "$HISTORY_DIR"/*.jsonl; do
    [ -e "$f" ] || continue
    keep=$(basename "$f" .jsonl)
    case "$keep" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
      *) continue ;;
    esac
    if [ "$keep" \< "$cutoff" ]; then
      rm -f "$f" 2>/dev/null && log "pruned history $keep"
    fi
  done
  # hard cap on total history size (~64 MiB) as a second line of defence
  local total
  total=$(du -sk "$HISTORY_DIR" 2>/dev/null | awk '{print $1}')
  if [ -n "$total" ] && [ "$total" -gt 65536 ]; then
    ls -1t "$HISTORY_DIR"/*.jsonl 2>/dev/null | tail -n +8 | while read -r old; do
      rm -f "$old" 2>/dev/null && log "pruned oversize history $(basename "$old")"
    done
  fi
}

log "runner start pid=$$ interval=${SAMPLE_INTERVAL}s retention=${HISTORY_RETENTION_DAYS}d"

PRUNE_COUNTER=0
while :; do
  if [ -x "$MON_DIR/collect.sh" ]; then
    "$MON_DIR/collect.sh" >> "$LOG_DIR/collect.out" 2>&1
    rc=$?
    [ "$rc" -ne 0 ] && log "collect.sh rc=$rc"
  else
    log "collect.sh missing or not executable"
  fi

  # prune once every ~60 samples (about once an hour at 60s)
  PRUNE_COUNTER=$(( PRUNE_COUNTER + 1 ))
  if [ "$PRUNE_COUNTER" -ge 60 ]; then
    prune_history
    PRUNE_COUNTER=0
  fi

  sleep "$SAMPLE_INTERVAL"
done
