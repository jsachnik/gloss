# Changelog

The version is written down once, in `app.py`. From there it reaches the
opening screen, the startup banner, and the `/api/health` probe.

That probe is also how a second launch finds the copy already running, so it
doubles as the version check that matters in practice: a running server serves
the code it was started with, so editing Gloss and relaunching hands you the old
one without a word, and the new code looks like it did nothing. When the running
copy's version differs from the launching one, the terminal now says so and says
how to restart.

Versions are `major.minor.patch` — the patch number moves for fixes, the minor
for refinements, the major for a change in how the reader itself works.

## 1.2.1 — 2026-10-14

- **A paper added twice at once vanished.** A PDF dropped into the library folder
  and then added with **Add PDF** was scanned as two identical files; the second
  matched the first by DOI, so the paper was recorded as a duplicate *of itself*,
  overwrote its own record, and disappeared from the list and map. A paper can no
  longer match itself or replace a stored record, and identical files in one scan
  count as one paper (the plainest file name is kept).
- **Add PDF reports on the file you added.** The verdict used to come from
  everything the scan found, so an unrelated problem could discard a good upload.
  It now compares the file's hash first, and says which paper a repeat duplicates.
- **PDFs dropped into the folder by hand are found.** Opening Home reads any
  unread PDFs in the library folder. Problems stay on screen until dismissed, and
  an exhausted AI allowance is explained in plain words; the PDF stays in the
  folder and is read the next time Gloss starts.

## 1.2.0 — 2026-10-07

- **A local library.** Papers are kept in a folder you choose (default
  `~/Gloss Library`; set `GLOSS_LIBRARY` to change it). Adding a PDF reads its
  title, authors, year, venue and DOI from the first pages, summarizes it once
  with the existing two-pass pipeline, and stores everything -- including every
  question you ask about it -- in a SQLite file beside the PDFs. Byte-identical
  and same-DOI/same-title copies are detected and left out.
- **Home** at `/home` replaces the opening screen: the library list, a **Map**
  of how papers connect (Articles or Authors), a **Timeline** by year, and
  a **Librarian** to ask what to read. The reader moved to `/read`; **Close**
  returns Home, and a paper's earlier questions reload when it is reopened.
- **Connections without embeddings.** Shared authors, citations (one paper's
  reference list naming another's title or DOI), shared topic words, and one
  batched model judgement per new paper over its best few candidates -- so the
  cost of adding a paper does not grow with the library. No line means no
  connection was found, and papers with none are marked.
- **The Librarian only recommends papers you have.** Answers are per-paper
  (what it asks, what it finds), then what they show together and what stays
  open. Any paper id the model invents is dropped by the server, and when the
  library cannot answer it says so and offers clearly-labelled unverified leads.
  The papers an answer uses are highlighted in blue in the list, map and
  timeline, with footnote numbers that jump to them.
- **Windows.** `run.bat` starts the app; the file chooser uses the Windows
  dialog. Background summarizing runs two requests at a time and waits out
  rate limits instead of failing the paper.

## 1.1.0 — 2026-08-22

- **The text on a page can now be selected and copied.** A page is a picture,
  so until now nothing on it could be. Gloss lays an invisible copy of the
  page's own words over the image, at the coordinates the glyphs occupy, and
  the selection you drag is over those -- the arrangement any PDF in a browser
  uses. Each page's words are fetched as the page comes near, the way its
  image is loaded, so a 200-page report costs what you read of it.
- **What lands on the clipboard is prose, not a column of fragments.** Copying
  out of a PDF usually hands over one line per line, hyphens and all. Every
  span here knows which paragraph and which line it came from, so a selection
  is put back together with the rule the extractor already uses: lines joined,
  a word split across a line break healed, a blank line between paragraphs.
  What you paste is what Gloss sent the model. Anything outside a paragraph --
  a heading, a table row -- keeps its line breaks, because running a table row
  together would be nonsense. A manuscript's line numbers are left out of the
  text layer altogether, so 24, 25, 26 never turn up threaded through a quote.
  Two things identify them, and neither would do alone: they share a right
  edge, and they count.
- **⌘C copies the whole paragraph under the pointer**, when nothing is
  selected -- the joined, dehyphenated text rather than the lines it happens
  to be set in. The passage flashes to say which one went.
- Under this, the transparent hit target over each paragraph is gone: the text
  layer now covers the page and would have swallowed the hover. Each page tests
  the pointer against its own paragraph rectangles instead, which is fewer
  elements than before, and the spans of a two-column page are put in reading
  order by column so a selection dragged down one column does not come back
  interleaved with the other.

## 1.0.2 — 2026-08-22

- **The summary cards sat a long way to the right of the text.** The margin
  column was placed against the edge of the widest page in the document, and
  every page is centred in that width -- so a paper carrying three landscape
  tables among 47 letter-size pages had all 131 of its cards pushed 162 px
  further right than the reading they annotate, with a leader line stretching
  across the gap to say so. Two things were being confused with each other: the
  edge of the paper and the end of the writing. Each page now reports where its
  ink actually stops, and the cards line up just past that -- on the middle
  page's column, so they stay in one straight line. A page whose text runs
  further right, and any card that comes to rest beside one, moves out far
  enough to keep off it; no card ever covers text.

## 1.0.1 — 2026-08-14

- **Every card in a Wiley paper was labelled "Abstract".** Wiley numbers its
  sections with a vertical bar padded by narrow no-break spaces — `3 | The
  Process of Scenario Planning` — and the extractor knew only the `3.` and `3)`
  forms. No heading after the abstract was recognized, so the running label
  never advanced and all 154 paragraphs of one paper claimed to be in its
  abstract. Nothing else was wrong with them, which is why it went unnoticed:
  the paragraphs, their positions and their notes were all correct, and only the
  section each one named was false — including in the prompt, where the wrong
  section is context the model is told to trust. Bars and spaced dashes now
  count as section numbering alongside periods and parens.
- **A section title that wrapped was cut off at the line break.** Only a
  heading's first line carries the number that identifies it, so the rest was
  discarded and the label read "2 | The Construction and Characteristics of".
  A heading now takes in the lines set directly beneath it in its own style.

Known gap, unchanged: a manuscript free to name its own sections — headings
that are neither a standard section word nor numbered — still has no heading
the extractor can recognize, and its cards all carry the first one found.

## 1.0 — 2026-08-12

First release.

Opens a PDF, recovers its paragraphs with their positions on the page, and
writes a note in the margin for each one, with the whole document as context.
Questions about the open document are answered from its text, with links back to
the passages the answer came from. Nothing leaves the machine but the paragraph
text sent to the configured model — and with a local provider, not even that.

Fixes that landed on the way here, each found on a real paper:

- **Elsevier papers produced no summaries at all.** Elsevier justifies text by
  stretching the spaces rather than the words, storing each stretched space as
  its own span at a larger font size than the words around it. Sizing a line by
  all of its spans sized it by its whitespace, so every line containing a space
  — the prose, all of it — missed the body size and was dropped as furniture.
  What survived was table cells and lone citations, enough of them that nothing
  reported the document as unreadable. Lines are now sized by their glyphs.
- **Launching twice started a second server.** Both worked, but each kept its
  own documents and summaries, so a paper opened in both windows was extracted
  and summarized twice over and half the margin notes arrived in a window that
  had been abandoned. A second launch now returns to the copy already running.
- **Summaries vanished when the zoom changed.** They stay on screen.
- **Recent documents** are capped at five.
