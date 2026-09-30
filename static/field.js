// EdgeMind crew field app (phones). Served by the hub at /field/<node> (API via the hub's proxy)
// or by a remote edge node itself at / (same origin). Live over SSE from the node.
const BASE = window.EM_BASE ?? "";
const HUB = !!window.EM_HUB;
let ST = null, TAB = "ask", KIND = "fix", seen = new Set(), PHOTO = null, FILTER = null, PHOTO_Q = null;
const FEED = [];

// ================================================================== crew picker (hub /m)
async function picker() {
  $("#picker").classList.remove("hidden");
  const render = async () => {
    try {
      const cfg = await api("/api/config");
      $("#crews").innerHTML = cfg.nodes.map((n) => `<li><a class="crew-card" style="--c:${devColor(n.id)}" href="/field/${encodeURIComponent(n.id)}">
        <span class="cc-dot"></span>
        <div class="cc-text"><b>${esc(n.label)}</b><span class="muted">${n.sites.map((x) => x[0].toUpperCase() + x.slice(1)).map(esc).join(", ")}</span></div>
        <span class="pill ${n.live ? "ok" : n.running ? "warn" : "bad"}">${n.live ? "Live" : n.running ? "No uplink" : "Offline"}</span>
        <span class="go">${ICON.chevron}</span></a></li>`).join("") || `<li class="empty">The hub is starting…</li>`;
    } catch { $("#crews").innerHTML = `<li class="empty">Hub not reachable</li>`; }
  };
  render();
  setInterval(render, 3000);
}

// ================================================================== crew app
function show(tab) {
  TAB = tab;
  document.querySelectorAll(".fnav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".fpane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab));
  refresh();
}

function render(st) {
  ST = st;
  document.title = `EdgeMind · ${st.label}`;
  document.body.style.setProperty("--dev", devColor(st.id));
  document.body.classList.toggle("offline", !st.online);
  $("#crew-name").textContent = st.label;
  $("#crew-sites").textContent = st.sites.map((x) => x[0].toUpperCase() + x.slice(1)).join(", ");
  $("#uplink").checked = st.online;
  const [cls, label] = !st.online ? ["bad", "Offline · working locally"] : st.live ? ["ok", "Live"] :
    st.connected ? ["ok", "Synced"] : ["warn", "No cloud · working locally"];
  $("#conn").className = `pill ${cls}`; $("#conn").textContent = label;
  syncLine();
  const s = st.stats;
  const review = s.conflicts + s.contradictions + s.suggestions;
  $("#rv-badge").textContent = review || "";
  const kinds = s.kinds || {};
  const chips = [["", "All", s.memories]].concat(Object.entries(kinds).sort((a, b) => b[1] - a[1]).map(([k, n]) => [k, k, n]));
  $("#kind-chips").innerHTML = chips.map(([k, label, n]) =>
    `<button class="fchip ${(FILTER || "") === k ? "active" : ""}" data-k="${esc(k)}">${esc(label)} <span class="muted">${n}</span></button>`).join("") +
    (s.photos ? `<span class="muted small">${s.photos} photos</span>` : "");
  $("#kind-chips").querySelectorAll("[data-k]").forEach((b) => b.addEventListener("click", () => { FILTER = b.dataset.k || null; PHOTO_Q = null; render(ST); refresh(); }));
  if (!$("#f-site").options.length) {
    $("#f-site").innerHTML = st.sites.map((k) => `<option>${esc(k)}</option>`).join("");
    $("#f-kind").innerHTML = st.kinds.map((k) => `<button type="button" data-k="${esc(k)}" class="${k === KIND ? "active" : ""}">${esc(k)}</button>`).join("");
    $("#f-kind").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      KIND = b.dataset.k;
      $("#f-kind").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      preview();
    }));
  }
}

function syncLine() {
  if (!ST) return;
  const s = ST.stats, ls = ST.last_sync || {};
  const q = s.outbox ? `${s.outbox} waiting to upload · ` : "";
  const when = ls.at ? `synced ${ago(ls.at)}` : "not synced yet";
  $("#sync-line").textContent = `${q}${s.memories} memories · ${when}`;
}

async function refresh() {
  try {
    if (TAB === "memory") await memory();
    else if (TAB === "review") {
      const box = $("#review");
      box.innerHTML = reviewHtml(await api(`${BASE}/review`));
      bindReview(box, BASE, refresh);
    } else if (TAB === "activity") {
      $("#feed").innerHTML = FEED.slice(0, 80).map((l) => logHtml(l, false)).join("") || `<li class="empty">Nothing yet.</li>`;
    }
  } catch { /* node busy / down */ }
}
const refreshSoon = debounce(refresh, 300);

async function memory() {
  const q = $("#find").value.trim();
  let docs, meta = "", scored = false;
  if (PHOTO_Q) {
    const r = await api(`${BASE}/search/photo`, { method: "POST", body: { photo: PHOTO_Q, limit: 8 }, timeout: 60000 });
    docs = r.results; scored = true;
    meta = `${r.results.length} visually similar · ${r.latency_ms} ms on the node (CLIP, offline) · clear the search to go back`;
  } else if (q) {
    const r = await api(`${BASE}/search`, { method: "POST", body: { q, limit: 10, kind: FILTER }, timeout: 30000 });
    docs = r.results; scored = true;
    meta = `${r.results.length} results · ${r.latency_ms} ms on the node · ${r.shards} Qdrant Edge shards · hybrid RRF + recency` +
      (r.miss ? " · nothing close: try Ask → Ask the fleet" : "");
  } else {
    docs = await api(`${BASE}/memory`);
    if (FILTER) docs = docs.filter((d) => d.kind === FILTER);
    meta = `${docs.length} memories on this node`;
  }
  $("#find-meta").textContent = meta;
  const first = seen.size === 0;
  $("#mem").innerHTML = docs.map((d) => itemHtml(d, { why: !scored, score: scored, fresh: !first && !scored && !seen.has(d.doc_id + d.updated_at) })).join("")
    || `<li class="empty">Nothing here.</li>`;
  bindThumbs($("#mem"), BASE);
  docs.forEach((d) => seen.add(d.doc_id + d.updated_at));
  $("#mem").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const doc = docs.find((x) => x.doc_id === b.dataset.id);
    if (b.dataset.act === "edit") return edit(doc);
    if (!confirm(`Delete "${doc.title}"?`)) return;
    await api(`${BASE}/memory/${doc.doc_id}`, { method: "DELETE" }).catch((e) => toast(e.message, "bad"));
    refresh();
  }));
}

function edit(doc) {
  $("#f-id").value = doc.doc_id; $("#f-title").value = doc.title; $("#f-text").value = doc.text;
  KIND = doc.kind; $("#f-site").value = doc.site; $("#f-site").disabled = true; $("#f-vis").value = doc.visibility || "auto";
  $("#f-kind").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x.dataset.k === KIND));
  $("#save").textContent = "Save edit"; $("#cancel-edit").classList.remove("hidden");
  show("add"); preview();
}
function setPhoto(dataUrl) {
  PHOTO = dataUrl;
  $("#f-photo-preview").classList.toggle("hidden", !dataUrl);
  $("#f-photo-clear").classList.toggle("hidden", !dataUrl);
  if (dataUrl) $("#f-photo-preview").src = dataUrl;
}
function clearForm() {
  setPhoto(null);
  $("#f-id").value = ""; $("#f-title").value = ""; $("#f-text").value = ""; $("#f-vis").value = "auto"; $("#f-site").disabled = false;
  $("#save").textContent = "Save on node"; $("#cancel-edit").classList.add("hidden"); $("#policy").innerHTML = "";
}

const preview = debounce(async () => {
  const text = `${$("#f-title").value} ${$("#f-text").value}`.trim();
  if (!text) { $("#policy").innerHTML = ""; return; }
  try {
    const d = await api(`${BASE}/policy/preview`, { method: "POST", body: { text, kind: KIND, visibility: $("#f-vis").value } });
    $("#policy").innerHTML = policyHtml(d);
  } catch { /* ignore */ }
}, 300);

async function ask(q) {
  const box = $("#answer");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted">Searching this node's memory…</p>`;
  try {
    const a = await api(`${BASE}/ask`, { method: "POST", body: { q }, timeout: 90000 });
    box.innerHTML = answerHtml(a);
    if (a.llm) streamAnswer(box, BASE, q);
    const af = box.querySelector(".ask-fleet");
    af.addEventListener("click", async () => {
      const r = await api(`${BASE}/ask-fleet`, { method: "POST", body: { q } });
      af.replaceWith(Object.assign(document.createElement("span"), {
        textContent: r.queued ? "Queued: the fleet hears it when signal returns." : "Sent to the fleet. Crews with a matching private note get asked to share it." }));
    });
  } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

function onEvent(ev) {
  if (!ev.state) return;
  render(ev.state);
  if (ev.log?.length) {
    for (const l of ev.log) {
      if (FEED.some((x) => x.id === l.id)) continue;
      FEED.push(l);
      const n = !ev.first && notable(l);
      if (n) toast(n[0], n[1]);
    }
    FEED.sort((a, b) => b.ts - a.ts);
    if (FEED.length > 150) FEED.length = 150;
  }
  refreshSoon();
}

function crewApp() {
  $("#app").classList.remove("hidden");
  if (HUB) $("#back").classList.remove("hidden");
  document.querySelectorAll(".fnav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#q").value.trim(); if (q) ask(q); });
  $("#samples").querySelectorAll(".chip-btn").forEach((b) => b.addEventListener("click", () => { $("#q").value = b.textContent; ask(b.textContent); }));
  $("#find").addEventListener("input", debounce(() => { PHOTO_Q = null; refresh(); }, 250));
  $("#f-photo").addEventListener("change", async (e) => {
    const f = e.target.files[0]; e.target.value = "";
    if (f) { try { setPhoto(await readPhoto(f)); } catch (err) { toast(err.message, "bad"); } }
  });
  $("#f-photo-clear").addEventListener("click", () => setPhoto(null));
  $("#find-photo").addEventListener("change", async (e) => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    try { PHOTO_Q = await readPhoto(f, 800); $("#find").value = ""; $("#find-meta").textContent = "Comparing photos on the node…"; await refresh(); }
    catch (err) { toast(err.message, "bad"); }
  });
  $("#add-form").addEventListener("input", preview);
  $("#f-vis").addEventListener("change", preview);
  $("#cancel-edit").addEventListener("click", clearForm);
  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { title: $("#f-title").value, text: $("#f-text").value, kind: KIND, site: $("#f-site").value,
      visibility: $("#f-vis").value, doc_id: $("#f-id").value || null, photo: PHOTO };
    if (!body.text.trim() && !PHOTO) return toast("Write something or add a photo");
    $("#save").disabled = true;
    try {
      const doc = await api(`${BASE}/memory`, { method: "POST", body, timeout: 30000 });
      toast(`Saved · ${STATE_LABEL[doc.sync_state] || doc.sync_state}` + (doc.contradicts?.length ? " · Discrepancy with fleet memory" : ""),
        doc.contradicts?.length ? "warn" : "ok");
      clearForm();
      show(doc.contradicts?.length ? "review" : "memory");
    } catch (err) { toast(err.message, "bad"); }
    $("#save").disabled = false;
  });
  $("#uplink").addEventListener("change", async (e) => {
    const on = e.target.checked;
    try {
      const r = await api(`${BASE}/online`, { method: "POST", body: { value: on }, timeout: 60000 });
      if (r.sync) toast(r.sync.ok ? `Signal back · uploaded ${r.sync.pushed}, received ${r.sync.pulled}` : r.sync.error, r.sync.ok ? "ok" : "warn");
    } catch (err) { toast(err.message, "bad"); }
  });
  let polledOnce = false;
  liveStream(`${BASE}/events`, onEvent, (on) => { if (!on) { $("#conn").className = "pill bad"; $("#conn").textContent = "Node not reachable"; } },
    async () => {  // fallback when a proxy buffers the stream (e.g. a public tunnel)
      const [st, log] = await Promise.all([api(`${BASE}/state`, { timeout: 6000 }), api(`${BASE}/log?limit=40`, { timeout: 6000 })]);
      const ev = { state: st, log, first: !polledOnce };
      polledOnce = true;
      return [ev];
    });
  setInterval(syncLine, 1000);
}

paintIcons();
if (HUB && !window.EM_DEV) picker(); else crewApp();
