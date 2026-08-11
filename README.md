# Gloss

Read a paper with a short summary of every paragraph in the margin beside it.

Each summary is written with the **whole document** as context, not just the
paragraph on its own — so a paragraph that opens "Taken together, the outage
results suggest…" gets a note that says *which* results and *whose* models,
instead of paraphrasing the words in front of it.

Summaries come from whichever LLM provider you configure — OpenAI, Anthropic,
TAMU AI, OpenRouter, or a model running locally under Ollama. The PDF itself
never leaves this Mac; only paragraph text is sent for summarizing, and with a
local model not even that.

## Setup

Double-click **`install.command`** in Finder. It builds a Python environment at
`~/.venvs/gloss` (outside this folder, because the Desktop is iCloud-synced
and a venv is thousands of files iCloud should never sync), then asks which
provider you want and writes a `.env` file with your answer.

Gloss talks to any endpoint that speaks the OpenAI chat-completions API, which
covers far more than OpenAI:

| Provider | Key needed | Notes |
|---|---|---|
| **TAMU AI** | TAMU NetID | Free. The default. Also reads `~/.config/tamu-ai/api-key`. |
| **OpenAI** | paid | |
| **Anthropic** | paid | Via their OpenAI-compatible layer, which Anthropic documents as intended for testing rather than production. |
| **OpenRouter** | paid | One key for most vendors. Set `GLOSS_MODEL` to a `vendor/model` id. |
| **Ollama** | none | A model on this Mac. No key, no cost, nothing leaves the machine. |
| **Anything else** | varies | `GLOSS_PROVIDER=custom` plus `GLOSS_BASE_URL` — Gemini, Groq, Together, LM Studio, vLLM. |

Your key lives in `.env` in this folder, which is gitignored and `chmod 600`.
It is never written into any source file. See `.env.example` for every option;
`python llm.py` prints the active configuration and does one round-trip.

**What it costs.** A 10-page paper is about **115,000 input tokens and 1,000
output** (one pass over the whole document, then one small call per paragraph).
That is free on TAMU, cents on a mini-tier hosted model, and a few tens of cents
on a frontier one — check your provider's current rates. Reopening a paper you
have already read costs nothing at all.

## Use

Double-click **`run.command`**. A browser window opens; leave the Terminal
window running while you read.

- **Choose a PDF** (or ⌘O, from anywhere) opens a normal Finder file chooser.
  You can also drag a PDF onto the window, or pick one from **Recent**.
- **Close** returns to that opening screen, with the paper you just closed at the
  top of Recent. The toolbar belongs to the open document, so it appears with it.
- Summaries fill in as they arrive — a long paper is readable within a couple of
  seconds. On a hosted provider the whole paper finishes in about a minute; on a
  local model, see the timing note below.
- **Hover a paragraph** to highlight its summary, or hover a summary to
  highlight its paragraph. A curve connects the active pair.
- **Question** (or ⌘K) opens a panel where you can ask anything about the open
  paper. Answers are drawn from the document itself and cite the passages they
  came from — click a citation to jump to that paragraph and flash it. If the
  paper doesn't answer the question, it says so instead of guessing. Esc closes
  the panel.
- **Summaries** toggles the margin; **+ / −** zoom.
- Reopening a paper you've read before is instant — summaries are cached in
  `~/.cache/gloss/` by file hash.

## How it works

| File | Role |
|---|---|
| `extract.py` | PDF → paragraphs with their positions on the page |
| `llm.py` | one thin client for any OpenAI-compatible provider |
| `summarize.py` | two-pass summarization, and question answering |
| `app.py` | Flask server, page rendering, routes |
| `static/reader.js` | margin layout, highlighting, progressive fill |

**Finding paragraphs** is the hard part, and it works at the line level rather
than trusting PyMuPDF's text blocks — a block can hold two paragraphs, or half of
one. Pages are cut into regions (horizontal bands, then full-width lines, then
columns), and paragraph breaks are recovered from three signals: a first-line
indent, a last line that ends short of the right margin, and a jump in leading.
Words hyphenated across a line break are rejoined, rotated watermarks and
figure-internal labels are dropped, an oversized drop cap is folded back into the
paragraph it opens, and a paragraph continuing across a column or page break is
stitched back together.

Cutting at full-width lines is what makes the columns findable. A line that
crosses the gutter belongs to neither column, so it acts as a horizontal
separator; without that step a full-width abstract set close above a two-column
body hides the gutter, the columns interleave, and every "paragraph" becomes one
line of column 1 welded to one line of column 2. Column width also bounds
stitching: a paragraph continues into a column of the same measure, never into a
table cell or a full-width caption that merely came next in reading order.

A document can also have more than one body font size — Nature sets its Methods
at 7.5 pt against a 9.3 pt body — so the extractor accepts a *set* of body sizes,
admitting a second one only when its lines are about as long as the main size's.
That test is what keeps a multi-page table out while letting a genuine second
body size in. Sizes are sampled from the opening pages and from further in, since
a Methods section nine pages deep is invisible to a sample of the front matter.

**Summarizing** runs in two passes. The first reads the document once and writes
a compact brief — question, methods, findings, acronyms. The second sends one
small request per paragraph carrying that brief, the section heading, and the
neighbouring paragraphs. One call per paragraph rather than one call per paper
means a failure costs a single margin note instead of the whole document.

**Answering questions** sends the document's paragraphs, each labelled with its
id, and asks for citations by that id — which is what makes a citation clickable.
Short papers are sent whole, since that is always the best context. Long ones
can't be, and no OpenAI-compatible endpoint here offers embeddings, so the
ranking is lexical:
idf-weighted word overlap between the question and each paragraph, plus the
opening pages for framing. Crude next to semantic search, but it needs no index
and no extra dependency, and it finds the passages that use the words being asked
about.

## Testing

```bash
~/.venvs/gloss/bin/python tests/test_extract.py
~/.venvs/gloss/bin/python tests/test_llm.py
```

`test_llm.py` runs against a stub server — no key, no network. It covers the
ways a provider client fails *quietly*: a gateway that streams when you didn't
ask, a reasoning model that inlines its chain of thought, an error returned as
HTTP 200, a retryable error versus one that must not be retried, and a failed
row that must come back as a failure rather than an empty summary.

`test_extract.py` runs against real papers on the Desktop, each of which broke an
earlier version of the extractor: a two-column PNAS paper, one with a rotated
sidebar watermark, a line-numbered manuscript, a Nature Energy paper that breaks
three assumptions at once (full-width abstract over the columns, a drop cap, and
Methods set smaller nine pages in), and a scanned PDF that must be reported
rather than silently rendered empty.

Beyond counts, it checks the two ways extraction goes wrong quietly: passages the
document definitely contains that must survive, and text that must *not* appear
because it can only exist if two separate elements were welded together at a seam
the layout does not have. It also checks that a highlight band hugs its own text
— a band covering ground its text does not is how a paragraph stitched across a
column break used to look.

To see what the extractor found on any page, open
`/debug/<sha>/<page>` — it draws every detected paragraph box over the page
image. Red boxes get summarized, blue ones are too short to need it.

## Limits

- **Scanned PDFs** with no text layer are detected and reported, not read.
  Adding OCR would be a contained change (`tesseract` is already installed).
- **Reference lists and figure captions** are excluded from summarizing.
- **Tables get shredded into cells.** Each cell becomes its own paragraph, too
  short to summarize, so no card appears and nothing is sent to the model — but
  the cells do sit in the document text that questions are answered from. A
  sentence running across a page break whose next page opens with a table is
  delivered as two adjacent cards rather than one, which is the deliberate price
  of never splicing a caption into the middle of a sentence.
- On a **two-column page** both columns' paragraphs share one right-hand margin,
  so cards there sit near their paragraph rather than exactly level with it.
  Reading order is always preserved, and the connector curve shows the pairing.
- Complex front pages (a title block beside a sidebar) are segmented
  best-effort; a section label may occasionally be missing rather than wrong.
- **Local models are much slower than hosted ones, by a lot.** Measured on this
  Mac with `qwen3.6` (23 GB): 70 s for the whole-document pass and ~84 s per
  paragraph, which works out to roughly an hour for a 76-paragraph paper rather
  than the ~1 minute TAMU takes. That is inherent — a paper is ~115,000 input
  tokens and local prompt processing is far slower. Use a smaller model
  (`ollama pull qwen3:4b`) if you want local summaries at a usable speed; the
  quality drop matters less for margin notes than it would for prose. Gloss
  automatically drops to 2 parallel requests and a 15-minute timeout for local
  providers, because one local server queues rather than parallelizes.

## License

MIT — see [LICENSE](LICENSE).
