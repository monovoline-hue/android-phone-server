#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# Mi10Pro Server Monitoring V1 - bootstrap (run once when Termux launches)
#
# Steps:
#   1. Acquire a partial wake lock (termux-wake-lock) so Android Doze does not
#      freeze Termux while it is in the background.
#   2. Ensure sshd is listening on 8022 (idempotent).
#   3. Run collect.sh once so current.json exists immediately.
#   4. Launch watchdog-sshd.sh if it is not already running.
#
# Triggered from ~/.bashrc (interactive Termux launch). Safe to run repeatedly.
# ============================================================================

PATH="/data/data/com.termux/files/usr/bin:/system/bin:/system/xbin:$PATH"
export PATH

MON_DIR="${MON_DIR:-$HOME/zonira-monitor}"
mkdir -p "$MON_DIR" 2>/dev/null

# 1) Hold a partial wake lock. Under MIUI the device may still suspend Termux in
#    the background; this is the first layer of defense against freeze-kills.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

# 2) Ensure sshd is up (idempotent: restart only if 8022 is not listening).
if ! (ss -ltn 2>/dev/null | grep -q ':8022' || netstat -ltn 2>/dev/null | grep -q ':8022'); then
  sshd
fi

# 3) Immediate first collection so the cloud sees data without waiting.
bash "$MON_DIR/collect.sh"

# 4) Launch the resilient watchdog if not already running.
if ! pgrep -f "$MON_DIR/watchdog-sshd.sh" >/dev/null 2>&1; then
  nohup /data/data/com.termux/files/usr/bin/bash "$MON_DIR/watchdog-sshd.sh" >/dev/null 2>&1 &
fi

echo "Mi10Pro monitor bootstrap done"
