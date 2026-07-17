#!/usr/bin/env python3
"""cc-slower statusline feeder.

Claude Code invokes the configured statusline command with a JSON payload on
stdin that (for Pro/Max subscribers) includes official `rate_limits` data for
the 5-hour and 7-day windows. This script:

  1. writes that data to <state_dir>/usage.json (atomic) where the cc_slower
     hook picks it up as its primary usage source, and
  2. prints a status line. If you already have a statusline command, pass it
     as arguments (e.g. `cc_slower_statusline.py -- ~/bin/my_statusline.sh`)
     and it will be exec'd with the same stdin after the usage file is
     written; otherwise a compact default line is printed.

Stdlib-only, fail-open: any error still prints a status line and exits 0.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def state_dir() -> Path:
    d = os.environ.get("CC_SLOWER_STATE_DIR")
    if d:
        return Path(d)
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg) / "cc-slower"


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}

    limits = payload.get("rate_limits") or {}
    pct = {}
    if isinstance(limits, dict) and limits:
        out = {}
        for name in ("five_hour", "seven_day"):
            w = limits.get(name)
            if not isinstance(w, dict):
                continue
            entry = {}
            if w.get("used_percentage") is not None:
                entry["used_percentage"] = w["used_percentage"]
                pct[name] = float(w["used_percentage"])
            if w.get("resets_at") is not None:
                entry["resets_at"] = w["resets_at"]
            if entry:
                out[name] = entry
        if out:
            try:
                d = state_dir()
                d.mkdir(parents=True, exist_ok=True)
                out["written_at"] = time.time()
                tmp = d / "usage.json.tmp"
                tmp.write_text(json.dumps(out))
                os.replace(tmp, d / "usage.json")
            except OSError:
                pass

    # Chain to the user's own statusline command if one was given. It was a
    # shell command string originally, so run it through the shell again.
    args = [a for a in sys.argv[1:] if a != "--"]
    if args:
        try:
            res = subprocess.run(" ".join(args), shell=True, input=raw,
                                 capture_output=True, text=True, timeout=5)
            sys.stdout.write(res.stdout)
            return 0
        except (OSError, subprocess.TimeoutExpired):
            pass

    model = (payload.get("model") or {}).get("display_name", "Claude")
    cwd = os.path.basename(
        (payload.get("workspace") or {}).get("current_dir", "") or "")
    bits = [model]
    if cwd:
        bits.append(cwd)
    if "five_hour" in pct:
        bits.append(f"5h {pct['five_hour']:.0f}%")
    if "seven_day" in pct:
        bits.append(f"7d {pct['seven_day']:.0f}%")
    print(" | ".join(bits))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - statusline must never crash
        print("cc-slower statusline error")
        sys.exit(0)
