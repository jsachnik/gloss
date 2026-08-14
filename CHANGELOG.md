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
