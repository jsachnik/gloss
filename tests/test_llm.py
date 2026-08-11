#!/usr/bin/env python3
"""Offline tests for the provider client.

Runs against a stub HTTP server on localhost, so no key and no network are
needed.  Every case here is a way the client can fail *silently* — returning
something plausible instead of raising — which is exactly what a live smoke test
against one provider would not catch.

    ~/.venvs/gloss/bin/python tests/test_llm.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402

failures = []
calls = {"count": 0}
mode = {"name": "ok"}


def check(name, condition, detail=""):
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: {detail}")
        failures.append(f"{name}: {detail}")


class Stub(BaseHTTPRequestHandler):
    """Serves one canned response per `mode`, counting requests."""

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _completion(self, content):
        return json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        })

    def do_GET(self):
        self._send(200, json.dumps({"object": "list", "data": [{"id": "stub-model"}]}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        calls["count"] += 1
        name = mode["name"]

        if name == "ok":
            self._send(200, self._completion("hello"))
        elif name == "thinking":
            self._send(200, self._completion(
                "<think>Let me consider this carefully.</think>the answer"))
        elif name == "empty_choices":
            # The shape a gateway produces when it answers 200 with an error body.
            self._send(200, json.dumps({"id": "x", "object": "chat.completion",
                                        "created": 0, "model": "stub",
                                        "choices": []}))
        elif name == "sse":
            # TAMU streams unless stream=false is sent explicitly. Serve a stream
            # only when the client omitted the field — proving the client sends it.
            if body.get("stream") is False:
                self._send(200, self._completion("not streamed"))
            else:
                self._send(200, 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
                           "text/event-stream")
        elif name == "flaky":
            # Fail twice with a retryable status, then succeed.
            if calls["count"] <= 2:
                self._send(503, json.dumps({"error": {"message": "overloaded"}}))
            else:
                self._send(200, self._completion("recovered"))
        elif name == "bad_request":
            self._send(400, json.dumps({"error": {"message": "no such model"}}))
        elif name == "one_bad_row":
            if "BOOM" in json.dumps(body):
                self._send(400, json.dumps({"error": {"message": "bad row"}}))
            else:
                self._send(200, self._completion("fine"))


server = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_port}/v1"

config = llm.Settings(provider="stub", base_url=BASE, model="stub-model",
                      key="none", local=False)


def reset(name):
    mode["name"] = name
    calls["count"] = 0


print("Provider client:")

reset("ok")
check("plain completion", llm.chat("hi", config=config) == "hello")

# A gateway that streams when you didn't ask parses into a response with no
# choices — indistinguishable from an empty answer unless stream=False is sent.
reset("sse")
check("sends stream=False explicitly", llm.chat("hi", config=config) == "not streamed",
      "client let the server stream, so the reply parsed as empty")

# Reasoning models inline their chain of thought in the content field.
reset("thinking")
check("strips <think> blocks", llm.chat("hi", config=config) == "the answer",
      f"got {llm.chat('hi', config=config)!r}")

reset("empty_choices")
try:
    llm.chat("hi", config=config)
    check("empty choices raises", False, "returned instead of raising")
except llm.SummarizerUnavailable as exc:
    check("empty choices raises a readable error", "stub" in str(exc), str(exc))
except IndexError as exc:
    check("empty choices raises a readable error", False,
          f"IndexError leaked out instead: {exc}")

reset("flaky")
check("retries a 503 and recovers", llm.chat("hi", config=config) == "recovered")
check("retried exactly as needed", calls["count"] == 3, f"{calls['count']} calls")

reset("bad_request")
before = calls["count"]
try:
    llm.chat("hi", config=config)
    check("400 raises", False, "returned instead of raising")
except llm.SummarizerUnavailable:
    check("400 raises", False, "should surface the SDK error, not ours")
except Exception:
    check("400 raises without retrying", calls["count"] - before == 1,
          f"{calls['count'] - before} calls — a bad request must not be retried")

print("\nBatch runner:")

reset("ok")
rows = [f"row {i}" for i in range(6)]
results, batch_failures = llm.run_batch(rows, lambda r: r, config=config)
check("all rows return", results == ["hello"] * 6, str(results))
check("no failures", batch_failures == [])

# One bad row must not discard the finished ones.
reset("one_bad_row")
rows = ["fine 1", "BOOM", "fine 2"]
results, batch_failures = llm.run_batch(rows, lambda r: r, config=config)
check("failures are returned, not raised", len(batch_failures) == 1, str(batch_failures))
check("good rows survive a bad one", results[0] == "fine" and results[2] == "fine",
      str(results))
check("failed row is None, never a silent empty string", results[1] is None,
      repr(results[1]))

# The cache is what makes reopening a paper free.
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    cache = os.path.join(tmp, "c.json")
    reset("ok")
    llm.run_batch(["a", "b"], lambda r: r, cache_path=cache, config=config)
    first = calls["count"]
    llm.run_batch(["a", "b"], lambda r: r, cache_path=cache, config=config)
    check("cache hits skip the network", calls["count"] == first,
          f"{calls['count'] - first} extra calls on the second run")

    # Bumping prompt_version must invalidate, or edited prompts serve stale text.
    before = calls["count"]
    llm.run_batch(["a", "b"], lambda r: r, cache_path=cache, prompt_version=2,
                  config=config)
    check("prompt_version bump invalidates", calls["count"] > before,
          "a changed prompt would have served stale summaries")

# The cache key must match the tamu-ai skill's derivation exactly, or every
# summary cached before llm.py existed is silently regenerated.
import hashlib  # noqa: E402

messages = [{"role": "user", "content": "hi"}]
expected = hashlib.sha256(json.dumps(
    [1, "m", messages, []], sort_keys=True, default=str).encode()).hexdigest()
check("cache key matches the previous implementation",
      llm._cache_key(1, "m", messages) == expected)

print("\nConfiguration:")

for name, spec in llm.PROVIDERS.items():
    ok = bool(spec.base_url) or name == "custom"
    check(f"provider '{name}' is usable", ok, "no base_url and not 'custom'")

os.environ["GLOSS_PROVIDER"] = "nope"
try:
    llm.settings()
    check("unknown provider is rejected", False, "accepted 'nope'")
except llm.SummarizerUnavailable as exc:
    check("unknown provider names the valid ones", "openai" in str(exc), str(exc))
finally:
    del os.environ["GLOSS_PROVIDER"]

check("a local provider gets fewer workers",
      llm.Settings("ollama", BASE, "m", "k", local=True).workers < config.workers)
check("a local provider gets a longer timeout",
      llm.Settings("ollama", BASE, "m", "k", local=True).timeout > config.timeout)
check("a dead local server is reported, not raised",
      "not answering" in (llm._local_alive("http://127.0.0.1:9") or "not answering"))

server.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All provider-client tests passed.")
