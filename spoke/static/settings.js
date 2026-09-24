const FIELDS = ["store_path", "stale_days", "accepted_absences", "llm_provider", "llm_model", "llm_base_url", "board_stylesheet", "brand_name", "brand_author", "brand_author_url", "brand_logo_url", "brand_tagline", "ntfy_topic", "ntfy_server"];

function clearFieldErrors() {
  for (const name of FIELDS) {
    const wrap = document.getElementById(`field-${name}`);
    wrap.classList.remove("has-error");
    wrap.querySelector(".field-error").textContent = "";
  }
}

function showFieldError(name, message) {
  const wrap = document.getElementById(`field-${name}`);
  if (!wrap) return false;
  wrap.classList.add("has-error");
  wrap.querySelector(".field-error").textContent = message;
  return true;
}

function setStatus(text, cls) {
  const el = document.getElementById("save-status");
  el.textContent = text;
  el.className = cls || "";
}

function showOverrideNotices(data) {
  const overridden = data.overridden || {};
  const effective = data.effective || {};
  for (const name of FIELDS) {
    const wrap = document.getElementById(`field-${name}`);
    if (!wrap) continue;
    let notice = wrap.querySelector(".env-override-notice");
    const envVar = overridden[name];
    if (envVar) {
      if (!notice) {
        notice = document.createElement("p");
        notice.className = "env-override-notice";
        wrap.appendChild(notice);
      }
      notice.textContent =
        `Currently overridden by ${envVar}=${effective[name]} -- this setting will not ` +
        `take effect until that variable is unset. The value below is what is saved in ` +
        `the file and what you are editing.`;
    } else if (notice) {
      notice.remove();
    }
  }
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  document.getElementById("config-path").textContent =
    data.config_path || "(no config file path known -- settings cannot be saved from this page)";
  // The form edits and POSTs the FILE values only (FIX 1) -- never the
  // effective, possibly env-overridden values -- so a save can never bake
  // a temporary env override into config.toml by accident.
  const file = data.file || {};
  document.getElementById("store_path").value = file.store_path || "";
  document.getElementById("stale_days").value = file.stale_days ?? "";
  document.getElementById("accepted_absences").value = (file.accepted_absences || []).join("\n");
  document.getElementById("llm_provider").value = file.llm_provider || "";
  /* The provider is a CHOICE, not free text -- it used to be a text box
     for a setting that did nothing, so a typo produced no error and no
     effect. The list comes from the server, which is where the providers
     are defined. */
  const providers = document.getElementById("llm_provider");
  providers.replaceChildren(...(data.llm_providers || []).map((name) => {
    const o = document.createElement("option");
    o.value = o.textContent = name;
    o.selected = name === file.llm_provider;
    return o;
  }));
  document.getElementById("llm_model").value = file.llm_model || "";
  document.getElementById("llm_base_url").value = file.llm_base_url || "";

  /* Presence, never the value. */
  const state = document.getElementById("key-state");
  if (state) {
    state.textContent = data.llm_credential_present
      ? "set in this environment"
      : "not set \u2014 the contradiction pass will refuse rather than skip";
  }
  document.getElementById("board_stylesheet").value = file.board_stylesheet || "";
  document.getElementById("brand_name").value = file.brand_name || "";
  document.getElementById("brand_author").value = file.brand_author || "";
  document.getElementById("brand_author_url").value = file.brand_author_url || "";
  document.getElementById("brand_logo_url").value = file.brand_logo_url || "";
  document.getElementById("brand_tagline").value = file.brand_tagline || "";
  document.getElementById("ntfy_topic").value = file.ntfy_topic || "";
  document.getElementById("ntfy_server").value = file.ntfy_server || "";
  showOverrideNotices(data);
  if (!data.config_path) {
    document.getElementById("save-settings").disabled = true;
  }
}

document.getElementById("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearFieldErrors();
  setStatus("", "");
  const saveButton = document.getElementById("save-settings");
  saveButton.disabled = true;
  try {
    const absences = document
      .getElementById("accepted_absences")
      .value.split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    const body = {
      store_path: document.getElementById("store_path").value.trim(),
      stale_days: Number(document.getElementById("stale_days").value),
      accepted_absences: absences,
      llm_provider: document.getElementById("llm_provider").value.trim(),
      llm_model: document.getElementById("llm_model").value.trim() || null,
      llm_base_url: document.getElementById("llm_base_url").value.trim() || null,
      board_stylesheet: document.getElementById("board_stylesheet").value.trim() || null,
      brand_name: document.getElementById("brand_name").value.trim() || null,
      brand_author: document.getElementById("brand_author").value.trim() || null,
      brand_author_url: document.getElementById("brand_author_url").value.trim() || null,
      brand_logo_url: document.getElementById("brand_logo_url").value.trim() || null,
      brand_tagline: document.getElementById("brand_tagline").value.trim() || null,
      ntfy_topic: document.getElementById("ntfy_topic").value.trim() || null,
      ntfy_server: document.getElementById("ntfy_server").value.trim() || null,
    };
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const j = await res.json();
      // The store is resolved on every request now, so a changed path
      // is read at once -- "Saved." is the whole truth.
      setStatus("Saved.", "ok");
      await loadConfig();
    } else {
      const j = await res.json();
      const detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      // The server names the offending field as "field: reason". Route the
      // message onto that field's own error line when we recognize the
      // field name; otherwise show it as a general refusal so nothing is
      // silently dropped.
      const idx = detail.indexOf(":");
      const maybeField = idx > -1 ? detail.slice(0, idx).trim() : "";
      if (FIELDS.includes(maybeField) && showFieldError(maybeField, detail.slice(idx + 1).trim())) {
        setStatus(`Not saved -- ${maybeField} was refused.`, "bad");
      } else {
        setStatus(`Not saved -- ${detail}`, "bad");
      }
    }
  } finally {
    saveButton.disabled = false;
  }
});

loadConfig();

installReport(document.getElementById("bar"), () => ({ view: "settings" }));

/* ── capability axes ────────────────────────────────────────────────
 * The axes live in the project registry, not config.toml, so this is a
 * separate form with its own save. Rows are kept in the order shown --
 * that is the reading order of the scale -- and the share column is
 * recomputed as weights change, because the share is what the
 * composite actually uses. */
const axesRows = () => [...document.querySelectorAll("#axes-rows tr")];

function axesRead() {
  return axesRows().map((tr) => ({
    key: tr.querySelector("input.key").value.trim(),
    label: tr.querySelector("input.label").value.trim(),
    weight: Number(tr.querySelector("input.weight").value) || 0,
  }));
}

function axesShares() {
  const rows = axesRows();
  const read = axesRead();
  const total = read.reduce((a, x) => a + Math.max(0, x.weight), 0) || 1;
  const keys = read.map((x) => x.key);
  rows.forEach((tr, i) => {
    tr.querySelector("td.share").textContent = read[i].weight > 0
      ? `${Math.round((read[i].weight / total) * 100)}%` : "—";
    tr.classList.toggle("dup", !!read[i].key && keys.indexOf(read[i].key) !== i);
  });
}

function axesRow(a) {
  const tr = document.createElement("tr");
  const cell = (cls, value, type = "text", extra = {}) => {
    const td = document.createElement("td");
    const input = document.createElement("input");
    input.className = cls; input.type = type; input.value = value;
    Object.assign(input, extra);
    input.oninput = axesShares;
    td.append(input);
    return td;
  };
  tr.append(
    cell("key", a.key || "", "text", { placeholder: "reliability", spellcheck: false, autocomplete: "off" }),
    cell("label", a.label || "", "text", { placeholder: "Reliability", autocomplete: "off" }),
    cell("weight", a.weight ?? 1, "number", { min: 0, step: "any" }),
  );
  const share = document.createElement("td"); share.className = "share"; tr.append(share);
  const ops = document.createElement("td"); ops.className = "ops";
  const mk = (label, title, fn) => {
    const b = document.createElement("button"); b.type = "button";
    b.textContent = label; b.title = title; b.onclick = fn; return b;
  };
  ops.append(
    mk("↑", "Move up", () => { const p = tr.previousElementSibling; if (p) p.before(tr); axesShares(); }),
    mk("↓", "Move down", () => { const n = tr.nextElementSibling; if (n) n.after(tr); axesShares(); }),
    mk("Remove", "Remove this axis", () => { tr.remove(); axesShares(); }),
  );
  tr.append(ops);
  return tr;
}

async function axesLoad(project) {
  const url = project ? `/api/axes?project=${encodeURIComponent(project)}` : "/api/axes";
  const res = await fetch(url);
  if (!res.ok) return;
  const d = await res.json();
  const none = !d.projects.length;
  document.getElementById("axes-noproject").hidden = !none;
  document.getElementById("axes-form").hidden = none;
  if (none) return;
  const sel = document.getElementById("axes-project");
  sel.replaceChildren(...d.projects.map((p) => {
    const o = document.createElement("option"); o.value = p; o.textContent = p;
    o.selected = p === d.project; return o;
  }));
  document.getElementById("axes-max").value = d.scale_max;
  const body = document.getElementById("axes-rows");
  body.replaceChildren(...d.axes.map(axesRow));
  axesShares();
}

document.getElementById("axes-project").onchange = (ev) => axesLoad(ev.target.value);
document.getElementById("axes-add").onclick = () => {
  document.getElementById("axes-rows").append(axesRow({ key: "", label: "", weight: 1 }));
  axesShares();
  const last = axesRows().at(-1);
  if (last) last.querySelector("input.key").focus();
};

document.getElementById("axes-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const status = document.getElementById("axes-status");
  status.className = ""; status.textContent = "Saving…";
  const body = {
    project: document.getElementById("axes-project").value,
    axes: axesRead(),
    scale_max: Number(document.getElementById("axes-max").value) || 0,
  };
  const res = await fetch("/api/axes", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) {
    status.className = "bad";
    status.textContent = `Not saved: ${(payload && payload.detail) || res.status}`;
    return;
  }
  status.className = "ok";
  const n = payload.axes.length;
  status.textContent = `Saved ${n} ax${n === 1 ? "is" : "es"} on ${payload.project}, scored 0–${payload.scale_max}. The scale reads them now.`;
  axesShares();
};

axesLoad();