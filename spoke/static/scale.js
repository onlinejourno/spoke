/* The scale view. Every number the server computed, the server computed.

   No composite is calculated here, and none is inferred from the cells:
   the withholding rule is the point of the whole layer, and a second
   implementation of it in JavaScript would be a second thing to drift.
   If `composite` is null this page renders the server's reason,
   verbatim. */

/* Which tab is showing. "" is the all-axes grid; anything else is one
   axis key, drilled into across every row. */
let axisTab = "";

const $ = (id) => document.getElementById(id);
const say = (m) => { $("status").textContent = m; };

function fill(value, max) {
  // Index into the sequential ramp. Never red-to-green; see scale.css.
  const step = Math.round((value / max) * 5);
  return `var(--s${Math.max(0, Math.min(5, step))})`;
}

function renderTypes(d) {
  // The engine's own type list, labelled in the project's words. The
  // page shipped four hardcoded options once; the header beside them
  // said "package" while the option said "product".
  const sel = $("type");
  const current = sel.value;
  sel.replaceChildren(...d.node_types.map((t) => {
    const o = document.createElement("option");
    o.value = t;
    o.textContent = (d.vocabulary || {})[t] || t;
    o.title = o.textContent === t ? "" : `the ${t} type`;
    return o;
  }));
  sel.value = d.node_types.includes(current) ? current : d.node_type;
}

async function load() {
  const type = $("type").value;
  const d = await loadJSON(
    `/api/scale?type=${encodeURIComponent(type)}`, "the scale", say);
  if (d === null) return;
  say("");
  renderTypes(d);
  // With no axes there is nothing the legend explains and nothing a
  // composite can say but "no axes" -- fifteen times. The empty state
  // leads, alone, with the one command that changes it.
  const bare = d.axes.length === 0;
  $("noaxes").hidden = !bare;
  $("legend").hidden = bare;
  $("gridwrap").hidden = bare;
  $("composites").hidden = bare;
  if (bare) { renderSkipped(d); return; }
  if (axisTab && !d.axes.some((a) => a.key === axisTab)) axisTab = "";
  renderRamp(d);
  renderFreshness(d);
  renderTabs(d);
  if (axisTab) renderAxis(d); else renderGrid(d);
  renderComposites(d);
  renderSkipped(d);
}

function renderRamp(d) {
  const ramp = $("ramp");
  ramp.replaceChildren();
  for (let v = 0; v <= d.max_value; v++) {
    const li = document.createElement("li");
    li.style.background = fill(v, d.max_value);
    li.style.color = v / d.max_value > 0.6 ? "var(--on-dark)" : "var(--fg)";
    li.textContent = String(v);
    ramp.append(li);
  }
}

/* The legend states the ACTUAL threshold the server applied, never a
   number this file knows on its own -- a second copy of the rule would
   be a second thing to drift, and the drift would be silent. */
function renderFreshness(d) {
  const days = (d.score_stale_days || {}).measured;
  for (const el of document.querySelectorAll(".f-days")) {
    el.textContent = days == null ? "not set" : `${days} days`;
  }
}

/* One tab per axis, plus the all-axes grid. The grid answers "how do
   these compare"; an axis tab answers "what is our reliability, actually"
   -- and the second question is the one somebody asks when they are about
   to do something about it, so it gets room for the evidence. */
function renderTabs(d) {
  const nav = $("tabs");
  nav.hidden = d.axes.length === 0;
  if (!d.axes.length) { nav.replaceChildren(); return; }

  const mk = (key, label, weight) => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "tab";
    b.setAttribute("aria-selected", String(axisTab === key));
    b.append(label);
    if (weight != null) {
      const w = document.createElement("span");
      w.className = "w";
      w.textContent = `${Math.round(weight * 100)}%`;
      b.append(w);
    }
    b.onclick = () => { axisTab = key; load(); };
    return b;
  };
  nav.replaceChildren(
    mk("", "All axes", null),
    ...d.axes.map((a) => mk(a.key, a.label, a.share)),
  );
}

/* One axis, down every row: the score, where it came from, when, and
   what was actually seen. Ordered by score, with the unscored LAST and
   marked as unscored -- never sorted as if they were zeros. */
function renderAxis(d) {
  const axis = d.axes.find((a) => a.key === axisTab);
  const table = $("grid");
  const empty = $("empty");
  table.replaceChildren();

  const note = $("axisnote");
  note.hidden = false;
  note.replaceChildren();
  /* Counted, not merely measured: a stale or undated measurement no
     longer reaches the composite, so counting it here would contradict
     the composite card directly below it. */
  const counted = d.rows.filter((r) => cellOf(r).freshness === "fresh").length;
  const p = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = `${axis.label} — ${Math.round(axis.share * 100)}% of the composite weight. `;
  p.append(
    strong,
    // Phrased to sidestep subject-verb agreement entirely: "0 of 1
    // product have/has" is awkward whichever way it is written.
    `Measured and still current on ${counted} of ${d.rows.length} ` +
    `${d.kind}${d.rows.length === 1 ? "" : "s"}. ` +
    `The rest are not evidence of anything yet.`,
  );
  note.append(p);

  if (!d.rows.length) {
    table.hidden = true; empty.hidden = false;
    empty.textContent = `No ${d.kind} nodes to score yet.`;
    return;
  }
  table.hidden = false; empty.hidden = true;

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of [d.kind, axis.label, "Basis", "Observed", "What was seen"]) {
    hr.append(th(label));
  }
  thead.append(hr);
  table.append(thead);

  const rank = { measured: 0, asserted: 1, unverified: 2 };
  const rows = [...d.rows].sort((a, b) => {
    const ca = cellOf(a), cb = cellOf(b);
    const sa = ca.value === null ? -1 : 0, sb = cb.value === null ? -1 : 0;
    if (sa !== sb) return sb - sa;                       // unscored last
    if (ca.value !== cb.value) return (cb.value ?? 0) - (ca.value ?? 0);
    return (rank[ca.basis] ?? 9) - (rank[cb.basis] ?? 9);
  });

  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const c = cellOf(row);
    const tr = document.createElement("tr");
    if (c.value === null) tr.className = "unscored";
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = row.name;
    tr.append(name, cellFor(c, d.max_value, row.name));

    const basis = document.createElement("td");
    basis.textContent = c.basis || "not scored";
    const on = document.createElement("td");
    on.textContent = c.on || "—";
    if (c.freshness === "stale") on.textContent += ` · ${c.age_days}d ago · stale`;
    else if (c.freshness === "undated") on.textContent = "no date recorded · undated";
    const obs = document.createElement("td");
    obs.className = "obs";
    if (c.note) {
      obs.textContent = c.note;
    } else if (c.value === null) {
      const em = document.createElement("em");
      em.textContent = "Nobody has assessed this. Not a zero.";
      obs.append(em);
    } else {
      obs.textContent = "— no note recorded";
    }
    tr.append(basis, on, obs);
    tbody.append(tr);
  }
  table.append(tbody);
}

function cellOf(row) {
  return row.cells.find((c) => c.axis === axisTab)
      || { axis: axisTab, value: null, basis: null, note: null, on: null, by: null };
}

function renderGrid(d) {
  $("axisnote").hidden = true;
  const table = $("grid");
  table.replaceChildren();
  const empty = $("empty");

  if (!d.rows.length) {
    table.hidden = true;
    empty.hidden = false;
    empty.textContent = `No ${d.kind} nodes to score yet.`;
    return;
  }
  table.hidden = false;
  empty.hidden = true;

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  hr.append(th(d.kind));
  for (const a of d.axes) {
    const cell = th(a.label);
    const share = document.createElement("span");
    share.className = "share";
    share.textContent = `${Math.round(a.share * 100)}% of the weight`;
    cell.append(share);
    hr.append(cell);
  }
  thead.append(hr);
  table.append(thead);

  const tbody = document.createElement("tbody");
  for (const row of d.rows) {
    const tr = document.createElement("tr");
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = row.name;
    tr.append(name);
    for (const c of row.cells) {
      tr.append(cellFor(c, d.max_value, row.name));
    }
    tbody.append(tr);
  }
  table.append(tbody);
}

function th(text) {
  const el = document.createElement("th");
  el.scope = "col";
  el.textContent = text;
  return el;
}

function cellFor(c, max, rowName) {
  const td = document.createElement("td");
  const scored = c.value !== null && c.value !== undefined;
  /* Freshness is its own class beside the basis one, never instead of
     it: a stale measurement is still a measurement somebody took, and a
     cell that dropped the basis would lose the more important half. */
  td.className = `cell ${scored ? c.basis : "none"}` +
    (c.freshness === "stale" || c.freshness === "undated" ? ` ${c.freshness}` : "");

  const n = document.createElement("span");
  n.className = "n";
  /* An em dash, not a 0. A zero is a finding; this is an absence, and a
     view that renders them the same way ranks the unexamined below the
     genuinely bad. */
  n.textContent = scored ? String(c.value) : "—";

  const basis = document.createElement("span");
  basis.className = "basis";
  basis.textContent = scored ? c.basis : "not scored";

  td.append(n, basis);

  /* The word, always -- the stripe above never carries this alone. */
  if (c.freshness === "stale" || c.freshness === "undated") {
    const f = document.createElement("span");
    f.className = "fresh";
    f.textContent = c.freshness === "stale"
      ? `stale · ${c.age_days}d old · does not count`
      : "undated · does not count";
    td.append(f);
  }

  if (scored) {
    td.style.setProperty("--fill", fill(c.value, max));
    td.style.setProperty("--ink", c.value / max > 0.6 ? "var(--on-dark)" : "var(--fg)");
    const bar = document.createElement("span");
    bar.className = "bar";
    const i = document.createElement("i");
    i.style.width = `${(c.value / max) * 100}%`;
    bar.append(i);
    td.append(bar);
  }

  const detail = [
    `${rowName} · ${c.axis}`,
    scored ? `${c.value} of ${max}, ${c.basis}` : "not scored",
    c.on ? `observed ${c.on}` : null,
    c.freshness === "stale" ? `stale: ${c.age_days} days old, no longer counted`
      : c.freshness === "undated" ? "undated: no observation date, so not counted"
      : null,
    c.by ? `by ${c.by}` : null,
    c.note || null,
  ].filter(Boolean).join("\n");
  td.title = detail;
  td.setAttribute("aria-label", detail.replace(/\n/g, ". "));
  return td;
}

function renderComposites(d) {
  const box = $("composites");
  box.replaceChildren();
  if (!d.rows.length) return;
  const h = document.createElement("h2");
  h.textContent = "Composite";
  box.append(h);

  for (const row of d.rows) {
    const div = document.createElement("div");
    div.className = "comp";
    const title = document.createElement("h3");
    title.textContent = row.title || row.name;
    div.append(title);

    if (row.composite === null) {
      const p = document.createElement("p");
      p.className = "withheld";
      const lead = document.createElement("strong");
      lead.textContent = "No composite. ";
      p.append(lead, row.withheld_reason);
      div.append(p);
    } else {
      const v = document.createElement("p");
      v.className = "value";
      v.textContent = `${row.composite} / ${d.max_value}`;
      div.append(v);
      if (row.missing.length) {
        const m = document.createElement("p");
        m.className = "meta";
        m.textContent = `Not counted: ${row.missing.join(", ")}`;
        div.append(m);
      }
    }
    /* The threshold travels with EVERY result, issued or withheld -- a
       default nobody chose is an unstated assumption, and printing it is
       what keeps it from being invisible. On a WITHHELD row the server's
       own reason already carries both figures, so repeating them here
       would be the same sentence twice. */
    if (row.composite !== null) {
      const meta = document.createElement("p");
      meta.className = "meta";
      meta.textContent =
        `${Math.round(row.measured_weight * 100)}% of the axis weight is measured; ` +
        `this project requires ${Math.round(row.threshold * 100)}%.`;
      div.append(meta);
    }
    box.append(div);
  }
}


$("type").onchange = load;
load();

installReport(document.querySelector("header") || document.body, () => ({
  type: $("type").value,
  axis: axisTab || "(grid)",
}));
