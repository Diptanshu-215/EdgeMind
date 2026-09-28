// EdgeMind dashboard. Plain JS, no build step. Talks directly to each process:
// the console (:8000), the sync gateway (:8100) and each tablet (:8001, :8002).
const STATE_LABEL = {
  synced: "✓ synced", pending: "⏳ queued", local_only: "🔒 device-only", suggested: "★ a crew needs this",
  redacted_synced: "✂ synced redacted", conflict: "⚠ conflict",
};
const DECISION_LABEL = { local: "stays on this tablet", sync: "syncs to the fleet", sync_redacted: "syncs a redacted copy",
  suggest: "suggested for sharing" };
let CFG = null;
const DEV = {}; // id -> { url, el, r, tab, filter, state, seen }
let cloudState = { up: false };

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return new Date(ts * 1000).toLocaleTimeString();
};
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

async function api(url, opts = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeout || 8000);
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" }, method: opts.method || "GET", signal: ctl.signal,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  } finally { clearTimeout(timer); }
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), 3200);
}
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

// ================================================================== device panels
function mountDevice(d) {
  const host = $(`#dev-${d.id}`);
  host.innerHTML = "";
  host.appendChild($("#device-tpl").content.cloneNode(true));
  const r = (c) => $(c, host);
  DEV[d.id] = { ...d, el: host, r, tab: "memory", filter: "all", state: null, seen: new Set(), up: false };
  const dv = DEV[d.id];
  r(".dev-name").textContent = d.label;
  r(".dev-sites").textContent = `${d.id} · receives: ${d.sites.join(" + ")}`;
  host.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(d.id, b.dataset.tab)));
  host.querySelectorAll(".fchip").forEach((b) => b.addEventListener("click", () => {
    dv.filter = b.dataset.f;
    host.querySelectorAll(".fchip").forEach((x) => x.classList.toggle("active", x === b));
    refreshTab(d.id);
  }));
  r(".online").addEventListener("change", async (e) => {
    const on = e.target.checked;
    r(".net-label").textContent = on ? "Online" : "Offline";
    try {
      const res = await api(`${d.url}/online`, { method: "POST", body: { value: on }, timeout: 30000 });
      if (res.sync) toast(res.sync.ok ? `${d.id} back online · ↑${res.sync.pushed} ↓${res.sync.pulled}` +
        (res.sync.conflicts ? ` · ${res.sync.conflicts} conflict` : "") : `${d.id}: ${res.sync.error}`);
    } catch (err) { toast(err.message); }
    refresh();
  });
  r(".sync-now").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const s = await api(`${d.url}/sync`, { method: "POST", timeout: 30000 });
      const bytes = [...s.full, ...s.partial, ...s.points].reduce((a, x) => a + x.bytes, 0);
      toast(s.ok ? `${d.id}: ↑${s.pushed} ↓${s.pulled} · ${kb(bytes)} · ${s.ms} ms` : `${d.id}: ${s.error}`);
    } catch (err) { toast(err.message); }
    e.target.disabled = false; refresh();
  });
  r(".ask").addEventListener("submit", (e) => { e.preventDefault(); ask(d.id); });

  const form = r(".add-form");
  ["input", "change"].forEach((ev) => form.addEventListener(ev, debounce(() => preview(d.id), 300)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { title: r(".f-title").value, text: r(".f-text").value, kind: r(".f-kind").value,
      site: r(".f-site").value, visibility: r(".f-vis").value, doc_id: r(".f-id").value || null };
    if (!body.text.trim()) return toast("Write something first");
    try {
      const doc = await api(`${d.url}/memory`, { method: "POST", body, timeout: 30000 });
      toast(`Saved on ${d.id} · ${STATE_LABEL[doc.sync_state] || doc.sync_state}` +
        (doc.contradicts?.length ? " · ⚠ contradicts a fleet memory" : ""));
      clearForm(d.id);
      showTab(d.id, doc.contradicts?.length ? "review" : "memory");
    } catch (err) { toast(err.message); }
    refresh();
  });
  r(".cancel").addEventListener("click", () => clearForm(d.id));
}

function showTab(id, tab) {
  const dv = DEV[id];
  dv.tab = tab;
  dv.el.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  dv.el.querySelectorAll(".tab").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab));
  refreshTab(id);
}
function clearForm(id) {
  const { r } = DEV[id];
  r(".f-id").value = ""; r(".f-title").value = ""; r(".f-text").value = ""; r(".f-vis").value = "auto";
  r(".f-site").disabled = false; r(".save").textContent = "Save on tablet";
  r(".cancel").classList.add("hidden"); r(".policy-preview").innerHTML = "";
}
function editDoc(id, doc) {
  const { r } = DEV[id];
  r(".f-id").value = doc.doc_id; r(".f-title").value = doc.title; r(".f-text").value = doc.text;
  r(".f-kind").value = doc.kind; r(".f-site").value = doc.site; r(".f-site").disabled = true;
  r(".f-vis").value = doc.visibility || "auto";
  r(".save").textContent = "Save edit"; r(".cancel").classList.remove("hidden");
  showTab(id, "add"); preview(id);
}

async function preview(id) {
  const { r, url } = DEV[id];
  const text = `${r(".f-title").value} ${r(".f-text").value}`.trim();
  if (!text) { r(".policy-preview").innerHTML = ""; return; }
  try {
    const d = await api(`${url}/policy/preview`, { method: "POST", body: { text, kind: r(".f-kind").value, visibility: r(".f-vis").value } });
    const s = d.scores;
    const bar = (label, v) => `<span>${label}</span><span class="bar"><i style="width:${Math.round(v * 100)}%"></i></span><span class="mono">${v.toFixed(2)}</span>`;
    r(".policy-preview").innerHTML =
      `<div><span class="decision">${esc(DECISION_LABEL[d.decision])}</span> · queue priority ${d.priority}</div>` +
      `<div class="bars">${bar("team value", s.team_value)}${bar("fleet demand", s.fleet_demand)}${bar("share score", s.share_score)}</div>` +
      `<div>${d.reasons.map((x) => "• " + esc(x)).join("<br>")}</div>` +
      (d.uploaded_text ? `<div class="redacted">fleet copy: ${esc(d.uploaded_text)}</div>` : "");
  } catch { /* device down: ignore */ }
}

async function ask(id) {
  const { r, url } = DEV[id];
  const q = r(".q").value.trim();
  if (!q) return;
  const box = r(".answer");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted">Thinking on the tablet…</p>`;
  try {
    const a = await api(`${url}/ask`, { method: "POST", body: { q }, timeout: 90000 });
    const text = esc(a.answer).replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>');
    box.innerHTML = `<p>${text}</p>
      <div class="meta"><span class="tag">${esc(a.engine)}</span><span class="tag">search ${a.search_ms} ms</span>
        <span class="tag">answer ${a.ms} ms</span><span class="tag">best match ${a.top_match.toFixed(2)}</span>
        ${DEV[id].state && !DEV[id].state.online ? '<span class="tag warn">offline</span>' : ""}</div>
      <ol>${a.sources.map((s) => `<li>${esc(s.title)}</li>`).join("")}</ol>
      <div class="miss"><span>${a.miss ? "This tablet doesn't really know this." : "Not what you needed?"}</span>
        <button class="btn small go ask-fleet">Ask the fleet</button></div>`;
    const af = box.querySelector(".ask-fleet");
    if (af) af.addEventListener("click", async () => {
      await api(`${url}/ask-fleet`, { method: "POST", body: { q } });
      af.replaceWith(Object.assign(document.createElement("span"), { textContent: "Sent. Other crews' tablets will check their private notes." }));
      refresh();
    });
  } catch (err) { box.innerHTML = `<p class="muted">${esc(err.message)}</p>`; }
}

function itemHtml(d, opts = {}) {
  const fleet = d.layer === "mirror";
  const state = `<span class="st st-${d.sync_state}">${STATE_LABEL[d.sync_state] || esc(d.sync_state)}</span>`;
  const where = `<span class="tag">${fleet ? "fleet mirror" : "this tablet"}</span>`;
  const dup = d.similar_to ? `<span class="tag" title="similarity ${d.similar_to.score}">≈ ${esc(d.similar_to.title)}</span>` : "";
  const contra = d.contradicts?.length ? `<span class="tag warn">⚠ ${esc(d.contradicts[0].mine)} vs ${esc(d.contradicts[0].theirs)}</span>` : "";
  const by = d.author_device && d.author_device !== "hq-console" ? ` · by ${esc(d.author_device)}` : "";
  const why = opts.why && d.policy && !fleet ? `<div class="why">${d.policy.reasons.map(esc).join("<br>")}</div>` : "";
  return `<li class="item ${opts.fresh ? "fresh" : ""}">
    <div class="item-top"><span class="item-title">${esc(d.title)}</span><span class="score">${esc(d.kind)} · ${esc(d.site)}${d.version ? " · v" + d.version : ""}</span></div>
    <div class="item-text">${esc(d.text)}</div>
    <div class="meta">${state}${where}${dup}${contra}<span>${by}</span></div>${why}
    <div class="actions"><button class="btn small" data-act="edit" data-id="${d.doc_id}">Edit</button>
      <button class="btn small danger" data-act="del" data-id="${d.doc_id}">Delete</button></div></li>`;
}

async function refreshTab(id) {
  const dv = DEV[id];
  if (!dv.up) return;
  const { r, url, tab } = dv;
  try {
    if (tab === "memory") {
      let docs = await api(`${url}/memory`);
      if (dv.filter === "own") docs = docs.filter((d) => d.layer === "local");
      if (dv.filter === "fleet") docs = docs.filter((d) => d.layer === "mirror");
      const first = dv.seen.size === 0;
      r(".memory").innerHTML = docs.length ? docs.map((d) => itemHtml(d, { why: true, fresh: !first && !dv.seen.has(d.doc_id + d.updated_at) })).join("")
        : `<li class="empty">Nothing here yet.</li>`;
      docs.forEach((d) => dv.seen.add(d.doc_id + d.updated_at));
      r(".memory").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
        const doc = docs.find((x) => x.doc_id === b.dataset.id);
        if (b.dataset.act === "edit") return editDoc(id, doc);
        if (!confirm(`Delete "${doc.title}"?`)) return;
        await api(`${url}/memory/${doc.doc_id}`, { method: "DELETE" });
        refresh();
      }));
    } else if (tab === "queue") {
      const q = await api(`${url}/outbox`);
      r(".queue").innerHTML = q.length ? q.map((x) => `<li class="item"><div class="item-top">
          <span class="item-title">${esc(x.title)}</span><span class="tag">P${x.priority} · ${esc(x.op)}</span></div>
          <div class="meta"><span class="tag">${esc(x.kind)}</span><span>queued ${ago(x.queued_at)}</span></div></li>`).join("")
        : `<li class="empty">Queue empty: everything shareable is in the fleet.</li>`;
    } else if (tab === "review") {
      renderReview(id, await api(`${url}/review`));
    }
  } catch { /* device down */ }
}

function renderReview(id, rv) {
  const { r, url } = DEV[id];
  const cards = [];
  for (const s of rv.suggestions) {
    const why = (s.policy?.reasons || []).find((x) => x.includes("searched")) || "";
    cards.push(`<div class="rcard suggest"><span class="kind">★ Another crew needs this</span>
      <div><b>${esc(s.title)}</b><div class="clamp">${esc(s.text)}</div></div>
      <div class="muted small">${esc(why)}</div>
      <div class="actions"><button class="btn small primary" data-share="${s.doc_id}">Share with the fleet</button></div></div>`);
  }
  for (const c of rv.conflicts) {
    cards.push(`<div class="rcard conflict"><span class="kind">⚠ Edit conflict</span>
      <div><b>${esc(c.local.title)}</b> <span class="muted small">· you and ${esc(c.cloud.author_device)} both changed it (fleet v${c.cloud.version})</span></div>
      <div class="versions"><div><h4>This tablet</h4>${esc(c.local.text)}</div><div><h4>Fleet · ${esc(c.cloud.author_device)}</h4>${c.cloud.deleted ? "<i>deleted</i>" : esc(c.cloud.text)}</div></div>
      <div class="actions"><button class="btn small" data-s="mine" data-id="${c.doc_id}">Keep mine</button>
        <button class="btn small" data-s="theirs" data-id="${c.doc_id}">Keep theirs</button>
        <button class="btn small primary" data-s="merge" data-id="${c.doc_id}">Merge both</button></div></div>`);
  }
  const hl = (text, raw) => esc(text).replace(esc(raw), `<mark>${esc(raw)}</mark>`);
  for (const c of rv.contradictions) {
    cards.push(`<div class="rcard contra"><span class="kind">⚠ Conflicting information: ${esc(c.detail.mine)} vs ${esc(c.detail.theirs)}</span>
      <div class="versions"><div><h4>${esc(c.doc.title)}</h4>${hl(c.doc.text, c.detail.mine)}</div>
        <div><h4>${esc(c.other.title)}</h4>${hl(c.other.text, c.detail.theirs)}</div></div>
      <div class="muted small">Same topic (${esc(c.detail.about)}), different ${esc(c.detail.unit)} value. Check the SOP before work.</div>
      <div class="actions"><button class="btn small" data-dismiss="${c.doc_id}|${c.other_id}">Checked, dismiss</button></div></div>`);
  }
  const box = r(".review");
  box.innerHTML = cards.join("") || `<div class="empty">Nothing to review.</div>`;
  box.querySelectorAll("[data-share]").forEach((b) => b.addEventListener("click", async () => {
    await api(`${url}/suggestions/${b.dataset.share}/share`, { method: "POST", timeout: 30000 });
    toast("Shared fleet-wide"); refresh();
  }));
  box.querySelectorAll("[data-s]").forEach((b) => b.addEventListener("click", async () => {
    await api(`${url}/conflicts/${b.dataset.id}/resolve`, { method: "POST", body: { strategy: b.dataset.s } });
    toast(`Resolved: ${b.dataset.s}`); refresh();
  }));
  box.querySelectorAll("[data-dismiss]").forEach((b) => b.addEventListener("click", async () => {
    const [doc_id, other_id] = b.dataset.dismiss.split("|");
    await api(`${url}/contradictions/dismiss`, { method: "POST", body: { doc_id, other_id } });
    refresh();
  }));
}

function renderDevice(id, st) {
  const dv = DEV[id];
  const { r, el } = dv;
  dv.state = st;
  el.classList.toggle("offline", !st.online);
  r(".down").classList.add("hidden");
  r(".online").checked = st.online;
  r(".net-label").textContent = st.online ? "Online" : "Offline";
  const conn = r(".conn");
  conn.className = "pill conn " + (st.connected ? "ok" : st.online ? "warn" : "bad");
  conn.textContent = st.connected ? "● connected" : st.online ? "● cloud unreachable" : "● offline: on-device only";
  const ls = st.last_sync || {};
  const how = ls.partial?.length ? `partial snapshot ${kb(ls.partial.reduce((a, x) => a + x.bytes, 0))}` :
    ls.full?.length ? `full snapshot ${kb(ls.full.reduce((a, x) => a + x.bytes, 0))}` :
    ls.points?.length ? "point sync" : "";
  r(".last-sync").textContent = ls.at ? `last sync ${ago(ls.at)}${ls.ok ? (how ? " · " + how : "") : ` · ${ls.error}`}` : "not synced yet";
  const s = st.stats;
  r(".s-mem").textContent = s.memories;
  r(".s-queue").textContent = s.outbox;
  r(".s-local").textContent = s.by_state.local_only || 0;
  const review = s.conflicts + s.contradictions + s.suggestions;
  r(".s-review").textContent = review;
  r(".s-review-box").classList.toggle("hot", review > 0);
  r(".review-badge").textContent = review || "";
  if (!r(".f-kind").options.length) {
    r(".f-kind").innerHTML = st.kinds.map((k) => `<option>${k}</option>`).join("");
    r(".f-site").innerHTML = st.sites.map((k) => `<option>${k}</option>`).join("");
  }
  const flow = $(`#flow-${id.slice(-1)}`);
  if (flow) flow.className = "flow-line " + (st.connected ? "on" : "off");
}

function deviceDown(id) {
  const dv = DEV[id];
  dv.up = false;
  dv.r(".down").classList.remove("hidden");
  const flow = $(`#flow-${id.slice(-1)}`);
  if (flow) flow.className = "flow-line off";
}

// ================================================================== cloud column + metrics
async function renderCloud() {
  const gw = CFG.gateway;
  let fleet = null;
  try { fleet = await api(`${gw}/fleet`, { timeout: 3000 }); } catch { fleet = null; }
  cloudState.up = !!fleet;
  $("#cloud").classList.toggle("down", !fleet);
  $("#cloud-pill").className = "pill " + (fleet ? "ok" : "bad");
  $("#cloud-pill").textContent = fleet ? "● up" : "● down";
  $("#cloud-toggle").textContent = fleet ? "Stop cloud" : "Start cloud";
  $("#cloud-toggle").className = "btn " + (fleet ? "danger-outline" : "go");
  $("#m-cloud").textContent = fleet ? fleet.count : "down";
  if (!fleet) {
    $("#cloud-mode").textContent = "Server processes stopped. Tablets keep working from their own shards.";
    $("#cloud-list").innerHTML = `<li class="empty">Cloud is down. Write and search on the tablets: changes queue up.</li>`;
    $("#heads").innerHTML = ""; $("#demand-box").classList.add("hidden");
    return [];
  }
  $("#cloud-mode").textContent = fleet.mode + (fleet.snapshots ? " · devices sync by snapshots" : "");
  $("#heads").innerHTML = Object.entries(fleet.heads).map(([s, h]) => `<span class="tag">${esc(s)} · seq ${h}</span>`).join("");
  $("#demand-box").classList.toggle("hidden", !fleet.demand.length);
  $("#demand").innerHTML = fleet.demand.map((d) => `<li><b>${esc(d.device)}</b>: “${esc(d.query)}” <span class="muted">${ago(d.ts)}</span></li>`).join("");
  const [docs, glog] = await Promise.all([api(`${gw}/memory`), api(`${gw}/log?limit=40`)]);
  $("#cloud-count").textContent = docs.length;
  $("#cloud-list").innerHTML = docs.map((d) => `<li class="item"><div class="item-top">
      <span class="item-title">${esc(d.title)}</span><span class="tag">v${d.version}</span></div>
      <div class="item-text clamp">${esc(d.text)}</div>
      <div class="meta"><span class="tag">${esc(d.site)}</span><span class="tag">${esc(d.kind)}</span>
      ${d.redacted ? '<span class="st st-redacted_synced">✂ redacted</span>' : ""}<span>by ${esc(d.author_device)}</span></div></li>`).join("")
    || `<li class="empty">Empty</li>`;
  return glog.map((l) => ({ ts: l.ts, device: "cloud", level: l.event === "conflict" ? "warn" : "info", event: l.event,
    detail: `${l.device !== "hq-console" ? l.device + ": " : ""}${l.detail}` }));
}

async function renderMetrics() {
  const a = Object.values(DEV)[0];
  if (!a?.up) return;
  try {
    const m = await api(`${a.url}/metrics`);
    $("#m-search").textContent = m.search_p50_ms != null ? `${m.shard_p50_ms} · ${m.shard_p95_ms} ms` : "search to measure";
    const ls = m.last_sync || {};
    const part = (ls.partial || [])[0];
    if (part) {
      $("#m-sync").textContent = `${kb(part.bytes)}${part.full_bytes ? " of " + kb(part.full_bytes) : ""}`;
      $("#m-sync-label").textContent = "partial snapshot vs full shard (crew A)";
    } else if (m.bytes_full_total) {
      $("#m-sync").textContent = kb(m.bytes_full_total);
      $("#m-sync-label").textContent = "full snapshots so far (crew A)";
    } else {
      $("#m-sync").textContent = m.bytes_points_total ? kb(m.bytes_points_total) : "–";
      $("#m-sync-label").textContent = "point sync so far (crew A)";
    }
    const st = a.state?.stats;
    if (st) $("#m-private").textContent = `${st.private_pct}%`;
    const queued = Object.values(DEV).reduce((n, d) => n + (d.state?.stats.outbox || 0), 0);
    $("#m-queue").textContent = queued;
  } catch { /* ignore */ }
}

// ================================================================== main loop
let busy = false;
async function refresh() {
  if (busy || !CFG) return;
  busy = true;
  try {
    const cfg = await api("/api/config", { timeout: 3000 });
    CFG.procs = cfg.procs;
    const boot = $("#boot");
    boot.classList.toggle("hidden", cfg.phase === "ready");
    $("#boot-msg").textContent = cfg.phase === "error" ? `Startup failed: ${cfg.error}` : `${cfg.phase}…`;
    $("#proc-qdrant").className = "chip mono " + (cfg.procs.qdrant ? "up" : cfg.bundled_qdrant ? "off" : "");
    $("#proc-qdrant").textContent = cfg.bundled_qdrant ? "qdrant" : `qdrant: ${cfg.qdrant}`;
    $("#proc-gateway").className = "chip mono " + (cfg.procs.gateway ? "up" : "off");
    const logs = await Promise.all(Object.keys(DEV).map(async (id) => {
      const dv = DEV[id];
      try {
        const st = await api(`${dv.url}/state`, { timeout: 3000 });
        dv.up = true; renderDevice(id, st);
        if (dv.tab !== "add") refreshTab(id);
        return await api(`${dv.url}/log?limit=30`);
      } catch { deviceDown(id); return []; }
    }));
    const cloudLog = await renderCloud();
    const all = [...cloudLog, ...logs.flat()].sort((x, y) => y.ts - x.ts).slice(0, 80);
    $("#log").innerHTML = all.map((l) => `<li class="${l.level}"><span class="t">${clock(l.ts)}</span>
      <span class="d ${l.device}">${l.device.replace("tablet-", "tab ")}</span>
      <span><span class="e">${esc(l.event)}</span> ${esc(l.detail)}</span></li>`).join("");
    await renderMetrics();
  } catch (e) {
    console.warn(e);
  } finally { busy = false; }
}

async function boot() {
  CFG = await api("/api/config");
  CFG.devices.forEach(mountDevice);
  $("#cloud-toggle").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const stop = cloudState.up;
    toast(stop ? "Stopping Qdrant Server and gateway processes…" : "Starting Qdrant Server and gateway…");
    try { await api(`/api/cloud/${stop ? "stop" : "start"}`, { method: "POST", timeout: 120000 }); }
    catch (err) { toast(err.message); }
    e.target.disabled = false; refresh();
  });
  $("#reset").addEventListener("click", async (e) => {
    if (!confirm("Wipe all tablets and the cloud, then reload the demo data?")) return;
    e.target.disabled = true;
    toast("Resetting: restarting processes…");
    try { await api("/api/reset", { method: "POST", timeout: 300000 }); } catch (err) { toast(err.message); }
    Object.keys(DEV).forEach((id) => { clearForm(id); DEV[id].seen = new Set(); DEV[id].r(".answer").classList.add("hidden"); });
    e.target.disabled = false; toast("Demo data loaded"); refresh();
  });
  refresh();
  setInterval(refresh, 2000);
}
boot();
