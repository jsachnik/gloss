"""Background summarizing for library papers.

One worker thread drains a queue, so adding twenty PDFs does not fire twenty
whole-paper passes at once.  Results land in the same ~/.cache/gloss files the
reader uses, which is what makes opening a paper afterwards instant.
"""

from __future__ import annotations

import os
import queue
import threading
import time

import extract
import summarize

from . import db

# The gateway rate-limits bursts. Two parallel requests is a gentler default than
# the reader's eight; a value set in .env or the environment still wins.
os.environ.setdefault("GLOSS_WORKERS", "2")
ATTEMPTS = 4

_queue: "queue.Queue[str]" = queue.Queue()
_queued: set[str] = set()
_lock = threading.Lock()
_started = False


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "429" in text or "rate limit" in text


def _summarize_with_retries(doc, sha):
    """Both passes, waiting out rate limits. Finished work is cached on disk, so
    each retry only repeats what has not succeeded yet."""
    failures = []
    for attempt in range(ATTEMPTS):
        try:
            brief = summarize.build_brief(doc, sha)
            _, failures = summarize.summarize_document(doc, sha, brief)
            if not failures:
                return brief, []
        except Exception as exc:                            # noqa: BLE001
            if not _is_rate_limit(exc) or attempt == ATTEMPTS - 1:
                raise
        if attempt < ATTEMPTS - 1:
            time.sleep(45 * (attempt + 1))
    return brief, failures


def _run(sha: str) -> None:
    conn = db.connect()
    row = conn.execute("SELECT pdf_path FROM papers WHERE sha=?", (sha,)).fetchone()
    if not row:
        return
    try:
        conn.execute("UPDATE papers SET summary_status='running' WHERE sha=?", (sha,))
        conn.commit()
        doc = extract.extract(row["pdf_path"])
        brief, failures = _summarize_with_retries(doc, sha)
        status = "done" if not failures else "partial"
        conn.execute("UPDATE papers SET brief=?, summary_status=? WHERE sha=?",
                     (brief, status, sha))
    except Exception as exc:                                # noqa: BLE001
        print(f"[library] summarizing {sha} failed: {type(exc).__name__}: {exc}")
        conn.execute("UPDATE papers SET summary_status='error' WHERE sha=?", (sha,))
    conn.commit()


def _worker() -> None:
    while True:
        sha = _queue.get()
        try:
            _run(sha)
        finally:
            with _lock:
                _queued.discard(sha)
            _queue.task_done()


def enqueue(sha: str) -> None:
    global _started
    with _lock:
        if sha in _queued:
            return
        _queued.add(sha)
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    _queue.put(sha)


def enqueue_missing() -> int:
    """Queue every non-duplicate paper that has not been summarized yet."""
    rows = db.connect().execute(
        "SELECT sha FROM papers WHERE duplicate_of IS NULL AND meta_status='ok' "
        "AND summary_status IN ('none','error','running','partial')").fetchall()
    for r in rows:
        enqueue(r["sha"])
    return len(rows)
