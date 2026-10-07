"""The Librarian: points the reader at papers in the library, honestly.

Retrieval is lexical (idf-weighted word overlap over paper cards) because the
gateway offers no embeddings.  The model is only allowed to recommend ids it was
shown, and the server enforces that rather than trusting it.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter

import llm

from . import db
from .metadata import norm_text, parse_json

TOP_N = 12
THIN = 0.12            # best score below this: tell the model the match is weak
_STOP = set("""a an and are as at be but by can could do does for from has have how i if in
is it its of on or should so that the their there these this to was we were what when which
who why will with would you your about any not paper papers article articles""".split())

SYSTEM = """\
You are the Gloss Librarian. You help a researcher navigate THEIR LOCAL library of
papers. You are given a list of library papers, each with an id like P3. Treat that
list as the whole library for this question.

RULES
1. Use ONLY the listed papers as the library. Refer to papers ONLY by the ids
   provided. NEVER invent an id, title, author, or claim a paper is in the library
   when it is not in the list.
2. Everything you say about a paper must come from its card (title, keywords,
   summary). Do not add findings, numbers or methods the card does not support. If
   the card does not say what a paper found about this question, say less rather
   than guess.
3. If the listed papers do not genuinely address the question, say so plainly in
   "lead", leave "papers" empty, and set "gap_flag" to true. Do not oversell thin
   matches. Include only papers that really help; at most 6, most useful first.
4. When the library is weak, you MAY suggest external leads from your general
   knowledge in "external_suggestions": search terms, author names, or specific
   papers to look up. They are UNVERIFIED and NOT in the library, so never put them
   in "papers". Do not give bibliographic details (year, journal) for an external
   paper unless you are certain.

Return ONLY a JSON object, no prose outside it, no code fences:
{
  "lead": "1-2 sentences: the direct answer, or how well the library covers this",
  "papers": [
    {"id": "P3",
     "asks": "the question or aim this paper addresses, 1-2 sentences",
     "finds": "what it finds or shows that bears on the user's question, 1-2 sentences"}
  ],
  "together": "2-4 sentences on what these papers jointly establish and where they agree or differ; empty string if fewer than 2 papers",
  "open_questions": ["what the papers leave unanswered, 1-4 short items"],
  "gap_flag": true or false,
  "external_suggestions": [{"type": "search_term" | "author" | "paper", "text": "..."}]
}
"""


def _tokens(text: str) -> list[str]:
    return [w for w in norm_text(text).split() if w not in _STOP and len(w) > 2]


def _cards() -> list[dict]:
    conn = db.connect()
    authors: dict[str, list[str]] = {}
    for r in conn.execute("""SELECT pa.sha, a.display_name FROM paper_authors pa
                             JOIN authors a ON a.id = pa.author_id ORDER BY pa.ord"""):
        authors.setdefault(r["sha"], []).append(r["display_name"])
    cards = []
    for r in conn.execute("SELECT * FROM papers WHERE duplicate_of IS NULL AND meta_status='ok'"):
        kw = json.loads(r["keywords_json"] or "[]")
        cards.append({
            "sha": r["sha"], "title": r["title"], "year": r["year"], "venue": r["venue"],
            "authors": authors.get(r["sha"], []), "keywords": kw,
            "brief": (r["brief"] or r["abstract"] or ""),
        })
    return cards


def retrieve(question: str, cards: list[dict]):
    """Rank cards by idf-weighted overlap with the question. Returns [(score, card)]."""
    q = Counter(_tokens(question))
    docs = []
    for c in cards:
        text = " ".join([c["title"] or "", " ".join(c["authors"]),
                         " ".join(c["keywords"]), c["brief"]])
        docs.append(Counter(_tokens(text)))
    n = len(cards) or 1
    df = Counter(w for d in docs for w in d)
    scored = []
    for card, d in zip(cards, docs):
        total = sum(math.log(1 + n / df[w]) * (1 + math.log(d[w]))
                    for w in q if w in d)
        norm = sum(math.log(1 + n / df[w]) for w in q if w in df) or 1.0
        scored.append((total / norm if norm else 0.0, card))
    scored.sort(key=lambda t: -t[0])
    return scored


def _chat_patiently(prompt: str) -> str:
    """One completion, waiting out the gateway's rate limit (which the background
    summarizer can trip) instead of failing the question."""
    for wait in (12, 25, 45, None):
        try:
            return llm.chat(prompt)
        except Exception as exc:                            # noqa: BLE001
            limited = "429" in str(exc) or "ratelimit" in type(exc).__name__.lower()
            if not limited or wait is None:
                raise
            time.sleep(wait)


def ask(question: str, history: list[dict] | None = None) -> dict:
    cards = _cards()
    if not cards:
        return {"answer": "The library is empty. Add some PDFs first.", "lead": "",
                "papers": [], "together": "", "open_questions": [], "gap_flag": True,
                "external_suggestions": []}
    ranked = retrieve(question, cards)[:TOP_N]
    best = ranked[0][0] if ranked else 0.0
    ids = {f"P{i + 1}": card for i, (_, card) in enumerate(ranked)}

    listing = "\n\n".join(
        f"[{pid}] {c['title']}\n  Authors: {'; '.join(c['authors'][:6])} | Year: {c['year']}"
        f" | {c['venue'] or ''}\n  Keywords: {', '.join(c['keywords'][:8])}\n"
        f"  Summary: {c['brief'][:700]}" for pid, c in ids.items())
    thin = ("NOTE: lexical retrieval found only weak matches; be strict about "
            "relevance.\n\n") if best < THIN else ""
    recent = "".join(f"{t['role'].upper()}: {t['content'][:400]}\n"
                     for t in (history or [])[-4:])
    convo = f"Conversation so far:\n{recent}\n" if recent else ""

    prompt = (f"{SYSTEM}\nThe library holds {len(cards)} papers. The {len(ids)} most "
              f"relevant by keyword are:\n\n{listing}\n\n{thin}{convo}"
              f"User question: {question}")
    raw = _chat_patiently(prompt)
    try:
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
    except (ValueError, json.JSONDecodeError):
        return {"answer": raw.strip(), "lead": raw.strip(), "papers": [], "together": "",
                "open_questions": [], "gap_flag": False, "external_suggestions": [],
                "parse_error": True}

    papers, dropped = [], []
    for item in parsed.get("papers") or []:
        item = item if isinstance(item, dict) else {}
        card = ids.get(str(item.get("id")))
        if card is None:                      # an id we never sent: hallucinated
            dropped.append(item.get("id"))
            continue
        if any(p["sha"] == card["sha"] for p in papers):
            continue
        papers.append({"n": len(papers) + 1, "sha": card["sha"], "title": card["title"],
                       "year": card["year"], "authors": card["authors"][:3],
                       "asks": str(item.get("asks", "")).strip(),
                       "finds": str(item.get("finds") or item.get("why") or "").strip()})
    external = [{"type": e.get("type", "search_term"), "text": e.get("text", ""),
                 "verified": False}                      # never trust a model's "verified"
                for e in parsed.get("external_suggestions") or [] if isinstance(e, dict)]
    open_q = [str(q).strip() for q in parsed.get("open_questions") or [] if str(q).strip()]
    lead = str(parsed.get("lead") or parsed.get("answer") or "").strip()
    return {"answer": lead, "lead": lead, "papers": papers,
            "together": str(parsed.get("together") or "").strip() if len(papers) > 1 else "",
            "open_questions": open_q[:4],
            "gap_flag": bool(parsed.get("gap_flag")) or not papers,
            "external_suggestions": external, "dropped_ids": dropped,
            "retrieval_score": round(best, 3)}
