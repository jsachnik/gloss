/* Gloss -- page rendering, margin layout, and progressive summary fill.
 *
 * Page geometry comes from the server in PDF points; multiplying by the zoom
 * factor converts to CSS pixels, which is how a summary card finds the y of the
 * paragraph it belongs to.  Page boxes are given an explicit height up front so
 * the layout is stable before any image has loaded.
 */

const GAP = 28;          // px between the last ink on a page and its cards
const CARD_GAP = 8;      // minimum px between stacked cards
const MARGIN_W = 340;
const MAX_RECENTS = 5;   // matches app.py, which is what persists the list

const state = {
  sha: null,
  doc: null,
  zoom: parseFloat(localStorage.getItem("zoom")) || 1.25,
  summaries: {},
  failures: new Set(),
  active: null,
  cards: new Map(),      // paragraph id -> card element
  widths: { pages: 0, doc: 0 },   // px, with and without the margin column
  columns: null,         // where the summary column starts, per page
  bands: new Map(),      // paragraph id -> [band elements]
  rects: new Map(),      // page number -> [{id, bbox, summarizable}]
  text: new Map(),       // page number -> [spans], as fetched
  watcher: null,         // fills a page's text layer as it nears the viewport
  showMargin: true,
  polling: null,
};

const $ = (id) => document.getElementById(id);
const paragraph = (id) => state.doc?.paragraphs.find((p) => p.id === id);
const el = (tag, cls) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  return node;
};

/* --- loading -------------------------------------------------------------- */

async function post(url, body, isForm) {
  const options = { method: "POST" };
  if (isForm) {
    options.body = body;
  } else {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = false;
}

async function openResult(result) {
  if (!result || result.cancelled) return;
  $("error").hidden = true;
  state.sha = result.sha;
  state.summaries = {};
  state.failures = new Set();
  state.active = null;
  const doc = await fetch(`/api/doc/${result.sha}`).then((r) => r.json());
  state.doc = doc;
  $("title").textContent = doc.title;
  $("start").hidden = true;
  $("reader").hidden = false;
  $("ask").disabled = false;
  document.body.classList.add("reading");
  rememberRecent(result.path, result.title || doc.title);
  // Clear the thread: answers about the previous paper would be misleading
  // sitting under a new one's title.
  const thread = $("ask-thread");
  thread.innerHTML = "";
  const hint = el("p", "ask-empty");
  hint.textContent = "Questions are answered from this document, with links " +
                     "back to the passages the answer came from.";
  thread.appendChild(hint);
  render();
  poll();
}

/* Back to the screen the app starts on. Everything tied to the open document
 * goes with it -- the poll timer above all, which would otherwise keep asking
 * for summaries of a paper nobody is reading. */
function closeDoc() {
  clearInterval(state.polling);
  state.polling = null;
  state.sha = null;
  state.doc = null;
  state.summaries = {};
  state.failures = new Set();
  state.active = null;
  state.cards.clear();
  state.bands.clear();
  state.rects.clear();
  state.text.clear();
  state.watcher?.disconnect();
  state.watcher = null;
  $("pages").innerHTML = "";
  $("margin").innerHTML = "";
  $("leaders").innerHTML = "";
  $("reader").hidden = true;
  $("start").hidden = false;
  $("error").hidden = true;
  document.body.classList.remove("reading");
  $("title").textContent = "";
  $("status").textContent = "";
  $("ask").disabled = true;
  openAsk(false);
  window.scrollTo({ top: 0 });
}

/* The Recent list is rendered when the page loads, so a paper opened since then
 * is missing from it -- and after closing that paper, its absence reads as
 * "that didn't count". Put it at the top, as the server already has on disk. */
function rememberRecent(path, title) {
  if (!path) return;              // a dropped file has no path to reopen it by
  const list = $("recent-list");
  list.querySelectorAll(".recent").forEach((button) => {
    if (button.dataset.path === path) button.parentElement.remove();
  });
  const button = el("button", "recent");
  button.dataset.path = path;
  button.textContent = title;
  const item = el("li");
  item.appendChild(button);
  list.prepend(item);
  while (list.children.length > MAX_RECENTS) list.lastElementChild.remove();
  $("recents").hidden = false;
}

const guard = (fn) => async (...args) => {
  try { await fn(...args); } catch (err) { showError(err.message); }
};

const choose = guard(async () => openResult(await post("/api/pick")));
const openPath = guard(async (path) => openResult(await post("/api/open", { path })));

/* --- rendering ------------------------------------------------------------ */

function render() {
  const { doc, zoom } = state;
  const pages = $("pages");
  const margin = $("margin");
  pages.innerHTML = "";
  margin.innerHTML = "";
  $("leaders").innerHTML = "";
  state.cards.clear();
  state.bands.clear();

  // Every paragraph rectangle, indexed by the page it sits on: the pointer is
  // matched against these instead of against a div per rectangle, because the
  // text layer now covers the page and would swallow the hover.
  state.rects.clear();
  doc.paragraphs.forEach((para) => {
    para.rects.forEach((rect) => {
      if (!state.rects.has(rect.page)) state.rects.set(rect.page, []);
      state.rects.get(rect.page).push({
        id: para.id, bbox: rect.bbox, summarizable: para.summarizable,
      });
    });
  });

  const widest = Math.max(...doc.pages.map((p) => p.width)) * zoom;
  const columns = cardColumns(widest);
  state.columns = columns;
  state.widths = { pages: widest, doc: Math.max(widest, columns.right + MARGIN_W) };
  const docEl = $("doc");
  docEl.style.width = `${state.widths.doc}px`;
  pages.style.width = `${widest}px`;
  margin.style.left = `${columns.base}px`;
  margin.style.width = `${columns.right - columns.base + MARGIN_W}px`;

  const pageEls = [];
  doc.pages.forEach((page) => {
    const box = el("div", "page");
    box.style.width = `${page.width * zoom}px`;
    box.style.height = `${page.height * zoom}px`;
    box.style.marginLeft = `${(widest - page.width * zoom) / 2}px`;

    const img = el("img");
    img.src = `/api/page/${state.sha}/${page.number}.png`;
    img.loading = "lazy";
    img.alt = `Page ${page.number + 1}`;
    box.appendChild(img);

    const label = el("div", "page-number");
    label.textContent = page.number + 1;
    box.appendChild(label);

    // Filled once the page nears the viewport, as the image is loaded.
    const layer = el("div", "text-layer");
    layer.dataset.page = page.number;
    box.appendChild(layer);

    // Belt and braces: a window left in the background gets no observer
    // callbacks, and the page under the pointer is the one about to be read.
    box.addEventListener("mouseenter", () => loadText(layer));
    box.addEventListener("mousemove", (event) => {
      const id = paraAt(page.number, box, event);
      if (id !== null) activate(id, false);
    });
    box.addEventListener("click", (event) => {
      const id = paraAt(page.number, box, event);
      // A click that ends a drag is the end of a selection, not a request to
      // go and look at the card.
      if (id !== null) activate(id, selectedText() === "");
    });

    pages.appendChild(box);
    pageEls[page.number] = box;
  });

  // Hit targets and highlight bands live inside their page, so their
  // coordinates are simply the paragraph rectangle scaled by the zoom.
  doc.paragraphs.forEach((para) => {
    const bands = [];
    para.rects.forEach((rect) => {
      const host = pageEls[rect.page];
      if (!host) return;
      const [x0, y0, x1, y1] = rect.bbox;
      const style = `left:${x0 * zoom}px;top:${y0 * zoom}px;` +
                    `width:${(x1 - x0) * zoom}px;height:${(y1 - y0) * zoom}px`;

      const band = el("div", "band");
      band.style.cssText = style;
      host.appendChild(band);
      bands.push(band);
    });
    state.bands.set(para.id, bands);
  });

  doc.paragraphs.filter((p) => p.summarizable).forEach((para) => {
    const card = el("div", "card pending");
    card.style.width = `${MARGIN_W}px`;
    card.style.left = `${columns.at(para.anchor.page) - columns.base}px`;
    card.dataset.id = para.id;

    const section = el("span", "sec");
    section.textContent = para.section || "";
    const body = el("div", "body");
    body.textContent = "Summarizing this paragraph...";
    card.append(section, body);

    card.addEventListener("mouseenter", () => activate(para.id, false));
    card.addEventListener("click", () => activate(para.id, true));
    margin.appendChild(card);
    state.cards.set(para.id, card);
    fillCard(card, para.id);
  });

  // Zooming re-renders everything, so the active pair has to be marked again --
  // otherwise the leader line lands on a card that no longer looks selected.
  if (state.active !== null) {
    state.cards.get(state.active)?.classList.add("on");
    (state.bands.get(state.active) || []).forEach((b) => b.classList.add("on"));
  }

  watchPages();
  requestAnimationFrame(() => layoutCards(pageEls));
  $("zoom-level").textContent = `${Math.round(zoom * 100)}%`;
}

/* Where the summary column starts, measured per page.
 *
 * Anchored to the edge of the paper, a card sits most of an inch of white
 * beyond the last word it is describing -- and because every page is centred
 * in the width of the widest one, a single oversized page (a landscape table,
 * a wider figure page) shoves the cards of the whole document out to match it.
 * So each page gets its own column, starting just past its own ink, and the
 * cards settle on the middle one.  A page whose text reaches past that column
 * moves its own cards right, far enough that a card never covers text.
 */
function cardColumns(widest) {
  const { doc, zoom } = state;
  const perPage = [];
  doc.pages.forEach((page) => {
    const ink = (page.content_right || page.width) * zoom;
    perPage[page.number] = (widest - page.width * zoom) / 2 + ink + GAP;
  });
  const carded = [...new Set(doc.paragraphs.filter((p) => p.summarizable)
                                           .map((p) => p.anchor.page))]
    .map((number) => perPage[number])
    .filter(Number.isFinite)
    .sort((a, b) => a - b);
  const base = carded.length ? carded[Math.floor(carded.length / 2)] : widest + GAP;
  return {
    base,
    right: Math.max(base, ...perPage.filter(Number.isFinite)),
    at: (page) => Math.max(base, perPage[page] ?? base),
  };
}

/* Stack cards down the margin: each one wants to sit level with its paragraph,
 * and is pushed down only as far as the card above it requires. */
function layoutCards(pageEls) {
  const { doc, zoom, columns } = state;
  if (!columns) return;
  // Measured once: reading a page box's geometry inside the loop below would
  // make the browser re-do layout for every card.
  const spans = pageEls.map((box) => [box.offsetTop, box.offsetTop + box.offsetHeight]);
  let cursor = 0;
  doc.paragraphs.filter((p) => p.summarizable).forEach((para) => {
    const card = state.cards.get(para.id);
    if (!card) return;
    const host = pageEls[para.anchor.page];
    if (!host) return;
    const wanted = host.offsetTop + para.anchor.y * zoom;
    const top = Math.max(wanted, cursor);
    card.style.top = `${top}px`;
    card.dataset.wanted = wanted;
    const bottom = top + card.offsetHeight;
    // A card pushed down the margin can come to rest beside a page wider than
    // its own -- a landscape table -- so it takes that page's column as well.
    let x = columns.at(para.anchor.page);
    spans.forEach(([pageTop, pageBottom], number) => {
      if (pageTop < bottom && top < pageBottom) x = Math.max(x, columns.at(number));
    });
    card.style.left = `${x - columns.base}px`;
    cursor = bottom + CARD_GAP;
  });
  $("doc").style.minHeight = `${Math.max($("pages").offsetHeight, cursor)}px`;
  if (state.active !== null) drawLeader(state.active);
}

/* --- the text layer ------------------------------------------------------- */

/* A page is a picture, so nothing on it can be selected.  A transparent copy of
 * its words, laid over the image where the glyphs are, is what makes a passage
 * selectable and copyable -- the trick a PDF in a browser has always used.  The
 * words render invisibly; only the selection over them shows.
 */

const SERIF = '"Times New Roman", Times, serif';
const SANS = "Helvetica, Arial, sans-serif";
const MONO = 'ui-monospace, "SF Mono", Menlo, monospace';

let ruler = null;        // one canvas, reused: measuring spans in the document
                         // would force a layout apiece, and a page holds a few
                         // hundred of them

function measure(font, text) {
  ruler = ruler || document.createElement("canvas").getContext("2d");
  ruler.font = font;
  return ruler.measureText(text).width;
}

/* PyMuPDF's font flags as a CSS font -- 2 italic, 4 serifed, 8 monospaced,
 * 16 bold.  The family only has to be close, since what it draws is invisible
 * and the width is corrected below; but a near miss keeps that correction near
 * 1, and a correction near 1 is what keeps a selection on its glyphs. */
function fontOf(span, size) {
  const family = span.flags & 8 ? MONO : span.flags & 4 ? SERIF : SANS;
  return `${span.flags & 2 ? "italic " : ""}${span.flags & 16 ? "700 " : ""}` +
         `${size}px ${family}`;
}

/* The paragraph whose rectangle holds a point on a page, in PDF points. */
function enclosing(page, x, y, onlyCarded) {
  for (const rect of state.rects.get(page) || []) {
    if (onlyCarded && !rect.summarizable) continue;
    const [x0, y0, x1, y1] = rect.bbox;
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return rect.id;
  }
  return null;
}

/* The same question asked of a mouse event over a page box. */
function paraAt(page, box, event) {
  const spot = box.getBoundingClientRect();
  return enclosing(page, (event.clientX - spot.left) / state.zoom,
                   (event.clientY - spot.top) / state.zoom, true);
}

const selectedText = () => (window.getSelection()?.toString() || "").trim();

function fillText(layer) {
  const number = Number(layer.dataset.page);
  const spans = state.text.get(number);
  if (!spans || layer.childElementCount) return;
  const { zoom } = state;
  const batch = document.createDocumentFragment();
  spans.forEach((span) => {
    const size = (span.y1 - span.y0) * zoom;
    const font = fontOf(span, size);
    const node = document.createElement("span");
    node.textContent = span.text;
    node.style.cssText = `left:${span.x0 * zoom}px;top:${span.y0 * zoom}px;` +
                         `font:${font};line-height:1`;
    const drawn = measure(font, span.text);
    if (drawn > 0 && span.x1 > span.x0) {
      node.style.transform = `scaleX(${(span.x1 - span.x0) * zoom / drawn})`;
    }
    // Which paragraph the span belongs to, and which line of the page it is
    // on: between them, a selection can be put back together as prose.
    const owner = enclosing(number, (span.x0 + span.x1) / 2,
                            (span.y0 + span.y1) / 2, false);
    if (owner !== null) node.dataset.para = owner;
    node.dataset.line = span.line;
    batch.appendChild(node);
  });
  layer.appendChild(batch);
}

async function loadText(layer) {
  const number = Number(layer.dataset.page);
  if (!state.text.has(number)) {
    const sha = state.sha;
    try {
      const data = await fetch(`/api/text/${sha}/${number}.json`)
        .then((r) => r.json());
      if (state.sha !== sha) return;    // the reader moved on to another paper
      state.text.set(number, data.spans || []);
    } catch {
      return;               // the page is still readable, just not selectable
    }
  }
  fillText(layer);
}

/* Fill a page's text layer as it comes near, the way its image is loaded: a
 * reader who opens a 200-page report and reads four pages pays for four.  The
 * spans stay in state.text, so zooming -- which rebuilds every layer -- costs
 * nothing over the network. */
function watchPages() {
  state.watcher?.disconnect();
  state.watcher = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      state.watcher.unobserve(entry.target);
      loadText(entry.target);
    });
  }, { rootMargin: "600px" });
  document.querySelectorAll(".text-layer")
          .forEach((layer) => state.watcher.observe(layer));
}

/* --- copying -------------------------------------------------------------- */

/* Put back together what a selection covers.
 *
 * Every line of the page is its own element, so the browser's own copy hands
 * over a column of fragments with the typesetter's hyphens still in them.  Each
 * span carries the paragraph and the line it came from, which is enough to
 * rebuild the sentences the way the extractor does in _join -- so a quote
 * pasted into notes reads as prose, and reads the same as the text Gloss sent
 * the model.  Anything outside a paragraph -- a heading, a table row -- keeps
 * its line breaks, because running a table row together would be nonsense.
 */
function knit(fragment) {
  let out = "";
  let para = null;
  let line = null;
  fragment.querySelectorAll("[data-line]").forEach((span) => {
    const text = span.textContent;
    if (!text) return;
    const owner = span.dataset.para ?? "";
    if (para === null) out = text;
    else if (owner !== para) out = `${out.trimEnd()}\n\n${text}`;
    else if (span.dataset.line === line) out += text;
    else if (owner === "") out = `${out.trimEnd()}\n${text}`;
    else out = mend(out, text);
    para = owner;
    line = span.dataset.line;
  });
  return out.replace(/[^\S\n]+/g, " ").trim();
}

/* One line onto the end of the last, healing a word the typesetter broke. */
function mend(out, text) {
  const before = out.trimEnd();
  const after = text.trimStart();
  if (/[-\u00ad]$/.test(before) && !before.endsWith("--") && /^\p{Ll}/u.test(after)) {
    return before.slice(0, -1) + after;     // word split across two lines
  }
  if (before.endsWith("\u2013") && /^\p{Ll}/u.test(after)) {
    return before + after;                  // compound broken at an en dash
  }
  return `${before} ${after}`;
}

document.addEventListener("copy", (event) => {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return;
  const text = knit(selection.getRangeAt(0).cloneContents());
  // Nothing of the page in the selection: a summary card, an answer, or a few
  // words within one line -- all of which the browser already copies as they
  // read.
  if (!text) return;
  event.clipboardData.setData("text/plain", text);
  event.preventDefault();
});

/* --- interaction ---------------------------------------------------------- */

function activate(id, scrollCard) {
  if (state.active === id) return;
  if (state.active !== null) {
    state.cards.get(state.active)?.classList.remove("on");
    (state.bands.get(state.active) || []).forEach((b) => b.classList.remove("on"));
  }
  state.active = id;
  const card = state.cards.get(id);
  card?.classList.add("on");
  (state.bands.get(id) || []).forEach((b) => b.classList.add("on"));
  drawLeader(id);
  if (scrollCard && card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/* A curve from the paragraph to its card, drawn only for the active pair --
 * without it, a card pushed far down the margin looks unrelated to the text. */
function drawLeader(id) {
  const svg = $("leaders");
  svg.innerHTML = "";
  const card = state.cards.get(id);
  if (!card || !state.showMargin) return;
  const para = paragraph(id);
  const bands = state.bands.get(id);
  if (!para || !bands || !bands.length) return;

  const docBox = $("doc").getBoundingClientRect();
  const from = bands[0].getBoundingClientRect();
  const to = card.getBoundingClientRect();
  const x1 = from.right - docBox.left;
  const y1 = from.top - docBox.top + Math.min(from.height / 2, 10);
  const x2 = to.left - docBox.left;
  const y2 = to.top - docBox.top + 14;
  const mid = x1 + (x2 - x1) / 2;
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
  svg.appendChild(path);
}

/* --- summaries ------------------------------------------------------------ */

/* Put a card into whatever state its paragraph is already in.  Cards are built
 * fresh by render(), which zooming calls, so this runs both as summaries arrive
 * and as the margin is rebuilt -- otherwise a zoom would drop every finished
 * summary back to "Summarizing...", with no poll still running to refill it. */
function fillCard(card, id) {
  const body = card.querySelector(".body");
  if (state.failures.has(id)) {
    card.classList.remove("pending");
    card.classList.add("failed");
    body.textContent = "Couldn't summarize this paragraph. ";
    const retry = el("button", "retry");
    retry.textContent = "Try again";
    retry.addEventListener("click", async (event) => {
      event.stopPropagation();
      body.textContent = "Retrying...";
      try {
        const result = await post("/api/resummarize", { sha: state.sha, id });
        card.classList.remove("failed");
        state.failures.delete(id);
        state.summaries[id] = result.summary;   // so a later zoom keeps it
        body.textContent = result.summary;
      } catch (err) {
        body.textContent = `Still failing: ${err.message}`;
      }
      requestAnimationFrame(() => layoutCards(collectPageEls()));
    });
    body.appendChild(retry);
  } else if (state.summaries[id] !== undefined) {
    card.classList.remove("pending", "failed");
    body.textContent = state.summaries[id];
  }
}

function applySummaries(data) {
  Object.entries(data.summaries || {}).forEach(([id, text]) => {
    const key = Number(id);
    if (state.summaries[key] === text) return;
    state.summaries[key] = text;
    const card = state.cards.get(key);
    if (card) fillCard(card, key);
  });

  (data.failures || []).forEach((id) => {
    if (state.failures.has(id)) return;
    state.failures.add(id);
    const card = state.cards.get(id);
    if (card) fillCard(card, id);
  });
}

const collectPageEls = () => Array.from($("pages").children);

function setStatus(data) {
  const total = state.doc.paragraphs.filter((p) => p.summarizable).length;
  const done = Object.keys(state.summaries).length + state.failures.size;
  const box = $("status");
  if (data.stage === "error") {
    box.textContent = "";
    showError(data.error || "Summarizing failed.");
  } else if (data.stage === "brief") {
    box.innerHTML = '<span class="spinner"></span>Reading the whole paper...';
  } else if (data.stage === "done") {
    box.textContent = state.failures.size
      ? `${done - state.failures.size} of ${total} summarized, ${state.failures.size} failed`
      : `${total} paragraphs summarized`;
  } else {
    box.innerHTML = `<span class="spinner"></span>Summarizing ${done} of ${total}...`;
  }
}

function poll() {
  clearInterval(state.polling);
  const tick = async () => {
    if (!state.sha) return;
    try {
      const data = await fetch(`/api/summaries/${state.sha}`).then((r) => r.json());
      applySummaries(data);
      setStatus(data);
      requestAnimationFrame(() => layoutCards(collectPageEls()));
      if (data.stage === "done" || data.stage === "error") clearInterval(state.polling);
    } catch { /* transient; the next tick retries */ }
  };
  tick();
  state.polling = setInterval(tick, 1200);
}

/* --- asking questions ----------------------------------------------------- */

function openAsk(open) {
  const panel = $("ask-panel");
  panel.hidden = !open;
  if (open) $("ask-input").focus();
}

/* Scroll a cited paragraph into view and flash it, so a citation in the answer
 * lands the reader on the actual passage rather than just naming it. */
function jumpTo(id) {
  const para = paragraph(id);
  const bands = state.bands.get(id);
  if (!para || !bands || !bands.length) return;
  activate(id, false);
  bands[0].scrollIntoView({ block: "center", behavior: "smooth" });
  flash(id);
}

/* Say that a passage is the one meant, without moving anything. */
function flash(id) {
  (state.bands.get(id) || []).forEach((band) => {
    band.classList.remove("flash");
    void band.offsetWidth;              // restart the animation
    band.classList.add("flash");
  });
}

/* Render an answer, turning [12] citations into buttons. Built as text nodes
 * and elements rather than innerHTML: the answer is model output, and it must
 * never be able to inject markup into the page. */
function renderAnswer(container, text) {
  // Handles the two things the model actually emits: **bold** and [12]
  // citations. Everything else stays literal text.
  const pattern = /\*\*([^*\n]+)\*\*|\[(\d+(?:\s*,\s*\d+)*)\]/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    container.appendChild(document.createTextNode(text.slice(last, match.index)));
    if (match[1] !== undefined) {
      const strong = document.createElement("strong");
      strong.textContent = match[1];
      container.appendChild(strong);
    } else {
      match[2].split(",").forEach((raw) => {
        const label = raw.trim();
        const id = Number(label);
        if (!state.bands.has(id)) {
          container.appendChild(document.createTextNode(`[${label}]`));
          return;
        }
        const chip = el("button", "cite");
        chip.type = "button";
        chip.textContent = label;
        chip.title = "Jump to this passage";
        chip.addEventListener("click", () => jumpTo(id));
        container.appendChild(chip);
      });
    }
    last = pattern.lastIndex;
  }
  container.appendChild(document.createTextNode(text.slice(last)));
}

async function submitQuestion(event) {
  event.preventDefault();
  const input = $("ask-input");
  const question = input.value.trim();
  if (!question || !state.sha) return;

  $("ask-thread").querySelector(".ask-empty")?.remove();
  const thread = $("ask-thread");
  const q = el("p", "ask-q");
  q.textContent = question;
  const a = el("div", "ask-a");
  a.textContent = "Reading the paper...";
  thread.append(q, a);
  a.scrollIntoView({ block: "nearest" });

  input.value = "";
  input.disabled = true;
  $("ask-send").disabled = true;
  try {
    const result = await post("/api/ask", { sha: state.sha, question });
    a.textContent = "";
    renderAnswer(a, result.answer);
  } catch (err) {
    a.classList.add("failed");
    a.textContent = err.message;
  } finally {
    input.disabled = false;
    $("ask-send").disabled = false;
    input.focus();
    a.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

$("ask").addEventListener("click", () => openAsk($("ask-panel").hidden));
$("ask-close").addEventListener("click", () => openAsk(false));
$("ask-form").addEventListener("submit", submitQuestion);
$("ask-input").addEventListener("keydown", (event) => {
  // Enter sends; Shift+Enter is a newline, as in any chat box.
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});

/* --- controls ------------------------------------------------------------- */

function setZoom(next) {
  state.zoom = Math.min(2.2, Math.max(0.6, Math.round(next * 20) / 20));
  localStorage.setItem("zoom", state.zoom);
  if (state.doc) render();
}

$("close").addEventListener("click", closeDoc);
$("open2")?.addEventListener("click", choose);
$("zoom-in").addEventListener("click", () => setZoom(state.zoom + 0.1));
$("zoom-out").addEventListener("click", () => setZoom(state.zoom - 0.1));
$("toggle-margin").addEventListener("click", (event) => {
  state.showMargin = !state.showMargin;
  $("doc").classList.toggle("no-margin", !state.showMargin);
  event.currentTarget.setAttribute("aria-pressed", String(state.showMargin));
  const widths = state.widths;
  $("doc").style.width = `${state.showMargin ? widths.doc : widths.pages}px`;
});
// Delegated, because entries are added to this list as documents are opened.
$("recent-list").addEventListener("click", (event) => {
  const button = event.target.closest(".recent");
  if (button) openPath(button.dataset.path);
});

document.addEventListener("keydown", (event) => {
  if (event.metaKey && event.key === "o") { event.preventDefault(); choose(); }
  if (event.metaKey && event.key === "k" && state.sha) {
    event.preventDefault();
    openAsk(true);
    return;
  }
  if (event.key === "Escape" && !$("ask-panel").hidden) {
    openAsk(false);
    return;
  }
  // Zoom keys must not fire while the question box has focus.
  if (event.target.tagName === "TEXTAREA" || event.target.tagName === "INPUT") return;
  if (event.key === "=" || event.key === "+") setZoom(state.zoom + 0.1);
  if (event.key === "-") setZoom(state.zoom - 0.1);
  // With nothing selected, copy the paragraph under the pointer whole -- the
  // joined, dehyphenated text, not the lines it happens to be set in.
  if ((event.metaKey || event.ctrlKey) && event.key === "c"
      && state.active !== null && selectedText() === "") {
    const para = paragraph(state.active);
    if (para) {
      navigator.clipboard.writeText(para.text);
      flash(state.active);
      event.preventDefault();
    }
  }
});

/* Browsers withhold the real path of a dragged file, so send the bytes. */
let dragDepth = 0;
window.addEventListener("dragenter", (event) => {
  event.preventDefault();
  if (++dragDepth === 1) $("drop").hidden = false;
});
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) $("drop").hidden = true; });
window.addEventListener("drop", guard(async (event) => {
  event.preventDefault();
  dragDepth = 0;
  $("drop").hidden = true;
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  openResult(await post("/api/upload", form, true));
}));

window.addEventListener("resize", () => {
  if (state.doc) requestAnimationFrame(() => layoutCards(collectPageEls()));
});
