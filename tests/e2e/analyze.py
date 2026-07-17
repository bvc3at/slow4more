#!/usr/bin/env python3
"""Assert that the cc-slower hook actually delayed API traffic.

Reads the stub's requests.jsonl, splits main-loop requests by phase marker,
and compares inter-request gaps: the "high" utilization phase must show
substantially larger gaps than the "low" phase.
"""

import json
import statistics
import sys


def main(path):
    phases = {}
    current = "init"
    for raw in open(path):
        e = json.loads(raw)
        if e["kind"] == "phase":
            current = e["phase"]
        elif e["kind"] == "messages" and e.get("main"):
            phases.setdefault(current, []).append(e["t"])

    gaps = {}
    for name, ts in phases.items():
        ts.sort()
        gaps[name] = [b - a for a, b in zip(ts, ts[1:])]

    for name in ("low", "high"):
        if not gaps.get(name):
            print(f"FAIL: no request gaps recorded for phase '{name}'")
            return 1
        g = gaps[name]
        print(f"phase {name:5s}: {len(g)+1} main requests, "
              f"gaps median={statistics.median(g):.1f}s "
              f"min={min(g):.1f}s max={max(g):.1f}s")

    lo, hi = statistics.median(gaps["low"]), statistics.median(gaps["high"])
    ok = True
    if lo > 10.0:
        print(f"FAIL: low-utilization phase should be fast, median gap {lo:.1f}s")
        ok = False
    if hi < lo + 15.0:
        print(f"FAIL: high-utilization phase not throttled enough "
              f"(median {hi:.1f}s vs low {lo:.1f}s; expected >= +15s)")
        ok = False
    print("E2E RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  "/work/logs/requests.jsonl"))
