/* First run: four steps, each drawn from /api/setup and each an action.
 *
 * The page never decides the order or which step is next -- the server
 * says (`next`), and the page marks steps done, current or waiting
 * from that. After every action it asks again. So a step done by hand
 * on the terminal, or from another tab, shows as done here too. */

const $ = (id) => document.getElementById(id);
const say = (m) => { $("status").textContent = m; };

let setup = null;

async function refresh() {
  const d = await loadJSON("/api/setup", "the setup state", say);
  if (d === null) return;
  setup = d;
  say("");
  render();
}

function mark(id, state) {
  const li = $(id);
  li.classList.remove("done", "current", "waiting");
  li.classList.add(state);
}

function render() {
  const d = setup;
  const order = ["store", "project", "content", "axes"];
  const nextIdx = order.indexOf(d.next);
  order.forEach((step, i) => {
    const id = `step-${step}`;
    if (d.next === "done" || i < nextIdx) mark(id, "done");
    else if (i === nextIdx) mark(id, "current");
    else mark(id, "waiting");
  });
  $("done").hidden = d.next !== "done";
  $("done-nodes").textContent = d.ledger_nodes;
  // The headline is a claim about the map; it changes when the map does.
  // The install's own name, never the package's: the bar already
  // carries whatever the settings screen set.
  const brand = document.querySelector("#bar .brand").textContent.trim() || "Spoke";
  $("headline").textContent = d.next === "done"
    ? `${brand} is set up`
    : d.ledger_nodes ? "Almost there" : "Nothing is on the map yet";

  // step 1
  const r1 = $("step-store").querySelector(".result");
  if (d.store.ok) {
    r1.textContent = `Reading ${d.store.path}.`;
  } else {
    r1.textContent = d.store.error ? `${d.store.error}.` : "";
  }
  const cands = d.store_candidates || [];
  $("store-candidates").hidden = !cands.length;
  $("candidate-list").replaceChildren(...cands.map((c) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "candidate";
    const path = document.createElement("code");
    path.textContent = c.path;
    const n = document.createElement("span");
    n.className = "count";
    n.textContent = `${c.records} record${c.records === 1 ? "" : "s"}`;
    b.append(path, n);
    b.onclick = () => { $("store-path").value = c.path; $("store-form").requestSubmit(); };
    li.append(b);
    return li;
  }));

  // step 2
  const r2 = $("step-project").querySelector(".result");
  if (d.projects.length) {
    const p = d.projects.find((x) => x.name === d.active_project) || d.projects[0];
    r2.textContent = `${p.name}: ${p.repos.length} repo${p.repos.length === 1 ? "" : "s"}` +
      (d.projects.length > 1 ? ` (${d.projects.length} projects registered)` : "") + ".";
  } else {
    r2.textContent = "";
  }

  // step 3
  const r3 = $("step-content").querySelector(".result");
  r3.textContent = d.ledger_nodes ? `${d.ledger_nodes} node${d.ledger_nodes === 1 ? "" : "s"} on the map.` : "";

  // step 4
  const r4 = $("step-axes").querySelector(".result");
  r4.textContent = d.axes ? `${d.axes} ax${d.axes === 1 ? "is" : "es"} declared for ${d.active_project}.` : "";
  if (d.active_project) {
    $("axes-cmd").textContent =
      `spoke projects axes --project ${d.active_project} --set 'reliability=Reliability:30'`;
  }
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === "string" ? detail : `${res.status}`);
  }
  return payload;
}

$("store-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const result = $("step-store").querySelector(".result");
  const path = $("store-path").value.trim();
  if (!path) { result.textContent = "A path is needed."; return; }
  // The settings endpoint is the ONE writer of config.toml; this page
  // sends the same shape the Settings screen does, with the file's
  // other values carried through unchanged.
  let file = {};
  try { file = (await loadJSON("/api/config", "the config", say) || {}).file || {}; } catch (_) { /* fresh */ }
  try {
    await post("/api/config", {
      store_path: path,
      accepted_absences: file.accepted_absences || [],
      stale_days: file.stale_days || 30,
      llm_provider: file.llm_provider || "groq",
      llm_base_url: file.llm_base_url || null,
      llm_model: file.llm_model || null,
      board_stylesheet: file.board_stylesheet || null,
      brand_name: file.brand_name || null,
      brand_author: file.brand_author || null,
      brand_author_url: file.brand_author_url || null,
      brand_logo_url: file.brand_logo_url || null,
      brand_tagline: file.brand_tagline || null,
      ntfy_topic: file.ntfy_topic || null,
      ntfy_server: file.ntfy_server || null,
    });
    result.textContent = "Saved.";
  } catch (e) {
    result.textContent = `Not saved: ${e.message}`;
  }
  refresh();
};

$("project-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const result = $("step-project").querySelector(".result");
  const name = $("project-name").value.trim();
  const repos = $("project-repos").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const r = await post("/api/projects", { name, repos });
    result.textContent = `${r.verb}: ${r.name} — ${r.repos.join(", ")}`;
  } catch (e) {
    result.textContent = `Not registered: ${e.message}`;
  }
  refresh();
};

async function scan(write) {
  const result = $("step-content").querySelector(".result");
  const out = $("scan-out");
  const buttons = [$("scan-preview"), $("scan-write")];
  buttons.forEach((b) => { b.disabled = true; });
  result.textContent = write ? "Scanning and writing…" : "Scanning…";
  out.hidden = true;
  try {
    const r = await post("/api/scan", { write });
    const c = r.counts;
    result.textContent = write
      ? `${c.written} written, ${c.unchanged} unchanged, ${c.diverged} left alone, ${c.withdrawn} withdrawn` +
        (c.refused ? `, ${c.refused} refused` : "") + "."
      : `${c.proposals} proposals: ${c.written} would be written, ${c.unchanged} already match, ${c.diverged} left alone.`;
    const text = [...r.lines, ...r.notes.map((n) => `note: ${n}`)].join("\n");
    out.textContent = text || "(nothing to show)";
    out.hidden = false;
  } catch (e) {
    result.textContent = `Scan did not run: ${e.message}`;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
  if (write) refresh();
}

$("scan-preview").onclick = () => scan(false);
$("scan-write").onclick = () => scan(true);

// The third door. Deliberately does NOT re-point this server at what it
// builds: the demo carries its own store, registry and config, and
// rewriting the installer's config to show them a sample would be the
// tool helping itself to the thing it is asking permission for.
$("demo-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const result = $("step-demo").querySelector(".result");
  const out = $("demo-out");
  const button = $("demo-form").querySelector("button");
  const dir = $("demo-dir").value.trim();
  if (!dir) { result.textContent = "A folder is needed."; return; }
  button.disabled = true;
  out.hidden = true;
  result.textContent = "Cloning two repositories and building the map…";
  try {
    const r = await post("/api/demo", { dir });
    result.textContent = `Built under ${r.root}: ${r.nodes} nodes on the map, ` +
      `from ${r.scanned.proposals} proposals the scan made.`;
    $("demo-cmd").textContent = r.serve_command;
    out.hidden = false;
  } catch (e) {
    result.textContent = `The sample map was not built: ${e.message}`;
  } finally {
    button.disabled = false;
  }
};

refresh();

installReport($("bar"), () => ({ view: "setup", next: setup ? setup.next : "unknown" }));
