#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run dashboard/fetch.py on a fixed cadence.

Why this exists:
  On the cloud host a systemd timer is the preferred driver, and on Windows
  Task Scheduler would be - but in locked-down environments neither may be
  available to the deploying process. This runner needs no privileges: start
  it, keep it in the foreground or background, stop it with Ctrl-C / SIGTERM.

It also serves as the fallback driver on the server, so the cadence logic
lives in exactly one place instead of being re-implemented per platform.

Usage:
  python3 run_loop.py            # loop forever at MI10PRO_POLL_SECONDS (60)
  python3 run_loop.py --once     # single fetch, exit (for manual checks)
  MI10PRO_POLL_SECONDS=30 python3 run_loop.py
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # .../mi10pro-monitor
FETCH = BASE / "dashboard" / "fetch.py"
INTERVAL = int(os.environ.get("MI10PRO_POLL_SECONDS", "60"))

_running = True


def _stop(signum, _frame):
    global _running
    _running = False
    print(f"\n[loop] signal {signum} received, stopping after this pass",
          flush=True)


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def one_pass():
    if not FETCH.exists():
        print(f"[loop] FATAL fetch.py not found at {FETCH}", flush=True)
        return 2
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, str(FETCH)],
            capture_output=True, text=True, cwd=str(BASE),
            encoding="utf-8", errors="replace", timeout=180,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        print(f"[{stamp()}] rc={p.returncode} {out.splitlines()[0] if out else ''}",
              flush=True)
        if err:
            print(f"[{stamp()}] stderr: {err[:400]}", flush=True)
        return p.returncode
    except subprocess.TimeoutExpired:
        print(f"[{stamp()}] fetch timed out (180s)", flush=True)
        return 124
    except Exception as e:
        print(f"[{stamp()}] fetch error: {e}", flush=True)
        return 1


def main():
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if "--once" in sys.argv:
        return one_pass()

    print(f"[loop] fetch={FETCH}", flush=True)
    print(f"[loop] interval={INTERVAL}s  (Ctrl-C to stop)", flush=True)

    while _running:
        started = time.time()
        one_pass()
        elapsed = time.time() - started
        wait = max(5, INTERVAL - int(elapsed))
        # Sleep in 1s slices so a stop signal is honoured promptly instead of
        # leaving a stale collector running for up to a full interval.
        for _ in range(wait):
            if not _running:
                break
            time.sleep(1)

    print("[loop] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
