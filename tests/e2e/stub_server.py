#!/usr/bin/env python3
"""Anthropic Messages API stub for slow4more end-to-end tests (see README.md
in this directory).

Speaks just enough of the streaming protocol to drive Claude Code through a
scripted conversation: N assistant turns that call the Bash tool, then a
final end_turn. Every request is timestamped to a JSONL log so the test can
measure inter-request gaps (= where the PreToolUse hook sleeps).

Control endpoint: POST /control/phase {"phase": "<name>"} marks a phase
boundary in the log.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("STUB_PORT", "8399"))
LOG = os.environ.get("STUB_LOG", "/work/logs/requests.jsonl")
ROUNDS = int(os.environ.get("STUB_TOOL_ROUNDS", "6"))

_lock = threading.Lock()
_counter = [0]


def log_entry(d):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with _lock, open(LOG, "a") as f:
        f.write(json.dumps(d) + "\n")


def next_id(prefix):
    with _lock:
        _counter[0] += 1
        return f"{prefix}_{_counter[0]:06d}"


def count_tool_rounds(messages):
    n = 0
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_use"
                for c in content):
            n += 1
    return n


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default access log
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for ev, data in events:
            chunk = f"event: {ev}\ndata: {json.dumps(data)}\n\n"
            self.wfile.write(chunk.encode())
        self.close_connection = True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True})
        if path.startswith("/v1/models"):
            model = path.rsplit("/", 1)[-1]
            if model and model != "models":
                return self._json(200, {"id": model, "type": "model",
                                        "display_name": "Stub"})
            return self._json(200, {"data": [
                {"id": "claude-stub-1", "type": "model",
                 "display_name": "Stub"}], "has_more": False})
        log_entry({"t": time.time(), "kind": "other", "method": "GET",
                   "path": path})
        return self._json(404, {"error": {"type": "not_found"}})

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}

        if path.startswith("/control/phase"):
            log_entry({"t": time.time(), "kind": "phase",
                       "phase": body.get("phase", "?")})
            return self._json(200, {"ok": True})

        if path.endswith("count_tokens"):
            return self._json(200, {"input_tokens": 128})

        if not path.rstrip("/").endswith("/messages"):
            log_entry({"t": time.time(), "kind": "other", "method": "POST",
                       "path": path})
            return self._json(404, {"error": {"type": "not_found"}})

        messages = body.get("messages", [])
        tools = body.get("tools") or []
        has_bash = any(t.get("name") == "Bash" for t in tools
                       if isinstance(t, dict))
        rounds_done = count_tool_rounds(messages)
        main = has_bash
        log_entry({"t": time.time(), "kind": "messages", "main": main,
                   "rounds_done": rounds_done, "n_msgs": len(messages),
                   "stream": bool(body.get("stream"))})

        mid = next_id("msg")
        model = body.get("model", "claude-stub")
        usage_start = {"input_tokens": 900, "output_tokens": 1,
                       "cache_creation_input_tokens": 400,
                       "cache_read_input_tokens": 8000}

        if main and rounds_done < ROUNDS:
            tuid = next_id("toolu")
            cmd = f"echo stub-round-{rounds_done + 1}"
            block = {"type": "tool_use", "id": tuid, "name": "Bash",
                     "input": {}}
            partial = json.dumps({"command": cmd})
            stop_reason = "tool_use"
            full_content = [{"type": "tool_use", "id": tuid, "name": "Bash",
                             "input": {"command": cmd}}]
        else:
            text = "All scripted rounds complete."
            block = {"type": "text", "text": ""}
            partial = None
            stop_reason = "end_turn"
            full_content = [{"type": "text", "text": text}]

        if body.get("stream"):
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": mid, "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": usage_start}}),
                ("content_block_start", {"type": "content_block_start",
                                         "index": 0,
                                         "content_block": block}),
            ]
            if partial is not None:
                events.append(("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta",
                              "partial_json": partial}}))
            else:
                events.append(("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta",
                              "text": full_content[0]["text"]}}))
            events += [
                ("content_block_stop", {"type": "content_block_stop",
                                        "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": stop_reason,
                                             "stop_sequence": None},
                                   "usage": {"output_tokens": 25}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            return self._sse(events)

        return self._json(200, {
            "id": mid, "type": "message", "role": "assistant",
            "model": model, "content": full_content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": dict(usage_start, output_tokens=25),
        })


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"stub listening on 127.0.0.1:{PORT}, rounds={ROUNDS}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
