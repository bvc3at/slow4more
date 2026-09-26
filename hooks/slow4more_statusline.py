#!/usr/bin/env python3
"""slow4more statusline feeder.

Claude Code invokes the configured statusline command with a JSON payload on
stdin that (for Pro/Max subscribers) includes official `rate_limits` data for
the 5-hour and 7-day windows. This script:

  1. merges that data into <state_dir>/usage.json (atomic, flock-protected)
     where the slow4more hook picks it up as its primary usage source. Each
     session only knows the rate_limits of its own API responses, so per
     window a later resets_at wins, the same window keeps the higher
     used_percentage, and expired windows are dropped: an idle session's
     stale view never overwrites a newer one. And it
  2. prints a status line. If you already have a statusline command, pass it
     as arguments (e.g. `slow4more_statusline.py -- ~/bin/my_statusline.sh`)
     and it will be exec'd with the same stdin after the usage file is
     written; otherwise a compact default line is printed.

Stdlib-only, fail-open: any error still prints a status line and exits 0.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WINDOWS: tuple = ('five_hour', 'seven_day')
SAME_WINDOW_SECONDS: float = 3600.0


def state_dir() -> Path:
    d = os.environ.get("SLOW4MORE_STATE_DIR")
    if d:
        return Path(d)
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg) / "slow4more"


def _resets_at(entry: dict) -> float | None:
    try:
        return float(entry['resets_at'])
    except (KeyError, TypeError, ValueError):
        return None


def _pick(old: dict | None, new: dict | None) -> dict | None:
    if old is None or new is None:
        return new or old
    old_reset: float | None = _resets_at(old)
    new_reset: float | None = _resets_at(new)
    if old_reset is None or new_reset is None:
        return new
    if abs(new_reset - old_reset) >= SAME_WINDOW_SECONDS:
        return new if new_reset > old_reset else old
    old_pct: float = float(old.get('used_percentage') or 0)
    new_pct: float = float(new.get('used_percentage') or 0)
    return new if new_pct >= old_pct else old


def merge_windows(existing: dict, incoming: dict, now: float) -> dict:
    merged: dict = {}
    for name in WINDOWS:
        live: list = []
        for entry in (existing.get(name), incoming.get(name)):
            if not isinstance(entry, dict):
                live.append(None)
                continue
            reset: float | None = _resets_at(entry)
            live.append(entry if reset is None or reset > now else None)
        chosen: dict | None = _pick(live[0], live[1])
        if chosen is not None:
            merged[name] = chosen
    return merged


def write_usage(incoming: dict, now: float) -> None:
    d: Path = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    path: Path = d / 'usage.json'
    with open(d / 'usage.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = {}
        out: dict = merge_windows(
            existing if isinstance(existing, dict) else {}, incoming, now)
        out['written_at'] = now
        tmp: Path = d / 'usage.json.tmp'
        tmp.write_text(json.dumps(out))
        os.replace(tmp, path)


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
        for name in WINDOWS:
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
                write_usage(out, time.time())
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
        print("slow4more statusline error")
        sys.exit(0)
