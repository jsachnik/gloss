/* Gloss -- page rendering, margin layout, and progressive summary fill.
 *
 * Page geometry comes from the server in PDF points; multiplying by the zoom
 * factor converts to CSS pixels, which is how a summary card finds the y of the
 * paragraph it belongs to.  Page boxes are given an explicit height up front so
 * the layout is stable before any image has loaded.
 */

const GAP = 28;          // px between the page column and the margin
const CARD_GAP = 8;      // minimum px between stacked cards
const MARGIN_W = 340;
const MAX_RECENTS = 12;  // matches app.py, which is what persists the list

const state = {
  sha: null,
  doc: null,
  zoom: parseFloat(localStorage.getItem("zoom")) || 1.25,
  summaries: {},
  failures: new Set(),
  active: null,
  cards: new Map(),      // paragraph id -> card element
  bands: new Map(),      // paragraph id -> [band elements]
  showMargin: true,
  polling: null,
};

const $ = (id) => document.getElementById(id);
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

  const widest = Math.max(...doc.pages.map((p) => p.width)) * zoom;
  const docEl = $("doc");
  docEl.style.width = `${widest + GAP + MARGIN_W}px`;
  pages.style.width = `${widest}px`;
  margin.style.left = `${widest + GAP}px`;
  margin.style.width = `${MARGIN_W}px`;

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

      // Only what has a card gets a hit target.  Everything keeps a band, so a
      // cited passage can still be flashed, but hovering a table cell or a
      // one-line caption used to highlight it and pair it with nothing.
      if (!para.summarizable) return;
      const hit = el("div", "hit");
      hit.style.cssText = style;
      hit.addEventListener("mouseenter", () => activate(para.id, false));
      hit.addEventListener("click", () => activate(para.id, true));
      host.appendChild(hit);
    });
    state.bands.set(para.id, bands);
  });

  doc.paragraphs.filter((p) => p.summarizable).forEach((para) => {
    const card = el("div", "card pending");
    card.style.width = `${MARGIN_W}px`;
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
  });

  requestAnimationFrame(() => layoutCards(pageEls));
  $("zoom-level").textContent = `${Math.round(zoom * 100)}%`;
}

/* Stack cards down the margin: each one wants to sit level with its paragraph,
 * and is pushed down only as far as the card above it requires. */
function layoutCards(pageEls) {
  const { doc, zoom } = state;
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
    cursor = top + card.offsetHeight + CARD_GAP;
  });
  $("doc").style.minHeight = `${Math.max($("pages").offsetHeight, cursor)}px`;
  if (state.active !== null) drawLeader(state.active);
}

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
  const para = state.doc.paragraphs.find((p) => p.id === id);
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

function applySummaries(data) {
  Object.entries(data.summaries || {}).forEach(([id, text]) => {
    const key = Number(id);
    if (state.summaries[key] === text) return;
    state.summaries[key] = text;
    const card = state.cards.get(key);
    if (!card) return;
    card.classList.remove("pending", "failed");
    card.querySelector(".body").textContent = text;
  });

  (data.failures || []).forEach((id) => {
    if (state.failures.has(id)) return;
    state.failures.add(id);
    const card = state.cards.get(id);
    if (!card) return;
    card.classList.remove("pending");
    card.classList.add("failed");
    const body = card.querySelector(".body");
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
        body.textContent = result.summary;
      } catch (err) {
        body.textContent = `Still failing: ${err.message}`;
      }
      requestAnimationFrame(() => layoutCards(collectPageEls()));
    });
    body.appendChild(retry);
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
  const para = state.doc?.paragraphs.find((p) => p.id === id);
  const bands = state.bands.get(id);
  if (!para || !bands || !bands.length) return;
  activate(id, false);
  bands[0].scrollIntoView({ block: "center", behavior: "smooth" });
  bands.forEach((band) => {
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
  const widest = Math.max(...(state.doc?.pages || [{ width: 600 }]).map((p) => p.width)) * state.zoom;
  $("doc").style.width = state.showMargin ? `${widest + GAP + MARGIN_W}px` : `${widest}px`;
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
