#!/usr/bin/env bash
# End-to-end test, run INSIDE the container (see tests/e2e/Dockerfile).
# Fully isolated: fresh $HOME, stub API on localhost, --network none safe.
set -uo pipefail

export HOME=/work/home
export SLOW4MORE_STATE_DIR=/work/state
export SLOW4MORE_CONFIG=/work/slow4more-config.json
export ANTHROPIC_BASE_URL=http://127.0.0.1:8399
export ANTHROPIC_API_KEY=sk-ant-stub-not-a-real-key
export DISABLE_TELEMETRY=1
export DISABLE_ERROR_REPORTING=1
export DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

mkdir -p /work/home /work/logs /work/state /work/proj/.claude

python3 /repo/tests/e2e/stub_server.py >/work/logs/stub.log 2>&1 &
STUB_PID=$!
trap 'kill $STUB_PID 2>/dev/null' EXIT
for i in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8399/health >/dev/null 2>&1 && break
  sleep 0.5
done

# Short cache TTL so the test finishes quickly: sleep cap = 30 * 0.8 = 24s.
cat > "$SLOW4MORE_CONFIG" <<'JSON'
{
  "provider": "file",
  "usage_file": "/work/usage.json",
  "cache_ttl_seconds": 30,
  "min_interval_seconds": 1,
  "min_sleep_seconds": 2,
  "notify_threshold_seconds": 10,
  "log_level": "debug"
}
JSON

cat > /work/proj/.claude/settings.json <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /repo/hooks/slow4more.py",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
JSON

echo '{"hasCompletedOnboarding": true}' > /work/home/.claude.json

phase() { # name utilization elapsed_fraction
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys, time, urllib.request
name, u, elapsed = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
resets = time.time() + (1 - elapsed) * 18000
with open("/work/usage.json", "w") as f:
    json.dump({"five_hour": {"utilization": u, "resets_at": resets},
               "seven_day": {"utilization": u * 0.4,
                             "resets_at": time.time() + 500000}}, f)
req = urllib.request.Request("http://127.0.0.1:8399/control/phase",
                             data=json.dumps({"phase": name}).encode(),
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=5)
print(f"--- phase {name}: utilization={u} elapsed={elapsed}")
PY
}

cd /work/proj
PROMPT="Follow the tool-use instructions."

phase low 0.10 0.40
claude -p "$PROMPT" --model claude-stub-1 --allowedTools Bash --max-turns 20 \
  >/work/logs/claude-low.log 2>&1 || echo "claude(low) exit=$?"

phase high 0.85 0.40
claude -p "$PROMPT" --model claude-stub-1 --allowedTools Bash --max-turns 20 \
  >/work/logs/claude-high.log 2>&1 || echo "claude(high) exit=$?"

echo "--- statusline feeder standalone check"
echo '{"model":{"display_name":"Stub"},"workspace":{"current_dir":"/work/proj"},"rate_limits":{"five_hour":{"used_percentage":61.5,"resets_at":4000000000},"seven_day":{"used_percentage":12.5,"resets_at":4000500000}}}' \
  | python3 /repo/hooks/slow4more_statusline.py
python3 - <<'PY'
import json
d = json.load(open("/work/state/usage.json"))
assert d["five_hour"]["used_percentage"] == 61.5, d
assert d["seven_day"]["resets_at"] == 4000500000, d
print("statusline feeder OK:", json.dumps(d))
PY

echo "--- hook log tail"
tail -n 20 /work/state/slow4more.log 2>/dev/null || echo "(no hook log?)"
echo "--- stub request log"
cat /work/logs/requests.jsonl 2>/dev/null || echo "(no requests?)"
echo "--- stub stderr tail"
tail -n 10 /work/logs/stub.log
echo "--- claude output (low phase) tail"
tail -n 5 /work/logs/claude-low.log
echo "--- claude output (high phase) tail"
tail -n 5 /work/logs/claude-high.log

python3 /repo/tests/e2e/analyze.py /work/logs/requests.jsonl
