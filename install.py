#!/usr/bin/env python3
"""cc-slower installer.

Dry-run by default: prints exactly what would change. Pass --apply to write.

  python3 install.py                 # show the plan
  python3 install.py --apply         # write config + merge settings.json
  python3 install.py --apply --settings /path/to/settings.json
  python3 install.py --apply --cache-ttl 300   # if you are NOT on a
                                               # subscription 1h cache

It merges a PreToolUse hook and (unless --no-statusline) a statusline command
into the given Claude Code settings file, preserving everything else in it.
An existing statusline command is chained, not replaced: it keeps rendering
your status line while cc-slower captures the official rate_limits data.
Other statusLine keys (e.g. "padding") are preserved. Chaining caveat: the
wrapped command is re-parsed by the shell, so embedded quoting (arguments
that contain spaces) may be mangled — plain script paths and simple flags
are fine.
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOOK = REPO / "hooks" / "cc_slower.py"
FEEDER = REPO / "hooks" / "cc_slower_statusline.py"


def build_config(cache_ttl: int) -> dict:
    return {
        "provider": "auto",
        "cache_ttl_seconds": cache_ttl,
        "activation_utilization": 0.5,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write files (default: dry-run)")
    ap.add_argument("--settings",
                    default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--config",
                    default=os.path.join(
                        os.environ.get("XDG_CONFIG_HOME",
                                       os.path.expanduser("~/.config")),
                        "cc-slower", "config.json"))
    ap.add_argument("--cache-ttl", type=int, default=3600,
                    help="prompt cache TTL seconds: 3600 for Claude "
                         "subscription (default), 300 for API-key auth")
    ap.add_argument("--no-statusline", action="store_true",
                    help="skip statusline integration (hook falls back to "
                         "local transcript accounting)")
    args = ap.parse_args()

    for f in (HOOK, FEEDER):
        if not f.exists():
            print(f"missing {f}; run from the cc-slower repo", file=sys.stderr)
            return 1

    settings_path = Path(args.settings)
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        settings = {}

    # Sleep cap must fit inside the hook timeout with headroom.
    cap = int(args.cache_ttl * 0.8)
    hook_entry = {
        "type": "command",
        "command": f"python3 {HOOK}",
        "timeout": cap + 120,
    }
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    ours = None
    for matcher in pre:
        for h in matcher.get("hooks", []):
            if "cc_slower.py" in h.get("command", ""):
                ours = h
    if ours:
        ours.update(hook_entry)
    else:
        pre.append({"matcher": "*", "hooks": [hook_entry]})

    if not args.no_statusline:
        sl = settings.get("statusLine") or {}
        existing = sl.get("command", "")
        if "cc_slower_statusline" not in existing:
            cmd = f"python3 {FEEDER}"
            if existing:
                cmd = f"{cmd} -- {existing}"
            # Update in place so keys like "padding" survive.
            sl.update({"type": "command", "command": cmd})
            settings["statusLine"] = sl

    config_path = Path(args.config)
    config = build_config(args.cache_ttl)
    if config_path.exists():
        try:
            merged = json.loads(config_path.read_text())
        except ValueError:
            merged = {}
        merged.update({k: v for k, v in config.items() if k not in merged})
        config = merged

    print(f"== {settings_path} ==")
    print(json.dumps(settings, indent=2))
    print(f"\n== {config_path} ==")
    print(json.dumps(config, indent=2))

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write these files.")
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.with_suffix(".json.cc-slower.bak")
        backup.write_text(settings_path.read_text())
        print(f"\nbacked up settings to {backup}")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    print("installed. Restart running Claude Code sessions to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
