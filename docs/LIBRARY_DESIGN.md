# Gloss Library — Architecture & Design

## A. Architecture Overview & File Layout

Keep the existing reader untouched. Add a **library layer** (DB + metadata + edges) and a **Home SPA** alongside the current reader view.

```
gloss/
  app.py                  # CHANGED: add library + librarian routes; serve Home
  extract.py              # unchanged
  llm.py                  # unchanged
  summarize.py            # CHANGED: persist Q&A, expose keyword extraction
  library/                # NEW package
    __init__.py
    db.py                 # SQLite connection, schema, migrations
    ingest.py             # add PDF -> hash, copy, extract, summarize, metadata, edges
    metadata.py           # title/authors/year/venue/DOI/abstract extraction
    edges.py              # connection computation (incremental)
    librarian.py          # retrieval + chat
    jsonparse.py          # tolerant JSON parser (shared)
  static/
    reader.js, reader.css # CHANGED: back-to-Home, Q&A history load/save
    home.js               # NEW: Home controller (list/search/librarian)
    graph.js              # NEW: cytoscape graph + timeline
    home.css              # NEW
    vendor/cytoscape.min.js  # vendored
  templates/
    index.html            # CHANGED: Home shell + reader shell (hash-routed)
```

**Recommendation: Cytoscape.js** (vendored). It handles both the force graph *and* a deterministic timeline layout (`preset` positions), has built-in click/select events and good perf to ~2–3k nodes. One lib for both views beats mixing d3-force + a timeline hand-roll.

**Routing:** single page, hash routes `#/home`, `#/graph`, `#/timeline`, `#/read/<sha>`. Reader stays a DOM section; we just show/hide.

---

## B. Storage Design

**PDFs:** user-chosen library folder `LIB_DIR` (persisted in `recents.json` → rename to `config.json`). On add, copy PDF into `LIB_DIR/pdfs/<sha>.pdf` (canonical). Original path recorded for reference. Caches (`pages`, `summaries`, `briefs`) stay in `~/.cache/gloss/` keyed by sha — unchanged, so the reader keeps working.

**SQLite** at `LIB_DIR/gloss.db`. Chosen over JSON for search, joins (authors/edges), and concurrent reads.

```sql
CREATE TABLE papers (
  sha TEXT PRIMARY KEY,
  pdf_path TEXT,            -- canonical copy
  orig_path TEXT,
  title TEXT, year INTEGER, venue TEXT, doi TEXT,
  abstract TEXT,
  brief TEXT,               -- whole-doc summary (also cached on disk)
  keywords_json TEXT,       -- ["concept", ...]
  meta_source TEXT,         -- 'pdf' | 'llm' | 'mixed'
  added_at TEXT, summarized_at TEXT
);
CREATE TABLE authors (
  id INTEGER PRIMARY KEY,
  name_norm TEXT UNIQUE,    -- lowercased, punctuation-stripped
  display_name TEXT
);
CREATE TABLE paper_authors (
  sha TEXT, author_id INTEGER, ord INTEGER,
  PRIMARY KEY (sha, author_id)
);
CREATE TABLE refs (             -- extracted reference strings per paper
  sha TEXT, raw TEXT, norm_title TEXT, doi TEXT
);
CREATE TABLE edges (
  a TEXT, b TEXT,            -- sha pair, a<b lexicographically
  etype TEXT,               -- shared_author|citation|shared_keyword|llm_related
  weight REAL,              -- 0..1
  detail TEXT,              -- JSON: {authors:[...]} / {keywords:[...]} / {reason:"..."}
  PRIMARY KEY (a,b,etype)
);
CREATE TABLE qa (
  id INTEGER PRIMARY KEY, sha TEXT,
  question TEXT, answer TEXT, citations_json TEXT, asked_at TEXT
);
CREATE TABLE librarian_chats (
  id INTEGER PRIMARY KEY, created_at TEXT, title TEXT
);
CREATE TABLE librarian_turns (
  id INTEGER PRIMARY KEY, chat_id INTEGER,
  role TEXT, content TEXT, meta_json TEXT, created_at TEXT
);
CREATE INDEX idx_pa_author ON paper_authors(author_id);
CREATE INDEX idx_refs_norm ON refs(norm_title);
```

**Migration of existing cache:** on first launch with a chosen `LIB_DIR`, run `migrate_from_cache()`:
1. Read `recents.json`; for each known sha with a cached `summaries` + `briefs` entry, insert a `papers` row (brief from cache, `summarized_at` from file mtime).
2. If the original PDF path still exists, copy to `pdfs/<sha>.pdf`; else mark `pdf_path=NULL` (reader can still show cached pages/summaries).
3. Metadata + keywords + edges are computed lazily (background queue) so migration is instant and non-blocking.

---

## C. Metadata Extraction

Pipeline (cheap→expensive, stop early):

1. **PyMuPDF `doc.metadata`** → title/author if non-empty and plausible (reject "untitled", generic).
2. **DOI regex** on first 2 pages: `10\.\d{4,9}/[-._;()/:A-Z0-9]+` (case-insensitive). If found, store; optional local crossref lookup is out of scope (local-first) — DOI alone is enough for citation matching.
3. **First-page LLM extraction** (always run once; cache in DB). Feed first page text (truncate ~4000 chars) + any PDF-metadata hints.

Parse with the tolerant parser (strip ```json fences, find first `{`…last `}`, `json.loads`, fallback to regex field grab). Claude = no `response_format`, so prompt forces JSON.

**Prompt (metadata.py):**

```
You extract bibliographic metadata from the first page of an academic PDF.
Return ONLY a JSON object, no prose, no code fences.

Schema:
{
  "title": string,
  "authors": [string, ...],      // full names in reading order, [] if unknown
  "year": integer|null,
  "venue": string|null,          // journal/conference, null if unknown
  "doi": string|null,
  "abstract": string|null,       // verbatim if present, else null
  "keywords": [string, ...]      // 5-10 topical concepts you infer, lowercase noun phrases
}

Rules:
- Use ONLY information present in the text. Do not guess authors or venue.
- If a field is not clearly present, use null (or [] for lists).
- keywords are your inferred topics even if no keyword section exists.

PDF metadata hints (may be wrong): TITLE_HINT="{pdf_title}" AUTHOR_HINT="{pdf_author}"

First page text:
---
{first_page_text}
---
```

Author normalization: lowercase, strip accents/periods, collapse "Lastname, F." vs "F. Lastname" to `firstinitial lastname` form for `name_norm`; keep original as `display_name`.

**References extraction** (for citation edges): LLM pass over the References section (locate heading "References"/"Bibliography"; take text after it). Return `[{raw, title, doi}]`. Store normalized titles (`norm_title` = lowercased, alphanumeric only). This runs once per paper, cached.

---

## D. Connection Edges (no embeddings, incremental O(K))

Four edge types, all lexical/structural except one bounded LLM pass.

| etype | computed from | weight |
|---|---|---|
| `shared_author` | ≥1 shared `author_id` | `min(1, 0.4 * shared_count)` |
| `citation` | one paper's `refs.norm_title`/`doi` matches another's title/doi | 0.9 (strong) |
| `shared_keyword` | Jaccard of keyword sets | `jaccard` if ≥0.15 else none |
| `llm_related` | LLM pairwise judgment on top-K candidates | from LLM (0..1) |

**"Not connected"** = **absence of any edge row**. We never store zero-weight edges. The UI shows isolated nodes with no lines; the "connections" network simply reflects stored edges. A paper with no edges is explicitly surfaced in Home ("3 papers not connected to the rest").

**Incremental algorithm when paper `X` is added (O(K) LLM calls):**

1. Compute `shared_author`, `citation`, `shared_keyword` edges against *all* existing papers — these are pure SQL/set ops, cheap, no LLM. (These scale O(N) in CPU but zero LLM cost.)
2. **Candidate generation for LLM:** score every other paper by a lexical signal =
   `2.0*shared_author + 3.0*citation + keyword_jaccard`. Take **top-K = 8** that *don't already* have a strong (`citation`) edge.
3. For those ≤8 candidates, run **one batched LLM call** (all pairs in a single prompt) → O(1) call, O(K) tokens per new paper. Store returned `llm_related` edges only where `related=true`.

Total LLM cost per added paper = 1 metadata call + 1 refs call + 1 batched relation call = **3 calls**, independent of library size. Fully cacheable (keyed by sha; relation results keyed by sorted sha pair).

**Pairwise relation prompt (edges.py):**

```
You judge how academic papers relate. Return ONLY a JSON array, no prose, no fences.

NEW PAPER:
  id: {X_sha}
  title: {X_title}
  keywords: {X_keywords}
  brief: {X_brief_truncated_800}

CANDIDATES:
{for each: - id, title, keywords, brief (truncated 400)}

For EACH candidate, output:
{
  "id": "<candidate id>",
  "related": true|false,
  "relation": "builds_on|contradicts|same_method|same_problem|background|unrelated",
  "weight": 0.0-1.0,     // confidence it is a meaningful research connection
  "reason": "one sentence, concrete, no invented facts"
}

Rules:
- related=false and relation="unrelated" when they only share generic terms.
- Base judgments only on the titles/keywords/briefs given. Do not invent shared citations.
Output: [ {...}, ... ]
```

Store rows where `related=true`; `detail={relation,reason}`.

Edges are **merged** across types (two papers can have both `shared_author` and `llm_related`). The displayed edge weight = max of its type weights; tooltip lists all types.

---

## E. Librarian

**Retrieval without embeddings** = BM25-ish lexical scoring over **paper cards** (title + keywords + brief + author names), computed in Python:

- Tokenize question → terms (lowercase, stopword-strip).
- Build an inverted index over cards (cached in memory, rebuilt on library change).
- Score with TF-IDF; take **top 12** cards.

**Context assembly:** pass those 12 cards (id, title, authors, year, keywords, brief truncated 500 chars). Never pass full PDFs. Include a count of total library papers so the model knows coverage.

**System prompt (librarian.py):**

```
You are the Librarian for a LOCAL research library of {N} papers. You help the
user decide which LOCAL papers to read and you are rigorously honest about gaps.

You will be given CANDIDATE PAPERS (the most lexically relevant 

<!-- Part 2 (sections E-H), generated separately because part 1 was truncated -->

# E. Librarian Design

## Retrieval (no embeddings)
Pre-build an in-memory TF-IDF index over **paper cards** at startup (rebuilt on paper add). Each card = `title + authors + year + venue + abstract + brief + keywords`. On query:

1. Tokenize query (lowercase, strip punctuation, drop stopwords).
2. Score every paper card with cosine over TF-IDF vectors (sklearn-free; hand-rolled dict math — no new dep).
3. Take **top-12** cards. If the best score < `MIN_SCORE` (tune ~0.05), set a `thin_retrieval` hint so the LLM leans toward `gap_flag`.

## Context assembly
Build a compact context block; **never send full briefs** — truncate each brief to ~400 chars to control tokens:

```
[P12] Title: ...
      Authors: A; B; C | Year: 2021 | Venue: ...
      Keywords: k1, k2, k3
      Brief: <=400 chars...
```
Include prior librarian turns (last 4) for conversational continuity.

## System prompt (exact)
```
You are the Gloss Librarian. You help a researcher navigate THEIR LOCAL
library of papers. You are given a numbered list of library papers (each
with an id like P7). You must answer ONLY using these papers as the
"library." 

RULES:
1. Recommend which library papers the user should read or revisit to
   answer their question. Reference papers ONLY by the ids provided
   (P1, P7, ...). NEVER invent a paper id, title, author, or claim a
   paper is in the library if it is not in the provided list.
2. If the provided papers do NOT genuinely address the question, say so
   plainly in the answer and set "gap_flag": true. Do not pretend thin
   matches are strong.
3. When the library is weak, you MAY suggest external directions: search
   terms, author names, or specific papers to look up. These are
   UNVERIFIED guesses from your general knowledge — they are NOT in the
   library. Put them in "external_suggestions" with "verified": false.
   Never put external suggestions in "papers".
4. "papers" must be a subset of the provided ids, each with a short,
   concrete "why" grounded in that paper's card.

Return ONLY a JSON object, no prose outside it, in this exact shape:
{
  "answer": "<natural-language answer, 2-6 sentences>",
  "papers": [{"id": "P7", "why": "<why this paper is relevant>"}],
  "gap_flag": true|false,
  "external_suggestions": [
    {"type": "search_term|author|paper", "text": "...", "verified": false}
  ]
}
If no library paper is relevant, "papers" must be [] and "gap_flag" true.
```
Append after the card list: `User question: <q>`. If `thin_retrieval`, prepend: `NOTE: lexical retrieval found only weak matches; scrutinize relevance carefully.`

## Parsing & mapping
Reuse the tolerant JSON parser from `llm.py` (extract first `{...}` block, repair trailing commas). Map `P7 -> sha` via the retrieval order. **Drop any `papers[].id` not in the sent set** (defense against hallucinated ids) and log it. Force `verified: false` on all external suggestions regardless of model output. Persist the turn to `librarian_turns` (query, raw answer JSON, resolved sha list).

---

# F. API Routes

| Method | Path | Payload | Response |
|---|---|---|---|
| GET | `/api/library` | — | `{papers:[{sha,title,authors,year,venue,keywords,has_summary}]}` |
| GET | `/api/library/search?q=` | — | filtered papers list (title/author/keyword substring) |
| POST | `/api/library/folder` | `{path}` | `{ok, path}` — set/change library folder |
| POST | `/api/library/scan` | — | `{added:[sha], skipped:[sha], errors:[]}` — ingest new PDFs in folder (hash-dedup) |
| GET | `/api/paper/<sha>` | — | `{meta, brief, summaries, qa:[...]}` full card |
| POST | `/api/ask` *(extended)* | `{sha, question}` | `{answer, citations}` **and persists to `qa`** |
| GET | `/api/qa/<sha>` | — | `{qa:[{id,question,answer,citations,ts}]}` |
| POST | `/api/librarian` | `{chat_id?, question}` | `{chat_id, answer, papers:[{sha,title,why}], gap_flag, external_suggestions}` |
| GET | `/api/librarian/chats` | — | `{chats:[{id,title,ts}]}` |
| GET | `/api/librarian/<chat_id>` | — | `{turns:[...]}` |
| GET | `/api/graph?mode=article\|author` | — | `{nodes:[{id,label,type,...}], edges:[{source,target,etype,weight}]}` |
| GET | `/api/timeline` | — | `{papers:[{sha,title,year,authors}]}` sorted by year |
| GET | `/api/author/<author_id>` | — | `{name, papers:[{sha,title,year}]}` |

Keep existing routes (`/api/open`, `/api/doc/<sha>`, `/api/page/...png`, `/api/summaries/<sha>`, `/api/resummarize`). Librarian `papers` returned with resolved `sha` so frontend can link directly.

**Edge building triggers:** `/api/library/scan` runs ingest → metadata → summarize → `rebuild_edges_for(sha)` (O(K), section D) → TF-IDF index refresh. Return progress; frontend polls or does one call per file.

---

# G. Frontend

Single SPA served by `index.html`; hash-router already defined. Add `static/home.js`, `static/graph.js`, `static/timeline.js`, `static/librarian.js`, shared `static/sidebar.js`. Reader (`reader.js`) stays intact.

## Router
```
#/home      -> renderHome()
#/graph     -> renderGraph()
#/timeline  -> renderTimeline()
#/read/<sha>-> openReader(sha)   // existing reader
```
A persistent top nav bar on all non-reader views: **Home · Graph · Timeline** + "Set Library Folder" + search box. Reader hides the bar, shows **Close** → `location.hash = '#/home'`.

## Home (`#/home`)
Two-column:
- **Left (main):** Librarian chat. Message list; each librarian answer renders `answer` text, then a **"Papers to read"** card list (clickable → `#/read/<sha>`, each with `why`), then if `gap_flag` a yellow **"Library gap"** banner, then **"Unverified external leads"** (greyed, labelled *not in your library*) listing search terms / authors / papers. Chat history dropdown loads prior chats.
- **Right (rail):** Upload button (`/api/upload` → auto-ingest → appears in list) + recents + "Browse full library" link that filters the list via `/api/library/search`.

## Graph (`#/graph`)
- Cytoscape.js canvas, `cose`/`fcose` layout.
- Toggle pill **Articles ⇄ Authors** → refetch `/api/graph?mode=`.
  - Article mode: nodes=papers, edges colored by `etype` (shared_author=blue, citation=green solid w/ arrow, shared_keyword=grey dashed, llm_related=purple). Edge width ∝ `weight`. **No edge = not connected** (nothing drawn — honest absence).
  - Author mode: nodes=authors, edge between two authors if they co-authored a library paper; node size ∝ #papers.
- Click node → opens shared **sidebar** (slide-in right):
  - **Article node:** title, meta, Gloss **brief**, keyword chips, "Open in Reader" button → `#/read/<sha>`, and a connections list ("Related: P3 (shared author), P9 (cites)").
  - **Author node:** name + list of that author's local papers; each row clickable → opens that paper's article sidebar (or directly to reader).
- Legend box (edge-type colors). Hover edge → tooltip with `etype` + weight + (for llm_related) the stored relation reason.

## Timeline (`#/timeline`)
- Horizontal year axis; papers as dots/cards grouped by `year` (null-year papers in an "Undated" lane). Simple CSS flex columns per year — no heavy lib.
- Click a card → same article sidebar as graph. "Open in Reader" from sidebar.
- Reuses `sidebar.js` so behavior is identical across graph/timeline.

## Reader changes (minimal, non-breaking)
- Add **Close → Home** (change existing close handler to set `#/home`).
- **Persisted Q&A:** on reader open, `GET /api/qa/<sha>` populates the Question panel history. `/api/ask` already answers; now the server persists each turn, and the panel appends from the response. Scroll-back shows prior questions with their paragraph-id citations (existing citation rendering reused).

## Shared sidebar contract (`sidebar.js`)
```
openSidebar({type:'article', sha})  // fetch /api/paper/<sha>
openSidebar({type:'author', id})    // fetch /api/author/<id>
```
Used by graph + timeline; article → reader navigation goes through the router so Close returns correctly.

---

# H. Build Order (riskiest first)

**M1 — Metadata + edge pipeline (riskiest).**
Implement ingest → metadata LLM extraction → authors normalization → `rebuild_edges_for` (shared_author, citation, shared_keyword, top-K llm_related). *Test:* ingest 8 known papers; assert author rows, assert a known shared-author edge exists, assert a known citation edge, dump edges to JSON and eyeball. **Risk:** metadata/JSON parse failures, author-name dedup ("J. Smith" vs "John Smith"); mitigate with the tolerant parser + a normalization function (lowercase, initials-collapse, store `canonical_name`), and a `metadata_confidence` field; flag low-confidence for manual fix.

**M2 — Storage + migration.**
Create `gloss.db`, migrate existing `~/.cache/gloss` by sha256: for each cached summary/brief, insert a `papers` row + link cached PDF path. *Test:* run migration twice (idempotent); row count == distinct cached shas; existing reader still opens via `/api/open`. **Risk:** breaking existing reader — gate behind `has_summary`; keep old cache as source of truth, DB as index.

**M3 — Library API + Home list + Reader Q&A persistence.**
`/api/library`, `/scan`, `/paper/<sha>`, extended `/api/ask` + `/api/qa`. *Test:* ask 3 questions in reader, reopen paper, history present; close returns Home; Home lists all migrated papers.

**M4 — Librarian.**
TF-IDF index + `/api/librarian` + Home chat UI. *Test (critical honesty cases):* (a) ask something clearly in library → correct papers + `gap_flag:false`; (b) ask an off-topic question → `gap_flag:true`, `papers:[]`, external suggestions labelled unverified; (c) **hallucination probe:** confirm no returned `papers.sha` is outside the sent top-12 (unit-test the id filter). **Risk:** fabricated citations — enforced by server-side id filtering, not trust in the model.

**M5 — Graph view.**
`/api/graph` both modes + Cytoscape render + sidebar + article/author toggle. *Test:* node counts match DB; edge colors per `etype`; author node lists correct papers; "no edges" renders cleanly for an isolated paper. **Risk:** layout perf on large libraries — cap initial render,