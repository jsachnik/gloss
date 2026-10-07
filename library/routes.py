"""Flask routes for the library: Home, papers, graph, timeline, Librarian."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from . import db, edges, ingest, librarian, summarizer

bp = Blueprint("library", __name__)
_ingest_lock = threading.Lock()


def _authors_by_paper(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute("""SELECT pa.sha, a.id, a.display_name FROM paper_authors pa
                             JOIN authors a ON a.id = pa.author_id ORDER BY pa.ord"""):
        out.setdefault(r["sha"], []).append({"id": r["id"], "name": r["display_name"]})
    return out


def _paper_row(r, authors) -> dict:
    return {"sha": r["sha"], "title": r["title"], "year": r["year"], "venue": r["venue"],
            "doi": r["doi"], "filename": r["filename"],
            "keywords": json.loads(r["keywords_json"] or "[]"),
            "authors": authors.get(r["sha"], []),
            "summary_status": r["summary_status"] or "none"}


@bp.route("/home")
def home():
    return render_template("home.html")


@bp.route("/api/library")
def library():
    conn = db.connect()
    authors = _authors_by_paper(conn)
    rows = conn.execute("SELECT * FROM papers WHERE duplicate_of IS NULL AND meta_status='ok' "
                        "ORDER BY title COLLATE NOCASE").fetchall()
    dups = conn.execute("SELECT COUNT(*) FROM papers WHERE duplicate_of IS NOT NULL").fetchone()[0]
    connected = {r[0] for r in conn.execute("SELECT a FROM edges UNION SELECT b FROM edges")}
    papers = [dict(_paper_row(r, authors), connected=r["sha"] in connected) for r in rows]
    return jsonify({"papers": papers, "duplicates": dups, "folder": str(db.library_dir())})


@bp.route("/api/paper/<sha>")
def paper(sha):
    conn = db.connect()
    r = conn.execute("SELECT * FROM papers WHERE sha=?", (sha,)).fetchone()
    if not r:
        return jsonify({"error": "unknown paper"}), 404
    authors = _authors_by_paper(conn)
    out = _paper_row(r, authors)
    out["abstract"] = r["abstract"]
    out["brief"] = r["brief"] or ""
    titles = {x["sha"]: x["title"] for x in conn.execute("SELECT sha,title FROM papers")}
    related: dict[str, dict] = {}
    for e in conn.execute("SELECT * FROM edges WHERE a=? OR b=?", (sha, sha)):
        other = e["b"] if e["a"] == sha else e["a"]
        slot = related.setdefault(other, {"sha": other, "title": titles.get(other, "?"),
                                          "types": [], "weight": 0.0, "reasons": []})
        detail = json.loads(e["detail"] or "{}")
        slot["types"].append(e["etype"])
        slot["weight"] = max(slot["weight"], e["weight"])
        if detail.get("reason"):
            slot["reasons"].append(detail["reason"])
        if e["etype"] == "citation":
            slot["reasons"].append("cites it" if detail.get("citing") == sha else "is cited by it")
        if e["etype"] == "shared_author":
            slot["reasons"].append("shared author: " + ", ".join(detail.get("authors", [])))
    out["related"] = sorted(related.values(), key=lambda s: -s["weight"])
    out["qa_count"] = conn.execute("SELECT COUNT(*) FROM qa WHERE sha=?", (sha,)).fetchone()[0]
    return jsonify(out)


@bp.route("/api/author/<int:author_id>")
def author(author_id):
    conn = db.connect()
    a = conn.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
    if not a:
        return jsonify({"error": "unknown author"}), 404
    rows = conn.execute("""SELECT p.sha, p.title, p.year FROM papers p
        JOIN paper_authors pa ON pa.sha = p.sha
        WHERE pa.author_id=? AND p.duplicate_of IS NULL ORDER BY p.year""",
                        (author_id,)).fetchall()
    return jsonify({"id": author_id, "name": a["display_name"],
                    "papers": [dict(r) for r in rows]})


@bp.route("/api/graph")
def graph():
    mode = request.args.get("mode", "article")
    conn = db.connect()
    authors = _authors_by_paper(conn)
    papers = {r["sha"]: r for r in conn.execute(
        "SELECT * FROM papers WHERE duplicate_of IS NULL AND meta_status='ok'")}

    if mode == "author":
        count: dict[int, int] = {}
        names: dict[int, str] = {}
        pair: dict[tuple, int] = {}
        for sha, lst in authors.items():
            if sha not in papers:
                continue
            ids = [a["id"] for a in lst]
            for a in lst:
                count[a["id"]] = count.get(a["id"], 0) + 1
                names[a["id"]] = a["name"]
            for i, x in enumerate(ids):
                for y in ids[i + 1:]:
                    k = (min(x, y), max(x, y))
                    pair[k] = pair.get(k, 0) + 1
        return jsonify({
            "nodes": [{"id": f"a{i}", "author_id": i, "label": names[i], "count": count[i]}
                      for i in count],
            "edges": [{"source": f"a{x}", "target": f"a{y}", "weight": min(1, n / 3), "n": n}
                      for (x, y), n in pair.items()]})

    agg: dict[tuple, dict] = {}
    for e in conn.execute("SELECT * FROM edges"):
        if e["a"] not in papers or e["b"] not in papers:
            continue
        slot = agg.setdefault((e["a"], e["b"]), {"types": [], "weight": 0.0, "reasons": []})
        slot["types"].append(e["etype"])
        slot["weight"] = max(slot["weight"], e["weight"])
        detail = json.loads(e["detail"] or "{}")
        if detail.get("reason"):
            slot["reasons"].append(detail["reason"])
    degree: dict[str, int] = {}
    for (a, b) in agg:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    return jsonify({
        "nodes": [{"id": sha, "label": r["title"], "year": r["year"],
                   "first_author": (authors.get(sha) or [{"name": ""}])[0]["name"],
                   "degree": degree.get(sha, 0)} for sha, r in papers.items()],
        "edges": [{"source": a, "target": b, **v} for (a, b), v in agg.items()]})


@bp.route("/api/qa/<sha>")
def qa_history(sha):
    rows = db.connect().execute(
        "SELECT question, answer, asked_at FROM qa WHERE sha=? ORDER BY id", (sha,)).fetchall()
    return jsonify({"qa": [dict(r) for r in rows]})


def save_qa(sha: str, question: str, answer: str, cited) -> None:
    conn = db.connect()
    conn.execute("INSERT INTO qa(sha,question,answer,cited_json) VALUES(?,?,?,?)",
                 (sha, question, answer, json.dumps(cited)))
    conn.commit()


@bp.route("/api/librarian", methods=["POST"])
def ask_librarian():
    body = request.json or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question first."}), 400
    try:
        return jsonify(librarian.ask(question[:2000], body.get("history")))
    except Exception as exc:                                    # noqa: BLE001
        if "429" in str(exc) or "ratelimit" in type(exc).__name__.lower():
            return jsonify({"error": "The AI service is busy right now (rate limit) -- "
                                     "summaries are being written in the background. "
                                     "Please ask again in a minute."}), 503
        return jsonify({"error": f"The Librarian could not answer: {exc}"}), 500


def _process_new(report_holder: dict) -> None:
    with _ingest_lock:
        report_holder["report"] = ingest.scan(log=lambda *_: None)
        summarizer.enqueue_missing()


@bp.route("/api/library/scan", methods=["POST"])
def scan():
    holder: dict = {}
    _process_new(holder)
    r = holder["report"]
    return jsonify({"added": len(r["added"]), "duplicates": [d[0] for d in r["duplicates"]],
                    "same_file": r["same_file"], "errors": r["errors"]})


@bp.route("/api/library/upload", methods=["POST"])
def upload():
    handle = request.files.get("file")
    if not handle or not handle.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please choose a PDF file."}), 400
    target = db.library_dir() / os.path.basename(handle.filename)
    stem, n = target.stem, 1
    while target.exists():                       # never overwrite a different paper
        n += 1
        target = target.with_name(f"{stem} ({n}).pdf")
    handle.save(target)
    holder: dict = {}
    _process_new(holder)
    r = holder["report"]
    if r["errors"]:
        return jsonify({"error": f"Could not read {target.name}: {r['errors'][0][1]}"}), 422
    if r["same_file"] or r["duplicates"]:
        target.unlink(missing_ok=True)           # a repeat adds nothing; keep the folder clean
        return jsonify({"duplicate": True,
                        "message": "That paper is already in your library."})
    return jsonify({"added": len(r["added"])})


@bp.route("/api/library/status")
def status():
    rows = db.connect().execute(
        "SELECT summary_status, COUNT(*) n FROM papers WHERE duplicate_of IS NULL "
        "GROUP BY summary_status").fetchall()
    return jsonify({r["summary_status"] or "none": r["n"] for r in rows})
