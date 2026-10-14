/* Gloss Library home: library list, map (network), timeline, Librarian.
 * Model output is only ever inserted with textContent -- never innerHTML. */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const getJSON = (url) => fetch(url).then((r) => r.json());
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

const state = { papers: [], view: "library", mode: "article", history: [], graph: null,
                highlight: new Map() };   // sha -> footnote number, set by the Librarian
const openReader = (sha) => { window.location.href = `/read?open=${encodeURIComponent(sha)}`; };
const byline = (p) => (p.authors.length
  ? p.authors.slice(0, 3).map((a) => a.name).join(", ") + (p.authors.length > 3 ? " et al." : "")
  : "Unknown authors");
const shortLabel = (p) => `${(p.authors[0]?.name || "?").split(" ").pop()} ${p.year || ""}`.trim();

/* --- loading the library ---------------------------------------------------- */

async function loadLibrary() {
  const data = await getJSON("/api/library");
  state.unscanned = data.unscanned || 0;           // PDFs in the folder nobody has read yet
  const signature = JSON.stringify(data.papers.map((p) => [p.sha, p.summary_status, p.connected]));
  if (signature === state.signature) return;        // nothing changed: leave the page alone
  state.signature = signature;
  state.papers = data.papers;
  const keep = ["paper-list", "timeline"].map((id) => [$(id), $(id).scrollTop, $(id).scrollLeft]);
  renderList();
  $("count").textContent = `${state.papers.length} papers` +
    (data.duplicates ? ` (${data.duplicates} duplicate${data.duplicates > 1 ? "s" : ""} ignored)` : "");
  if (state.view === "timeline") renderTimeline();
  keep.forEach(([node, top, left]) => { node.scrollTop = top; node.scrollLeft = left; });
  // The map is deliberately NOT rebuilt here. This runs on every status poll
  // while summaries are written; re-running the layout then makes the map
  // collapse and spring back every few seconds. It is built when the user opens
  // the Map, switches Articles/Authors, or adds a paper (see addFiles).
}

const STATUS_TEXT = { none: "queued", running: "summarizing…", done: "summarized",
                      partial: "summarized", error: "summary failed" };

function paperCard(p, onClick, tag = "li") {
  const card = el(tag, "pcard");
  card.dataset.sha = p.sha;
  card.appendChild(el("h3", "", p.title));
  card.appendChild(el("div", "meta", `${byline(p)} · ${p.year || "n.d."}${p.venue ? " · " + p.venue : ""}`));
  const chips = el("div");
  const st = el("span", "chip" + (p.summary_status === "done" || p.summary_status === "partial" ? "" : " dim"),
                STATUS_TEXT[p.summary_status] || p.summary_status);
  chips.appendChild(st);
  if (p.connected === false) chips.appendChild(el("span", "chip dim", "not connected"));
  card.appendChild(chips);
  card.addEventListener("click", onClick);
  markCard(card);
  return card;
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const list = $("paper-list");
  list.innerHTML = "";
  state.papers
    .filter((p) => !q || [p.title, p.venue, ...p.keywords, ...p.authors.map((a) => a.name)]
      .join(" ").toLowerCase().includes(q))
    .forEach((p) => list.appendChild(paperCard(p, () => showPaper(p.sha))));
}

async function pollStatus() {
  try {
    const s = await getJSON("/api/library/status");
    const pending = (s.none || 0) + (s.running || 0);
    $("summary-status").textContent = pending ? `Summarizing ${pending} paper${pending > 1 ? "s" : ""}…` : "";
    if (pending) { setTimeout(pollStatus, 8000); loadLibrary(); }
  } catch (e) { /* the server is gone; nothing to update */ }
}

/* --- Librarian highlights ---------------------------------------------------
 * The papers an answer uses are marked in blue wherever they appear -- the list,
 * the map, the timeline -- with the footnote number the answer gave them. A new
 * question clears the marks. */

function markCard(card) {
  card.querySelector(".fn-badge")?.remove();
  const n = state.highlight.get(card.dataset.sha);
  card.classList.toggle("hl", n !== undefined);
  if (n !== undefined) card.appendChild(el("span", "fn-badge", String(n)));
}

function setHighlight(papers) {
  state.highlight = new Map((papers || []).map((p) => [p.sha, p.n]));
  document.body.classList.toggle("has-hl", state.highlight.size > 0);
  document.querySelectorAll(".pcard").forEach(markCard);
  const bar = $("hl-bar");
  bar.hidden = state.highlight.size === 0;
  bar.textContent = "";
  if (state.highlight.size) {
    bar.appendChild(document.createTextNode(
      `The Librarian is using ${state.highlight.size} paper${state.highlight.size > 1 ? "s" : ""} (shown in blue)  `));
    const clear = el("button", "link", "Clear");
    clear.addEventListener("click", () => setHighlight([]));
    bar.appendChild(clear);
  }
  if (state.graph) draw();
}

/* Take the reader to a paper in whichever view is on screen. */
function focusPaper(sha) {
  if (state.view === "map" && state.graph) {
    const g = state.graph, c = $("graph");
    const mine = state.papers.find((p) => p.sha === sha);
    const node = state.mode === "author"
      ? g.nodes.find((n) => (mine?.authors || []).some((a) => a.id === n.author_id))
      : g.nodes.find((n) => n.id === sha);
    if (node) {
      g.ox = c.clientWidth / 2 - node.x * g.scale;
      g.oy = c.clientHeight / 2 - node.y * g.scale;
      g.pulse = { id: node.id, until: performance.now() + 1600 };
      pulseDraw();
    }
    return;
  }
  const card = document.querySelector(`#view-${state.view} .pcard[data-sha="${sha}"]`);
  if (card) {
    card.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    card.classList.add("pulse");
    setTimeout(() => card.classList.remove("pulse"), 1600);
  }
}

function pulseDraw() {
  draw();
  if (state.graph && state.graph.pulse && performance.now() < state.graph.pulse.until) {
    requestAnimationFrame(pulseDraw);
  }
}

/* --- sidebar --------------------------------------------------------------- */

function openDetail(build) {
  const body = $("detail-body");
  body.innerHTML = "";
  build(body);
  $("detail").hidden = false;
}

async function showPaper(sha) {
  const p = await getJSON(`/api/paper/${sha}`);
  openDetail((body) => {
    body.appendChild(el("h2", "", p.title));
    const meta = el("p", "muted");
    p.authors.forEach((a, i) => {
      const link = el("a", "author", a.name);
      link.addEventListener("click", () => showAuthor(a.id));
      meta.appendChild(link);
      if (i < p.authors.length - 1) meta.appendChild(document.createTextNode(", "));
    });
    meta.appendChild(document.createTextNode(` — ${p.year || "n.d."}${p.venue ? ", " + p.venue : ""}`));
    body.appendChild(meta);
    const open = el("button", "btn primary", "Open PDF with Gloss notes");
    open.addEventListener("click", () => openReader(p.sha));
    body.appendChild(open);
    if (p.qa_count) body.appendChild(el("p", "muted", `${p.qa_count} saved question${p.qa_count > 1 ? "s" : ""}`));

    body.appendChild(el("h4", "", "Summary"));
    body.appendChild(el("p", "", p.brief || p.abstract || "Summary not ready yet."));
    if (p.keywords.length) {
      const wrap = el("div");
      p.keywords.forEach((k) => wrap.appendChild(el("span", "chip", k)));
      body.appendChild(wrap);
    }
    body.appendChild(el("h4", "", `Connected papers (${p.related.length})`));
    if (!p.related.length) body.appendChild(el("p", "muted", "Not connected to anything else in the library yet."));
    const ul = el("ul", "linklist");
    p.related.forEach((r) => {
      const li = el("li"), b = el("button");
      b.appendChild(document.createTextNode(r.title));
      b.appendChild(el("span", "sub", [...new Set(r.types)].join(", ").replace(/_/g, " ") +
        (r.reasons[0] ? " — " + r.reasons[0] : "")));
      b.addEventListener("click", () => showPaper(r.sha));
      li.appendChild(b); ul.appendChild(li);
    });
    body.appendChild(ul);
  });
}

async function showAuthor(id) {
  const a = await getJSON(`/api/author/${id}`);
  openDetail((body) => {
    body.appendChild(el("h2", "", a.name));
    body.appendChild(el("p", "muted", `${a.papers.length} paper${a.papers.length === 1 ? "" : "s"} in your library`));
    const ul = el("ul", "linklist");
    a.papers.forEach((p) => {
      const li = el("li"), b = el("button");
      b.appendChild(document.createTextNode(p.title));
      b.appendChild(el("span", "sub", String(p.year || "n.d.")));
      b.addEventListener("click", () => showPaper(p.sha));
      li.appendChild(b); ul.appendChild(li);
    });
    body.appendChild(ul);
  });
}

$("detail-close").addEventListener("click", () => { $("detail").hidden = true; });

/* --- timeline ---------------------------------------------------------------- */

function renderTimeline() {
  const root = $("timeline");
  root.innerHTML = "";
  const groups = new Map();
  [...state.papers].sort((a, b) => (a.year || 9999) - (b.year || 9999) || a.title.localeCompare(b.title))
    .forEach((p) => {
      const key = p.year || "Undated";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    });
  groups.forEach((papers, year) => {
    const col = el("div", "year-col");
    col.appendChild(el("h3", "", String(year)));
    papers.forEach((p) => col.appendChild(paperCard(p, () => showPaper(p.sha), "div")));
    root.appendChild(col);
  });
}

/* --- map: a small force-directed graph on a canvas -------------------------- */

const EDGE_STYLE = {
  citation: { color: "#2e8b57", label: "cites" },
  shared_author: { color: "#3b73c4", label: "shared author" },
  llm_related: { color: "#8b5fbf", label: "related (AI-judged)" },
  shared_keyword: { color: "#999999", label: "shared topic words" },
};
const enabledTypes = new Set(Object.keys(EDGE_STYLE));

function buildLegend() {
  const box = $("legend");
  box.innerHTML = "";
  if (state.mode === "author") { box.appendChild(el("span", "", "Lines join co-authors; size = papers in library")); return; }
  Object.entries(EDGE_STYLE).forEach(([type, s]) => {
    const label = el("label");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = enabledTypes.has(type);
    cb.addEventListener("change", () => { cb.checked ? enabledTypes.add(type) : enabledTypes.delete(type); draw(); });
    const swatch = el("i"); swatch.style.background = s.color;
    label.append(cb, swatch, document.createTextNode(s.label));
    box.appendChild(label);
  });
}

async function loadGraph() {
  buildLegend();
  const data = await getJSON(`/api/graph?mode=${state.mode}`);
  const canvas = $("graph");
  const w = canvas.clientWidth || 800, h = canvas.clientHeight || 500;
  const index = new Map();
  const nodes = data.nodes.map((n, i) => {
    const angle = (i / data.nodes.length) * Math.PI * 2;
    const node = { ...n, x: w / 2 + Math.cos(angle) * 150, y: h / 2 + Math.sin(angle) * 150, vx: 0, vy: 0 };
    index.set(n.id, node);
    return node;
  });
  const links = data.edges.map((e) => ({ ...e, s: index.get(e.source), t: index.get(e.target) }))
    .filter((e) => e.s && e.t);
  state.graph = { nodes, links, scale: 1, ox: 0, oy: 0, alpha: 1, hover: null, drag: null };
  tick();
}

const HL_COLOR = "#2f7bf5";

/* The footnote label for a map node, or "" -- an author node is marked when any
 * highlighted paper is theirs. */
function highlightOf(n) {
  if (!state.highlight.size) return "";
  if (state.mode === "article") return state.highlight.has(n.id) ? String(state.highlight.get(n.id)) : "";
  const nums = [];
  state.papers.forEach((p) => {
    if (state.highlight.has(p.sha) && p.authors.some((a) => a.id === n.author_id)) nums.push(state.highlight.get(p.sha));
  });
  return nums.length ? nums.sort((a, b) => a - b).join(",") : "";
}

function radius(n) { return state.mode === "author" ? 5 + 3 * Math.sqrt(n.count) : 7 + 2.2 * Math.sqrt(n.degree); }

function step(g, w, h) {
  const { nodes, links } = g;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
      const f = (state.mode === "author" ? 2200 : 5200) / d2 * g.alpha;
      dx /= d; dy /= d;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
  }
  links.forEach((l) => {
    if (state.mode === "article" && !l.types.some((t) => enabledTypes.has(t))) return;
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 140) * 0.02 * (0.4 + (l.weight || 0.5)) * g.alpha;
    l.s.vx += dx / d * f; l.s.vy += dy / d * f; l.t.vx -= dx / d * f; l.t.vy -= dy / d * f;
  });
  nodes.forEach((n) => {
    n.vx += (w / 2 - n.x) * 0.004 * g.alpha; n.vy += (h / 2 - n.y) * 0.004 * g.alpha;
    if (n === g.drag) { n.vx = n.vy = 0; return; }
    n.vx *= 0.82; n.vy *= 0.82; n.x += n.vx; n.y += n.vy;
  });
  g.alpha = Math.max(0.02, g.alpha * 0.975);
}

function fitGraph(g, w, h) {
  if (!g.nodes.length) return;
  const xs = g.nodes.map((n) => n.x), ys = g.nodes.map((n) => n.y);
  const x0 = Math.min(...xs) - 40, x1 = Math.max(...xs) + 40;
  const y0 = Math.min(...ys) - 30, y1 = Math.max(...ys) + 40;
  g.scale = Math.min(2.2, Math.max(0.4, Math.min(w / (x1 - x0), h / (y1 - y0))));
  g.ox = w / 2 - ((x0 + x1) / 2) * g.scale;
  g.oy = h / 2 - ((y0 + y1) / 2) * g.scale;
}

let raf = null;
function tick() {
  cancelAnimationFrame(raf);
  const canvas = $("graph");
  const g = state.graph;
  if (!g || state.view !== "map") return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  step(g, w, h);
  draw();
  if (g.alpha > 0.03 || g.drag) raf = requestAnimationFrame(tick);
  else if (!g.fitted) { fitGraph(g, w, h); g.fitted = true; draw(); }
}

function draw() {
  const g = state.graph, canvas = $("graph");
  if (!g) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== canvas.clientWidth * dpr) {
    canvas.width = canvas.clientWidth * dpr; canvas.height = canvas.clientHeight * dpr;
  }
  const ctx = canvas.getContext("2d");
  const css = getComputedStyle(document.body);
  const ink = css.getPropertyValue("--ink").trim() || "#222";
  const accent = css.getPropertyValue("--accent").trim() || "#7a1c1c";
  ctx.setTransform(dpr * g.scale, 0, 0, dpr * g.scale, g.ox * dpr, g.oy * dpr);
  ctx.clearRect(-g.ox / g.scale, -g.oy / g.scale, canvas.width / g.scale, canvas.height / g.scale);

  const near = new Set();
  if (g.hover) { near.add(g.hover.id); g.links.forEach((l) => { if (l.s === g.hover) near.add(l.t.id); if (l.t === g.hover) near.add(l.s.id); }); }

  g.links.forEach((l) => {
    let color = "#999", types = l.types;
    if (state.mode === "article") {
      types = l.types.filter((t) => enabledTypes.has(t));
      if (!types.length) return;
      const order = ["citation", "shared_author", "llm_related", "shared_keyword"];
      color = EDGE_STYLE[order.find((t) => types.includes(t))].color;
    }
    const dim = g.hover && !(l.s === g.hover || l.t === g.hover);
    ctx.globalAlpha = dim ? 0.08 : 0.35 + 0.5 * (l.weight || 0.5);
    ctx.strokeStyle = color;
    ctx.lineWidth = (state.mode === "author" ? 1 + (l.n || 1) : 1 + 2 * (l.weight || 0.5)) / Math.sqrt(g.scale);
    ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
  });

  g.nodes.forEach((n) => {
    const dim = g.hover && !near.has(n.id);
    ctx.globalAlpha = (dim || (state.highlight.size && !highlightOf(n))) ? 0.3 : 1;
    ctx.beginPath(); ctx.arc(n.x, n.y, radius(n), 0, Math.PI * 2);
    ctx.fillStyle = n.degree === 0 && state.mode === "article" ? "#bbb" : accent;
    ctx.fill();
    const hl = highlightOf(n);
    if (hl) {
      ctx.globalAlpha = 1;
      const pulsing = g.pulse && g.pulse.id === n.id && performance.now() < g.pulse.until;
      ctx.beginPath(); ctx.arc(n.x, n.y, radius(n) + (pulsing ? 9 : 4), 0, Math.PI * 2);
      ctx.lineWidth = (pulsing ? 4 : 3) / Math.sqrt(g.scale); ctx.strokeStyle = HL_COLOR; ctx.stroke();
      ctx.fillStyle = HL_COLOR; ctx.font = `bold ${12 / Math.sqrt(g.scale)}px system-ui, sans-serif`;
      ctx.textAlign = "center"; ctx.fillText(hl, n.x, n.y - radius(n) - 8);
    }
  });

  // Labels: busiest nodes first, hovered one and its neighbours before the rest,
  // and any label that would land on one already drawn is skipped. Zooming in
  // makes room, so more of them appear.
  const panel = css.getPropertyValue("--panel").trim() || "#fff";
  ctx.font = `${11 / Math.sqrt(g.scale)}px system-ui, sans-serif`;
  ctx.textAlign = "center"; ctx.lineJoin = "round";
  const placed = [];
  const weight = (n) => (n === g.hover ? 1e9 : near.has(n.id) && g.hover ? 1e6 : 0) +
    (state.mode === "author" ? n.count : n.degree);
  [...g.nodes].sort((a, b) => weight(b) - weight(a)).forEach((n) => {
    const label = state.mode === "author" ? n.label
      : `${(n.first_author || "?").split(" ").pop()} ${n.year || ""}`.trim();
    const w = ctx.measureText(label).width + 6, hgt = 14 / Math.sqrt(g.scale);
    const box = { x: n.x - w / 2, y: n.y + radius(n) + 2, w, h: hgt };
    const must = n === g.hover;
    if (!must && placed.some((b) => box.x < b.x + b.w && b.x < box.x + box.w &&
                                      box.y < b.y + b.h && b.y < box.y + box.h)) return;
    placed.push(box);
    ctx.globalAlpha = g.hover && !near.has(n.id) ? 0.35 : 1;
    ctx.lineWidth = 3 / Math.sqrt(g.scale); ctx.strokeStyle = panel;
    ctx.strokeText(label, n.x, box.y + hgt - 3);
    ctx.fillStyle = ink; ctx.fillText(label, n.x, box.y + hgt - 3);
  });
  ctx.globalAlpha = 1;
}

function nodeAt(ev) {
  const g = state.graph; if (!g) return null;
  const rect = $("graph").getBoundingClientRect();
  const x = (ev.clientX - rect.left - g.ox) / g.scale, y = (ev.clientY - rect.top - g.oy) / g.scale;
  let best = null, bd = Infinity;
  g.nodes.forEach((n) => { const d = Math.hypot(n.x - x, n.y - y); if (d <= radius(n) + 4 && d < bd) { best = n; bd = d; } });
  return best;
}

(function wireCanvas() {
  const canvas = $("graph");
  let pan = null, moved = false, start = null;
  canvas.addEventListener("mousedown", (ev) => {
    const g = state.graph; if (!g) return;
    moved = false; start = { x: ev.clientX, y: ev.clientY };
    const n = nodeAt(ev);
    if (n) g.drag = n; else pan = { x: ev.clientX - g.ox, y: ev.clientY - g.oy };
  });
  window.addEventListener("mousemove", (ev) => {
    const g = state.graph; if (!g) return;
    const rect = canvas.getBoundingClientRect();
    const far = start && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 4;
    if (g.drag) {
      if (!far && !moved) return;                 // a click, not a drag
      moved = true; g.alpha = Math.max(g.alpha, 0.3);
      g.drag.x = (ev.clientX - rect.left - g.ox) / g.scale; g.drag.y = (ev.clientY - rect.top - g.oy) / g.scale;
      tick();
    } else if (pan) { if (!far && !moved) return; moved = true; g.ox = ev.clientX - pan.x; g.oy = ev.clientY - pan.y; draw(); }
    else if (state.view === "map") {
      const n = nodeAt(ev);
      if (n !== g.hover) { g.hover = n; draw(); }
      const tip = $("tip");
      if (n) {
        tip.textContent = state.mode === "author" ? `${n.label} — ${n.count} paper(s)` : n.label;
        tip.style.left = `${ev.clientX - rect.left + 14}px`; tip.style.top = `${ev.clientY - rect.top + 14}px`; tip.hidden = false;
      } else tip.hidden = true;
      canvas.style.cursor = n ? "pointer" : "grab";
    }
  });
  window.addEventListener("mouseup", () => {
    const g = state.graph; if (!g) return;
    if (g.drag && !moved) {
      const n = g.drag;
      state.mode === "author" ? showAuthor(n.author_id) : showPaper(n.id);
    }
    g.drag = null; pan = null;
  });
  canvas.addEventListener("wheel", (ev) => {
    const g = state.graph; if (!g) return;
    ev.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    const k = Math.exp(-ev.deltaY * 0.0012), ns = Math.min(3, Math.max(0.3, g.scale * k));
    g.ox = mx - (mx - g.ox) * ns / g.scale; g.oy = my - (my - g.oy) * ns / g.scale; g.scale = ns;
    draw();
  }, { passive: false });
})();

document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  state.mode = b.dataset.mode;
  $("detail").hidden = true;
  loadGraph();
}));

/* --- tabs ----------------------------------------------------------------- */

function setView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  ["library", "map", "timeline"].forEach((v) => { $(`view-${v}`).hidden = v !== view; });
  if (view === "timeline") renderTimeline();
  if (view === "map") loadGraph();
  history.replaceState(null, "", `#${view}`);
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));
$("search").addEventListener("input", renderList);

/* --- adding PDFs ------------------------------------------------------------ */

function notice(text, sticky) {
  const box = $("notice");
  clearTimeout(notice.timer);
  box.textContent = text; box.hidden = !text;
  box.title = sticky ? "Click to dismiss" : "";
  box.onclick = () => { box.hidden = true; };
  if (text && !sticky) notice.timer = setTimeout(() => { box.hidden = true; }, 9000);
}

/* PDFs dropped into the library folder by hand: read them when Home opens. */
async function autoScan() {
  const n = state.unscanned;
  if (!n) return;
  notice(`Found ${n} new PDF${n > 1 ? "s" : ""} in your library folder \u2014 reading ${n > 1 ? "them" : "it"}\u2026`, true);
  try {
    const r = await postJSON("/api/library/scan");
    const bits = [];
    if (r.added) bits.push(`Added ${r.added} paper${r.added > 1 ? "s" : ""}.`);
    if (r.duplicates.length) bits.push(`Already in your library: ${r.duplicates.join(", ")}.`);
    r.errors.forEach(([name, why]) => bits.push(`Could not read ${name}: ${why}`));
    notice(bits.join(" ") || "Nothing new to add.", r.errors.length > 0);
  } catch (err) { notice(`Could not read the new PDFs: ${err.message}`, true); }
  await loadLibrary(); pollStatus();
  if (state.view === "map") loadGraph();
}

async function addFiles(files) {
  const pdfs = [...files].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (!pdfs.length) { notice("Please choose PDF files."); return; }
  $("add").disabled = true;
  let added = 0;
  const problems = [];
  for (const [i, file] of pdfs.entries()) {
    notice(`Reading ${file.name} (${i + 1} of ${pdfs.length})… this takes a moment.`);
    const form = new FormData(); form.append("file", file);
    try {
      const r = await fetch("/api/library/upload", { method: "POST", body: form });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) problems.push(data.error || `${file.name} failed`);
      else if (data.duplicate) problems.push(data.message || `${file.name}: already in your library`);
      else added += data.added || 0;
    } catch (err) { problems.push(`${file.name}: ${err.message}`); }
  }
  $("add").disabled = false;
  notice(`Added ${added} paper${added === 1 ? "" : "s"}.` + (problems.length ? " " + problems.join(" · ") : ""),
         problems.length > 0);
  await loadLibrary(); pollStatus();
  if (added && state.view === "map") loadGraph();     // new papers change the network
}
$("add").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
let depth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); if (++depth === 1) $("drop").hidden = false; });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", () => { if (--depth <= 0) $("drop").hidden = true; });
window.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; $("drop").hidden = true; addFiles(e.dataTransfer.files); });

/* --- Librarian ------------------------------------------------------------- */

function footnote(p) {
  const chip = el("button", "fn", String(p.n));
  chip.type = "button";
  chip.title = `Find "${p.title}" on screen`;
  chip.addEventListener("click", () => focusPaper(p.sha));
  return chip;
}

function renderAnswer(box, r) {
  if (r.lead) box.appendChild(el("p", "lead", r.lead));
  (r.papers || []).forEach((p) => {
    const block = el("div", "ans-paper");
    const head = el("div", "ans-title");
    head.appendChild(footnote(p));
    const who = `${p.authors.length ? p.authors[0].split(" ").pop() : "?"}${p.year ? " " + p.year : ""}`;
    head.appendChild(el("span", "", `${who} \u2014 ${p.title}`));
    block.appendChild(head);
    const ul = el("ul", "ans-list");
    if (p.asks) { const li = el("li"); li.append(el("b", "", "Asks: "), document.createTextNode(p.asks)); ul.appendChild(li); }
    if (p.finds) { const li = el("li"); li.append(el("b", "", "Finds: "), document.createTextNode(p.finds)); ul.appendChild(li); }
    block.appendChild(ul);
    box.appendChild(block);
  });
  if (r.together) {
    box.appendChild(el("h4", "ans-h", "Together"));
    box.appendChild(el("p", "", r.together));
  }
  if (r.open_questions && r.open_questions.length) {
    box.appendChild(el("h4", "ans-h", "Open questions"));
    const ul = el("ul", "ans-list");
    r.open_questions.forEach((q) => ul.appendChild(el("li", "", q)));
    box.appendChild(ul);
  }
  if (r.gap_flag) box.appendChild(el("div", "gap",
    r.papers && r.papers.length ? "Partial coverage: your library only partly answers this."
                                : "Gap: your library doesn't really answer this yet."));
  if (r.external_suggestions && r.external_suggestions.length) {
    const ext = el("div", "ext");
    ext.appendChild(el("h4", "", "Leads outside your library \u2014 unverified"));
    const ul = el("ul");
    r.external_suggestions.forEach((s) => ul.appendChild(el("li", "", `${s.type.replace("_", " ")}: ${s.text}`)));
    ext.appendChild(ul);
    box.appendChild(ext);
  }
}

$("chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("chat-input"), question = input.value.trim();
  if (!question) return;
  const chat = $("chat");
  chat.appendChild(el("div", "msg-q", question));
  const answer = el("div", "msg-a", "Checking the library…");
  chat.appendChild(answer);
  chat.scrollTop = chat.scrollHeight;
  input.value = ""; $("chat-send").disabled = true;
  setHighlight([]);                      // a new question starts with a clean slate
  try {
    const r = await postJSON("/api/librarian", { question, history: state.history });
    answer.textContent = "";
    renderAnswer(answer, r);
    setHighlight(r.papers);
    state.history.push({ role: "user", content: question }, { role: "assistant", content: r.answer });
  } catch (err) { answer.textContent = err.message; }
  $("chat-send").disabled = false;
  chat.scrollTop = chat.scrollHeight;
});
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); }
});

/* --- start ---------------------------------------------------------------- */

(async function init() {
  await loadLibrary();
  pollStatus();
  autoScan();
  const hash = location.hash.slice(1);
  if (["map", "timeline", "library"].includes(hash)) setView(hash);
})();

/* A resized window changes the canvas; fit the map to it again. */
window.addEventListener("resize", () => {
  if (state.graph && state.view === "map") {
    const c = $("graph");
    fitGraph(state.graph, c.clientWidth, c.clientHeight);
    draw();
  }
});
