/* Records: the memory files beside the ledger, read and corrected here.
 *
 * The rail lists every record with its hook -- the index line's tail,
 * the first thing a session reads about it. The main pane shows one
 * record as it is on disk beside the version being proposed, and the
 * save goes through the store's gate and commits in one step. */

const $ = (id) => document.getElementById(id);
const say = (text, cls = "") => { $("status").textContent = text; $("status").className = cls; };

let rows = [];
let current = null;      // name of the open record
let loadedBody = null;   // what Current showed when it was opened

function hookOf(r) {
  // "- [Title](file.md) — hook" -> "hook"; the description otherwise.
  const line = r.index_line || "";
  const i = line.indexOf(" — ");
  return i >= 0 ? line.slice(i + 3).trim() : (r.description || "");
}

async function loadList() {
  rows = await loadJSON("/api/records", "the records", say) || [];
  renderList();
}

function renderList() {
  const q = $("filter").value.trim().toLowerCase();
  const shown = rows.filter((r) => !q || r.name.toLowerCase().includes(q) || hookOf(r).toLowerCase().includes(q));
  $("count").textContent = q ? `${shown.length} of ${rows.length}` : String(rows.length);
  $("nomatch").hidden = shown.length > 0;
  $("list").replaceChildren(...shown.map((r) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "listitem");
    const name = document.createElement("span"); name.className = "name"; name.textContent = r.name;
    const hook = document.createElement("span"); hook.className = "hook"; hook.textContent = hookOf(r);
    b.append(name, hook);
    if (r.name === current) b.setAttribute("aria-current", "true");
    b.onclick = () => open(r.name);
    return b;
  }));
}

function dirty() {
  return current !== null && $("proposed").value !== loadedBody;
}

async function open(name) {
  if (dirty() && !confirm("Discard the unsaved change to " + current + "?")) return;
  const r = await loadJSON(`/api/records/${encodeURIComponent(name)}`, "the record", say);
  if (r === null) return;
  current = name;
  loadedBody = r.body;
  $("empty").hidden = true;
  $("record").hidden = false;
  $("title").textContent = name;
  $("hook").textContent = hookOf(r);
  $("hook").hidden = !hookOf(r);
  const desc = (r.meta && r.meta.description) || "";
  $("description").textContent = desc;
  $("description").hidden = !desc || desc === hookOf(r);
  $("current").textContent = r.body;
  $("proposed").value = r.body;
  say("");
  setAdvisories([]);
  reflectEdit();
  renderList();
}

function reflectEdit() {
  const d = dirty();
  $("save").disabled = !d;
  $("revert").disabled = !d;
  $("editstate").textContent = d ? "edited — not saved" : "unchanged";
  $("editstate").classList.toggle("dirty", d);
}

function setAdvisories(advisories) {
  const el = $("advisories");
  el.replaceChildren(...(advisories || []).map((a) => {
    const li = document.createElement("li");
    li.textContent = `${a.file}: ${a.kind} — ${a.detail}`;
    return li;
  }));
  el.hidden = !(advisories && advisories.length);
}

async function save() {
  if (!dirty()) return;
  const body = $("proposed").value;
  // Disabled for the duration of the request so a double-click (or a
  // second click before the response lands) cannot fire two writes.
  $("save").disabled = true;
  try {
    const res = await fetch(`/api/records/${encodeURIComponent(current)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body, expected_body: loadedBody }),
    });
    if (res.ok) {
      const j = await res.json();
      say(`Saved and committed ${j.commit.slice(0, 8)}.`, "ok");
      setAdvisories(j.advisories);
      $("current").textContent = body;
      loadedBody = body;
    } else if (res.status === 409) {
      // The record changed on disk since it was loaded. Do NOT discard
      // what the user typed -- that would be a second data-loss bug
      // stacked on the one expected_body exists to prevent.
      say("Not saved: this record changed on disk since you opened it. Your edit is kept here; open the record again to see the new Current, then re-apply it.", "warn");
      setAdvisories([]);
    } else {
      let detail = `${res.status}`;
      try { const j = await res.json(); detail = Array.isArray(j.detail) ? j.detail.join("; ") : String(j.detail); } catch (_) { /* not JSON */ }
      say(`Refused — ${detail}. The file is unchanged.`, "bad");
      setAdvisories([]);
    }
  } finally {
    reflectEdit();
  }
}

$("save").onclick = save;
$("revert").onclick = () => { $("proposed").value = loadedBody; say(""); reflectEdit(); };
$("proposed").oninput = reflectEdit;
$("filter").oninput = renderList;
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
});
window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

loadList();

installReport($("bar"), () => ({ view: "records", record: current || "(none)", edited: dirty() }));
