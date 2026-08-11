"""One thin client for any OpenAI-compatible LLM endpoint.

Gloss talks to exactly one kind of API: `POST /chat/completions` in the
OpenAI shape.  That covers far more than the name suggests -- OpenAI, Anthropic,
TAMU AI, OpenRouter, and a local Ollama all speak it -- so a provider is three
lines of configuration rather than a new code path.

Deliberately *not* general: Gloss sends no sampling parameters and no
`response_format`.  Omitting them is what lets thinking-enabled models (Claude on
TAMU, the o-series) work here without special-casing, and it sidesteps the one
gateway quirk that fails silently rather than loudly.

`run_batch` is a port of the tamu-ai skill's batch runner, keeping its cache key
byte-identical so summaries cached before this module existed are still valid.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"

# Providers whose base_url and model ids were confirmed rather than recalled.
# `model` is only a suggestion the installer offers; the effective model always
# comes from GLOSS_MODEL in .env, so a default going stale is a one-line fix
# there rather than a code change.
@dataclass(frozen=True)
class Provider:
    base_url: str
    model: str | None          # None = the installer must ask; no safe default
    key_env: str | None        # None = no key required (local servers)
    local: bool = False
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "https://api.openai.com/v1", "gpt-5.4-mini", "OPENAI_API_KEY"),
    "anthropic": Provider(
        "https://api.anthropic.com/v1", "claude-sonnet-5", "ANTHROPIC_API_KEY",
        note="Anthropic documents this as a compatibility layer for testing "
             "rather than a production surface."),
    "tamu": Provider(
        "https://chat-api.tamu.ai/openai", "protected.Claude Sonnet 4.6",
        "TAMU_AI_API_KEY"),
    "openrouter": Provider(
        "https://openrouter.ai/api/v1", None, "OPENROUTER_API_KEY",
        note="Reaches Anthropic, Google and others through one key. Model ids "
             "are 'vendor/model' -- set GLOSS_MODEL."),
    "ollama": Provider(
        "http://localhost:11434/v1", None, None, local=True,
        note="A model running on this Mac. No key, no cost, nothing leaves "
             "the machine."),
    "custom": Provider(
        "", None, "GLOSS_API_KEY",
        note="Any other OpenAI-compatible endpoint -- Gemini, Groq, Together, "
             "LM Studio, vLLM. Set GLOSS_BASE_URL and GLOSS_MODEL."),
}

DEFAULT_PROVIDER = "tamu"
HOSTED_WORKERS = 8
LOCAL_WORKERS = 2          # one local server serves few requests at once
HOSTED_TIMEOUT = 180
LOCAL_TIMEOUT = 900        # local prompt processing is far slower
RETRIES = 4


class SummarizerUnavailable(RuntimeError):
    """No usable LLM configuration -- the reader still opens PDFs."""


# --- configuration ----------------------------------------------------------

def _load_env() -> None:
    """Read .env into the environment without overriding real env vars."""
    if not ENV_FILE.exists():
        return
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value
    except OSError:
        pass


@dataclass(frozen=True)
class Settings:
    provider: str
    base_url: str
    model: str
    key: str
    local: bool

    @property
    def workers(self) -> int:
        return LOCAL_WORKERS if self.local else HOSTED_WORKERS

    @property
    def timeout(self) -> int:
        return LOCAL_TIMEOUT if self.local else HOSTED_TIMEOUT

    def describe(self) -> str:
        return f"{self.provider} / {self.model}"


def settings() -> Settings:
    """Resolve the active configuration, or explain what's missing."""
    _load_env()
    name = (os.environ.get("GLOSS_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise SummarizerUnavailable(
            f"GLOSS_PROVIDER is '{name}', but must be one of: "
            f"{', '.join(sorted(PROVIDERS))}. Edit .env in the Gloss folder.")
    spec = PROVIDERS[name]

    base_url = (os.environ.get("GLOSS_BASE_URL") or spec.base_url).strip()
    if not base_url:
        raise SummarizerUnavailable(
            f"Provider '{name}' needs GLOSS_BASE_URL set in .env.")

    model = (os.environ.get("GLOSS_MODEL") or spec.model or "").strip()
    if not model:
        raise SummarizerUnavailable(
            f"Provider '{name}' has no default model -- set GLOSS_MODEL in .env."
            + (f" {spec.note}" if spec.note else ""))

    key = ""
    if spec.key_env:
        key = (os.environ.get(spec.key_env) or "").strip()
        # The tamu-ai skill's own key file stays a fallback, so an existing
        # TAMU setup keeps working with no .env at all.
        if not key and name == "tamu":
            legacy = Path.home() / ".config" / "tamu-ai" / "api-key"
            if legacy.exists():
                try:
                    key = legacy.read_text(encoding="utf-8").strip()
                except OSError:
                    key = ""
        if not key:
            raise SummarizerUnavailable(
                f"No API key for '{name}'. Set {spec.key_env} in the .env file "
                f"in the Gloss folder, or in your shell. Run install.command to "
                f"be walked through it.")
    # The OpenAI client requires a non-empty key even where none is used.
    return Settings(name, base_url.rstrip("/"), model, key or "not-needed",
                    spec.local)


def check_ready() -> str:
    """Return '' when summarizing can run, else a sentence explaining why not."""
    try:
        config = settings()
    except SummarizerUnavailable as exc:
        return str(exc)
    try:
        import openai  # noqa: F401
    except ImportError:
        return ("The 'openai' package isn't installed. Run install.command.")
    if config.local and not _local_alive(config.base_url):
        return (f"No local model server is answering at {config.base_url}. "
                f"Start it (for Ollama: run 'ollama serve' in a Terminal) and "
                f"reload this page.")
    return ""


def _local_alive(base_url: str, timeout: float = 2.0) -> bool:
    """Connection-refused from inside a thread pool is an unreadable traceback;
    probe once up front so the UI can say plainly that nothing is running."""
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=timeout) as r:
            return r.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def local_models(base_url: str | None = None, timeout: float = 3.0) -> list[str]:
    """Model ids a local server has available (used by install.command)."""
    url = (base_url or PROVIDERS["ollama"].base_url).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/models", timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return []
    return sorted(m.get("id", "") for m in payload.get("data", []) if m.get("id"))


# --- the client -------------------------------------------------------------

_clients: dict[tuple, object] = {}
_clients_lock = threading.Lock()


def _client(config: Settings):
    import openai
    key = (config.base_url, config.key, config.timeout)
    with _clients_lock:
        if key not in _clients:
            # max_retries=0: retries are handled below, with our own backoff.
            # Leaving the SDK's on as well would multiply the waits.
            _clients[key] = openai.OpenAI(
                base_url=config.base_url, api_key=config.key,
                timeout=config.timeout, max_retries=0)
        return _clients[key]


_THINK_RE = re.compile(r"\A\s*<(think|thinking|reasoning)>.*?</\1>\s*",
                       re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str | None) -> str:
    """Drop a leading <think>...</think> block from a reply.

    Reasoning models served through an OpenAI-compatible shim (qwen3 on Ollama,
    DeepSeek-R1, some gateway configurations) inline their chain of thought in
    the content field rather than a separate one.  Without this, every margin
    note would open with the model's deliberation.  Anchored at the start and
    matched to a closing tag, so it can't eat real prose that mentions a tag.
    """
    text = (text or "").strip()
    previous = None
    while text != previous:
        previous = text
        text = _THINK_RE.sub("", text, count=1).strip()
    return text


def _retryable(exc) -> bool:
    import openai
    return isinstance(exc, (openai.RateLimitError, openai.APITimeoutError,
                            openai.APIConnectionError, openai.InternalServerError))


def chat(messages, model: str | None = None, config: Settings | None = None) -> str:
    """One completion. `messages` may be a plain string.

    Sends no sampling parameters at all -- see the module docstring.
    """
    config = config or settings()
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    client = _client(config)

    last = None
    for attempt in range(RETRIES):
        try:
            # stream=False must be sent EXPLICITLY. The OpenAI SDK omits the
            # field for a non-streaming call, and the TAMU gateway then streams
            # anyway -- returning an SSE body the SDK parses into a response
            # with no choices. That looks exactly like an empty completion, so
            # it is a silent, confusing failure rather than an error.
            response = client.chat.completions.create(
                model=model or config.model, messages=messages, stream=False)
            # Some gateways answer HTTP 200 with an error body, which the SDK
            # parses into a response carrying no usable choice -- an IndexError
            # several frames down. Name the provider instead.
            choices = getattr(response, "choices", None)
            if not choices:
                raise SummarizerUnavailable(
                    f"{config.provider} returned no completion for this request "
                    f"(model '{model or config.model}').")
            content = _strip_reasoning(choices[0].message.content)
            if not content:
                raise SummarizerUnavailable(
                    f"{config.provider} returned an empty completion "
                    f"(model '{model or config.model}').")
            return content
        except SummarizerUnavailable:
            raise
        except Exception as exc:                            # noqa: BLE001
            last = exc
            if not _retryable(exc):
                raise
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt + 0.5)
    raise last or SummarizerUnavailable("unknown failure")


# --- batch ------------------------------------------------------------------

class _Cache:
    """Thread-safe JSON cache so a re-run never re-pays for finished work."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = False
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key):
        if not self.path:
            return None
        with self.lock:
            return self.data.get(key)

    def put(self, key, value):
        if not self.path:
            return
        with self.lock:
            self.data[key] = value
            self.dirty = True

    def save(self):
        if not self.path or not self.dirty:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
            self.dirty = False


def _cache_key(prompt_version, model, messages) -> str:
    """Byte-identical to the tamu-ai skill's derivation, so summaries cached
    before this module existed are still served rather than regenerated."""
    return hashlib.sha256(json.dumps(
        [prompt_version, model, messages, []],
        sort_keys=True, default=str).encode()).hexdigest()


def run_batch(items, build_messages, model: str | None = None, workers=None,
              cache_path=None, prompt_version=1, parse=None, quiet=True,
              config: Settings | None = None):
    """One completion per item, in parallel, with retry and a disk cache.

    Returns (results, failures). Failures are RETURNED, not raised: one bad
    paragraph must never discard the rest of a finished document.
    """
    config = config or settings()
    items = list(items)
    model = model or config.model
    workers = workers or config.workers
    cache = _Cache(cache_path)
    results = [None] * len(items)
    failures = []
    done = 0
    lock = threading.Lock()
    started = time.time()

    def work(pair):
        index, item = pair
        messages = build_messages(item)
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        key = _cache_key(prompt_version, model, messages)
        hit = cache.get(key)
        if hit is not None:
            return index, item, hit, None
        try:
            reply = chat(messages, model=model, config=config)
            value = parse(reply) if parse else reply
            cache.put(key, value)
            return index, item, value, None
        except Exception as exc:                            # noqa: BLE001
            return index, item, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, pair) for pair in enumerate(items)]
        for future in as_completed(futures):
            index, item, value, error = future.result()
            with lock:
                done += 1
                if error:
                    failures.append((index, item, error))
                else:
                    results[index] = value
                if not quiet and done % 25 == 0:
                    rate = done / max(time.time() - started, 1e-3)
                    print(f"  {done}/{len(items)} ({rate:.1f}/s, "
                          f"{len(failures)} failed)", flush=True)
                if done % 50 == 0:
                    cache.save()
    cache.save()
    return results, failures


# --- CLI --------------------------------------------------------------------

def main() -> int:
    """`python llm.py` prints the active configuration and does one round-trip."""
    problem = check_ready()
    if problem:
        print(f"Not ready: {problem}", file=sys.stderr)
        return 1
    config = settings()
    print(f"Provider : {config.provider}")
    print(f"Endpoint : {config.base_url}")
    print(f"Model    : {config.model}")
    print(f"Workers  : {config.workers}  (timeout {config.timeout}s)")
    print("Round-trip:", chat("Reply with exactly: ok", config=config)[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
