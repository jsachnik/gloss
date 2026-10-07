"""How papers connect to each other, without embeddings.

Three edge types come from structure and cost no LLM calls:
    shared_author   a person on both author lists
    citation        one paper's reference list names the other's title or DOI
    shared_keyword  overlap in the topical words of the LLM-inferred keywords
A fourth, llm_related, is one batched LLM call per new paper over the few
candidates the structural signals rank highest -- so the cost of adding a paper
stays constant as the library grows.

"Not connected" is the absence of a row.  Zero-weight edges are never stored.
"""

from __future__ import annotations

import json
import re

import llm

from . import db
from .metadata import norm_text, parse_json

TOP_K = 8
MIN_JACCARD = 0.18
MIN_TITLE_CHARS = 25          # shorter titles match reference text by accident
_STOP = {"and", "the", "of", "in", "on", "for", "to", "a", "an", "with", "by",
         "from", "at", "as", "model", "models", "analysis", "effect", "effects"}

RELATE_PROMPT = """\
You judge how academic papers relate to one another. Return ONLY a JSON array,
no prose, no code fences.

NEW PAPER
id: {x_id}
title: {x_title}
keywords: {x_keywords}
abstract: {x_abstract}

CANDIDATES
{candidates}

For EACH candidate output one object:
{{"id": "<candidate id>", "related": true or false,
  "relation": "builds_on" | "contradicts" | "same_method" | "same_problem" | "background" | "unrelated",
  "weight": number from 0 to 1 (confidence this is a meaningful research connection),
  "reason": "one concrete sentence"}}

Rules:
- related=false and relation="unrelated" when they only share generic vocabulary.
- Judge ONLY from the titles, keywords and abstracts given. Do not invent shared
  citations or claims.
Output a JSON array with one object per candidate.
"""


def _words(keywords) -> set[str]:
    out = set()
    for phrase in keywords:
        out |= {w for w in norm_text(phrase).split() if w not in _STOP and len(w) > 2}
    return out


def _pair(x: str, y: str):
    return (x, y) if x < y else (y, x)


def _put(conn, x, y, etype, weight, detail):
    a, b = _pair(x, y)
    conn.execute("INSERT OR REPLACE INTO edges(a,b,etype,weight,detail) VALUES(?,?,?,?,?)",
                 (a, b, etype, float(weight), json.dumps(detail)))


def _papers(conn):
    """Every non-duplicate paper that has metadata, as dicts."""
    rows = conn.execute(
        "SELECT * FROM papers WHERE duplicate_of IS NULL AND meta_status='ok'").fetchall()
    authors = {}
    for r in conn.execute("""SELECT pa.sha, a.id, a.display_name FROM paper_authors pa
                             JOIN authors a ON a.id = pa.author_id"""):
        authors.setdefault(r["sha"], {})[r["id"]] = r["display_name"]
    papers = {}
    for r in rows:
        p = dict(r)
        p["keywords"] = json.loads(p["keywords_json"] or "[]")
        p["authors"] = authors.get(p["sha"], {})
        papers[p["sha"]] = p
    return papers


def structural_edges(conn, x: dict, others: list[dict]) -> dict[str, float]:
    """Write the three free edge types for x; return a candidate score per sha."""
    score: dict[str, float] = {}
    xwords = _words(x["keywords"])
    for o in others:
        s = 0.0
        shared = set(x["authors"]) & set(o["authors"])
        if shared:
            names = [x["authors"][i] for i in shared]
            _put(conn, x["sha"], o["sha"], "shared_author",
                 min(1.0, 0.4 * len(shared)), {"authors": names})
            s += 2.0 * len(shared)

        for citing, cited in ((x, o), (o, x)):
            nt = cited.get("norm_title") or ""
            hit = (len(nt) >= MIN_TITLE_CHARS and nt in (citing.get("ref_text") or ""))
            if not hit and cited.get("doi"):
                hit = norm_text(cited["doi"]) in (citing.get("ref_text") or "")
            if hit:
                _put(conn, x["sha"], o["sha"], "citation", 0.9,
                     {"citing": citing["sha"], "cited": cited["sha"]})
                s += 3.0

        owords = _words(o["keywords"])
        if xwords and owords:
            jac = len(xwords & owords) / len(xwords | owords)
            if jac >= MIN_JACCARD:
                _put(conn, x["sha"], o["sha"], "shared_keyword", jac,
                     {"words": sorted(xwords & owords)})
            s += jac
        score[o["sha"]] = s
    return score


def _judged(conn, x, y) -> bool:
    a, b = _pair(x, y)
    return conn.execute("SELECT 1 FROM judged WHERE a=? AND b=?", (a, b)).fetchone() is not None


def llm_edges(conn, x: dict, candidates: list[dict]) -> None:
    """One batched call: how does x relate to each of these candidates?"""
    if not candidates:
        return
    block = "\n\n".join(
        f"id: {c['sha']}\ntitle: {c['title']}\nkeywords: {', '.join(c['keywords'])}\n"
        f"abstract: {(c.get('abstract') or '')[:500]}" for c in candidates)
    reply = llm.chat(RELATE_PROMPT.format(
        x_id=x["sha"], x_title=x["title"], x_keywords=", ".join(x["keywords"]),
        x_abstract=(x.get("abstract") or "")[:800], candidates=block))
    verdicts = parse_json(reply)
    sent = {c["sha"] for c in candidates}
    for v in verdicts if isinstance(verdicts, list) else []:
        cid = v.get("id")
        if cid not in sent:                  # model invented an id: ignore it
            continue
        a, b = _pair(x["sha"], cid)
        conn.execute("INSERT OR REPLACE INTO judged(a,b) VALUES(?,?)", (a, b))
        if v.get("related") and v.get("relation") != "unrelated":
            try:
                w = max(0.05, min(1.0, float(v.get("weight", 0.5))))
            except (TypeError, ValueError):
                w = 0.5
            _put(conn, x["sha"], cid, "llm_related", w,
                 {"relation": v.get("relation"), "reason": v.get("reason", "")})
    conn.commit()


def rebuild_for(sha: str, use_llm: bool = True) -> dict:
    """Recompute every edge touching one paper.  Returns counts for reporting."""
    conn = db.connect()
    conn.execute("DELETE FROM edges WHERE (a=? OR b=?) AND etype != 'llm_related'",
                 (sha, sha))
    papers = _papers(conn)
    x = papers.get(sha)
    if not x:
        return {"edges": 0}
    others = [p for s, p in papers.items() if s != sha]
    score = structural_edges(conn, x, others)
    conn.commit()
    if use_llm:
        ranked = sorted((p for p in others if not _judged(conn, sha, p["sha"])),
                        key=lambda p: -score[p["sha"]])
        llm_edges(conn, x, ranked[:TOP_K])
    n = conn.execute("SELECT COUNT(*) FROM edges WHERE a=? OR b=?", (sha, sha)).fetchone()[0]
    return {"edges": n}
