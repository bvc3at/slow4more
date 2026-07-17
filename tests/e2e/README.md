# End-to-end test harness

cc-slower's whole job is timing: insert sleeps between a *real* Claude Code
process's tool calls, based on live usage numbers. Unit tests cover the
controller math; only a real Claude Code binary firing real `PreToolUse`
hooks can prove the integration works. This directory is that proof, sealed
in a container.

```
docker container (--network none)
┌────────────────────────────────────────────────────────┐
│ claude -p ... ──HTTP──▶ stub_server.py (fake Messages  │
│    │                    API, scripts 6 Bash rounds,    │
│    │ PreToolUse         timestamps every request to    │
│    ▼                    requests.jsonl)                │
│ cc_slower.py hook                                      │
│    reads /work/usage.json (written per phase)          │
│    sleeps before each tool call when utilization high  │
└────────────────────────────────────────────────────────┘
                 analyze.py asserts on the gaps
```

## Run it

From the repo root:

```bash
docker build -f tests/e2e/Dockerfile -t cc-slower-test .
docker run --rm --network none cc-slower-test
```

Exit code 0 and `E2E RESULT: PASS` on the last line means success. The run
takes a few minutes — the high-utilization phase really sleeps.

## What happens inside

1. `run_test.sh` sets up an isolated `$HOME`, points `ANTHROPIC_BASE_URL` at
   the local stub with a fake API key, and starts `stub_server.py`.
2. The stub speaks just enough of the streaming Messages protocol to march
   Claude Code through six scripted Bash tool rounds, then `end_turn`. Every
   request is timestamped to `requests.jsonl`.
3. Two sessions run with `cache_ttl_seconds: 30` (sleep cap 24 s):
   - **low** phase: `usage.json` says 10% utilization at 40% elapsed —
     no throttling expected;
   - **high** phase: 85% utilization at 40% elapsed — the hook must sleep
     before each tool call.
4. `analyze.py` splits the request timestamps by phase marker and fails
   unless the low phase's median inter-request gap stays under 10 s and the
   high phase's median exceeds the low one by at least 15 s.
5. The statusline feeder is also checked standalone: a synthetic
   `rate_limits` payload must round-trip into `usage.json`.

## Isolation guarantees

- **No network:** run with `--network none`; everything talks to
  `127.0.0.1`. Network is only needed at *build* time (apt + npm).
- **No real credentials:** `ANTHROPIC_API_KEY` is a fake string; the stub
  never checks it.
- **Never touches your `~/.claude`:** `$HOME` is `/work/home` inside the
  container; hook state and config live under `/work`.

## Knobs

| env var | default | meaning |
|---|---|---|
| `STUB_PORT` | 8399 | stub listen port |
| `STUB_TOOL_ROUNDS` | 6 | scripted Bash rounds per session |
| `STUB_LOG` | `/work/logs/requests.jsonl` | request timestamp log |

In CI this runs on a weekly schedule and on manual dispatch (see
`.github/workflows/e2e.yml`) rather than on every push: it takes minutes and
depends on the latest npm release of Claude Code, so it can break for
upstream reasons — which is exactly what it's there to catch.
