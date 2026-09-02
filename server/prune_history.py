#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prune dashboard/data/history.jsonl to a rolling retention window.

Why this exists:
  fetch.py only ever appends. On a host polling every 60s that is ~1440
  entries and ~360 KB per day, growing without bound. This keeps a rolling
  window and is run daily by mi10pro-prune.timer.

Safety properties:
  - Atomic: writes to a temp file in the same directory, then os.replace().
    A crash mid-run leaves the original file untouched rather than truncated.
  - Size guard: also trims to a hard line cap if kept entries are still large.
  - Honest about what it dropped: prints a one-line summary for the journal.

Usage:
  python3 prune_history.py            # keep MI10PRO_KEEP_DAYS (default 30)
  MI10PRO_KEEP_DAYS=7 python3 prune_history.py
  python3 prune_history.py --dry-run
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Resolve relative to THIS script's directory, which is the layout in both
# deployments: on the cloud the script lives in dashboard/ (data/ sibling), in
# the repo it lives in server/ (data/ sibling). The previous parent.parent +
# "dashboard" join only worked on the cloud and broke inside the repo tree.
DATA_DIR = Path(__file__).resolve().parent / "data"
HISTORY = DATA_DIR / "history.jsonl"

KEEP_DAYS = int(os.environ.get("MI10PRO_KEEP_DAYS", "30"))
MAX_LINES = int(os.environ.get("MI10PRO_MAX_LINES", "60000"))   # ~40 days @60s


def parse_ts(line):
    """Return the entry's aware datetime, or None if unusable.

    Falls back to the nested raw timestamp because backfilled records from the
    phone carry the device's own timestamp rather than the server's.
    """
    try:
        obj = json.loads(line)
    except Exception:
        return None
    for key in ("ts",):
        val = obj.get(key)
        if val:
            try:
                return datetime.fromisoformat(val)
            except Exception:
                pass
    raw = (obj.get("raw") or {}).get("timestamp")
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            pass
    return None


def to_aware(dt):
    """Attach the LOCAL zone to naive stamps.

    fetch.py writes datetime.now(), i.e. the server's local wall clock
    (Asia/Shanghai via the service unit). Assuming UTC here would skew every
    comparison by the UTC offset and prune up to 8h of extra history.
    """
    return dt if dt.tzinfo else dt.astimezone()


def main():
    dry = "--dry-run" in sys.argv
    if not HISTORY.exists():
        print(f"[prune] {HISTORY} missing; nothing to do")
        return 0

    raw_lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(raw_lines)
    if total == 0:
        print("[prune] history empty; nothing to do")
        return 0

    cutoff = datetime.now().astimezone() - timedelta(days=KEEP_DAYS)

    kept, undated = [], 0
    for line in raw_lines:
        if not line.strip():
            continue
        dt = parse_ts(line)
        if dt is None:
            # Never silently discard what we cannot date - treat as fresh so a
            # parser regression cannot quietly delete the whole history.
            undated += 1
            kept.append(line)
            continue
        if to_aware(dt) >= cutoff:
            kept.append(line)

    dropped = total - len(kept)

    # Second safety net: hard cap on line count.
    if len(kept) > MAX_LINES:
        extra = len(kept) - MAX_LINES
        kept = kept[-MAX_LINES:]
        dropped += extra
    else:
        extra = 0

    if dry:
        print(f"[prune] DRY RUN total={total} keep={len(kept)} "
              f"drop={dropped} (age>{KEEP_DAYS}d: {dropped - extra}, cap: {extra}) "
              f"undated={undated}")
        return 0

    if dropped == 0:
        print(f"[prune] nothing to drop (total={total}, window={KEEP_DAYS}d)")
        return 0

    fd, tmp = tempfile.mkstemp(dir=str(HISTORY.parent), prefix=".history.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        os.replace(tmp, HISTORY)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    print(f"[prune] total={total} keep={len(kept)} dropped={dropped} "
          f"(window={KEEP_DAYS}d, undated_kept={undated})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
