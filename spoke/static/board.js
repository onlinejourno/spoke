/* The board draws what the server already decided.

   Layout, zoom layers, flag roll-up, severity, shape and badge all
   arrive computed from /api/lens. Nothing here recomputes any of them:
   a browser-side force layout is untestable and unpredictable, and a
   second copy of the flag rules in JavaScript is a second thing to
   drift. This file positions SVG, handles clicks, and nothing else --
   which is also why it needs no graph library. */

const ZOOMS = ["far", "middle", "close", "closest"];
// The map is never drawn smaller than this many pixels per layout unit.
// Below it, the canvas scrolls. Labels are compensated (see board.css)
// so the floor is about spacing, not legibility.
const MIN_SCALE = 0.6;
// Above this many hubs, only the ones that carry a flag, are selected,
// or neighbour the selection keep a visible label; the rest are dots
// that label on hover. A map reads by its hubs, not by every name.
const LABEL_BUDGET = 40;
const shorten = (s, max = 26) =>
  s.length <= max ? s : s.slice(0, 12) + "\u2026" + s.slice(-(max - 13));
const sevRank = (sev) => ({ bad: 3, warn: 2, held: 1, none: 0 })[sev] || 0;
const SHAPES = {
  // Type survives for a reader who cannot distinguish the colours.
  project: (r) => polygon(6, r),
  product: (r) => `M${-r},${-r * 0.7}H${r}V${r * 0.7}H${-r}Z`,
  capability: (r) => polygon(3, r),
  decision: (r) => `M0,${-r}L${r},0L0,${r}L${-r},0Z`,
  item: (r) => `M0,${-r}a${r},${r} 0 1,0 0.01,0Z`,
  client: (r) => polygon(5, r),
  clients: (r) => polygon(5, r),
};

function polygon(sides, r) {
  const pts = [];
  for (let i = 0; i < sides; i++) {
    const a = (Math.PI * 2 * i) / sides - Math.PI / 2;
    pts.push(`${(r * Math.cos(a)).toFixed(2)},${(r * Math.sin(a)).toFixed(2)}`);
  }
  return `M${pts.join("L")}Z`;
}

const state = { lens: "product", zoom: 0, flags: new Set(), data: null, selected: null };

const $ = (id) => document.getElementById(id);
const say = (msg) => { $("status").textContent = msg; };

async function load() {
  const params = new URLSearchParams({ name: state.lens, zoom: ZOOMS[state.zoom] });
  for (const f of state.flags) params.append("flag", f);
  const d = await loadJSON(`/api/lens?${params}`, `the ${state.lens} lens`, say);
  if (d === null) return;
  state.data = d;
  say("");
  renderLenses();
  renderFilters();
  renderDetail();
  renderQuery();
  renderSkipped(state.data);
  // A map with nothing on it must say so and say what to do -- ground
  // showing through is the palette's word for "not recorded", but a
  // whole page of it is indistinguishable from a page that failed.
  const bare = state.data.nodes.length === 0;
  $("empty").hidden = !bare;
  $("canvaswrap").hidden = bare;
  if (bare) renderEmpty();
  draw();
  renderKey();
}

/* Three reasons a lens draws nothing, told apart -- because a blank
 * canvas that does not say which is indistinguishable from a page that
 * failed. The server sends the counts; the page only phrases them. */
function renderEmpty() {
  const d = state.data;
  const types = (d.hub_types || []).join(" or ");
  const title = $("empty-title"), why = $("empty-why"), how = $("empty-how");
  how.replaceChildren();
  const link = (href, text) => { const a = document.createElement("a"); a.href = href; a.textContent = text; return a; };
  if (!d.ledger_total) {
    title.textContent = "Nothing on the map yet";
    why.textContent = "The ledger has no nodes, so no lens has anything to anchor on.";
    how.append("The ", link("/setup", "first-run steps"), " put the first ones there: scan a repo, or report a problem from any page.");
  } else if (!d.hub_type_count) {
    title.textContent = `No ${types} nodes`;
    why.textContent = `This lens anchors on ${types} nodes and the ledger has none of that type` +
      ` (it has ${d.ledger_total} node${d.ledger_total === 1 ? "" : "s"} of other types).`;
    if (state.lens === "client") {
      how.textContent = "Clients are not derived by the scan -- nothing in a repo says who it serves. " +
        "Record one when you have one: `spoke ledger new <name> --type client --rel serves:<product>`.";
    } else {
      how.textContent = `Record one with \`spoke ledger new <name> --type ${state.lens}\`, or switch to a lens whose type exists.`;
    }
  } else {
    const flags = [...state.flags].join(", ");
    title.textContent = "Nothing carries that flag";
    why.textContent = `${d.hub_type_count} ${types} node${d.hub_type_count === 1 ? "" : "s"} exist, ` +
      `but none is flagged ${flags} through this lens.`;
    how.textContent = "That is the good outcome. Clear the filter to draw them.";
  }
}

function renderLenses() {
  const nav = $("lenses");
  nav.replaceChildren(...state.data.lenses.map((name) => {
    const b = document.createElement("button");
    b.type = "button";
    // The project's own word for the type, where it has one: a tab that
    // says "product" beside a key and a grid that say "package" is the
    // package overriding the install in the one place it is most seen.
    // The lens NAME (what the API takes) is unchanged; only the label.
    b.textContent = (state.data.vocabulary || {})[name] || name;
    b.title = name === b.textContent ? "" : `the ${name} lens`;
    b.setAttribute("aria-pressed", String(name === state.lens));
    b.onclick = () => { state.lens = name; state.selected = null; load(); };
    return b;
  }));
}

function renderFilters() {
  const nav = $("filters");
  nav.replaceChildren(...state.data.flag_kinds.map((kind) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = kind;
    b.setAttribute("aria-pressed", String(state.flags.has(kind)));
    b.onclick = () => {
      state.flags.has(kind) ? state.flags.delete(kind) : state.flags.add(kind);
      load();
    };
    return b;
  }));
}

/* The default view is a query, not a graph: on load you get the flagged
   hub nodes, and the map is what you drill INTO from one of them. */
/* The rail groups by ORIGIN, not by hub. Eight hubs each showing
 * "1 held" are one fact -- one hold reaching eight -- and a rail that
 * lists them as eight problems misreads its own data. Every flag names
 * its origin; the payload says what that origin is and what it ruled. */
function renderQuery() {
  const d = state.data;
  const rank = { bad: 3, warn: 2, held: 1, none: 0 };
  const groups = new Map();
  for (const n of d.nodes) {
    if (!n.hub) continue;
    for (const f of n.flags) {
      const g = groups.get(f.origin) || { origin: f.origin, kinds: new Set(), hubs: new Set(), sev: "none" };
      g.kinds.add(f.kind);
      g.hubs.add(n.name);
      const sev = (d.severities || {})[f.kind] || "warn";
      if (rank[sev] > rank[g.sev]) g.sev = sev;
      groups.set(f.origin, g);
    }
  }
  const list = [...groups.values()].sort((a, b) =>
    rank[b.sev] - rank[a.sev] || b.hubs.size - a.hubs.size || a.origin.localeCompare(b.origin));

  $("querycount").textContent = list.length ? `(${list.length})` : "";
  $("queryempty").hidden = list.length > 0;
  $("querylist").replaceChildren(...list.map((g) => {
    const info = (d.origins || {})[g.origin] || {};
    const li = document.createElement("li");
    li.className = `origin sev-${g.sev}`;

    const head = document.createElement("div");
    head.className = "origin-head";
    const dot = document.createElement("span");
    dot.className = "dot"; dot.setAttribute("aria-hidden", "true");
    const kinds = document.createElement("span");
    kinds.className = "kinds";
    kinds.textContent = [...g.kinds].join(" · ");
    head.append(dot, kinds);
    const drawn = d.nodes.some((n) => n.name === g.origin);
    const title = document.createElement(drawn ? "button" : "span");
    title.className = "origin-title";
    if (drawn) { title.type = "button"; title.onclick = () => select(g.origin); }
    title.textContent = info.title || g.origin;
    head.append(title);
    if (info.kind || info.state) {
      const meta = document.createElement("span");
      meta.className = "origin-meta";
      meta.textContent = [info.kind, info.state].filter(Boolean).join(", ");
      head.append(meta);
    }
    li.append(head);

    if (info.ruling) {
      const r = document.createElement("p");
      r.className = "ruling";
      r.textContent = info.ruling;
      li.append(r);
    }

    const reach = document.createElement("div");
    reach.className = "reach";
    const label = document.createElement("span");
    label.className = "reach-label";
    label.textContent = g.hubs.size === 1 ? "reaches" : `reaches ${g.hubs.size}`;
    reach.append(label);
    for (const h of [...g.hubs].sort()) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip";
      b.textContent = h;
      b.onclick = () => select(h);
      reach.append(b);
    }
    li.append(reach);
    return li;
  }));
}

/* The key is DRAWN, not written: the swatches use the same severity
 * classes the nodes use, the glyphs the same SHAPES the canvas uses, and
 * the kinds-per-severity come from the payload. A legend authored by hand
 * is a second statement of what the encoding means, and two statements
 * drift. */
function renderKey() {
  const d = state.data;
  const key = $("key");
  key.replaceChildren();
  const words = d.vocabulary || {};

  const group = (title) => {
    const dt = document.createElement("dt");
    dt.textContent = title;
    key.append(dt);
    const dd = document.createElement("dd");
    key.append(dd);
    return dd;
  };

  // colour = the worst flag on the node
  const sev = group("Colour is the worst flag");
  const byLevel = {};
  for (const [kind, level] of Object.entries(d.severities || {})) {
    (byLevel[level] ||= []).push(kind);
  }
  for (const level of d.severity_order || []) {
    const kinds = byLevel[level];
    if (!kinds) continue;
    const item = document.createElement("span");
    item.className = `key-item sev-${level}`;
    const sw = document.createElement("span");
    sw.className = "dot";
    sw.setAttribute("aria-hidden", "true");
    item.append(sw, ` ${kinds.join(" · ")}`);
    sev.append(item);
  }
  const none = document.createElement("span");
  none.className = "key-item sev-none";
  const nsw = document.createElement("span");
  nsw.className = "dot";
  nsw.setAttribute("aria-hidden", "true");
  none.append(nsw, " no flag");
  sev.append(none);

  // shape = the node type, in the project's own words
  const shp = group("Shape is the type");
  for (const t of d.node_types || []) {
    const item = document.createElement("span");
    item.className = "key-item";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "-9 -9 18 18");
    svg.setAttribute("width", "14"); svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", (SHAPES[t] || SHAPES.item)(7));
    path.setAttribute("class", "glyph");
    svg.append(path);
    item.append(svg, ` ${words[t] || t}`);
    shp.append(item);
  }

  // size = hub or spoke
  const sz = group("Size is the role");
  for (const [r, label] of [[7, "hub"], [4, "spoke"]]) {
    const item = document.createElement("span");
    item.className = "key-item";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "-9 -9 18 18");
    svg.setAttribute("width", "14"); svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", SHAPES.product(r));
    path.setAttribute("class", "glyph");
    svg.append(path);
    item.append(svg, ` ${label}`);
    sz.append(item);
  }
}



function draw() {
  const svg = $("canvas");
  const nodes = state.data.nodes;
  const pos = state.data.positions;
  svg.replaceChildren();
  if (!nodes.length) return;

  const xs = nodes.map((n) => pos[n.name][0]);
  const ys = nodes.map((n) => pos[n.name][1]);
  // Padding in LABEL terms: a name runs to ~100px either side of its
  // glyph and a badge sits above it, so the padding is what those
  // need at the smallest scale the map is allowed, not a fixed 60.
  const pad = 170;
  // A timeline's axis sits above the first row; give it that room.
  const padTop = state.data.layout === "timeline" ? pad + 60 : pad;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - padTop, maxY = Math.max(...ys) + pad;
  const spanX = maxX - minX, spanY = maxY - minY;
  svg.setAttribute("viewBox", `${minX} ${minY} ${spanX} ${spanY}`);
  svg.classList.toggle("timeline", state.data.layout === "timeline");
  // Never shrink the drawing below one pixel per unit. A map that fits
  // more by shrinking everything, labels included, is how "close" drew
  // 86 nodes as a hairball; past the point where it fits, the canvas
  // scrolls and the labels stay the size the layout gave them room for.
  const wrap = svg.parentElement;
  const fit = Math.min(wrap.clientWidth / spanX, wrap.clientHeight / spanY);
  // Fit when it fits; shrink to at most MIN_SCALE when it does not, and
  // scroll past that. Labels and strokes are compensated below so they
  // render at their pixel size whatever the map's scale is.
  const k = Math.max(MIN_SCALE, Math.min(1, fit));
  svg.style.width = `${Math.round(spanX * k)}px`;
  svg.style.height = `${Math.round(spanY * k)}px`;
  svg.style.setProperty("--k", k);
  // When it fits, the grid centres it. When it overflows, open on the
  // node that matters -- the selected one, else the worst-flagged hub --
  // not on the middle of the ring, which is empty by construction.
  const focus = state.selected && pos[state.selected]
    ? state.selected
    : (nodes.filter((n) => n.hub && n.flags.length)
            .sort((a, b) => sevRank(b.severity) - sevRank(a.severity))[0] || {}).name;
  requestAnimationFrame(() => {
    const fx = focus && pos[focus] ? (pos[focus][0] - minX) * k : svg.clientWidth / 2;
    const fy = focus && pos[focus] ? (pos[focus][1] - minY) * k : svg.clientHeight / 2;
    wrap.scrollLeft = Math.max(0, fx - wrap.clientWidth / 2);
    wrap.scrollTop = Math.max(0, fy - wrap.clientHeight / 2);
  });

  if (state.data.layout === "timeline") drawAxis(svg, minX, minY, maxX);
  else drawCentre(svg, nodes, minX, minY, maxX, maxY);

  const drawn = new Set(nodes.map((n) => n.name));
  const near = new Set();
  for (const e of state.data.edges) {
    if (e.from === state.selected) near.add(e.to);
    if (e.to === state.selected) near.add(e.from);
  }
  svg.classList.toggle("has-selection", Boolean(state.selected));
  const hubCount = nodes.filter((n) => n.hub).length;
  // A hold reaches every node in its scope, so "flagged" alone would
  // label sixty nodes on a busy day; only warn and bad earn a label
  // past the budget. The badge still says "1 held" on the dot.
  const labelled = (n) => n.name === state.selected || near.has(n.name)
    || (n.hub && (hubCount <= LABEL_BUDGET || sevRank(n.severity) >= sevRank("warn")));
  for (const e of state.data.edges) {
    if (!drawn.has(e.from) || !drawn.has(e.to)) continue;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    const lit = state.selected && (e.from === state.selected || e.to === state.selected);
    line.setAttribute("class", lit ? "edge lit" : "edge");
    line.dataset.from = e.from; line.dataset.to = e.to;
    line.setAttribute("x1", pos[e.from][0]); line.setAttribute("y1", pos[e.from][1]);
    line.setAttribute("x2", pos[e.to][0]);   line.setAttribute("y2", pos[e.to][1]);
    line.append(titleEl(`${e.from} ${e.rel} ${e.to}`));
    svg.append(line);
  }

  nodes.forEach((n, i) => {
    const [x, y] = pos[n.name];
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", `node sev-${n.severity}${n.hub ? " hub" : ""} state-${n.state}`);
    g.setAttribute("transform", `translate(${x},${y})`);
    g.setAttribute("tabindex", "0");
    g.setAttribute("role", "button");
    g.setAttribute("aria-selected", String(n.name === state.selected));
    g.dataset.name = n.name;
    g.setAttribute(
      "aria-label",
      `${n.name}, ${n.hub ? "hub" : "spoke"}, ${n.kind || n.type}, ${n.state}. ${n.badge || "no flags"}`,
    );

    const quiet = !labelled(n);
    const r = n.hub ? 22 : (quiet ? 8 : 14);
    if (n.hub) {
      // The halo says "anchor" without spending colour on it.
      const halo = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      halo.setAttribute("class", "halo"); halo.setAttribute("r", r + 9);
      g.append(halo);
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", "glyph");
    path.setAttribute("d", (SHAPES[n.shape] || SHAPES.item)(r));
    g.append(path);

    // A name longer than the room the layout gives it is truncated from
    // the middle -- the tail is what tells sibling names apart -- and the
    // full name stays in the tooltip and the detail panel. A quiet
    // label is present but invisible until hover or focus.
    // Labels alternate between two rows, so two labelled neighbours on
    // a ring sized for dots do not overwrite each other.
    g.append(text(shorten(n.label || n.name), 0, r + 14 + (i % 2 ? 14 : 0), quiet ? "label quiet" : "label"));
    if (n.badge) g.append(text(n.badge, 0, -r - 6, "badge"));

    g.onclick = () => select(n.name);
    g.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n.name); } };
    svg.append(g);
  });
}

/* The centre of the canvas names what everything here is a hub OF.

   It is a LABEL, never a node: the ledger has no canonical root by
   design (see ledger/__init__.py -- there is deliberately no parent
   field), and putting a real node at the middle would re-impose the
   single hierarchy this whole model exists to remove. So it is drawn
   first, behind everything, with pointer-events off. It orients the
   reader; it is not part of the graph.

   The hub ring is laid out around the origin, so the middle is
   reliably empty -- but the label is derived from the drawn extent
   rather than assuming (0,0), so it stays centred whatever the layout
   does next. */
/* The time axis: a rule along the top with the ticks the server sent,
 * in the same scale the nodes were placed with. A timeline without an
 * axis is a row of things in some order. */
function drawAxis(svg, minX, minY, maxX) {
  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  g.setAttribute("class", "axis");
  g.setAttribute("aria-hidden", "true");
  const y = minY + 40;
  const rule = document.createElementNS("http://www.w3.org/2000/svg", "line");
  rule.setAttribute("x1", minX + 20); rule.setAttribute("x2", maxX - 20);
  rule.setAttribute("y1", y); rule.setAttribute("y2", y);
  rule.setAttribute("class", "axis-rule");
  g.append(rule);
  for (const t of state.data.axis || []) {
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", t.x); tick.setAttribute("x2", t.x);
    tick.setAttribute("y1", y - 6); tick.setAttribute("y2", y + 6);
    tick.setAttribute("class", `axis-tick ${t.kind}`);
    g.append(tick, text(t.label, t.x, y - 12, `axis-label ${t.kind}`));
  }
  svg.append(g);
}

function drawCentre(svg, nodes, minX, minY, maxX, maxY) {
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const hubs = nodes.filter((n) => n.hub).length;
  const spokes = nodes.length - hubs;
  const span = Math.min(maxX - minX, maxY - minY);

  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  g.setAttribute("class", "centre");
  g.setAttribute("transform", `translate(${cx},${cy})`);
  g.setAttribute("aria-hidden", "true");     // the counts are in the page text too

  // The centre says what the centre IS -- the hub -- and nothing else.
  // The product's name used to sit above it, which read as though the
  // hub were named Spoke; the name belongs in the masthead, once.
  g.append(text("Hub", 0, span * 0.0, "centre-word"));
  g.append(text(
    `${hubs} hub${hubs === 1 ? "" : "s"} · ${spokes} spoke${spokes === 1 ? "" : "s"}`,
    0, span * 0.045, "centre-count",
  ));
  svg.append(g);
}

function text(str, x, y, cls) {
  const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
  t.setAttribute("class", cls);
  t.setAttribute("x", x); t.setAttribute("y", y);
  t.setAttribute("text-anchor", "middle");
  t.textContent = str;
  return t;
}

function titleEl(str) {
  const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
  t.textContent = str;
  return t;
}

function select(name) {
  state.selected = name;
  const n = state.data.nodes.find((x) => x.name === name);
  const box = $("detail");
  box.hidden = !n;
  // Selection is drawn, not only listed: the selected glyph thickens
  // and, on a timeline, its edges light up while the rest stay faint.
  for (const g of document.querySelectorAll("svg .node")) {
    g.setAttribute("aria-selected", String(g.dataset.name === name));
  }
  for (const line of document.querySelectorAll("svg .edge")) {
    const lit = name && (line.dataset.from === name || line.dataset.to === name);
    line.classList.toggle("lit", !!lit);
  }
  if (!n) return;
  box.replaceChildren();
  const h = document.createElement("h2");
  h.textContent = n.label || n.name;
  const dl = document.createElement("dl");
  // Checklist and expectations come from the payload node when present.
  if (n.checklist && n.checklist.length) {
    const dt = document.createElement("dt"); dt.textContent = "checklist";
    const dd = document.createElement("dd"); dd.className = "written";
    const ol = document.createElement("ol"); ol.className = "checklist";
    for (const c of n.checklist) {
      const li = document.createElement("li");
      li.className = c.done ? "done" : "open";
      li.textContent = `${c.done ? "☑" : "☐"} ${c.text}`;
      ol.append(li);
    }
    dd.append(ol); dl.append(dt, dd);
  }
  if (n.expects && n.expects.length) {
    const dt = document.createElement("dt"); dt.textContent = "expects";
    const dd = document.createElement("dd");
    const ul = document.createElement("ul");
    for (const e of n.expects) {
      const li = document.createElement("li");
      const last = e.last || null;
      // A met check says what it saw, not only that it passed: "met" alone
      // is as opaque as a green light, and the whole point is the detail.
      const verdict = !last ? "never checked"
        : `${last.ok ? "met" : "UNMET"} ${last.at.slice(0, 16).replace("T", " ")}: ${last.detail}`;
      li.className = !last || !last.ok ? "unmet" : "met";
      li.textContent = `${e.url} — ${verdict}`;
      ul.append(li);
    }
    dd.append(ul); dl.append(dt, dd);
  }
  /* `written` marks a value a PERSON typed, which the stylesheet sets
     as prose. Everything else is derived and stays mono. The family is
     the provenance signal -- see tokens.css. */
  const row = (k, v, written) => {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    if (written) dd.className = "written";
    dl.append(dt, dd);
  };
  row("role", n.hub ? "hub — this lens anchors on it" : "spoke");
  row("type", n.kind || n.type);
  row("state", n.state);
  if (n.title) row("title", n.title, true);
  box.append(h, dl);

  if (n.flags.length) {
    const fh = document.createElement("h3");
    fh.textContent = "Flags";
    const ul = document.createElement("ul");
    for (const f of n.flags) {
      const li = document.createElement("li");
      /* Origin always travels with the flag. A flag rolled up from a
         layer this zoom does not draw is exactly the case where the
         reader cannot see the cause -- so it is named here. */
      li.textContent = `${f.kind} ← ${f.origin}: ${f.detail}`;
      ul.append(li);
    }
    box.append(fh, ul);
  }
  draw();
}

/* Detail, not zoom. Each step past the first ADDS a layer of node types;
 * the labels say which, in the project's words, from the payload. */
function renderDetail() {
  const d = state.data;
  const box = $("zoom");
  box.replaceChildren(...ZOOMS.map((z, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-pressed", String(i === state.zoom));
    b.textContent = i === 0 ? "hubs" : "+ " + ((d.zoom_adds || {})[z] || [z]).join(" · ");
    b.title = i === 0 ? "Only the hubs of this lens" : `Also draw: ${((d.zoom_adds || {})[z] || []).join(", ")}`;
    b.onclick = () => { state.zoom = i; load(); };
    return b;
  }));
}
load();

installReport($("bar"), () => ({
  lens: state.lens,
  zoom: ZOOMS[state.zoom],
  flags: [...state.flags].join(",") || "(none)",
  nodes: state.data ? String(state.data.nodes.length) : "not loaded",
  selected: state.selected || "(none)",
}));
