"""Turn a PDF into paragraphs that know where they are on the page.

The whole app hangs off this module: every summary card in the margin is
anchored to a paragraph's rectangles, so paragraphs have to be both textually
correct and positionally correct.

Why this works at the *line* level rather than using PyMuPDF's blocks: blocks
are not paragraphs.  Probing real papers showed a single PNAS block holding two
paragraphs, and a line-numbered manuscript splitting one sentence across several
blocks.  Lines, by contrast, carry clean geometry (bbox, font size, direction)
that the paragraph boundaries can be recovered from.

The three signals that recover a paragraph break, measured on a real two-column
PNAS page whose justified lines all end at x = 551.3:

    indent       paragraph-initial lines start 10.0 pt right of the column edge
    short line   a paragraph's last line ended 110.9 pt short, against 0.1 pt
                 for every line that continues
    leading      the baseline-to-baseline distance jumps at a paragraph break

Leading is used rather than raw vertical gap because double-spaced manuscripts
have inter-line gaps as large as their line heights; the *ratio* to the document's
own median leading is what stays meaningful across layouts.

Body text is identified by font size, but a document can have more than one body
size -- PNAS sets Materials and Methods smaller than the rest, and one report
here sets its narrative in 11 pt among 9 pt evidence listings.  Picking a single
size silently dropped whole sections while still producing a plausible-looking
set of paragraphs, so `_body_sizes` returns a set, sampled from the opening
pages *and* from further in (Nature puts its 7.5 pt Methods on page 9).

Before any of that, the page has to be cut into regions, and the cut that
matters is at full-width lines.  A line crossing the column gutter belongs to
neither column: it is a horizontal separator.  Skip that step and a full-width
abstract sitting close above a two-column body hides the gutter entirely, so the
columns interleave and every "paragraph" is one line of column 1 welded to one
line of column 2.  Region width is then also what limits stitching: a paragraph
continues into a column of the *same measure*, never into a table cell or a
full-width caption that merely came next in reading order.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

# --- tunables ---------------------------------------------------------------

SIZE_TOL = 0.7          # keep lines within this many points of the body size
LEADING_BREAK = 1.35    # leading > this * median leading starts a paragraph
INDENT_MIN = 4.0        # points of first-line indent that counts as a break
SHORT_FRAC = 0.12       # last line ending this fraction of column width short
JUSTIFIED_FRAC = 0.55   # fraction of flush-right lines that means "justified"
COL_GAP_MIN_PT = 8.0    # a column gutter is at least this many points wide
SPANNER_MAX_FRAC = 0.35  # above this share, full-width lines are the body itself
DROP_CAP_RATIO = 1.8    # an initial this many times body size is a drop cap
IMG_OVERLAP = 0.6       # drop a line this covered by an image (figure interior)
STITCH_MEASURE = 0.7    # a continuation's column is this close in width
MIN_PARA_CHARS = 180    # below this a paragraph is shown but not summarized
MIN_BODY_LINES = 25     # fewer than this in a whole document means "no text layer"
BODY_SHARE = 0.25       # a second body size must hold this share of the text
BODY_SPAN = 0.25        # ...and sit within this fraction of the dominant size
BODY_LINE_RATIO = 0.6   # ...and have lines about as long (rejects table cells)
LEAD_PAGES = 8          # opening pages that decide the dominant body size
SAMPLE_PAGES = 32       # further pages sampled when looking for a second one

_SECTION_WORDS = (r"abstract|introduction|background|methods?|materials|results?|"
                  r"discussion|conclusions?|references|bibliography|acknowledge?ments?|"
                  r"appendix|supplementary|data availability|significance")
# A standalone section name, optionally numbered: "Introduction", "3. Methods".
# Anchored at both ends so a sentence merely *starting* with "Results ..." is not
# mistaken for a heading -- that misfire put a Significance-box sentence in the
# section slot, which would then have been fed to the model as false context.
_SECTION_RE = re.compile(
    rf"^\s*(\d{{1,2}}(\.\d{{1,2}})*\s*[.)]?\s*)?({_SECTION_WORDS})\s*[:.]?\s*$", re.I)
# A numbered run-in heading: "1.2. Inequality of Impacts Across Subpopulations."
# Two digits max: section numbers are 1-99, so a citation year like "2019)"
# cannot masquerade as a numbered heading.
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}(\.\d{1,2})*\s*[.)]\s+\S")
_HEADING_RE = re.compile(
    rf"^\s*((\d{{1,2}}(\.\d{{1,2}})*\s*[.)]?\s*)?({_SECTION_WORDS})\s*[:.]?\s*$"
    r"|\s*\d{1,2}(\.\d{1,2})*\s*[.)]\s+\S)", re.I)
_STOP_RE = re.compile(
    r"^\s*(references|bibliography|works cited|literature cited|"
    r"acknowledge?ments?|supporting information)\s*$", re.I)
_LINENUM_RE = re.compile(r"\s+\d{1,4}\s*$")
_SENT_END_RE = re.compile(r"[.!?][\"'’”)\]]*\s*$")


def _clean_heading(text: str) -> str:
    """Trim a run-in heading back to the heading itself.

    Journals set subsection titles inline with the paragraph that follows, so
    the extracted line reads "1.2. Inequality of Impacts. To investigate ...".
    Keep the title, drop the prose that follows it.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if _NUMBERED_RE.match(text):
        head = re.match(r"^\s*\d{1,2}(\.\d{1,2})*\s*[.)]\s*", text)
        rest = text[head.end():]
        cut = re.search(r"\.\s+\S", rest)
        if cut:
            rest = rest[:cut.start() + 1]
        text = (head.group(0) + rest).strip()
    return text[:90].strip()


@dataclass
class Line:
    page: int
    bbox: list          # [x0, y0, x1, y1] in PDF points
    text: str
    size: float
    bold: bool

    @property
    def x0(self): return self.bbox[0]
    @property
    def y0(self): return self.bbox[1]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y1(self): return self.bbox[3]


@dataclass
class Paragraph:
    id: int
    text: str
    page: int
    rects: list = field(default_factory=list)   # [{"page": n, "bbox": [...]}, ...]
    section: str = ""
    kind: str = "body"                          # body | heading | tail
    anchor: dict = field(default_factory=dict)

    @property
    def summarizable(self) -> bool:
        return self.kind == "body" and len(self.text) >= MIN_PARA_CHARS

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "page": self.page,
            "rects": self.rects,
            "section": self.section,
            "kind": self.kind,
            "anchor": self.anchor,
            "chars": len(self.text),
            "summarizable": self.summarizable,
        }


class NoTextLayer(Exception):
    """Raised for scanned/image-only PDFs, which we cannot summarize."""


# --- page scanning ----------------------------------------------------------

def _ratio(part, whole):
    return part / whole if whole else 0.0


def _inked(spans):
    """The spans of a line that actually put marks on the page.

    Elsevier justifies by stretching the spaces rather than the words, and
    stores each stretched space as its own span at a *larger* font size: in one
    Energy paper the words are set at 7.97 pt and every space between them at
    9.96 pt.  A line's size taken over all its spans is then the space's size,
    not the text's, so every multi-word line in the document -- 1657 of 2460 --
    missed the body size by 2 pt and was filtered out as furniture.  What
    survived was the lines with no spaces in them: table cells and lone
    citations, none long enough to summarize, so the reader came up with pages
    but not one margin note.

    Whitespace has no size worth reading, so measure the line by its glyphs.
    """
    return [s for s in spans if s["text"].strip()] or list(spans)


def _line_size(spans) -> float:
    """The font size a line reads as: the largest of its inked spans."""
    return max(round(s["size"], 1) for s in _inked(spans))


def _sampled_pages(doc):
    """Page numbers to measure font sizes on: the opening, then a spread.

    The opening pages decide the dominant size, and reading only those is what
    keeps a long reference list from outvoting the body it belongs to.  But they
    cannot be the whole sample: Nature sets its Methods at 7.5 pt against a
    9.3 pt body and puts it on page 9, so a document-opening sample never sees
    that size and the entire Methods section is filtered away as furniture.
    """
    n = doc.page_count
    lead = list(range(min(LEAD_PAGES, n)))
    rest = list(range(len(lead), n))
    if len(rest) > SAMPLE_PAGES:
        step = len(rest) / SAMPLE_PAGES
        rest = [rest[int(i * step)] for i in range(SAMPLE_PAGES)]
    return lead, lead + rest


def _body_sizes(doc):
    """Every font size that carries body prose in this document.

    One size is usually right, but not always: a report here sets its narrative
    in 11 pt while carrying so many 9 pt evidence listings that 9 pt wins the
    document-wide count.  Filtering to 9 pt alone discarded every word of the
    prose while still yielding a plausible-looking set of paragraphs -- which is
    why the tests now check extraction coverage, not just paragraph counts.

    A second size qualifies on three counts: it holds a real share of the text,
    it sits close to the dominant size, and -- the one that does the real work --
    its lines are about as long as the dominant size's.  That last test is what
    separates a second column of prose from a big multi-page table, whose cells
    are set at a similar size but wrap after a couple of words.
    """
    lead_pages, sample = _sampled_pages(doc)
    lead_pages = set(lead_pages)
    chars, lead, lengths = Counter(), Counter(), {}
    for number in sample:
        page = doc[number]
        for block in page.get_text("dict")["blocks"]:
            if block["type"]:
                continue
            for line in block["lines"]:
                if tuple(round(v, 2) for v in line["dir"]) != (1.0, 0.0):
                    continue
                spans = line["spans"]
                text = "".join(s["text"] for s in spans).strip()
                if not text or not spans:
                    continue
                size = _line_size(spans)
                lengths.setdefault(size, []).append(len(text))
                # Weight by span rather than by line so a line mixing sizes is
                # attributed to each.
                for span in spans:
                    length = len(span["text"].strip())
                    chars[round(span["size"], 1)] += length
                    if number in lead_pages:
                        lead[round(span["size"], 1)] += length
    if not chars:
        raise NoTextLayer("no text spans found")

    dominant = (lead or chars).most_common(1)[0][0]
    if dominant not in lengths:
        lengths[dominant] = [1]
    reference = statistics.median(lengths[dominant])
    sizes = {dominant}
    for size, count in chars.items():
        if size == dominant:
            continue
        # Measured against the opening pages *and* against the whole sample,
        # because a second body size need only be substantial somewhere.  The
        # Wildfire report's 11 pt narrative is 40% of the opening but 19% of the
        # document, and Nature's 7.5 pt Methods is 0% of the opening and 82% of
        # the document; one scope alone throws away one of the two.
        share = max(_ratio(lead[size], lead[dominant]),
                    _ratio(count, chars[dominant]))
        if (share >= BODY_SHARE
                and abs(size - dominant) <= BODY_SPAN * dominant
                and size in lengths
                and statistics.median(lengths[size]) >= BODY_LINE_RATIO * reference):
            sizes.add(size)
    return sizes


def _raw_lines(page):
    """Every horizontal text line on a page, with geometry. No filtering yet."""
    data = page.get_text("dict")
    images = [pymupdf.Rect(b["bbox"]) for b in data["blocks"] if b["type"] == 1]
    out = []
    for block in data["blocks"]:
        if block["type"]:
            continue
        for line in block["lines"]:
            # Rotated text is running headers, watermarks and axis labels --
            # never body copy.  ("Downloaded from ..." sidebars are caught here.)
            if tuple(round(v, 2) for v in line["dir"]) != (1.0, 0.0):
                continue
            text = "".join(s["text"] for s in line["spans"])
            if not text.strip():
                continue
            rect = pymupdf.Rect(line["bbox"])
            if rect.get_area() > 0 and any(
                    (rect & img).get_area() > IMG_OVERLAP * rect.get_area()
                    for img in images):
                continue
            spans = _inked(line["spans"])
            out.append(Line(
                page=page.number,
                bbox=list(line["bbox"]),
                text=text,
                size=_line_size(spans),
                bold=any(s["flags"] & 16 or "bold" in s["font"].lower()
                         for s in spans),
            ))
    out.sort(key=lambda l: (round(l.y0, 1), l.x0))
    return out


def _strip_line_numbers(lines) -> None:
    """Line-numbered manuscripts carry a counter at the end of every line.

    Detected per page: if most lines end in a small number, that suffix is
    editorial furniture, not prose, so drop it in place.
    """
    by_page = {}
    for line in lines:
        by_page.setdefault(line.page, []).append(line)
    for page_lines in by_page.values():
        if len(page_lines) < 5:
            continue
        hits = [l for l in page_lines if _LINENUM_RE.search(l.text)]
        if len(hits) / len(page_lines) < 0.6:
            continue
        for line in hits:
            line.text = _LINENUM_RE.sub("", line.text)


def _attach_drop_caps(lines, caps) -> None:
    """Fold an oversized opening initial back into the paragraph it belongs to.

    A journal opens a section with a drop cap -- Nature Energy sets a 41.6 pt
    'T' beside 9.3 pt text -- and indents the first few lines to clear it.  The
    initial is not body-sized, so it is filtered out and the paragraph opens
    "o cost-effectively decarbonize".  The subtler damage is geometric: the
    lines set beside it look indented, and a first-line indent is exactly how a
    new paragraph announces itself, so the opening also shatters into fragments.

    Give the letter back to the first line beside it, and record those lines as
    starting at the initial's own left edge so they group as one paragraph.
    """
    by_page = {}
    for line in lines:
        by_page.setdefault(line.page, []).append(line)
    for cap in caps:
        beside = [l for l in by_page.get(cap.page, [])
                  if l.y0 < cap.y1 and l.y1 > cap.y0
                  and cap.x1 - 2 <= l.x0 <= cap.x1 + 12]
        if len(beside) < 2:
            continue            # a real drop cap is at least two lines deep
        beside.sort(key=lambda l: l.y0)
        beside[0].text = cap.text.strip() + beside[0].text
        for line in beside:
            line.bbox[0] = cap.x0


def _repeating_furniture(lines, n_pages) -> set:
    """Text that recurs near the top or bottom edge across pages: running heads."""
    if n_pages < 3:
        return set()
    seen = {}
    for line in lines:
        seen.setdefault(line.page, []).append(line)
    counter = Counter()
    for page_lines in seen.values():
        if not page_lines:
            continue
        top = min(l.y0 for l in page_lines)
        bottom = max(l.y1 for l in page_lines)
        span = max(bottom - top, 1)
        for line in page_lines:
            near_edge = (line.y0 - top) / span < 0.06 or (bottom - line.y1) / span < 0.06
            if near_edge:
                counter[re.sub(r"\d+", "#", line.text.strip())] += 1
    threshold = max(3, int(0.3 * n_pages))
    return {t for t, c in counter.items() if c >= threshold and len(t) < 200}


# --- geometry ---------------------------------------------------------------

def _gutter_scan(lines, page_width):
    """Find the cleanest vertical cut through a region: (x, lines it crosses).

    Sweeps candidate x positions across the middle of the page and counts how
    many lines each one would cut through, preferring the fewest crossings and
    then the widest clear gap.  Counting crossings rather than looking for a
    fully empty band means one full-width line (a spanning heading, a wide
    equation) cannot hide a genuine two-column layout -- and real gutters are
    narrow: the papers here run 10-15 pt on a 585 pt page, under 3% of the width.

    How many crossings are tolerable is the caller's decision, not this
    function's: `_split_columns` wants almost none, while `_split_at_spanners`
    wants the crossing lines themselves.
    """
    best = None
    x = 0.25 * page_width
    while x <= 0.75 * page_width:
        crossers = [l for l in lines if l.x0 < x - 1 and l.x1 > x + 1]
        left = [l for l in lines if l.x1 <= x]
        right = [l for l in lines if l.x0 >= x]
        if len(left) >= 3 and len(right) >= 3:
            gutter = min(l.x0 for l in right) - max(l.x1 for l in left)
            if gutter >= COL_GAP_MIN_PT:
                score = (len(crossers), -gutter)
                if best is None or score < best[0]:
                    best = (score, x, crossers)
        x += 2.0
    return (best[1], best[2]) if best else None


def _split_columns(lines, page_width):
    """Split a region's lines into columns, recursing so three columns work."""
    if len(lines) < 6:
        return [lines]
    found = _gutter_scan(lines, page_width)
    if found is None:
        return [lines]
    split, crossers = found
    if len(crossers) > max(1, int(0.05 * len(lines))):
        return [lines]
    left = [l for l in lines if (l.x0 + l.x1) / 2 < split]
    right = [l for l in lines if (l.x0 + l.x1) / 2 >= split]
    if len(left) < 3 or len(right) < 3:
        return [lines]
    return _split_columns(left, page_width) + _split_columns(right, page_width)


def _split_at_spanners(lines, page_width):
    """Cut a region at the lines that run across its full width.

    `_split_bands` only cuts where there is whitespace, and a journal will not
    always leave enough: Nature Energy sets a full-width abstract 19.6 pt above
    a two-column body, which is less than two body line heights.  The two
    regions then arrive here as one, the abstract's lines cross the gutter, and
    the column sweep gives up -- so the two columns interleave line by line and
    every "paragraph" is one line of column 1 welded to one line of column 2.

    A line that crosses the gutter is a horizontal separator in XY-cut terms:
    nothing above it can share a column with anything below it.  So cut the
    region into runs of consecutive lines that either all cross or all do not.

    Only done when it actually reveals columns.  Single-column text is all
    full-width, so the crossing lines there are not separators but the body
    itself, and slicing at them would shred it.
    """
    found = _gutter_scan(lines, page_width)
    if found is None:
        return [lines]
    crossers = found[1]
    if not crossers or len(crossers) > SPANNER_MAX_FRAC * len(lines):
        return [lines]
    spanning = {id(l) for l in crossers}
    rest = [l for l in lines if id(l) not in spanning]
    if len(_split_columns(rest, page_width)) < 2:
        return [lines]

    runs, current, was = [], [], None
    for line in sorted(lines, key=lambda l: (round(l.y0, 1), l.x0)):
        crosses = id(line) in spanning
        if was is not None and crosses != was:
            runs.append(current)
            current = []
        current.append(line)
        was = crosses
    runs.append(current)
    return runs if len(runs) > 1 else [lines]


def _split_bands(lines):
    """Split lines into horizontal bands separated by full-width whitespace.

    A journal front page stacks regions: title across the full width, then the
    abstract beside a Significance box, then the two-column body.  Splitting the
    page into columns in one go interleaves those regions, which is how a
    sidebar heading ended up labelling the introduction.
    """
    if len(lines) < 6:
        return [lines]
    ordered = sorted(lines, key=lambda l: l.y0)
    heights = [l.y1 - l.y0 for l in ordered if l.y1 > l.y0]
    threshold = max(1.8 * statistics.median(heights), 12.0) if heights else 12.0
    bands, current, bottom = [], [ordered[0]], ordered[0].y1
    for line in ordered[1:]:
        if line.y0 - bottom > threshold:
            bands.append(current)
            current, bottom = [line], line.y1
        else:
            current.append(line)
            bottom = max(bottom, line.y1)
    bands.append(current)
    return bands


def _segment(lines, page_width, depth=0):
    """Recursive XY-cut: horizontal bands, then columns inside each band.

    Returns regions already in reading order.  Bands are cut before columns so a
    full-width element (a spanning title, a wide figure) ends a region instead of
    silently bridging two columns.
    """
    if len(lines) < 6 or depth > 5:
        return [lines]
    bands = _split_bands(lines)
    if len(bands) > 1:
        out = []
        for band in bands:
            out.extend(_segment(band, page_width, depth + 1))
        return out
    # Cut at any full-width line still inside this band.  A line that crosses
    # the gutter belongs to neither column, and the column split would file it
    # under whichever one its midpoint fell in -- which is how a full-width
    # table caption ended up welded to the end of a left-column paragraph.
    stripes = _split_at_spanners(lines, page_width)
    if len(stripes) > 1:
        out = []
        for stripe in stripes:
            out.extend(_segment(stripe, page_width, depth + 1))
        return out
    # Now split into columns and stop.  Inside a single column reading order is
    # just top-to-bottom, and cutting further would leave regions too small to
    # measure indent and justification reliably.
    return _split_columns(lines, page_width)


def _percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _is_justified(lines) -> bool:
    """True when most lines reach the same right margin."""
    if len(lines) < 4:
        return False
    right = _percentile([l.x1 for l in lines], 0.9)
    flush = sum(1 for l in lines if abs(l.x1 - right) <= 2.0)
    return flush / len(lines) >= JUSTIFIED_FRAC


def _median_leading(lines) -> float:
    """Median baseline-to-baseline distance; robust to double spacing."""
    steps = [b.y0 - a.y0 for a, b in zip(lines, lines[1:]) if b.y0 - a.y0 > 0.5]
    if not steps:
        return max((l.y1 - l.y0) for l in lines) if lines else 12.0
    return statistics.median(steps)


def _group_paragraphs(column):
    """Group one column's lines into paragraphs using indent / short / leading."""
    if not column:
        return []
    column = sorted(column, key=lambda l: (round(l.y0, 1), l.x0))
    leading = _median_leading(column)
    left = _percentile([l.x0 for l in column], 0.15)
    right = _percentile([l.x1 for l in column], 0.9)
    width = max(right - left, 1.0)
    justified = _is_justified(column)

    groups, current = [], []
    for line in column:
        brk = False
        if current:
            prev = current[-1]
            if line.y0 - prev.y0 > LEADING_BREAK * leading + 1:
                brk = True                                   # extra vertical space
            if line.x0 - left > INDENT_MIN:
                brk = True                                   # first-line indent
            if justified and prev.x1 < right - SHORT_FRAC * width:
                brk = True                                   # previous line ran short
            elif not justified and _SENT_END_RE.search(prev.text) and \
                    prev.x1 < right - 0.25 * width:
                # Ragged-right text has no reliable right margin, so only treat a
                # very short line as terminal, and only if it ends a sentence.
                brk = True
        if brk and current:
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    return groups


def _join(lines) -> str:
    """Join a paragraph's lines, repairing words hyphenated across line ends."""
    out = ""
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if out.endswith(("-", "­")) and not out.endswith("--") and text[:1].islower():
            out = out[:-1] + text                       # word split across lines
        elif out.endswith("–") and text[:1].islower():
            out = out + text                            # compound broken at an en dash
        elif out:
            out = out + " " + text
        else:
            out = text
    return re.sub(r"\s+", " ", out).strip()


def _merge_rects(lines):
    """One rectangle per run of lines that sit together, as a highlight band."""
    rects = []
    for line in lines:
        box = [line.x0, line.y0, line.x1, line.y1]
        if rects and rects[-1]["page"] == line.page:
            x0, y0, x1, y1 = rects[-1]["bbox"]
            # Extend a band only downwards, and only while the lines still
            # overlap horizontally.  A paragraph stitched across a column break
            # jumps back to the top of the next column: the y test alone reads
            # that jump as "not far below", so one rectangle ended up drawn
            # across both columns and everything between them.
            below = -0.5 * (line.y1 - line.y0) <= line.y0 - y1 < 6
            overlaps = min(x1, line.x1) > max(x0, line.x0)
            if below and overlaps:
                rects[-1]["bbox"] = [min(x0, box[0]), min(y0, box[1]),
                                     max(x1, box[2]), max(y1, box[3])]
                continue
        rects.append({"page": line.page, "bbox": box})
    return rects


# --- top level --------------------------------------------------------------

def extract(path: str) -> dict:
    """Extract positioned paragraphs from a PDF.

    Raises NoTextLayer when the document has (almost) no extractable text, which
    means a scan; the caller should tell the user rather than show an empty reader.
    """
    doc = pymupdf.open(path)
    try:
        sizes = _body_sizes(doc)
    except NoTextLayer:
        raise NoTextLayer("This PDF has no selectable text (likely scanned).")
    body, largest = min(sizes), max(sizes)

    pages = [{"number": p.number, "width": p.rect.width, "height": p.rect.height}
             for p in doc]

    all_lines, heading_lines, drop_caps = [], [], []
    for page in doc:
        for line in _raw_lines(page):
            if any(abs(line.size - s) <= SIZE_TOL for s in sizes):
                all_lines.append(line)
            elif line.size >= DROP_CAP_RATIO * largest and len(line.text.strip()) <= 2:
                drop_caps.append(line)
            elif line.size > largest + SIZE_TOL and len(line.text.strip()) < 120 \
                    and _HEADING_RE.match(line.text):
                # Requiring the heading pattern keeps title blocks and pull
                # quotes out; a wrong section label is worse than none, since
                # it becomes misleading context in the summarization prompt.
                heading_lines.append(line)

    if len(all_lines) < MIN_BODY_LINES:
        raise NoTextLayer("This PDF has no selectable text (likely scanned).")

    _strip_line_numbers(all_lines)
    furniture = _repeating_furniture(all_lines, len(pages))
    all_lines = [l for l in all_lines
                 if re.sub(r"\d+", "#", l.text.strip()) not in furniture
                 and l.text.strip()]
    _attach_drop_caps(all_lines, drop_caps)

    # Run-in headings ("1.2. Inequality of Impacts...") are body-sized but bold,
    # so they are picked up here rather than by the size test above.  Those stay
    # in the body stream, because the paragraph they introduce continues on the
    # same line.  A line that is *only* a section name is different: it is set
    # apart from the prose, and leaving it in would splice a stray word into
    # whichever paragraph happens to share its vertical position.
    standalone = []
    for line in all_lines:
        if line.bold and len(line.text.strip()) < 120 and _HEADING_RE.match(line.text):
            heading_lines.append(line)
            if _SECTION_RE.match(line.text):
                standalone.append(id(line))
    if standalone:
        drop = set(standalone)
        all_lines = [l for l in all_lines if id(l) not in drop]
    heading_lines.sort(key=lambda l: (l.page, l.y0))

    by_page, heads_by_page = {}, {}
    for line in all_lines:
        by_page.setdefault(line.page, []).append(line)
    for line in heading_lines:
        heads_by_page.setdefault(line.page, []).append(line)

    # Paragraphs and headings both get a reading-order key of
    # (page, column index, y).  Sorting headings by (page, y) alone would be
    # wrong on a two-column page: a heading partway down the right column would
    # capture every left-column paragraph below its y.
    groups, heads = [], []
    for page in pages:
        page_lines = by_page.get(page["number"], [])
        if not page_lines:
            continue
        columns = _segment(page_lines, page["width"])
        bounds = [(min(l.x0 for l in c), max(l.x1 for l in c)) for c in columns]
        for ci, column in enumerate(columns):
            measure = bounds[ci][1] - bounds[ci][0]
            for group in _group_paragraphs(column):
                groups.append(((page["number"], ci, group[0].y0), group, measure))
        for line in heads_by_page.get(page["number"], []):
            mid = (line.x0 + line.x1) / 2
            ci = next((i for i, (lo, hi) in enumerate(bounds) if lo - 2 <= mid <= hi + 2),
                      min(range(len(bounds)),
                          key=lambda i: abs(mid - sum(bounds[i]) / 2)) if bounds else 0)
            if not bounds:
                continue
            lo, hi = bounds[ci]
            width = max(hi - lo, 1.0)
            # A real section heading is set flush with its column, or centred
            # over it.  One floating far to the right belongs to a sidebar whose
            # body text was filtered out as non-body -- adopting it would label
            # the whole rest of the document with a pull-quote's title.
            flush = line.x0 <= lo + 0.35 * width
            centred = abs(mid - (lo + hi) / 2) <= 0.12 * width
            if not (flush or centred):
                continue
            heads.append(((page["number"], ci, line.y0), line))

    groups.sort(key=lambda g: g[0])
    heads.sort(key=lambda h: h[0])

    # A paragraph that fills its column to the right margin continues into the
    # next column or page; stitch those halves back together.
    stitched = []
    for key, group, measure in groups:
        if stitched:
            prev_key, prev_col, prev_measure = stitched[-1]
            col_right = max(l.x1 for l in prev_col)
            prev_full = prev_col[-1].x1 >= col_right - 1.0
            starts_flush = True
            if len(group) > 1:
                starts_flush = group[0].x0 <= min(l.x0 for l in group[1:]) + INDENT_MIN
            # Text set to a different measure is a different element, not the
            # rest of this paragraph.  Without this, a paragraph that happened
            # to end its column full and unpunctuated adopted whatever came next
            # in reading order: a two-word table cell in one paper, and in
            # another a full-width table caption spliced mid-sentence.
            #
            # The cost is that a sentence running across a page break whose next
            # page opens with a table is delivered as two paragraphs rather than
            # one.  Two adjacent cards is a far smaller error than a caption
            # welded into the middle of a sentence.
            same_measure = (STITCH_MEASURE * prev_measure <= measure
                            <= prev_measure / STITCH_MEASURE)
            # Never stitch across an intervening heading: a new section always
            # starts a new paragraph, however full the previous column looked.
            divided = any(prev_key < hk <= key for hk, _ in heads)
            if prev_full and starts_flush and same_measure and not divided \
                    and not _SENT_END_RE.search(prev_col[-1].text):
                stitched[-1] = (prev_key, prev_col + group, prev_measure)
                continue
        stitched.append((key, group, measure))

    pending = list(heads)
    paragraphs, section, stop = [], "", False
    for key, group, _ in stitched:
        text = _join(group)
        if not text:
            continue
        first = group[0]
        while pending and pending[0][0] <= key:
            head_text = _clean_heading(pending.pop(0)[1].text)
            if head_text:
                section = head_text
                if _STOP_RE.match(head_text):
                    stop = True
        rects = _merge_rects(group)
        paragraphs.append(Paragraph(
            id=len(paragraphs),
            text=text,
            page=first.page,
            rects=rects,
            section=section,
            kind="tail" if stop else "body",
            anchor={"page": first.page, "x": first.x0, "y": first.y0},
        ))

    return {
        "pages": pages,
        "body_size": body,
        "paragraphs": [p.as_dict() for p in paragraphs],
        "full_text": "\n\n".join(p.text for p in paragraphs),
    }
