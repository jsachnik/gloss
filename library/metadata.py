"""Bibliographic metadata for one PDF: title, authors, year, venue, DOI, keywords.

Cheap signals first (PDF info dictionary, a DOI regex), then one LLM call over
the first page.  Claude models cannot be given response_format, so the prompt
demands JSON and `parse_json` tolerates fences and stray prose.
"""

from __future__ import annotations

import json
import re
import unicodedata

import pymupdf

import llm

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+[A-Za-z0-9])")
_REF_HEAD_RE = re.compile(
    r"^\s*(references?( and notes)?|bibliography|literature cited)\s*$", re.I | re.M)

PROMPT = """\
You extract bibliographic metadata from the first pages of an academic PDF.
Return ONLY a JSON object, no prose, no code fences.

Schema:
{{
  "title": string,
  "authors": [string, ...],
  "year": integer or null,
  "venue": string or null,
  "doi": string or null,
  "abstract": string or null,
  "keywords": [string, ...]
}}

Rules:
- Use ONLY information present in the text. Do not guess authors or venue.
- "authors" are full names in reading order, without affiliations or footnote marks.
- "year" is the publication year (not the year of a cited work); null if unclear.
- If a field is not clearly present use null (or [] for lists).
- "keywords" are 6-10 lowercase topical noun phrases you infer from the paper,
  specific enough to distinguish it (e.g. "cloud feedback", "wildfire smoke aerosols").

PDF info hints (may be wrong): TITLE_HINT="{title_hint}" AUTHOR_HINT="{author_hint}"
FILENAME: "{filename}"

Text:
---
{text}
---
"""


def parse_json(text: str):
    """Pull a JSON object out of a model reply, tolerating fences and prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\{\[].*[\}\]]", text, flags=re.S)
    if not match:
        raise ValueError(f"no JSON in reply: {text[:120]!r}")
    return json.loads(re.sub(r",\s*([}\]])", r"\1", match.group(0)))


def norm_text(text: str) -> str:
    """Lowercase alphanumerics only, for fuzzy title matching."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def norm_author(name: str) -> str:
    """'Fasullo, J. T.' and 'John T. Fasullo' both become 'j fasullo'."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[\d*†‡§¶]+", " ", name)
    if "," in name:
        last, _, first = name.partition(",")
    else:
        parts = name.replace(".", ". ").split()
        if len(parts) < 2:
            return norm_text(name)
        last, first = parts[-1], " ".join(parts[:-1])
    first_initial = norm_text(first)[:1]
    return f"{first_initial} {norm_text(last)}".strip()


def read_pdf(path: str) -> dict:
    """First-pages text, info hints, and the reference-list text."""
    with pymupdf.open(path) as pdf:
        info = pdf.metadata or {}
        head = "\n".join(pdf[i].get_text() for i in range(min(2, pdf.page_count)))
        full = "\n".join(page.get_text() for page in pdf)
    refs = ""
    heads = list(_REF_HEAD_RE.finditer(full))
    if heads:
        # The last heading is the real list; earlier ones are table-of-contents hits.
        refs = full[heads[-1].end():]
    return {"info": info, "head": head, "refs": refs}


def extract_metadata(path: str, filename: str) -> dict:
    data = read_pdf(path)
    info = data["info"]
    title_hint = (info.get("title") or "").strip()[:200]
    author_hint = (info.get("author") or "").strip()[:200]
    text = data["head"][:7000]

    meta = {"title": None, "authors": [], "year": None, "venue": None,
            "doi": None, "abstract": None, "keywords": []}
    reply = llm.chat(PROMPT.format(title_hint=title_hint, author_hint=author_hint,
                                   filename=filename, text=text))
    parsed = parse_json(reply)
    if isinstance(parsed, dict):
        meta.update({k: parsed.get(k) for k in meta if k in parsed})

    doi = meta.get("doi") or (DOI_RE.search(data["head"]) or [None, None])[1]
    meta["doi"] = doi.rstrip(".,;)").lower() if doi else None
    try:
        meta["year"] = int(meta["year"]) if meta.get("year") else None
    except (TypeError, ValueError):
        meta["year"] = None
    meta["authors"] = [a.strip() for a in (meta.get("authors") or [])
                       if isinstance(a, str) and a.strip()]
    meta["keywords"] = [k.strip().lower() for k in (meta.get("keywords") or [])
                        if isinstance(k, str) and k.strip()]
    meta["title"] = (meta.get("title") or title_hint or filename).strip()
    meta["ref_text"] = norm_text(data["refs"])[:400_000]
    return meta
