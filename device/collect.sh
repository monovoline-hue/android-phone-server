#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Mi10Pro Server Monitoring V1 - collector
#
# Design contract:
#   * READ-ONLY   - never writes outside ~/zonira-monitor/
#   * IDEMPOTENT  - safe to run repeatedly, no state assumptions
#   * ISOLATED    - a failing probe yields null, never aborts the whole run
#   * HONEST      - unavailable metrics are null, never guessed or faked
#   * FAST        - target < 5s per invocation
#
# Permission reality on Android 13 (Termux uid, no root):
#   /proc/uptime /proc/loadavg /proc/stat /proc/net/*  -> PERMISSION_DENIED
#   netlink (ip/ifconfig)                              -> PERMISSION_DENIED
#   dumpsys battery / display / power                  -> missing DUMP perm
#   /sys/class/power_supply/*                          -> PERMISSION_DENIED
# Workarounds used below (all verified on device):
#   uptime/loadavg -> `uptime` (uses sysinfo(2), bypasses /proc perms)
#   cpu usage      -> `top -bn1` (~0.28s)
#   battery temp   -> /sys/class/thermal/thermal_zone<type=battery>/temp
#   lan ip         -> curl %{local_ip} (userspace socket, bypasses netlink)
# ============================================================================

# Never trust an inherited PATH: under Termux:Boot or cron the environment may
# be minimal, and every probe here (getprop, uptime, pgrep, top) lives in the
# Termux prefix rather than /system/bin.
PATH="/data/data/com.termux/files/usr/bin:/system/bin:/system/xbin:$PATH"
export PATH

MON_DIR="${MON_DIR:-$HOME/zonira-monitor}"
CONF="$MON_DIR/monitor.conf"
OUT="$MON_DIR/current.json"
TMP="$MON_DIR/.current.json.tmp"
HISTORY_DIR="$MON_DIR/history"
LOG_DIR="$MON_DIR/logs"
LOG="$LOG_DIR/monitor.log"
CACHE_DIR="$MON_DIR/.cache"

mkdir -p "$MON_DIR" "$HISTORY_DIR" "$LOG_DIR" "$CACHE_DIR" 2>/dev/null

# ---------------------------------------------------------------------------
# Config (defaults; monitor.conf overrides - rules stay out of core logic)
# ---------------------------------------------------------------------------
SAMPLE_INTERVAL=60
HISTORY_RETENTION_DAYS=7
LOG_MAX_BYTES=262144          # 256 KiB
MAX_LOG_KEEP=5
LAN_IP_PROBE_URL="http://1.1.1.1"
LAN_IP_CACHE_TTL=600          # seconds
TAILSCALE_PKG="com.tailscale.ipn"
TERMUX_PKG="com.termux"
TAILSCALE_IP=""               # filled by monitor.conf; not self-discoverable
                              # (Termux egress bypasses the Tailscale tun)
POLL_TIMEOUT=5
TOP_TIMEOUT=5
CURL_TIMEOUT=4

[ -f "$CONF" ] && . "$CONF"

NOW_EPOCH=$(date +%s)
START_MS=$(( $(date +%s%N 2>/dev/null || echo "$NOW_EPOCH"000000000) / 1000000 ))
TS_ISO=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
rotatelog() {
  [ -f "$LOG" ] || return 0
  local sz
  sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  [ "${sz:-0}" -lt "$LOG_MAX_BYTES" ] && return 0
  local i
  for ((i = MAX_LOG_KEEP; i >= 1; i--)); do
    [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))" 2>/dev/null
  done
  mv -f "$LOG" "$LOG.1" 2>/dev/null
  : > "$LOG" 2>/dev/null
}

logline() {
  rotatelog
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
}

# Emit a JSON value: number, quoted string, or null.
jval() {
  case "$1" in
    null|'""') printf '%s' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

# Sanitise a string for JSON: keep only safe chars, else null.
# NOTE: the '-' MUST stay last in the tr class. Placing it before the space
# ("+- ") makes tr read it as the invalid range '+'..' ', which errors out and
# silently returns an empty string - that bug once blanked every getprop field.
jstr() {
  local s="$1"
  case "$s" in
    '' ) printf 'null' ;;
    * ) printf '"%s"' "$(printf '%s' "$s" | tr -cd 'A-Za-z0-9_.:/ +-' | cut -c1-64)" ;;
  esac
}

# --- thermal table ---------------------------------------------------------
# 92 zones exist on this device. Reading each with `cat` forks a process per
# file (2 per zone), which alone pushed a run to ~9s. We load every zone once
# using bash's builtin `read` (no fork) and query the table in-memory after.
THERMAL_TABLE=""

thermal_load() {
  local z t v
  THERMAL_TABLE=""
  for z in /sys/class/thermal/thermal_zone*; do
    read -r t < "$z/type" 2>/dev/null || continue
    read -r v < "$z/temp" 2>/dev/null || continue
    [ -z "$t" ] && continue
    THERMAL_TABLE="${THERMAL_TABLE}${t}=${v}"$'\n'
  done
}

# $1 = exact type name. Prints millidegrees, or nothing if absent.
thermal_by_type() {
  local k v
  while IFS='=' read -r k v; do
    if [ "$k" = "$1" ]; then
      printf '%s' "$v"
      return 0
    fi
  done <<< "$THERMAL_TABLE"
  return 1
}

# Max across zones matching prefix AND suffix.
# Both matter: "cpu-0-0-usr" is a real sensor while "cpu-0-0-step" is a
# threshold ladder value; mixing them would skew the reading.
thermal_max_pattern() {
  local pre="$1" suf="$2" k v max="" 
  while IFS='=' read -r k v; do
    case "$k" in
      "$pre"*"$suf") ;;
      *) continue ;;
    esac
    case "$v" in
      ''|*[!0-9-]*) continue ;;
    esac
    if [ -z "$max" ] || [ "$v" -gt "$max" ]; then max=$v; fi
  done <<< "$THERMAL_TABLE"
  [ -n "$max" ] && printf '%s' "$max" || return 1
}

# ---------------------------------------------------------------------------
# Probes - each returns "" on failure so the caller can emit null
# ---------------------------------------------------------------------------

probe_uptime_seconds() {
  local boot="$UPTIME_SINCE" up
  [ -z "$boot" ] && return 1
  up=$(date -d "$boot" +%s 2>/dev/null) || return 1
  [ -z "$up" ] && return 1
  printf '%s' $(( NOW_EPOCH - up ))
}

probe_boot_time() {
  [ -n "$UPTIME_SINCE" ] && printf '%s' "$UPTIME_SINCE"
}

probe_load() {
  # " 19:50:36 up 21 min,  load average: 0.04, 0.32, 0.63" -> "0.04 0.32 0.63"
  [ -z "$UPTIME_LINE" ] && return 1
  printf '%s' "$UPTIME_LINE" | sed -n 's/.*load average:[[:space:]]*//p' | tr -d ' ' | tr ',' ' '
}

probe_cpu_usage() {
  # top -bn1: "800%cpu 0%user 0%nice 0%sys 800%idle 0%iow 0%irq 0%sirq 0%host"
  local line total idle
  line=$(timeout "$TOP_TIMEOUT" top -bn1 2>/dev/null | grep -E '^[0-9]+%cpu' | head -1)
  [ -z "$line" ] && return 1
  total=$(printf '%s' "$line" | grep -oE '[0-9]+%cpu'  | head -1 | tr -dc '0-9')
  idle=$(printf  '%s' "$line" | grep -oE '[0-9]+%idle' | head -1 | tr -dc '0-9')
  [ -z "$total" ] || [ -z "$idle" ] && return 1
  [ "$total" -le 0 ] && return 1
  awk -v t="$total" -v i="$idle" 'BEGIN{ printf "%.1f", (t - i) * 100 / t }'
}

probe_mem() {
  # prints "total_kb avail_kb"
  local t a
  t=$(awk '/^MemTotal:/     {print $2; exit}' /proc/meminfo 2>/dev/null)
  a=$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null)
  [ -z "$t" ] || [ -z "$a" ] && return 1
  printf '%s %s' "$t" "$a"
}

probe_storage() {
  # df -k on the /data-backed path -> "total_kb used_kb avail_kb"
  local line
  line=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $2, $3, $4; exit}')
  [ -z "$line" ] && return 1
  printf '%s' "$line"
}

probe_lan_ip() {
  # curl's %{local_ip} is resolved in userspace -> no netlink permission needed.
  # Cached, because this is the only egress-dependent probe.
  local cache="$CACHE_DIR/lan_ip" age=999999 ip
  if [ -f "$cache" ]; then
    age=$(( NOW_EPOCH - $(stat -c %Y "$cache" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$LAN_IP_CACHE_TTL" ] && { cat "$cache" 2>/dev/null; return 0; }
  fi
  ip=$(timeout "$CURL_TIMEOUT" curl -s -o /dev/null \
        -w '%{local_ip}' --connect-timeout 3 "$LAN_IP_PROBE_URL" 2>/dev/null)
  case "$ip" in
    ''|*[!0-9.]*) [ -f "$cache" ] && { cat "$cache" 2>/dev/null; return 0; }; return 1 ;;
  esac
  printf '%s' "$ip" > "$cache" 2>/dev/null
  printf '%s' "$ip"
}

# Exact process-name match. Deliberately NOT `pgrep -f`:
# -f matches full command lines, so `pgrep -f com.tailscale.ipn` run from a
# shell matches THAT SHELL (its cmdline contains the pattern) - a guaranteed
# false positive. -x matches the process name only and cannot self-match.
proc_alive() {
  pgrep -x "$1" >/dev/null 2>&1
}

probe_sshd_pid() {
  pgrep -x sshd 2>/dev/null | head -1
}

probe_port_8022() {
  timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8022' >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------
logline "collect start"

# Load the thermal table once - every temperature probe reads from memory.
thermal_load

# --- android ---
# One `uptime` call feeds both the boot timestamp and the load averages.
UPTIME_SINCE=$(timeout "$POLL_TIMEOUT" uptime -s 2>/dev/null)
UPTIME_LINE=$(timeout "$POLL_TIMEOUT" uptime 2>/dev/null)
ANDROID_VERSION=$(getprop ro.build.version.release 2>/dev/null)
MIUI_VERSION=$(getprop ro.miui.ui.version.name 2>/dev/null)
BUILD_ID=$(getprop ro.build.display.id 2>/dev/null)
MODEL=$(getprop ro.product.model 2>/dev/null)
KERNEL=$(uname -r 2>/dev/null)
BOOT_COMPLETED=$(getprop sys.boot_completed 2>/dev/null)
UPTIME_S=$(probe_uptime_seconds)
BOOT_TIME=$(probe_boot_time)
BOOT_REASON=$(getprop ro.boot.bootreason 2>/dev/null)   # empty on this device

# --- battery ---
# temperature: verified identical to `dumpsys battery` (317 == 31.7C)
BAT_TEMP_RAW=$(thermal_by_type "battery" 2>/dev/null)
BAT_TEMP=""
[ -n "$BAT_TEMP_RAW" ] && BAT_TEMP=$(awk -v v="$BAT_TEMP_RAW" 'BEGIN{printf "%.1f", v/1000}')

# voltage: thermal pm8150b-vbat-lvl0 tracks `dumpsys battery voltage` within ~1%.
# Marked as estimated. (ibat zones were rejected: sign contradicts charge state.)
BAT_VOLT_RAW=$(thermal_by_type "pm8150b-vbat-lvl0" 2>/dev/null)
BAT_VOLT=""
[ -n "$BAT_VOLT_RAW" ] && BAT_VOLT="$BAT_VOLT_RAW"

# level/status/charging/current: no readable source without root/Termux:API.
BAT_LEVEL=""
BAT_STATUS=""
BAT_CHARGING=""
BAT_CURRENT=""
BAT_HEALTH=""
# how voltage was obtained: "thermal_vbat_estimated" (on-device, ~1% off) or
# "adb" (real fuel gauge, preferred whenever a fresh supplement exists)
[ -n "$BAT_VOLT" ] && VOLT_SRC="thermal_vbat_estimated" || VOLT_SRC=""

# Optional ADB-side supplement (written by the workstation adb-enrich tool).
# Only honoured when fresh, so a stale USB session cannot feed stale numbers.
ADB_SUP="$MON_DIR/battery_adb.json"
if [ -f "$ADB_SUP" ]; then
  SUP_AGE=$(( NOW_EPOCH - $(stat -c %Y "$ADB_SUP" 2>/dev/null || echo 0) ))
  if [ "$SUP_AGE" -le "${ADB_SUPPLEMENT_MAX_AGE:-600}" ]; then
    LVL=$(sed -n 's/.*"level"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$ADB_SUP" 2>/dev/null | head -1)
    ST=$(sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\?\([A-Za-z-]*\)"\?.*/\1/p' "$ADB_SUP" 2>/dev/null | head -1)
    CHG=$(sed -n 's/.*"charging"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p' "$ADB_SUP" 2>/dev/null | head -1)
    VOL=$(sed -n 's/.*"voltage_mv"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$ADB_SUP" 2>/dev/null | head -1)
    HLT=$(sed -n 's/.*"health"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$ADB_SUP" 2>/dev/null | head -1)
    [ -n "$LVL" ] && BAT_LEVEL="$LVL"
    [ -n "$ST" ]  && BAT_STATUS="$ST"
    [ -n "$CHG" ] && BAT_CHARGING="$CHG"
    # ADB reads the real fuel gauge, so prefer it over the thermal estimate.
    if [ -n "$VOL" ]; then
      BAT_VOLT="$VOL"
      VOLT_SRC="adb"
    fi
    [ -n "$HLT" ] && BAT_HEALTH="$HLT"
  fi
fi

# --- cpu ---
CPU_LOAD=$(probe_load)
LOAD_1=$(printf '%s' "$CPU_LOAD" | awk '{print $1}')
LOAD_5=$(printf '%s' "$CPU_LOAD" | awk '{print $2}')
LOAD_15=$(printf '%s' "$CPU_LOAD" | awk '{print $3}')
CPU_USAGE=$(probe_cpu_usage)
CPU_TEMP_RAW=$(thermal_max_pattern "cpu" "-usr" 2>/dev/null)
CPU_TEMP=""
[ -n "$CPU_TEMP_RAW" ] && CPU_TEMP=$(awk -v v="$CPU_TEMP_RAW" 'BEGIN{printf "%.1f", v/1000}')
CPU_CORES=$(ls -d /sys/devices/system/cpu/cpu[0-9]* 2>/dev/null | wc -l)

# --- memory ---
MEM=$(probe_mem)
MEM_TOTAL_MB=""
MEM_AVAIL_MB=""
MEM_USED_PCT=""
if [ -n "$MEM" ]; then
  MEM_TOTAL_MB=$(printf '%s' "$MEM" | awk '{printf "%d", $1/1024}')
  MEM_AVAIL_MB=$(printf '%s' "$MEM" | awk '{printf "%d", $2/1024}')
  MEM_USED_PCT=$(printf '%s' "$MEM" | awk '{printf "%.1f", ($1-$2)*100/$1}')
fi
SWAP_TOTAL_MB=$(awk '/^SwapTotal:/ {printf "%d", $2/1024; exit}' /proc/meminfo 2>/dev/null)
SWAP_FREE_MB=$(awk '/^SwapFree:/  {printf "%d", $2/1024; exit}' /proc/meminfo 2>/dev/null)

# --- storage ---
STO=""
STO_TOTAL_GB=""
STO_USED_GB=""
STO_FREE_GB=""
STO_USED_PCT=""
STO=$(probe_storage)
if [ -n "$STO" ]; then
  STO_TOTAL_GB=$(printf '%s' "$STO" | awk '{printf "%.1f", $1/1048576}')
  STO_USED_GB=$(printf '%s'  "$STO" | awk '{printf "%.1f", $2/1048576}')
  STO_FREE_GB=$(printf '%s'  "$STO" | awk '{printf "%.1f", $3/1048576}')
  STO_USED_PCT=$(printf '%s' "$STO" | awk '{printf "%.1f", $2*100/$1}')
fi

# --- network ---
LAN_IP=$(probe_lan_ip)
# Internet egress probe - separate from probe_lan_ip (that one is cached and
# answers "what is our LAN address", not "can we reach the internet right
# now"). connect-timeout 2 keeps the worst case cheap: offline -> ~2s, online
# -> <100ms. This tests plain internet egress only: Termux traffic bypasses
# the Tailscale tun, so it says nothing about Tailscale reachability.
probe_internet() {
  timeout 3 curl -s -o /dev/null --connect-timeout 2 "$LAN_IP_PROBE_URL" 2>/dev/null
}
NET_UP="null"; probe_internet && NET_UP="true" || NET_UP="false"
# Tailscale runs as a DIFFERENT uid (u0_a265) than Termux (u0_a264). Android's
# process isolation makes /proc/<pid> invisible and `ps` blind to it, so the
# device genuinely cannot observe its own Tailscale process - every workaround
# self-matches and yields a false positive. We therefore report null and let
# the server decide: it judges reachability by actually connecting over
# Tailscale SSH, which is the only meaningful test anyway.
TS_UP="null"

# --- services ---
TERMUX_UP="false"; proc_alive "$TERMUX_PKG"    && TERMUX_UP="true"
SSHD_PID=$(probe_sshd_pid)
SSHD_UP="false";   [ -n "$SSHD_PID" ]          && SSHD_UP="true"
TCP_8022="false";  probe_port_8022             && TCP_8022="true"
WD_UP="false";     pgrep -f "watchdog-sshd" >/dev/null 2>&1 && WD_UP="true"

# --- display ---
# dumpsys/cmd/settings all require permissions the Termux uid lacks.
DISPLAY_STATE=""

# --- package count ---
# Android 13 package-visibility restricts `pm list packages` to self only.
PKG_COUNT=""

# ---------------------------------------------------------------------------
# ZONIRA Monitor Bridge (V1.1): official-API battery + display over localhost.
# Health rule: HTTP success + schema match, nothing else. No pgrep -f (proven
# false positives). A dead/hung bridge costs at most 2s and leaves every
# field at its pre-bridge value (null or thermal estimate) - collect continues.
# ---------------------------------------------------------------------------
BRIDGE_UP="false"
DISPLAY_INTERACTIVE=""
BR_JSON=$(curl --max-time 2 -s http://127.0.0.1:8765/status 2>/dev/null)
case "$BR_JSON" in
  *'"zonira-monitor-bridge/v1"'*)
    BRIDGE_UP="true"
    jget() { printf '%s' "$BR_JSON" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\($2\).*/\1/p" | head -1; }
    BP=$(jget percentage '[0-9]*')
    BST=$(jget status '"[A-Z_()0-9]*"' | tr -d '"')
    BCH=$(jget charging 'true\|false')
    BPLG=$(jget plugged '"[A-Z_()0-9]*"' | tr -d '"')
    BHL=$(jget health '"[A-Z_()0-9]*"' | tr -d '"')
    BTMP=$(jget temperature_c '[0-9.]*')
    BVT=$(jget voltage_mv '[0-9]*')
    BDS=$(jget state '"ON"\|"OFF"' | tr -d '"')
    BIN=$(jget interactive 'true\|false')
    # Bridge reads the real fuel gauge (BatteryManager) - it outranks both the
    # thermal estimates and the ADB supplement. Individual fields degrade
    # independently: a missing one simply keeps its earlier value.
    [ -n "$BP" ]  && BAT_LEVEL="$BP"
    [ -n "$BST" ] && BAT_STATUS="$BST"
    [ -n "$BCH" ] && BAT_CHARGING="$BCH"
    [ -n "$BHL" ] && BAT_HEALTH="$BHL"
    [ -n "$BVT" ] && { BAT_VOLT="$BVT"; VOLT_SRC="zonira-monitor-bridge"; }
    [ -n "$BTMP" ] && BAT_TEMP="$BTMP"
    [ -n "$BDS" ] && DISPLAY_STATE="$BDS"
    [ -n "$BIN" ] && DISPLAY_INTERACTIVE="$BIN"
    ;;
esac

# ---------------------------------------------------------------------------
# Emit JSON
# ---------------------------------------------------------------------------
END_MS=$(( $(date +%s%N 2>/dev/null || echo "$NOW_EPOCH"000000000) / 1000000 ))
DURATION_MS=$(( END_MS - START_MS ))

{
printf '{\n'
printf '  "schema": "mi10pro-monitor/v1",\n'
printf '  "timestamp": "%s",\n' "$TS_ISO"
printf '  "epoch": %s,\n' "$NOW_EPOCH"
printf '  "hostname": %s,\n' "$(jstr "${MODEL:-localhost}")"
printf '  "collector": { "interval_seconds": %s, "duration_ms": %s },\n' \
  "$SAMPLE_INTERVAL" "$DURATION_MS"
printf '  "android": {\n'
printf '    "boot_completed": %s,\n' "$(jstr "$BOOT_COMPLETED")"
printf '    "uptime_seconds": %s,\n' "${UPTIME_S:-null}"
printf '    "boot_time": %s,\n' "$(jstr "$BOOT_TIME")"
printf '    "boot_reason": %s,\n' "$(jstr "$BOOT_REASON")"
printf '    "android_version": %s,\n' "$(jstr "$ANDROID_VERSION")"
printf '    "miui_version": %s,\n' "$(jstr "$MIUI_VERSION")"
printf '    "build_id": %s,\n' "$(jstr "$BUILD_ID")"
printf '    "kernel": %s,\n' "$(jstr "$KERNEL")"
printf '    "security_patch": %s\n' "$(jstr "$(getprop ro.build.version.security_patch 2>/dev/null)")"
printf '  },\n'
printf '  "battery": {\n'
printf '    "level": %s,\n' "${BAT_LEVEL:-null}"
printf '    "status": %s,\n' "$(jstr "$BAT_STATUS")"
printf '    "charging": %s,\n' "${BAT_CHARGING:-null}"
printf '    "plugged": %s,\n' "$(jstr "${BAT_PLUGGED:-$BPLG}")"
printf '    "temperature_c": %s,\n' "${BAT_TEMP:-null}"
printf '    "voltage_mv": %s,\n' "${BAT_VOLT:-null}"
printf '    "voltage_source": %s,\n' "$(jstr "$VOLT_SRC")"
printf '    "current_ma": %s,\n' "${BAT_CURRENT:-null}"
printf '    "health": %s,\n' "$(jstr "$BAT_HEALTH")"
printf '    "source": %s\n' "$( [ "$BRIDGE_UP" = "true" ] && echo '"zonira-monitor-bridge"' || { [ -n "$BAT_LEVEL" ] && echo '"adb-supplement"' || echo 'null'; } )"
printf '  },\n'
printf '  "cpu": {\n'
printf '    "usage_percent": %s,\n' "${CPU_USAGE:-null}"
printf '    "load_1": %s,\n' "${LOAD_1:-null}"
printf '    "load_5": %s,\n' "${LOAD_5:-null}"
printf '    "load_15": %s,\n' "${LOAD_15:-null}"
printf '    "temperature_c": %s,\n' "${CPU_TEMP:-null}"
printf '    "cores": %s\n' "${CPU_CORES:-null}"
printf '  },\n'
printf '  "memory": {\n'
printf '    "total_mb": %s,\n' "${MEM_TOTAL_MB:-null}"
printf '    "available_mb": %s,\n' "${MEM_AVAIL_MB:-null}"
printf '    "used_percent": %s,\n' "${MEM_USED_PCT:-null}"
printf '    "swap_total_mb": %s,\n' "${SWAP_TOTAL_MB:-null}"
printf '    "swap_free_mb": %s\n' "${SWAP_FREE_MB:-null}"
printf '  },\n'
printf '  "storage": {\n'
printf '    "total_gb": %s,\n' "${STO_TOTAL_GB:-null}"
printf '    "used_gb": %s,\n' "${STO_USED_GB:-null}"
printf '    "free_gb": %s,\n' "${STO_FREE_GB:-null}"
printf '    "used_percent": %s\n' "${STO_USED_PCT:-null}"
printf '  },\n'
printf '  "network": {\n'
printf '    "internet_reachable": %s,\n' "$NET_UP"
printf '    "lan_ip": %s,\n' "$(jstr "$LAN_IP")"
printf '    "tailscale_ip": %s,\n' "$(jstr "$TAILSCALE_IP")"
printf '    "tailscale_up": %s\n' "$TS_UP"
printf '  },\n'
printf '  "services": {\n'
printf '    "termux": %s,\n' "$TERMUX_UP"
printf '    "sshd": %s,\n' "$SSHD_UP"
printf '    "sshd_pid": %s,\n' "${SSHD_PID:-null}"
printf '    "tcp_8022": %s,\n' "$TCP_8022"
printf '    "monitor_bridge": %s,\n' "$BRIDGE_UP"
printf '    "watchdog": %s\n' "$WD_UP"
printf '  },\n'
printf '  "display": {\n'
printf '    "state": %s,\n' "$(jstr "$DISPLAY_STATE")"
printf '    "interactive": %s,\n' "${DISPLAY_INTERACTIVE:-null}"
printf '    "source": %s\n' "$( [ "$BRIDGE_UP" = "true" ] && echo '"zonira-monitor-bridge"' || echo 'null' )"
printf '  },\n'
printf '  "packages": { "installed_count": %s, "note": %s },\n' \
  "${PKG_COUNT:-null}" '"unavailable: Android 13 package-visibility limits pm to self"'
printf '  "_unsupported": {\n'
if [ "$BRIDGE_UP" = "true" ]; then
  printf '    "battery.current_ma": "UNRELIABLE: thermal ibat sign contradicts charge state",\n'
  printf '    "android.boot_reason": "UNSUPPORTED: not exposed on this build",\n'
else
  printf '    "battery.level": "PERMISSION_DENIED: /sys/class/power_supply (no root, no Termux:API, bridge down)",\n'
  printf '    "battery.current_ma": "UNRELIABLE: thermal ibat sign contradicts charge state",\n'
  printf '    "display.state": "PERMISSION_DENIED: dumpsys requires android.permission.DUMP",\n'
  printf '    "android.boot_reason": "UNSUPPORTED: not exposed on this build",\n'
fi
printf '    "network.tailscale_ip": "NOT_SELF_DISCOVERABLE: Termux egress bypasses Tailscale tun; declared in monitor.conf",\n'
printf '    "network.tailscale_up": "UNSUPPORTED: Tailscale runs as uid u0_a265, invisible to Termux (u0_a264). Judged server-side by real SSH reachability.",\n'
printf '    "packages.installed_count": "UNSUPPORTED: Android 13 package visibility"\n'
printf '  }\n'
printf '}\n'
} > "$TMP" 2>/dev/null

if [ -s "$TMP" ]; then
  mv -f "$TMP" "$OUT" 2>/dev/null
  # atomic-ish history append (jsonl, one sample per line)
  DAY=$(date '+%Y-%m-%d')
  tr -d '\n' < "$OUT" >> "$HISTORY_DIR/$DAY.jsonl" 2>/dev/null
  printf '\n' >> "$HISTORY_DIR/$DAY.jsonl" 2>/dev/null
  logline "collect ok uptime=${UPTIME_S:-na} cpu=${CPU_USAGE:-na} batt=${BAT_TEMP:-na}C mem=${MEM_USED_PCT:-na}%"
  exit 0
fi

rm -f "$TMP" 2>/dev/null
logline "collect FAILED: empty output"
exit 1
