// EdgeMind fleet dashboard. Views are driven by the sidebar (#/overview, #/crews/<id>, #/review,
// #/memory, #/activity, #/network). Everything arrives live over one SSE stream (/api/events) that
// the hub fans out from the gateway and every node; actions go through the hub's proxies.
let CFG = null;
const DEV = {};           // id -> crew panel state
const FEED = [];          // merged activity (cloud + nodes)
const FEED_KEYS = new Set();
let cloud = { up: false, fleet: null, heads: {} };
let lastProp = null, lastSync = null;
let VIEW = "overview", SEL = null;
const base = (id) => `/api/dev/${encodeURIComponent(id)}`;
const short = (label) => (label || "").split(" · ")[0];
const cap = (s) => s[0].toUpperCase() + s.slice(1);
// crew id -> short display name for logs ("tablet-A" -> "Line crew A")
function devName(id) {
  if (id === "cloud") return "Cloud";
  const n = CFG?.nodes.find((x) => x.id === id);
  return n ? short(n.label).replace(/^Line crew /, "Crew ") : id;
}
// events a person cares about (the full technical log stays in Activity)
const QUIET_EVENTS = new Set(["sync", "point delta", "partial snapshot", "full snapshot", "live channel", "point sync",
  "sync resumed", "startup", "possible duplicate"]);
const meaningful = (l) => !QUIET_EVENTS.has(l.event);
const KIND_ABBR = { fix: "Fix", incident: "Inc", manual: "SOP", reading: "Rdg", note: "Note" };
const VIEWS = {
  overview: "Fleet overview", crews: "Crews", review: "Review", memory: "Fleet memory", activity: "Activity", network: "Network",
};

// ================================================================== router
function go(view, sel) { location.hash = sel ? `#/${view}/${encodeURIComponent(sel)}` : `#/${view}`; }

function route() {
  const m = location.hash.match(/^#\/(\w+)(?:\/(.+))?/) || [];
  VIEW = VIEWS[m[1]] ? m[1] : "overview";
  if (m[2]) SEL = decodeURIComponent(m[2]);
  if (VIEW === "crews" && (!SEL || !DEV[SEL])) SEL = CFG?.nodes[0]?.id || null;
  document.querySelectorAll(".view").forEach((el) => { el.hidden = el.dataset.view !== VIEW; });
  document.querySelectorAll(".sidebar-nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === VIEW));
  $("#page-title").textContent = VIEW === "crews" && SEL && DEV[SEL] ? DEV[SEL].node.label : VIEWS[VIEW];
  $("#global-search").placeholder = VIEW === "memory" ? "Search fleet memory" : VIEW === "crews" ? "Search this crew's memory" : "Search memories";
  Object.values(DEV).forEach((d) => { d.el.hidden = d.id !== SEL; });
  renderCrewSwitch();
  renderSideCrews();
  if (VIEW === "crews" && SEL) refreshTab(SEL);
  if (VIEW === "review") renderReviewAll();
  if (VIEW === "memory") refreshCloud();
  if (VIEW === "network") drawMap();
  if (VIEW === "overview") renderOverview();
  if (VIEW === "activity") renderFeed();
  document.body.classList.remove("sidebar-open");
  window.scrollTo(0, 0);
}

// ================================================================== config + operator
async function loadConfig() {
  try { CFG = await api("/api/config", { timeout: 5000 }); } catch { return; }
  $("#boot").classList.toggle("hidden", CFG.phase === "ready");
  $("#boot-msg").textContent = CFG.phase === "error" ? `Startup failed: ${CFG.error}` : `${CFG.phase}…`;
  $("#proc-qdrant").className = "chip " + (CFG.procs.qdrant ? "up" : CFG.bundled_qdrant ? "off" : "");
  $("#proc-qdrant").classList.toggle("hidden", !CFG.bundled_qdrant);
  $("#proc-gateway").className = "chip " + (CFG.procs.gateway ? "up" : "off");
  $("#op-badge").innerHTML = CFG.operator ? ICON.unlock : ICON.lock;
  $("#op-badge").classList.toggle("on", !!CFG.operator);
  $("#op-badge").title = CFG.operator ? "Operator access unlocked" : "Unlock operator access";
  $("#side-op-status").textContent = CFG.operator ? "Operator access" : "Read-only";
  $("#side-op-btn").classList.toggle("hidden", !!CFG.operator);
  document.body.classList.toggle("operator", !!CFG.operator);
  document.body.classList.toggle("external", !!CFG.external);
  $("#nav-nodes-count").textContent = CFG.nodes.length || "";
  syncPanels();
  if (VIEW === "network") drawMap();
}

async function asOperator(fn) {
  if (!CFG?.operator) {
    const ok = await askPin();
    if (!ok) return;
  }
  try { await fn(); } catch (e) {
    if (e.status === 403) { CFG.operator = false; toast("Operator PIN required", "bad"); } else toast(e.message, "bad");
  }
}

function askPin() {
  return new Promise((resolve) => {
    const dlg = $("#pin-dlg");
    $("#pin").value = "";
    dlg.showModal();
    const done = (v) => { dlg.close(); $("#pin-form").onsubmit = null; resolve(v); };
    $("#pin-cancel").onclick = () => done(false);
    $("#pin-form").onsubmit = async (e) => {
      e.preventDefault();
      try { await api("/api/login", { method: "POST", body: { pin: $("#pin").value } }); await loadConfig(); done(true); }
      catch { toast("Wrong PIN", "bad"); }
    };
  });
}

// ================================================================== crews: switcher, sidebar list
function crewState(id) {
  const dv = DEV[id], st = dv?.state;
  if (!dv?.up || !st) return ["bad", "Unreachable"];
  if (!st.online) return ["warn", "No uplink"];
  if (st.live || st.connected) return ["ok", "Live"];
  return ["warn", "No cloud"];
}

function renderSideCrews() {
  const ul = $("#side-crews");
  if (!CFG) return;
  ul.innerHTML = CFG.nodes.map((n) => {
    const [cls, label] = crewState(n.id);
    const active = VIEW === "crews" && SEL === n.id ? "active" : "";
    return `<li class="${active}" data-id="${esc(n.id)}"><span class="dot" style="background:${devColor(n.id)}"></span>
      <span class="name">${esc(short(n.label))}</span><span class="state ${cls}">${label}</span></li>`;
  }).join("") || `<li class="muted">No crews yet</li>`;
  ul.querySelectorAll("li[data-id]").forEach((li) => li.addEventListener("click", () => go("crews", li.dataset.id)));
}
const renderSideSoon = debounce(renderSideCrews, 200);

function renderCrewSwitch() {
  const box = $("#crew-switch");
  if (!CFG) return;
  box.innerHTML = CFG.nodes.map((n) => {
    const [cls] = crewState(n.id);
    const rev = reviewCount(n.id);
    return `<button class="crew-pill ${SEL === n.id ? "active" : ""}" data-id="${esc(n.id)}">
      <span class="dot" style="background:${devColor(n.id)}"></span>${esc(short(n.label))}
      <span class="status-dot ${cls === "ok" ? "ok" : cls === "bad" ? "bad" : "warn"}"></span>${rev ? `<span class="badge">${rev}</span>` : ""}</button>`;
  }).join("");
  box.querySelectorAll("[data-id]").forEach((b) => b.addEventListener("click", () => go("crews", b.dataset.id)));
}

function reviewCount(id) {
  const s = DEV[id]?.state?.stats;
  return s ? s.conflicts + s.contradictions + s.suggestions : 0;
}

// ================================================================== crew panels
function syncPanels() {
  const ids = new Set(CFG.nodes.map((n) => n.id));
  for (const n of CFG.nodes) {
    if (!DEV[n.id]) mountDevice(n);
    const dv = DEV[n.id];
    dv.node = n;
    dv.r(".dev-sites").textContent = `${n.sites.map(cap).join(", ")} · ${n.kind === "remote" ? "remote node" : "on this hub"} · ${n.id}`;
    dv.r(".remove-node").classList.toggle("hidden", !CFG.operator);
    dv.r(".field-link").href = `/field/${encodeURIComponent(n.id)}`;
    dv.el.hidden = !(VIEW === "crews" && SEL === n.id);
  }
  for (const id of Object.keys(DEV)) {
    if (!ids.has(id)) { DEV[id].el.remove(); delete DEV[id]; }
  }
  if (VIEW === "crews" && (!SEL || !DEV[SEL])) route();
  renderSideCrews();
  renderCrewSwitch();
  renderOverviewSoon();
}

function mountDevice(n) {
  const host = document.createElement("section");
  host.className = "device";
  host.id = `dev-${n.id}`;
  host.hidden = true;
  host.style.setProperty("--dev", devColor(n.id));
  host.appendChild($("#device-tpl").content.cloneNode(true));
  $("#devices").appendChild(host);
  const r = (c) => $(c, host);
  const dv = DEV[n.id] = { id: n.id, node: n, el: host, r, tab: "memory", filter: "all", state: null, seen: new Set(), up: false,
    log: [], open: new Set() };
  r(".dev-name").textContent = n.label;
  host.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(n.id, b.dataset.tab)));
  host.querySelectorAll(".fchip").forEach((b) => b.addEventListener("click", () => {
    dv.filter = b.dataset.f;
    host.querySelectorAll(".fchip").forEach((x) => x.classList.toggle("active", x === b));
    refreshTab(n.id);
  }));
  r(".online").addEventListener("change", (e) => setUplink(n.id, e.target.checked));
  r(".sync-now").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const s = await api(`${base(n.id)}/sync`, { method: "POST", timeout: 60000 });
      const bytes = [...s.full, ...s.partial, ...s.points].reduce((a, x) => a + x.bytes, 0);
      toast(s.ok ? `Synced: sent ${s.pushed}, received ${s.pulled} (${kb(bytes)}, ${s.ms} ms)` : `${n.id}: ${s.error}`);
    } catch (err) { toast(err.message, "bad"); }
    e.target.disabled = false;
  });
  r(".scale-test").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const box = r(".answer");
    box.classList.remove("hidden");
    box.innerHTML = `<p class="muted">Building a 20,000-memory shard on this node and running 100 searches…</p>`;
    try {
      const t = await api(`${base(n.id)}/scale-test?n=20000`, { method: "POST", timeout: 300000 });
      box.innerHTML = `<div class="ai-head">Benchmark</div>
        <div class="scale"><div><b>${t.memories.toLocaleString()}</b><span>memories</span></div>
        <div><b>${t.p50_ms} ms</b><span>median search</span></div><div><b>${t.p95_ms} ms</b><span>95th percentile</span></div>
        <div><b>${t.disk_mb} MB</b><span>on disk</span></div></div>
        <p class="muted small">Same hybrid search as live queries, offline on this node's CPU (${t.queries} queries, excluding query embedding). Built in ${t.load_s} s, indexed in ${t.index_s} s.</p>`;
    } catch (err) { box.innerHTML = `<p class="muted">${esc(err.message)}</p>`; }
    e.target.disabled = false;
  });
  r(".remove-node").addEventListener("click", () => asOperator(async () => {
    if (!confirm(`Remove ${n.label}? Its data on this hub is deleted and its token revoked.`)) return;
    await api(`/api/nodes/${encodeURIComponent(n.id)}`, { method: "DELETE", timeout: 60000 });
    toast(`${short(n.label)} removed`); SEL = null; go("overview"); loadConfig();
  }));
  r(".ask").addEventListener("submit", (e) => { e.preventDefault(); ask(n.id); });
  const form = r(".add-form");
  ["input", "change"].forEach((ev) => form.addEventListener(ev, debounce(() => preview(n.id), 300)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { title: r(".f-title").value, text: r(".f-text").value, kind: r(".f-kind").value,
      site: r(".f-site").value, visibility: r(".f-vis").value, doc_id: r(".f-id").value || null };
    if (!body.text.trim()) return toast("Write something first");
    try {
      const doc = await api(`${base(n.id)}/memory`, { method: "POST", body, timeout: 30000 });
      toast(`Saved · ${STATE_LABEL[doc.sync_state] || doc.sync_state}` +
        (doc.contradicts?.length ? " · conflicts with a fleet memory, see Review" : ""), doc.contradicts?.length ? "warn" : "ok");
      clearForm(n.id);
      showTab(n.id, doc.contradicts?.length ? "review" : "memory");
    } catch (err) { toast(err.message, "bad"); }
  });
  r(".cancel").addEventListener("click", () => clearForm(n.id));
  dv.refreshSoon = debounce(() => refreshTab(n.id), 250);
  api(`${base(n.id)}/log?limit=40`).then((rows) => addLogs(n.id, rows)).catch(() => {});
}

async function setUplink(id, on) {
  const label = short(DEV[id]?.node.label);
  try {
    const res = await api(`${base(id)}/online`, { method: "POST", body: { value: on }, timeout: 60000 });
    if (res.sync) toast(res.sync.ok ? `${label} is back online: sent ${res.sync.pushed}, received ${res.sync.pulled}` +
      (res.sync.conflicts ? `, ${res.sync.conflicts} conflict` : "") : `${label}: ${res.sync.error}`, res.sync.ok ? "ok" : "warn");
    else toast(`${label} is offline and working locally`, "warn");
  } catch (err) { toast(err.message, "bad"); }
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
  r(".f-site").disabled = false; r(".save").textContent = "Save";
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
  const { r } = DEV[id];
  const text = `${r(".f-title").value} ${r(".f-text").value}`.trim();
  if (!text) { r(".policy-preview").innerHTML = ""; return; }
  try {
    const d = await api(`${base(id)}/policy/preview`, { method: "POST", body: { text, kind: r(".f-kind").value, visibility: r(".f-vis").value } });
    r(".policy-preview").innerHTML = policyHtml(d);
  } catch { /* node down */ }
}

async function ask(id) {
  const { r } = DEV[id];
  const q = r(".q").value.trim();
  if (!q) return;
  const box = r(".answer");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted">Searching this crew's memory…</p>`;
  try {
    const a = await api(`${base(id)}/ask`, { method: "POST", body: { q }, timeout: 90000 });
    box.innerHTML = answerHtml(a);
    if (a.llm) streamAnswer(box, base(id), q);
    const af = box.querySelector(".ask-fleet");
    af.addEventListener("click", async () => {
      const res = await api(`${base(id)}/ask-fleet`, { method: "POST", body: { q } });
      af.replaceWith(Object.assign(document.createElement("span"), {
        textContent: res.queued ? "Queued: the fleet hears it when the uplink returns." : "Sent. Other crews' nodes will check their private notes." }));
    });
  } catch (err) { box.innerHTML = `<p class="muted">${esc(err.message)}</p>`; }
}

function rowHtml(d, fresh, open) {
  const icon = d.thumb ? `<span class="kind-ico"><img src="${esc(d.thumb)}" alt=""/></span>`
    : `<span class="kind-ico k-${esc(d.kind)}">${esc(KIND_ABBR[d.kind] || d.kind)}</span>`;
  const by = d.author_device && d.author_device !== "hq-console" ? `by ${d.author_device}` : "";
  const sub = [d.kind, d.site, d.layer === "mirror" ? "fleet" : "this node", by].filter(Boolean).join(" · ");
  const tags = [
    d.version ? `<span class="tag">v${d.version}</span>` : "",
    d.similar_to ? `<span class="tag" title="similarity ${d.similar_to.score}">Similar to ${esc(d.similar_to.title)}</span>` : "",
    d.contradicts?.length ? `<span class="tag warn">${esc(d.contradicts[0].mine)} vs ${esc(d.contradicts[0].theirs)}</span>` : "",
  ].join("");
  const why = d.policy && d.layer !== "mirror" ? `<div class="why">${d.policy.reasons.map(esc).join("<br>")}</div>` : "";
  return `<li class="row ${fresh ? "fresh" : ""} ${open ? "open" : ""}" data-id="${esc(d.doc_id)}">
    <div class="row-main">
      <div class="row-name">${icon}<div><div class="row-title">${esc(d.title)}</div><div class="row-sub">${esc(sub)}</div></div></div>
      <div><span class="st st-${esc(d.sync_state)}">${esc(STATE_LABEL[d.sync_state] || d.sync_state)}</span></div>
      <div class="row-when">${ago(d.updated_at)}</div>
    </div>
    <div class="row-detail ${open ? "" : "hidden"}">
      ${d.thumb ? `<img class="thumb" src="${esc(d.thumb)}" alt="photo" data-photo="${esc(d.doc_id)}"/>` : ""}
      <div class="item-text">${esc(d.text)}</div>
      ${tags ? `<div class="meta">${tags}</div>` : ""}${why}
      <div class="actions"><button class="btn small ghost" data-hist="${esc(d.doc_id)}">History</button>
        <button class="btn small ghost" data-act="edit" data-id="${esc(d.doc_id)}">Edit</button>
        <button class="btn small ghost danger" data-act="del" data-id="${esc(d.doc_id)}">Delete</button></div>
    </div></li>`;
}

// Only the crew on screen reloads its lists; the others just keep their live state.
async function refreshTab(id) {
  const dv = DEV[id];
  if (!dv || !dv.up || VIEW !== "crews" || SEL !== id) return;
  const { r, tab } = dv;
  try {
    if (tab === "memory") {
      let docs = await api(`${base(id)}/memory`);
      if (dv.filter === "own") docs = docs.filter((d) => d.layer === "local");
      if (dv.filter === "fleet") docs = docs.filter((d) => d.layer === "mirror");
      const q = $("#global-search").value.trim().toLowerCase();
      if (q) docs = docs.filter((d) => `${d.title} ${d.text} ${d.kind} ${d.site}`.toLowerCase().includes(q));
      const first = dv.seen.size === 0;
      const list = r(".memory");
      list.innerHTML = docs.length ? docs.map((d) => rowHtml(d, !first && !dv.seen.has(d.doc_id + d.updated_at), dv.open.has(d.doc_id))).join("")
        : `<li class="empty">${q ? "No memories match your search." : "No memories here yet."}</li>`;
      docs.forEach((d) => dv.seen.add(d.doc_id + d.updated_at));
      list.querySelectorAll(".row-main").forEach((rm) => rm.addEventListener("click", () => {
        const row = rm.parentElement, did = row.dataset.id;
        const open = !row.classList.contains("open");
        row.classList.toggle("open", open);
        row.querySelector(".row-detail").classList.toggle("hidden", !open);
        if (open) dv.open.add(did); else dv.open.delete(did);
      }));
      bindThumbs(list, base(id));
      list.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
        const doc = docs.find((x) => x.doc_id === b.dataset.id);
        if (b.dataset.act === "edit") return editDoc(id, doc);
        if (!confirm(`Delete "${doc.title}"?`)) return;
        await api(`${base(id)}/memory/${doc.doc_id}`, { method: "DELETE" }).catch((e) => toast(e.message, "bad"));
      }));
    } else if (tab === "queue") {
      const q = await api(`${base(id)}/outbox`);
      r(".queue").innerHTML = q.length ? q.map((x) => `<li class="row"><div class="row-main static">
          <div class="row-name"><span class="kind-ico k-${esc(x.kind)}">${esc(KIND_ABBR[x.kind] || x.kind)}</span>
            <div><div class="row-title">${esc(x.title)}</div><div class="row-sub">${esc(x.op)} · priority ${x.priority}</div></div></div>
          <div><span class="st st-pending">Waiting</span></div><div class="row-when">${ago(x.queued_at)}</div></div></li>`).join("")
        : `<li class="empty">Nothing waiting. Everything shareable has reached the fleet.</li>`;
    } else if (tab === "review") {
      const box = r(".review");
      box.innerHTML = reviewHtml(await api(`${base(id)}/review`));
      bindReview(box, base(id), () => refreshTab(id));
    } else if (tab === "activity") {
      r(".dev-log").innerHTML = dv.log.slice(0, 60).map((l) => logHtml(l, false)).join("") || `<li class="empty">No activity yet.</li>`;
    }
  } catch { /* node down */ }
}

function renderDevice(id, st) {
  const dv = DEV[id];
  const { r, el } = dv;
  dv.state = st; dv.up = true;
  el.classList.toggle("offline", !st.online);
  r(".down").classList.add("hidden");
  r(".online").checked = st.online;
  const conn = r(".conn");
  const [cls, label] = !st.online ? ["bad", "Uplink off · working locally"] : st.live ? ["ok", "Live"] :
    st.connected ? ["ok", "Connected"] : ["warn", "Cloud unreachable · working locally"];
  conn.className = "pill conn " + cls; conn.textContent = label;
  renderLastSync(dv);
  const s = st.stats;
  r(".s-mem").textContent = s.memories;
  r(".s-queue").textContent = s.outbox;
  r(".s-local").textContent = s.by_state.local_only || 0;
  const review = reviewCount(id);
  r(".s-review").textContent = review;
  r(".s-review-box").classList.toggle("hot", review > 0);
  r(".review-badge").textContent = review || "";
  if (!r(".f-kind").options.length) {
    r(".f-kind").innerHTML = st.kinds.map((k) => `<option value="${esc(k)}">${esc(cap(k))}</option>`).join("");
    r(".f-site").innerHTML = st.sites.map((k) => `<option value="${esc(k)}">${esc(cap(k))}</option>`).join("");
  }
  if (st.propagation_ms != null && dv.prop !== st.propagation_ms) {
    dv.prop = st.propagation_ms;
    lastProp = { ms: st.propagation_ms, node: id };
  }
  const ls = st.last_sync || {};
  const moved = [...(ls.full || []), ...(ls.partial || []), ...(ls.points || [])];
  if (ls.ok && moved.some((x) => x.bytes) && (!lastSync || ls.at > lastSync.at)) {
    const kind = ls.partial?.length ? "partial snapshot" : ls.full?.length ? "full snapshot" : "point delta";
    lastSync = { at: ls.at, bytes: moved.reduce((a, x) => a + x.bytes, 0), kind, node: id };
  }
  renderMetrics();
  renderSideSoon();
  renderChromeSoon();
}

function renderLastSync(dv) {
  const st = dv.state;
  if (!st) return;
  const ls = st.last_sync || {};
  const how = ls.partial?.length ? `partial snapshot ${kb(ls.partial.reduce((a, x) => a + x.bytes, 0))}` :
    ls.full?.length ? `full snapshot ${kb(ls.full.reduce((a, x) => a + x.bytes, 0))}` :
    ls.points?.some((x) => x.count) ? `point delta ${kb(ls.points.reduce((a, x) => a + x.bytes, 0))}` : "";
  dv.r(".last-sync").textContent = ls.at ? `Synced ${ago(ls.at)}${ls.ok && how ? " · " + how : ""}` : "Not synced yet";
}

function deviceDown(id) {
  const dv = DEV[id];
  if (!dv) return;
  dv.up = false;
  dv.r(".down").classList.remove("hidden");
  dv.r(".conn").className = "pill conn bad";
  dv.r(".conn").textContent = "Not reachable";
  renderSideCrews();
  renderChromeSoon();
  if (VIEW === "network") drawMap();
}

function addLogs(id, rows) {
  const dv = DEV[id];
  for (const l of rows) {
    const key = `${id}:${l.id ?? l.ts}`;
    if (FEED_KEYS.has(key)) continue;
    FEED_KEYS.add(key);
    FEED.push(l);
    if (dv) dv.log.unshift(l);
  }
  if (dv) dv.log.sort((a, b) => b.ts - a.ts);
  renderFeedSoon();
}

// ================================================================== overview
function renderOverview() {
  if (!CFG) return;
  const ul = $("#ov-crews");
  ul.innerHTML = CFG.nodes.map((n) => {
    const st = DEV[n.id]?.state, s = st?.stats;
    const [cls, label] = crewState(n.id);
    const rev = reviewCount(n.id);
    const sync = st?.last_sync?.at ? `synced ${ago(st.last_sync.at)}` : "";
    return `<li class="ct-row" data-id="${esc(n.id)}">
      <div class="ct-crew"><span class="crew-dot" style="background:${devColor(n.id)}"></span>
        <div><div class="row-title">${esc(short(n.label))}</div><div class="row-sub">${esc(n.label.split(" · ")[1] || n.sites.map(cap).join(", "))}${sync ? " · " + sync : ""}</div></div></div>
      <div><span class="pill ${cls}">${label}</span></div>
      <div class="num">${s ? s.memories : "–"}</div>
      <div class="num ${s?.outbox ? "warn-text" : ""}">${s ? s.outbox : "–"}</div>
      <div class="num">${rev ? `<span class="badge">${rev}</span>` : "–"}</div>
      <div><label class="switch" title="Uplink" data-stop><input type="checkbox" data-uplink="${esc(n.id)}" ${st?.online ? "checked" : ""} ${st ? "" : "disabled"}/><span class="track"></span></label></div>
    </li>`;
  }).join("") || `<li class="empty">No crews yet. Add one, or join a laptop from “Connect a phone”.</li>`;
  ul.querySelectorAll(".ct-row").forEach((row) => row.addEventListener("click", (e) => {
    if (e.target.closest("[data-stop]")) return;
    go("crews", row.dataset.id);
  }));
  ul.querySelectorAll("[data-uplink]").forEach((cb) => cb.addEventListener("change", () => setUplink(cb.dataset.uplink, cb.checked)));

  // what needs a human
  const items = [];
  for (const n of CFG.nodes) {
    const st = DEV[n.id]?.state, s = st?.stats;
    if (!DEV[n.id]?.up) { items.push(["bad", `${short(n.label)} is not reachable`, "Check the node", `#/crews/${n.id}`]); continue; }
    if (!s) continue;
    if (s.suggestions) items.push(["violet", `${short(n.label)} has ${s.suggestions} private note${s.suggestions > 1 ? "s" : ""} another crew needs`, "Review", `#/review`]);
    if (s.conflicts) items.push(["bad", `${short(n.label)}: ${s.conflicts} edit conflict${s.conflicts > 1 ? "s" : ""}`, "Resolve", `#/review`]);
    if (s.contradictions) items.push(["warn", `${short(n.label)}: ${s.contradictions} conflicting value${s.contradictions > 1 ? "s" : ""}`, "Check", `#/review`]);
    if (!st.online) items.push(["warn", `${short(n.label)} has no uplink${s.outbox ? ` · ${s.outbox} waiting to upload` : ""}`, "Open", `#/crews/${n.id}`]);
  }
  if (!cloud.up) items.unshift(["bad", "The cloud is offline. Crews keep working and upload later.", "Network", "#/network"]);
  for (const d of (cloud.fleet?.demand || []).slice(0, 3)) items.push(["info", `A crew searched “${d.query}” and found nothing`, "Fleet memory", "#/memory"]);
  $("#ov-attention").innerHTML = items.length ? items.map(([cls, text, action, href]) =>
    `<li class="att ${cls}"><span class="att-dot"></span><span class="att-text">${esc(text)}</span><a class="link small" href="${href}">${action}</a></li>`).join("")
    : `<li class="empty">All clear. Nothing needs attention.</li>`;
  $("#ov-activity").innerHTML = FEED.filter(meaningful).slice(0, 7).map((l) => logHtml(l)).join("") || `<li class="empty">No activity yet.</li>`;
}
const renderOverviewSoon = debounce(() => { if (VIEW === "overview") renderOverview(); }, 250);

// sidebar badges, crew switcher, header summary: cheap, update on every change
const renderChromeSoon = debounce(() => {
  const total = Object.keys(DEV).reduce((a, id) => a + reviewCount(id), 0);
  $("#nav-review-badge").textContent = total || "";
  $("#nav-review-badge").hidden = !total;
  renderCrewSwitch();
  renderOverviewSoon();
  if (VIEW === "review") renderReviewAllSoon();
  if (VIEW === "crews" && SEL && DEV[SEL]) $("#page-title").textContent = DEV[SEL].node.label;
}, 200);

// ================================================================== review (all crews)
async function renderReviewAll() {
  const box = $("#review-all");
  if (!CFG) return;
  const parts = await Promise.all(CFG.nodes.map(async (n) => {
    if (!DEV[n.id]?.up) return "";
    try {
      const rv = await api(`${base(n.id)}/review`);
      if (!rv.suggestions.length && !rv.conflicts.length && !rv.contradictions.length) return "";
      return `<section class="card review-group" data-id="${esc(n.id)}">
        <div class="card-head"><h2><span class="crew-dot" style="background:${devColor(n.id)}"></span>${esc(n.label)}</h2>
          <a class="link small" href="#/crews/${esc(n.id)}">Open crew</a></div>
        <div class="review">${reviewHtml(rv)}</div></section>`;
    } catch { return ""; }
  }));
  const html = parts.join("");
  box.innerHTML = html || `<section class="card"><div class="empty">Nothing needs review. Requests from other crews, edit conflicts and conflicting values appear here.</div></section>`;
  box.querySelectorAll(".review-group").forEach((g) => bindReview(g, base(g.dataset.id), () => renderReviewAll()));
}
const renderReviewAllSoon = debounce(renderReviewAll, 600);

// ================================================================== fleet memory (cloud)
const refreshCloud = debounce(async () => {
  try {
    const [docs, glog] = await Promise.all([api("/api/cloud/memory"), api("/api/cloud/log?limit=40")]);
    $("#cloud-count").textContent = docs.length;
    $("#m-cloud").textContent = docs.length;
    const q = VIEW === "memory" ? $("#global-search").value.trim().toLowerCase() : "";
    const shown = q ? docs.filter((d) => `${d.title} ${d.text} ${d.kind} ${d.site} ${d.author_device}`.toLowerCase().includes(q)) : docs;
    $("#cloud-list").innerHTML = shown.map((d) => `<li class="row" data-id="${esc(d.doc_id)}"><div class="row-main mem-row">
        <div class="row-name"><span class="kind-ico k-${esc(d.kind)}">${esc(KIND_ABBR[d.kind] || d.kind)}</span>
          <div><div class="row-title">${esc(d.title)}</div><div class="row-sub">${esc(d.text)}</div></div></div>
        <div class="row-when">${esc(cap(d.site))}</div>
        <div class="row-when">${esc(d.author_device === "hq-console" ? "Seed data" : devName(d.author_device))}${d.redacted ? ' <span class="st st-redacted_synced">Redacted</span>' : ""}</div>
        <div class="row-when">v${d.version}</div></div>
        <div class="row-detail hidden"><div class="item-text">${esc(d.text)}</div></div></li>`).join("")
      || `<li class="empty">${q ? "No fleet memories match your search." : "No fleet memories yet."}</li>`;
    $("#cloud-list").querySelectorAll(".row-main").forEach((rm) => rm.addEventListener("click", () => {
      const row = rm.parentElement, open = !row.classList.contains("open");
      row.classList.toggle("open", open);
      row.querySelector(".row-detail").classList.toggle("hidden", !open);
    }));
    for (const l of glog) {
      const key = `cloud:${l.ts}:${l.event}`;
      if (FEED_KEYS.has(key)) continue;
      FEED_KEYS.add(key);
      FEED.push({ ts: l.ts, device: "cloud", level: /conflict|refused|rejected/.test(l.event) ? "warn" : "info", event: l.event,
        detail: `${l.device !== "hq-console" ? l.device + ": " : ""}${l.detail}` });
    }
    renderFeedSoon();
    renderMetrics();
  } catch { /* cloud down */ }
}, 300);

function renderCloud(data) {
  cloud.up = true;
  cloud.fleet = data.fleet;
  cloud.heads = data.heads || {};
  $("#cloud").classList.remove("down");
  $("#cloud-pill").className = "pill ok";
  $("#cloud-pill").textContent = "Cloud online";
  $("#side-cloud-dot").className = "status-dot ok";
  $("#cloud-toggle").textContent = "Stop cloud";
  $("#cloud-toggle").className = "btn small block stop";
  const f = data.fleet;
  if (f) {
    $("#cloud-mode").textContent = f.mode.replace(/^Qdrant Server /, "Qdrant Server at ") + (f.snapshots ? ". Crews sync by deltas and snapshots." : ". Crews sync point by point.");
    $("#demand-box").classList.toggle("hidden", !f.demand.length);
    $("#demand").innerHTML = f.demand.map((d) => `<li>“${esc(d.query)}”<div class="muted small">${esc(devName(d.device))} · ${ago(d.ts)}</div></li>`).join("");
    const known = new Set(CFG?.nodes.map((n) => n.id) || []);
    if (f.devices.some((d) => d.online && !d.revoked && !known.has(d.id) && d.url)) loadConfig();  // a laptop just joined
  }
  $("#heads").innerHTML = Object.entries(cloud.heads).map(([s, h]) => `<span class="tag">${esc(s)} ${h}</span>`).join("");
  refreshCloud();
  if (VIEW === "network") drawMap();
  renderOverviewSoon();
}

function cloudDown() {
  cloud.up = false;
  $("#cloud").classList.add("down");
  $("#cloud-pill").className = "pill bad";
  $("#cloud-pill").textContent = "Cloud offline";
  $("#side-cloud-dot").className = "status-dot bad";
  $("#cloud-toggle").textContent = "Start cloud";
  $("#cloud-toggle").className = "btn small block go";
  $("#m-cloud").textContent = "–";
  $("#cloud-mode").textContent = "The cloud is stopped. Every crew keeps working on its own node and uploads later.";
  $("#cloud-list").innerHTML = `<li class="empty">Unavailable while the cloud is offline.</li>`;
  $("#heads").innerHTML = "";
  $("#demand-box").classList.add("hidden");
  if (VIEW === "network") drawMap();
  renderOverviewSoon();
}

function renderFeed() {
  FEED.sort((a, b) => b.ts - a.ts);
  if (FEED.length > 300) FEED.length = 300;
  if (VIEW === "activity") $("#log").innerHTML = FEED.slice(0, 200).map((l) => logHtml(l)).join("");
  if (VIEW === "overview") $("#ov-activity").innerHTML = FEED.filter(meaningful).slice(0, 7).map((l) => logHtml(l)).join("") || `<li class="empty">No activity yet.</li>`;
  if (VIEW === "crews" && SEL && DEV[SEL]?.tab === "activity") DEV[SEL].refreshSoon();
}
const renderFeedSoon = debounce(renderFeed, 150);

function renderMetrics() {
  const nodes = Object.values(DEV);
  const live = nodes.filter((d) => d.up && d.state?.live).length;
  $("#m-nodes").textContent = `${live} of ${nodes.length}`;
  $("#m-prop").textContent = lastProp ? secs(lastProp.ms) : "—";
  if (lastProp) $("#m-prop-label").textContent = `Last write, searchable on ${short(DEV[lastProp.node]?.node.label) || lastProp.node}`;
  const sr = nodes.map((d) => d.state?.search).find((s) => s && s.p50 != null);
  $("#m-search").textContent = sr ? `${Math.round(sr.p50)} · ${Math.round(sr.p95)} ms` : "—";
  if (lastSync) {
    $("#m-sync").textContent = kb(lastSync.bytes);
    $("#m-sync-label").textContent = `${lastSync.kind} to ${short(DEV[lastSync.node]?.node.label) || lastSync.node}`;
  }
  $("#m-queue").textContent = nodes.reduce((n, d) => n + (d.state?.stats.outbox || 0), 0);
  const fleetCount = $("#cloud-count").textContent;
  $("#page-sub").textContent = `${live} of ${nodes.length} crews live${fleetCount ? ` · ${fleetCount} fleet memories` : ""}`;
}

// ================================================================== network map
function drawMap() {
  const svg = $("#map");
  if (!CFG) return;
  const nodes = CFG.nodes;
  const W = 1000, cx = W / 2, cy = 34, ny = 128;
  const step = Math.min(210, (W - 120) / Math.max(1, nodes.length));
  const x0 = cx - (step * (nodes.length - 1)) / 2;
  let html = `<g class="m-cloud ${cloud.up ? "up" : "down"}"><rect x="${cx - 100}" y="${cy - 22}" width="200" height="44" rx="10"/>
    <text x="${cx}" y="${cy - 3}" text-anchor="middle" class="m-title">Cloud</text>
    <text x="${cx}" y="${cy + 13}" text-anchor="middle" class="m-sub">${cloud.up ? "Qdrant Server · gateway" : "offline"}</text></g>`;
  nodes.forEach((n, i) => {
    const x = x0 + i * step;
    const st = DEV[n.id]?.state;
    const up = DEV[n.id]?.up;
    const cls = !up ? "dead" : !st?.online ? "off" : st.live && cloud.up ? "live" : "warn";
    const label = !up ? "not reachable" : !st?.online ? "uplink off" : st.live && cloud.up ? "live" : "no cloud";
    html += `<path id="p-${n.id}" class="m-link ${cls}" d="M ${cx} ${cy + 22} C ${cx} ${(cy + ny) / 2}, ${x} ${(cy + ny) / 2}, ${x} ${ny - 20}"/>`;
    html += `<g class="m-node ${cls}" style="--c:${devColor(n.id)}"><rect class="m-box" x="${x - 88}" y="${ny - 20}" width="176" height="46" rx="10"/>
      <rect class="m-bar" x="${x - 88}" y="${ny - 8}" width="3" height="22" rx="1.5"/>
      <text x="${x}" y="${ny - 2}" text-anchor="middle" class="m-title">${esc(short(n.label))}</text>
      <text x="${x}" y="${ny + 15}" text-anchor="middle" class="m-sub">${label}${st ? ` · ${st.stats.memories} memories` : ""}</text></g>`;
  });
  svg.innerHTML = html;
}

function pulse(id, up) {
  if (VIEW !== "network") return;
  const path = document.getElementById(`p-${id}`);
  if (!path) return;
  const ns = "http://www.w3.org/2000/svg";
  const dot = document.createElementNS(ns, "circle");
  dot.setAttribute("r", "6");
  dot.setAttribute("class", "m-dot");
  dot.style.setProperty("--c", devColor(id));
  const anim = document.createElementNS(ns, "animateMotion");
  anim.setAttribute("dur", "0.8s");
  anim.setAttribute("begin", "indefinite");
  anim.setAttribute("fill", "freeze");
  anim.setAttribute("path", path.getAttribute("d"));
  if (up) { anim.setAttribute("keyPoints", "1;0"); anim.setAttribute("keyTimes", "0;1"); anim.setAttribute("calcMode", "linear"); }
  dot.appendChild(anim);
  $("#map").appendChild(dot);
  anim.beginElement();
  setTimeout(() => dot.remove(), 900);
}
const drawMapSoon = debounce(() => { if (VIEW === "network") drawMap(); }, 150);

// ================================================================== live stream
function onHubEvent(ev) {
  if (ev.src === "cloud") {
    if (ev.down) cloudDown(); else renderCloud(ev.data);
    return;
  }
  const id = ev.src;
  if (!DEV[id]) { if (!ev.down) loadConfig(); return; }
  if (ev.down) { deviceDown(id); return; }
  const d = ev.data;
  renderDevice(id, d.state);
  if (d.log?.length) {
    addLogs(id, d.log);
    if (!d.first) {
      for (const l of d.log) {
        if (l.event === "pushed" || l.event === "deleted from fleet") pulse(id, true);
        if (/snapshot|point delta|received/.test(l.event)) pulse(id, false);
      }
    }
  }
  drawMapSoon();
  DEV[id].refreshSoon();
}

function setLive(on, mode) {
  $("#live-chip").innerHTML = `<span class="live-dot ${on ? "on" : ""}"></span><span>${on ? (mode === "polling" ? "Live, every 2 s" : "Live") : "Reconnecting"}</span>`;
}

// ================================================================== dialogs
function openJoin() {
  const dlg = $("#join-dlg");
  const setQr = (url) => { $("#join-qr").src = `/api/qr.svg?text=${encodeURIComponent(url)}`; $("#join-url").textContent = url; };
  // opened through a public URL (e.g. a Cloudflare tunnel)? hand phones that URL, not the LAN address
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const origin = local ? CFG.hub.console : location.origin;
  setQr(`${origin}/m`);
  $("#join-nodes").innerHTML = CFG.nodes.map((n) => {
    const url = `${origin}/field/${encodeURIComponent(n.id)}`;
    return `<li class="join-node"><span class="dot" style="background:${devColor(n.id)}"></span><b>${esc(n.label)}</b>
      <button type="button" class="btn small" data-qr="${esc(url)}">Show QR</button></li>`;
  }).join("");
  $("#join-nodes").querySelectorAll("[data-qr]").forEach((b) => b.addEventListener("click", () => setQr(b.dataset.qr)));
  $("#join-laptop").classList.toggle("hidden", !CFG.join_command);
  $("#join-code").textContent = CFG.join_code || "";
  $("#join-cmd").textContent = CFG.join_command || "";
  dlg.showModal();
}

function openAddNode() {
  const dlg = $("#node-dlg");
  $("#node-sites").innerHTML = Object.entries(CFG.sites).map(([k, v]) =>
    `<label class="check"><input type="checkbox" value="${esc(k)}" ${k === "global" ? "checked" : ""}/> ${esc(v)}</label>`).join("");
  $("#node-label").value = "";
  dlg.showModal();
  $("#node-cancel").onclick = () => dlg.close();
  $("#node-form").onsubmit = async (e) => {
    e.preventDefault();
    const sites = [...$("#node-sites").querySelectorAll("input:checked")].map((i) => i.value);
    if (!sites.length) return toast("Pick at least one site");
    dlg.close();
    toast("Starting the new crew node…");
    await asOperator(async () => {
      const n = await api("/api/nodes", { method: "POST", body: { label: $("#node-label").value, sites }, timeout: 240000 });
      toast(`${n.label} is live`, "ok"); await loadConfig(); go("crews", n.id);
    });
  };
}

// ================================================================== boot
function setupSearch() {
  const input = $("#global-search");
  input.addEventListener("input", debounce(() => {
    const q = input.value.trim();
    if (q && !["crews", "memory"].includes(VIEW)) { go("memory"); return; }
    if (VIEW === "memory") refreshCloud();
    if (VIEW === "crews" && SEL) refreshTab(SEL);
  }, 200));
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.focus(); input.select(); }
  });
}

async function boot() {
  paintIcons();
  await loadConfig();
  if (!CFG) { setTimeout(boot, 1500); return; }
  $("#join-btn").addEventListener("click", openJoin);
  $("#add-node-btn").addEventListener("click", () => asOperator(async () => openAddNode()));
  $("#op-badge").addEventListener("click", () => { if (!CFG.operator) askPin(); });
  $("#side-op-btn").addEventListener("click", () => { if (!CFG.operator) askPin(); });
  $("#sidebar-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
  $("#scrim").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
  $("#cloud-toggle").addEventListener("click", (e) => asOperator(async () => {
    e.target.disabled = true;
    const stop = cloud.up;
    toast(stop ? "Stopping the cloud…" : "Starting the cloud…");
    try { await api(`/api/cloud-${stop ? "stop" : "start"}`, { method: "POST", timeout: 180000 }); }
    finally { e.target.disabled = false; loadConfig(); }
  }));
  $("#reset").addEventListener("click", (e) => asOperator(async () => {
    if (!confirm("Reset all crews and the cloud to the demo data?")) return;
    e.currentTarget.disabled = true;
    toast("Resetting…");
    try { await api("/api/reset", { method: "POST", timeout: 400000 }); }
    finally {
      Object.keys(DEV).forEach((id) => { DEV[id].el.remove(); delete DEV[id]; });
      FEED.length = 0; FEED_KEYS.clear(); lastProp = null; lastSync = null;
      $("#reset").disabled = false; toast("Demo data loaded", "ok"); await loadConfig(); route();
    }
  }));
  setupSearch();
  window.addEventListener("hashchange", route);
  route();

  const polled = {};
  liveStream("/api/events", onHubEvent, setLive, async () => {  // fallback when a proxy buffers the stream
    const evs = [];
    const fleet = await api("/api/cloud/fleet", { timeout: 6000 }).catch(() => null);
    evs.push(fleet ? { src: "cloud", data: { heads: fleet.heads, fleet } } : { src: "cloud", down: true });
    await Promise.all((CFG?.nodes || []).map(async (n) => {
      try {
        const [st, log] = await Promise.all([api(`${base(n.id)}/state`, { timeout: 6000 }), api(`${base(n.id)}/log?limit=30`, { timeout: 6000 })]);
        evs.push({ src: n.id, data: { state: st, log, first: !polled[n.id] } });
        polled[n.id] = true;
      } catch { evs.push({ src: n.id, down: true }); }
    }));
    return evs;
  });
  setInterval(loadConfig, 5000);
  setInterval(() => {
    Object.values(DEV).forEach(renderLastSync);
    if (VIEW === "overview") renderOverview();
  }, 5000);
}
boot();
