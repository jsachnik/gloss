"""Margin summaries and document Q&A.

Model access goes through `llm.py`, which speaks to any OpenAI-compatible
endpoint -- OpenAI, Anthropic, TAMU AI, OpenRouter, or a local Ollama.

Margin summaries are produced in two passes.

The point of two passes is that a paragraph summarized alone is often
meaningless.  Real paragraphs open with "Taken together, the outage results
suggest ..." or "This metric, referred to as the swing, captures ...", and a
model shown only those words cannot say what results, or whose metric.

    Pass 1  read the whole document once, produce a compact brief
    Pass 2  summarize each paragraph with that brief, its section heading, and
            its immediate neighbours supplied as context

Pass 2 is one small call per paragraph rather than one large call over the
document: accuracy is better, and a failure costs a single margin note instead
of the entire paper.

No sampling parameters and no `response_format` are ever sent: summaries come
back as plain prose, which is all a margin note needs, and omitting them is what
lets thinking-enabled models work without special-casing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

import llm

CACHE_DIR = Path.home() / ".cache" / "gloss"

# None means "the model configured in .env" -- one model for everything, per
# the provider abstraction in llm.py.
PARAGRAPH_MODEL = None
BRIEF_MODEL = None

# Bump when either prompt below changes, or cached summaries written by the old
# wording will be served forever.
PROMPT_VERSION = 3

CHUNK = 16              # results are published to the UI after every chunk
NEIGHBOUR_CHARS = 400   # how much of the previous/next paragraph to pass along
BRIEF_HEAD = 20000      # long documents: chars taken from the front ...
BRIEF_TAIL = 8000       # ... and from the end, where conclusions live

_PREAMBLE_RE = re.compile(
    r"^\s*(here('s| is)( a)?( the)?( brief| short| concise)? summary\s*:?\s*|"
    r"summary\s*:\s*|this paragraph\s+)", re.I)


# Raised when no usable LLM configuration exists. Re-exported from llm so
# app.py's handling is unchanged.
SummarizerUnavailable = llm.SummarizerUnavailable


def check_ready() -> str:
    """Return '' if summarization can run, else a message explaining why not."""
    return llm.check_ready()


def _cache_path(kind: str, sha: str) -> Path:
    path = CACHE_DIR / kind
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{sha}.json"


def _clean(text: str) -> str:
    """Strip the preamble a chat model sometimes prefixes to a bare answer."""
    text = (text or "").strip()
    text = _PREAMBLE_RE.sub("", text).strip()
    text = re.sub(r"^[\"'“](.*)[\"'”]$", r"\1", text.strip()).strip()
    return re.sub(r"\s+", " ", text)


# --- pass 1: the paper brief ------------------------------------------------

BRIEF_PROMPT = """You are preparing background notes on a document. Another model \
will use your notes as context while it summarizes the document one paragraph at a \
time, so the notes must let it understand any paragraph in isolation.

Write a dense, factual brief covering:
- the title and what kind of document this is
- the central question or claim
- the data and methods used
- the main findings, including numbers that recur in the text
- key terms, acronyms and abbreviations, with what each means

Maximum 350 words. No preamble, no bullet headers like "Title:" - just the notes.

DOCUMENT:
"""


def _condense(full_text: str, sections) -> str:
    """Keep a long document inside one comfortable request."""
    if len(full_text) <= BRIEF_HEAD + BRIEF_TAIL:
        return full_text
    outline = " / ".join(dict.fromkeys(s for s in sections if s))[:2000]
    return (full_text[:BRIEF_HEAD]
            + f"\n\n[...document continues. Section outline: {outline}...]\n\n"
            + full_text[-BRIEF_TAIL:])


def build_brief(doc: dict, sha: str, force: bool = False) -> str:
    """Pass 1. Read the document once and cache a brief describing it."""
    # Keyed by model as well as prompt version: the paragraph cache already is
    # (the model is part of its hash), and without this a reader who switched
    # providers would go on being served a brief the previous provider wrote.
    try:
        current_model = BRIEF_MODEL or llm.settings().model
    except llm.SummarizerUnavailable:
        current_model = ""

    cache = _cache_path("briefs", sha)
    if cache.exists() and not force:
        try:
            stored = json.loads(cache.read_text())
            if (stored.get("version") == PROMPT_VERSION
                    and stored.get("model") == current_model):
                return stored["brief"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    paragraphs = doc["paragraphs"]
    body = [p for p in paragraphs if p["kind"] == "body"]
    text = _condense("\n\n".join(p["text"] for p in body),
                     [p["section"] for p in paragraphs])
    brief = _clean(llm.chat(BRIEF_PROMPT + text, BRIEF_MODEL))
    try:
        cache.write_text(json.dumps({"version": PROMPT_VERSION,
                                     "model": current_model, "brief": brief}))
    except OSError:
        pass
    return brief


# --- pass 2: one call per paragraph -----------------------------------------

PARAGRAPH_PROMPT = """You write margin notes beside the paragraphs of a document, \
so a reader can follow the argument while skimming.

BACKGROUND ON THE WHOLE DOCUMENT (use this to resolve vague references):
{brief}

SECTION THIS PARAGRAPH APPEARS IN: {section}

PARAGRAPH BEFORE IT (context only, do not summarize):
{previous}

PARAGRAPH AFTER IT (context only, do not summarize):
{following}

THE PARAGRAPH TO SUMMARIZE:
{text}

Write 1-2 sentences in plain language. Aim for about 30 words; up to roughly 45 is \
fine when the extra words genuinely earn their place. Don't pad to reach a length.

Say what this paragraph establishes, and where it helps, what it contributes to the \
argument. Use the background to resolve vague references like "this metric", "these \
results" or "taken together" by naming what they actually refer to - that resolution \
is the most useful thing a margin note can do.

Do not restate the abstract. Do not begin with "This paragraph", "The authors" or \
"The paper". Output only the summary sentence(s)."""


def _neighbour(paragraphs, index, step):
    """Text of the nearest body paragraph in one direction, for local context."""
    i = index + step
    while 0 <= i < len(paragraphs):
        para = paragraphs[i]
        if para["kind"] == "body" and para["chars"] > 40:
            return para["text"][:NEIGHBOUR_CHARS]
        i += step
    return "(none)"


def _build_prompt(job):
    return PARAGRAPH_PROMPT.format(
        brief=job["brief"],
        section=job["section"] or "(not identified)",
        previous=job["previous"],
        following=job["following"],
        text=job["text"],
    )


def summarize_document(doc, sha, brief, on_results=None, model=PARAGRAPH_MODEL,
                       cancelled=None):
    """Pass 2. Summarize every summarizable paragraph.

    on_results(mapping) is called after each chunk with {paragraph_id: summary},
    so the reader can fill margin notes as they arrive instead of waiting for the
    whole document.

    Returns {paragraph_id: summary} for everything that succeeded, plus a list of
    (paragraph_id, error).  Failures are reported, never silently blank: a card
    with no note would read as "this paragraph did not matter".
    """
    paragraphs = doc["paragraphs"]
    jobs = []
    for index, para in enumerate(paragraphs):
        if not para["summarizable"]:
            continue
        jobs.append({
            "id": para["id"],
            "text": para["text"],
            "section": para["section"],
            "brief": brief,
            "previous": _neighbour(paragraphs, index, -1),
            "following": _neighbour(paragraphs, index, +1),
        })

    cache_path = str(_cache_path("summaries", sha))
    summaries, failures = {}, []

    for start in range(0, len(jobs), CHUNK):
        if cancelled is not None and cancelled():
            break
        batch = jobs[start:start + CHUNK]
        # run_batch gives parallelism, retry with backoff, a disk cache keyed by
        # prompt content, and -- importantly -- returns failures instead of
        # raising, so one bad paragraph cannot discard the finished ones.
        results, batch_failures = llm.run_batch(
            batch,
            build_messages=_build_prompt,
            model=model,
            cache_path=cache_path,
            prompt_version=PROMPT_VERSION,
        )
        produced = {}
        for job, result in zip(batch, results):
            if result:
                produced[job["id"]] = _clean(result)
        for _, job, error in batch_failures:
            failures.append((job["id"], error))
        summaries.update(produced)
        if on_results and produced:
            on_results(produced)

    return summaries, failures


def resummarize(doc, sha, brief, paragraph_id, model=PARAGRAPH_MODEL):
    """Regenerate one paragraph's note, bypassing the cache."""
    paragraphs = doc["paragraphs"]
    index = next((i for i, p in enumerate(paragraphs) if p["id"] == paragraph_id), None)
    if index is None:
        raise KeyError(paragraph_id)
    para = paragraphs[index]
    prompt = _build_prompt({
        "id": para["id"],
        "text": para["text"],
        "section": para["section"],
        "brief": brief,
        "previous": _neighbour(paragraphs, index, -1),
        "following": _neighbour(paragraphs, index, +1),
    })
    return _clean(llm.chat(prompt, model))


# --- questions about the document -------------------------------------------

ANSWER_MODEL = None
ASK_BUDGET = 60000      # chars of document text sent with a question
SNIPPET_CHARS = 900     # per-paragraph cap when a long document is trimmed

_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")

ASK_PROMPT = """You are answering a reader's question about one specific document. \
Everything you need is below; the reader is looking at this document right now.

BACKGROUND ON THE DOCUMENT:
{brief}

NUMBERED PASSAGES FROM THE DOCUMENT:
{passages}

QUESTION:
{question}

Answer from the passages above. Rules:

- Cite the passages you used by their number in square brackets, like [12] or \
[3, 14]. Cite the specific passage that carries the claim, not a whole run of them.
- If the document does not answer the question, say so plainly and say what it \
does cover that is closest. Do not fill the gap from general knowledge.
- If you are drawing on your own knowledge to add context beyond the document, \
mark that sentence clearly as outside the document.
- Be direct and concrete. Give numbers and specifics where the document has them.
- A few sentences is usually right; use short paragraphs or a brief list if the \
question genuinely has several parts. No preamble, no restating the question."""


def _relevant(paragraphs, question, budget=ASK_BUDGET):
    """Choose which paragraphs to send with a question.

    Short documents go in whole -- that is always the best context.  Long ones
    (books, 300-page reports) cannot, and this gateway has no embeddings
    endpoint, so ranking is lexical: idf-weighted overlap between the question's
    words and each paragraph's.  Crude next to a semantic search, but it reliably
    finds the passages that actually use the words being asked about, and it
    needs no index, no extra dependency, and no network call.

    Selected paragraphs are returned in document order so the model reads them
    in the order the author wrote them.
    """
    body = [p for p in paragraphs if p["kind"] == "body" and p["text"].strip()]
    if not body:
        return []
    if sum(len(p["text"]) for p in body) <= budget:
        return body

    frequencies = Counter()
    tokenized = []
    for para in body:
        tokens = set(_WORD_RE.findall(para["text"].lower()))
        tokenized.append(tokens)
        frequencies.update(tokens)

    total = len(body)
    asked = set(_WORD_RE.findall(question.lower()))
    scored = []
    for index, (para, tokens) in enumerate(zip(body, tokenized)):
        overlap = asked & tokens
        score = sum(math.log(1 + total / (1 + frequencies[word])) for word in overlap)
        scored.append((score, index, para))
    scored.sort(key=lambda row: (-row[0], row[1]))

    chosen, used = [], 0
    for score, index, para in scored:
        if score <= 0:
            break
        length = min(len(para["text"]), SNIPPET_CHARS)
        if used + length > budget:
            continue
        chosen.append((index, para))
        used += length
    # Always include the opening, which carries the abstract and framing.
    for index, para in enumerate(body[:3]):
        if not any(i == index for i, _ in chosen):
            chosen.append((index, para))
    chosen.sort(key=lambda row: row[0])
    return [para for _, para in chosen]


def answer_question(doc, brief, question, model=ANSWER_MODEL):
    """Answer a question about the document. Returns (answer, cited_ids).

    Passages are labelled with their paragraph id, so a citation in the answer
    maps straight back to a highlightable region on the page.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")

    selected = _relevant(doc["paragraphs"], question)
    if not selected:
        raise ValueError("this document has no extractable text to search")

    trim = len(selected) > 40
    passages = []
    for para in selected:
        text = para["text"]
        if trim and len(text) > SNIPPET_CHARS:
            text = text[:SNIPPET_CHARS] + " [...]"
        heading = f" ({para['section']})" if para.get("section") else ""
        passages.append(f"[{para['id']}]{heading} {text}")

    reply = llm.chat(ASK_PROMPT.format(
        brief=brief or "(not available yet)",
        passages="\n\n".join(passages),
        question=question,
    ), model)

    answer = (reply or "").strip()
    valid = {p["id"] for p in selected}
    cited = []
    for group in _CITE_RE.findall(answer):
        for number in group.split(","):
            try:
                pid = int(number)
            except ValueError:
                continue
            if pid in valid and pid not in cited:
                cited.append(pid)
    return answer, cited


def sha_of(path: str) -> str:
    """Content hash of a PDF; the key for every cache in this app."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:20]
