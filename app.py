#!/usr/bin/env python3
"""Gloss -- read a paper with a summary of every paragraph in the margin.

Local Flask server plus a browser page.  Start it with run.command, or:

    ~/.venvs/gloss/bin/python app.py

Nothing leaves this machine except the paragraph text sent to the configured
LLM provider for summarizing -- and with a local provider (Ollama), not even
that.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import pymupdf
from flask import Flask, Response, abort, jsonify, redirect, render_template, request

import extract
import llm
import summarize
from library import db as library_db
from library import routes as library_routes
from library import summarizer as library_summarizer

# The one place the version is written down.  It reaches the opening screen,
# the /api/health probe below, and the startup banner from here; what changed in
# each version is recorded in CHANGELOG.md.
__version__ = "1.2.0"

CACHE_DIR = Path.home() / ".cache" / "gloss"
PAGE_DIR = CACHE_DIR / "pages"
UPLOAD_DIR = CACHE_DIR / "uploads"
RECENTS = CACHE_DIR / "recents.json"
RENDER_DPI = 144          # 2x the PDF's native 72 dpi, so pages stay sharp
MAX_RECENTS = 5
PORTS = (5017, 5018, 5019)   # bound in order; also where a running copy is sought

app = Flask(__name__)
app.register_blueprint(library_routes.bp)
library_db.init()

# sha -> everything known about one open document.  Single-user local app, so a
# dict behind a lock is the whole state layer.
_docs: dict[str, dict] = {}
_lock = threading.Lock()


# --- helpers ----------------------------------------------------------------

def _recents() -> list:
    # Capped on the way in as well as on the way out, so a list saved under a
    # larger limit shrinks to the current one instead of waiting for a write.
    try:
        return json.loads(RECENTS.read_text())[:MAX_RECENTS]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _remember(path: str, title: str) -> None:
    entries = [e for e in _recents() if e.get("path") != path]
    entries.insert(0, {"path": path, "title": title})
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        RECENTS.write_text(json.dumps(entries[:MAX_RECENTS]))
    except OSError:
        pass


def _title_for(path: str, doc: dict) -> str:
    # A paper in the library has a real title; fall back to the file name.
    row = library_db.connect().execute(
        "SELECT title FROM papers WHERE pdf_path=?", (path,)).fetchone()
    if row and row["title"]:
        return row["title"]
    return os.path.splitext(os.path.basename(path))[0]


def _start_summaries(sha: str) -> None:
    """Run both passes in the background so the reader is usable immediately."""
    entry = _docs[sha]

    def work():
        try:
            brief = summarize.build_brief(entry["doc"], sha)
            with _lock:
                entry["brief"] = brief
                entry["stage"] = "paragraphs"

            def publish(chunk):
                with _lock:
                    entry["summaries"].update(chunk)

            _, failures = summarize.summarize_document(
                entry["doc"], sha, brief, on_results=publish)
            with _lock:
                entry["failures"] = [pid for pid, _ in failures]
                entry["stage"] = "done"
        except summarize.SummarizerUnavailable as exc:
            with _lock:
                entry["stage"] = "error"
                entry["error"] = str(exc)
        except Exception as exc:                            # noqa: BLE001
            with _lock:
                entry["stage"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, daemon=True)
    entry["thread"] = thread
    thread.start()


def _open_pdf(path: str) -> dict:
    """Extract a PDF (or return the already-open one) and kick off summarizing."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    sha = summarize.sha_of(path)
    with _lock:
        if sha in _docs:
            _remember(path, _docs[sha]["title"])
            return {"sha": sha, "title": _docs[sha]["title"], "path": path}

    doc = extract.extract(path)                  # may raise NoTextLayer
    title = _title_for(path, doc)
    with _lock:
        _docs[sha] = {
            "path": path,
            "title": title,
            "doc": doc,
            "brief": "",
            "summaries": {},
            "failures": [],
            "stage": "brief",
            "error": "",
        }
    _remember(path, title)
    _start_summaries(sha)
    # The path goes back to the browser so closing a document can return to a
    # Recent list that includes it; the list itself was rendered at page load.
    return {"sha": sha, "title": title, "path": path}


def _entry(sha: str) -> dict:
    entry = _docs.get(sha)
    if not entry:
        abort(404)
    return entry


# --- routes -----------------------------------------------------------------

@app.route("/")
def root():
    return redirect("/home")


@app.route("/api/open-sha", methods=["POST"])
def open_sha():
    """Open a library paper in the reader by its hash."""
    sha = (request.json or {}).get("sha", "")
    row = library_db.connect().execute(
        "SELECT pdf_path FROM papers WHERE sha=?", (sha,)).fetchone()
    if not row:
        return jsonify({"error": "That paper is not in the library."}), 404
    return _ingest(row["pdf_path"])


@app.route("/read")
def index():
    warning = summarize.check_ready()
    provider = ""
    if not warning:
        try:
            provider = llm.settings().describe()
        except Exception:                                   # noqa: BLE001
            provider = ""
    return render_template("index.html", version=__version__,
                           recents=_recents(), warning=warning,
                           provider=provider)


@app.route("/api/health")
def health():
    """Identity probe used by a second launch to find this instance.

    It answers with the app name rather than a bare 200 so that an unrelated
    service already holding one of our ports is not mistaken for Gloss.
    """
    return jsonify({"app": "gloss", "version": __version__})


@app.route("/api/pick", methods=["POST"])
def pick():
    """Open the native macOS file chooser.

    A local app should feel like a Mac app; osascript gives a real Finder dialog
    without bundling one.  Drag-and-drop (below) is the fallback everywhere else.
    """
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$f = New-Object System.Windows.Forms.OpenFileDialog; "
              "$f.Filter = 'PDF files (*.pdf)|*.pdf'; $f.Title = 'Choose a PDF'; "
              "if ($f.ShowDialog() -eq 'OK') { $f.FileName } else { exit 1 }")
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps]
    else:
        script = ('POSIX path of (choose file with prompt "Choose a PDF" '
                  'of type {"com.adobe.pdf", "pdf"})')
        cmd = ["osascript", "-e", script]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return jsonify({"error": "Could not open the file chooser."}), 500
    if result.returncode != 0:
        return jsonify({"cancelled": True})       # user pressed Cancel
    return _ingest(result.stdout.strip())


@app.route("/api/open", methods=["POST"])
def open_path():
    return _ingest((request.json or {}).get("path", ""))


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accept a dragged-in PDF, since browsers withhold real paths."""
    handle = request.files.get("file")
    if not handle or not handle.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please drop a PDF file."}), 400
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / os.path.basename(handle.filename)
    handle.save(target)
    return _ingest(str(target))


def _ingest(path: str):
    if not path:
        return jsonify({"error": "No file chosen."}), 400
    try:
        return jsonify(_open_pdf(path))
    except extract.NoTextLayer as exc:
        return jsonify({"error": str(exc), "kind": "no-text"}), 422
    except FileNotFoundError:
        return jsonify({"error": f"File not found: {path}"}), 404
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"Could not open that PDF: {exc}"}), 500


@app.route("/api/doc/<sha>")
def get_doc(sha):
    entry = _entry(sha)
    return jsonify({
        "sha": sha,
        "title": entry["title"],
        "path": entry["path"],
        "pages": entry["doc"]["pages"],
        "paragraphs": entry["doc"]["paragraphs"],
    })


@app.route("/api/page/<sha>/<int:number>.png")
def page_image(sha, number):
    entry = _entry(sha)
    folder = PAGE_DIR / sha
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{number}.png"
    if not target.exists():
        with pymupdf.open(entry["path"]) as pdf:
            if number < 0 or number >= pdf.page_count:
                abort(404)
            pixmap = pdf[number].get_pixmap(dpi=RENDER_DPI)
            pixmap.save(target)
    return Response(target.read_bytes(), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/text/<sha>/<int:number>.json")
def page_text(sha, number):
    """Where every word on a page sits, for the reader's invisible text layer.

    The page itself is an image, so this is what makes it selectable.  Fetched
    a page at a time, as the images are: a reader who opens a 200-page report
    and reads four of them should pay for four.
    """
    entry = _entry(sha)
    with pymupdf.open(entry["path"]) as pdf:
        if number < 0 or number >= pdf.page_count:
            abort(404)
        spans = extract.page_spans(pdf[number])
    return jsonify({"page": number, "spans": spans})


@app.route("/api/summaries/<sha>")
def get_summaries(sha):
    entry = _entry(sha)
    with _lock:
        return jsonify({
            "summaries": dict(entry["summaries"]),
            "failures": list(entry["failures"]),
            "stage": entry["stage"],
            "error": entry["error"],
            "brief": entry["brief"],
        })


@app.route("/api/resummarize", methods=["POST"])
def resummarize():
    body = request.json or {}
    entry = _entry(body.get("sha", ""))
    try:
        pid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad paragraph id"}), 400
    try:
        text = summarize.resummarize(entry["doc"], body["sha"], entry["brief"], pid)
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    with _lock:
        entry["summaries"][str(pid)] = text
        entry["failures"] = [f for f in entry["failures"] if f != pid]
    return jsonify({"id": pid, "summary": text})


@app.route("/api/ask", methods=["POST"])
def ask():
    """Answer a question about the open document."""
    body = request.json or {}
    entry = _entry(body.get("sha", ""))
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question first."}), 400
    if len(question) > 2000:
        return jsonify({"error": "That question is too long."}), 400
    try:
        answer, cited = summarize.answer_question(
            entry["doc"], entry["brief"], question)
    except summarize.SummarizerUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                                # noqa: BLE001
        return jsonify({"error": f"Could not answer that: {exc}"}), 500
    library_routes.save_qa(body["sha"], question, answer, cited)
    return jsonify({"question": question, "answer": answer, "cited": cited})


@app.route("/debug/<sha>/<int:number>")
def debug_page(sha, number):
    """Page image with every detected paragraph boxed -- for tuning extraction."""
    entry = _entry(sha)
    with pymupdf.open(entry["path"]) as pdf:
        if number < 0 or number >= pdf.page_count:
            abort(404)
        page = pdf[number]
        for para in entry["doc"]["paragraphs"]:
            for rect in para["rects"]:
                if rect["page"] != number:
                    continue
                colour = (0.8, 0.1, 0.1) if para["summarizable"] else (0.4, 0.4, 0.9)
                page.draw_rect(pymupdf.Rect(rect["bbox"]), color=colour, width=0.8)
        buffer = io.BytesIO(page.get_pixmap(dpi=110).tobytes("png"))
    return Response(buffer.getvalue(), mimetype="image/png")


def _free_port():
    for port in PORTS + (0,):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    return PORTS[0]


def _running_instance():
    """(url, version) of a Gloss already serving on one of our ports, or ("", "").

    Without this, launching twice starts a second server on the next free port.
    Both would work, but each keeps its own in-memory documents and summaries,
    so the same paper opened in the two windows would be extracted and
    summarized twice over -- and the second copy's margin notes would arrive in
    a window the reader had stopped looking at.

    The version comes back too because the copy already running is running the
    code it was started with.  Edit Gloss, relaunch, and you are handed the old
    server without a word -- the new code appears to have done nothing at all.
    """
    for port in PORTS:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=1.0) as reply:
                payload = json.loads(reply.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError,
                json.JSONDecodeError):
            continue          # nothing there, or not us -- keep looking
        if payload.get("app") == "gloss":
            return f"http://127.0.0.1:{port}/", str(payload.get("version", ""))
    return "", ""


if __name__ == "__main__":
    # Double-clicking run.command a second time should return the reader to the
    # copy already running, not start a rival one.
    running, running_version = _running_instance()
    if running:
        print(f"Gloss is already running at {running}")
        if running_version != __version__:
            # Reusing it would serve the code that copy started with, which is
            # how an update can look like it did nothing.
            print(f"  NOTE: that copy is version {running_version or 'unknown'}; "
                  f"this one is {__version__}.")
            print("  To run the newer code, press Ctrl-C in the window running")
            print("  Gloss and start it again.")
        print("Opening it in your browser.  This window can be closed;")
        print("the window running Gloss is the one to keep open.")
        webbrowser.open(running)
        raise SystemExit(0)

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"Gloss {__version__} running at {url}")
    print("Leave this window open while you read.  Press Ctrl-C to quit.")
    notice = summarize.check_ready()
    if notice:
        print(f"  NOTE: {notice}")
    else:
        # Name the provider and model: a misconfiguration is otherwise only
        # visible once a summary silently comes from the wrong place.
        try:
            print(f"  Using {llm.settings().describe()}")
        except Exception:                                   # noqa: BLE001
            pass
    queued = library_summarizer.enqueue_missing()
    if queued:
        print(f"  Library: summarizing {queued} paper(s) in the background.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
