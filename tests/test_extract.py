#!/usr/bin/env python3
"""Extraction tests against real PDFs.

Deliberately run against papers on the Desktop rather than synthetic fixtures:
every one of these was chosen because it broke an earlier version of the
extractor.  Run with:

    ~/.venvs/gloss/bin/python tests/test_extract.py

Standalone on purpose -- no pytest dependency, so it runs in the app's own venv.
"""

import glob
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extract  # noqa: E402

DESKTOP = os.path.expanduser("~/Desktop")


def find(pattern):
    hits = glob.glob(os.path.join(DESKTOP, pattern))
    return hits[0] if hits else None


# (glob, what it exercises, min paras, max paras, min median chars,
#  must_contain, must_not_contain)
#
# `must_contain` holds passages that have to survive extraction. They exist
# because the Wildfire report sets its narrative in 11 pt and its evidence
# listings in 9 pt: picking one document-wide body size silently dropped every
# word of the narrative, and every count-based check still passed.
#
# `must_not_contain` is the opposite failure, and it is the one that reaches the
# model as if it were prose: two elements welded together at a seam. Nothing
# about the paragraph's length or position gives it away, so it has to be named.
CASES = [
    ("Wildfire Smoke Trends*.pdf", "two body font sizes",        60, 220, 200,
     ["Wildfire smoke is the mix of gases and tiny particles"], []),
    # Nature Energy breaks three assumptions at once: a full-width abstract sits
    # 19.6 pt above the two-column body (too close for a whitespace band, so the
    # columns interleaved line by line), a 41.6 pt drop cap opens the article
    # (eating the "T" and shattering the opening into one-line fragments), and
    # Methods is set at 7.5 pt on page 9 (invisible to a sample of the opening
    # pages, so the whole section was filtered away as furniture).
    # The low median is honest: two full-page tables get shredded into cells,
    # none of which is long enough to summarize. Over-splitting of the prose is
    # caught by the share-of-characters check below, which is the better guard.
    ("s41560-021-00796-8.pdf",     "drop cap, abstract over columns, late "
                                   "Methods font",               60, 200, 120,
     ["To cost-effectively decarbonize the electric power sector, some "
      "combination of the following technological solutions must be employed",
      "For this research we used the GenX model"],
     # A paragraph that ends its column mid-sentence must not adopt whatever
     # comes next in reading order -- here, the caption of the table that opens
     # the following page.
     ["low-carbon generation, Table 2"]),
    ("annurev-publhealth*.pdf",    "rotated watermark + tables", 30, 150, 300,
     ["In the context of climate change"], []),
    ("guikema*.pdf",               "two column + figure labels", 20, 100, 300,
     ["Taken together, the outage results suggest"], []),
    ("christy 2026.pdf",           "two column",                 40, 200, 250, [],
     # Same seam, the other shape: a full-width table caption spliced into the
     # middle of a sentence in a left-hand column.
     ["Weather Bureau (24-hr) Table 1"]),
    ("Ch1_Hereid_Manuscript*.pdf", "line-numbered manuscript",   50, 250, 250, [],
     []),
]

WATERMARK_MARKERS = ("Downloaded from", "annualreviews.org", "EVANS LIBRARY")

failures = []
skipped = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")
    return condition


for pattern, description, lo, hi, min_median, must_contain, must_not in CASES:
    path = find(pattern)
    if not path:
        skipped.append(f"{pattern} (not on Desktop)")
        continue

    label = os.path.basename(path)[:38]
    data = extract.extract(path)
    body = [p for p in data["paragraphs"] if p["kind"] == "body"]
    lengths = [p["chars"] for p in body]
    median = statistics.median(lengths) if lengths else 0

    check(label, lo <= len(body) <= hi,
          f"{len(body)} body paragraphs, expected {lo}-{hi} ({description})")

    # The failure mode this guards against is the extractor shattering justified
    # text into one paragraph per line, which is what happened before column
    # detection worked.
    check(label, median >= min_median,
          f"median paragraph {median:.0f} chars, expected >= {min_median}")

    # Over-splitting, measured by share of *text* rather than share of
    # paragraphs. Counting paragraphs punished documents containing a table,
    # whose cells are legitimately short, land under the summarizable threshold,
    # and never become cards. What actually matters is whether running prose got
    # shattered -- and that shows up as a large fraction of the document's
    # characters sitting in tiny paragraphs.
    total_chars = sum(p["chars"] for p in body) or 1
    short_chars = sum(p["chars"] for p in body if p["chars"] < 80)
    check(label, short_chars <= 0.10 * total_chars,
          f"{short_chars / total_chars:.0%} of text sits in sub-80-char "
          f"paragraphs -- prose is being over-split")

    # Rotated watermarks must never reach the model as if they were prose.
    dirty = [p for p in body if any(m in p["text"] for m in WATERMARK_MARKERS)]
    check(label, not dirty, f"{len(dirty)} paragraphs contain watermark text")

    # Hyphens broken across line ends must be rejoined.
    broken = [p for p in body if "- " in p["text"] and
              any(w.endswith("-") for w in p["text"].split())]
    check(label, len(broken) <= 0.1 * max(len(body), 1),
          f"{len(broken)} paragraphs still contain line-break hyphens")

    # Every rectangle has to sit on a real page and inside its bounds, or the
    # margin overlay will point at nothing.
    sizes = {p["number"]: (p["width"], p["height"]) for p in data["pages"]}
    bad = 0
    for para in data["paragraphs"]:
        for rect in para["rects"]:
            if rect["page"] not in sizes:
                bad += 1
                continue
            width, height = sizes[rect["page"]]
            x0, y0, x1, y1 = rect["bbox"]
            if x0 < -2 or y0 < -2 or x1 > width + 2 or y1 > height + 2 or x1 <= x0:
                bad += 1
    check(label, bad == 0, f"{bad} rectangles fall outside their page")

    check(label, all(p["rects"] for p in data["paragraphs"]),
          "some paragraphs have no rectangles to anchor a card to")

    # A highlight band has to hug the text it highlights. Measured as rectangle
    # area per character, against this document's own typical paragraph so the
    # test does not care about font size or page size.
    #
    # A paragraph stitched across a column break used to fail this badly: its
    # single rectangle ran from the bottom of column 1 to the top of column 2,
    # so it enclosed both columns and everything between them -- 4.9x the
    # document's normal density, against 2.0x for the loosest honest paragraph
    # in this whole set. On screen that highlights half a page at once.
    density = []
    for para in data["paragraphs"]:
        if para["chars"] < 200:
            continue        # short paragraphs are dominated by their last line
        area = sum((r["bbox"][2] - r["bbox"][0]) * (r["bbox"][3] - r["bbox"][1])
                   for r in para["rects"])
        density.append((area / para["chars"], para["id"]))
    if density:
        typical = statistics.median(d for d, _ in density)
        loosest, worst_id = max(density)
        check(label, loosest <= 3.0 * typical,
              f"paragraph {worst_id}'s rectangles are {loosest / typical:.1f}x "
              f"looser than this document's typical paragraph -- a band is "
              f"covering ground its text does not")

    # Coverage. The count and median checks above all passed on a document whose
    # narrative prose was being silently discarded -- a single document-wide font
    # size had picked the 9 pt evidence listings over the 11 pt body, and what
    # survived merely *looked* like a healthy set of paragraphs. Comparing the
    # extracted characters against the PDF's own text is what catches that.
    import pymupdf
    with pymupdf.open(path) as pdf:
        raw = sum(len(pg.get_text().strip()) for pg in pdf)
    got = sum(p["chars"] for p in data["paragraphs"])
    coverage = got / max(raw, 1)
    check(label, coverage >= 0.45,
          f"only {coverage:.0%} of the document's text was extracted")

    # Sentences the document definitely contains. Coverage alone is too blunt to
    # catch a document losing one of two body font sizes, because what survives
    # still looks like a reasonable amount of text.
    # Matched inside a single paragraph, never across the joined text: a phrase
    # that spans a paragraph boundary would pass the first check without the
    # passage surviving intact, and fail the second on a seam that isn't there.
    for phrase in must_contain:
        check(label, any(phrase in p["text"] for p in data["paragraphs"]),
              f"no single paragraph holds a known passage: {phrase!r}")

    # Text that must not appear, because it only can if two separate elements
    # were joined at a seam the layout does not have.
    for phrase in must_not:
        guilty = [p["id"] for p in data["paragraphs"] if phrase in p["text"]]
        check(label, not guilty,
              f"paragraph {guilty[0] if guilty else ''} splices two elements "
              f"together: {phrase!r}")

    print(f"  {label:40s} {len(body):4d} paras  median {median:5.0f} ch  "
          f"[{description}]")

# A scanned PDF must be reported, not silently rendered as an empty reader.
scanned = find("Siff_LInknET.pdf")
if scanned:
    try:
        extract.extract(scanned)
        failures.append("Siff_LInknET.pdf: expected NoTextLayer, got a result")
    except extract.NoTextLayer:
        print(f"  {'Siff_LInknET.pdf':40s} correctly reported as having no text layer")
else:
    skipped.append("Siff_LInknET.pdf (not on Desktop)")

print()
for note in skipped:
    print(f"  SKIPPED: {note}")
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("\nAll extraction tests passed.")
