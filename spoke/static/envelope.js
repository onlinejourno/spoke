/* What every page says about the SAME envelope.
 *
 * The server builds one envelope (`board._envelope`) and every surface
 * renders the same three things from it: what was skipped, and what went
 * wrong when a request failed. Both pages had their own copy of that,
 * byte for byte, comment for comment.
 *
 * That is not a tidiness complaint. Before the headings came from the
 * payload, the two copies had DRIFTED: one page said every entry in this
 * list was a dangling relation, the other that every entry was
 * unreadable, about the identical field. A page does not get to decide
 * what a defect was, and two copies of the code that says so is how a
 * page starts deciding again.
 *
 * Loaded before each page's own script; no module system, no build step.
 */

/* The skipped list, grouped by KIND, with the heading for each kind
 * taken from the payload rather than authored here. */
function renderSkipped(d) {
  const foot = document.getElementById("skipped");
  const items = d.skipped || [];
  foot.hidden = items.length === 0;
  if (!items.length) return;
  foot.replaceChildren();
  const byKind = new Map();
  for (const s of items) {
    if (!byKind.has(s.kind)) byKind.set(s.kind, []);
    byKind.get(s.kind).push(s);
  }
  for (const [kind, hits] of byKind) {
    const h = document.createElement("strong");
    const noun = hits.length === 1 ? "entry" : "entries";
    h.textContent = `${hits.length} ${noun} ${(d.skip_headings || {})[kind] || kind}:`;
    const ul = document.createElement("ul");
    for (const s of hits) {
      const li = document.createElement("li");
      li.textContent = s.detail;
      ul.append(li);
    }
    foot.append(h, ul);
  }
}

/* Why a request failed, in the server's own words.
 *
 * The API refuses with a `detail` that names the thing -- which lens, which
 * axis, which radius. The scale page used to print the bare status code, so
 * a 422 explaining exactly what was wrong rendered as "422". A tool built to
 * stop absence rendering as assurance must not throw away the one sentence
 * saying what went wrong.
 *
 * Returns the parsed body, or null after reporting the failure via `say`. */
async function loadJSON(url, what, say) {
  let res;
  try {
    res = await fetch(url);
  } catch (e) {
    /* The server being down is a different answer from the server
     * refusing, and saying "could not load" for both hides which. */
    say(`Could not reach the server for ${what}: ${e.message}`);
    return null;
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* body was not JSON */ }
    say(`Could not load ${what}: ${detail}`);
    return null;
  }
  return await res.json();
}

/* Report a problem found IN this surface, FROM this surface.
 *
 * The page captures what it knows about its own state -- lens, zoom,
 * filters, whatever `contextFn` returns -- so the reader types only what
 * they saw. The report becomes an open ledger item through the same gate
 * everything else meets, and the response names the node: a report that
 * vanishes into a store is the failure this tool exists to stop.
 *
 * `bar` is the element the control is appended to; `contextFn` returns a
 * flat {key: value} of strings. */
function installReport(bar, contextFn) {
  const open = document.createElement("button");
  open.type = "button";
  open.className = "report-open";
  open.textContent = "Report a problem";
  bar.append(open);

  const dlg = document.createElement("dialog");
  dlg.className = "report";
  dlg.innerHTML = `
    <h2>What did you find?</h2>
    <p>Say what you saw, in your words. This page records what it knew about
       itself alongside it. It becomes an open item in the ledger and is raised
       at the start of the next session.</p>
    <label class="visually-hidden" for="report-text">What did you find</label>
    <textarea id="report-text" required></textarea>
    <pre class="context" aria-label="What this page knows about itself"></pre>
    <div class="actions">
      <button type="button" class="cancel">Cancel</button>
      <button type="button" class="primary">Record it</button>
    </div>
    <div class="result" aria-live="polite"></div>`;
  document.body.append(dlg);

  const text = dlg.querySelector("textarea");
  const ctxEl = dlg.querySelector(".context");
  const result = dlg.querySelector(".result");
  const submit = dlg.querySelector(".primary");

  open.onclick = () => {
    const ctx = { page: location.pathname, ...(contextFn ? contextFn() : {}) };
    ctxEl.textContent = Object.entries(ctx).map(([k, v]) => `${k}: ${v}`).join("\n");
    result.textContent = "";
    dlg.showModal();
    text.focus();
  };
  dlg.querySelector(".cancel").onclick = () => dlg.close();

  let recorded = false;
  const reset = () => { recorded = false; submit.textContent = "Record it"; text.value = ""; };
  const send = async () => {
    if (recorded) { dlg.close(); reset(); return; }
    const body = {
      page: location.pathname,
      text: text.value,
      context: Object.fromEntries(
        Object.entries(contextFn ? contextFn() : {}).map(([k, v]) => [k, String(v)])),
    };
    submit.disabled = true;
    try {
      const res = await fetch("/api/report", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail = `${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
        result.textContent = `Not recorded: ${detail}`;
        return;
      }
      const d = await res.json();
      result.replaceChildren();
      const strong = document.createElement("strong");
      strong.textContent = "Recorded ";
      const code = document.createElement("code");
      code.textContent = d.name;
      result.append(strong, code, document.createTextNode(` — ${d.raised}`));
      recorded = true;
      submit.textContent = "Done";
    } catch (e) {
      result.textContent = `Could not reach the server: ${e.message}`;
    } finally {
      submit.disabled = false;
    }
  };
  submit.onclick = send;
  dlg.addEventListener("close", reset);
}
