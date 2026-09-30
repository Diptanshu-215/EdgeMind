// Shared by the dashboard (app.js) and the crew field app (field.js). Plain JS, no build step.
const ICON = {
  home: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>`,
  grid: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></svg>`,
  server: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>`,
  cloud: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 18h10.5a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.2 9.1 4.5 4.5 0 0 0 7 18z"/></svg>`,
  map: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M11 7.2 6.2 16.8M13 7.2l4.8 9.6"/></svg>`,
  file: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>`,
  checkList: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 11 3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>`,
  activity: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>`,
  phone: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M11 18h2"/></svg>`,
  plus: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>`,
  sync: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/></svg>`,
  zap: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 3 5 14h6l-1 7 8-11h-6z"/></svg>`,
  search: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/></svg>`,
  lock: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="11" width="15" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/></svg>`,
  unlock: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="11" width="15" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 7.7-1.5"/></svg>`,
  arrowUpRight: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>`,
  close: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>`,
  camera: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h3l2-2.5h6L17 7h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/></svg>`,
  clock: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
  alert: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4 2.8 19.5a1 1 0 0 0 .9 1.5h16.6a1 1 0 0 0 .9-1.5z"/><path d="M12 10v4M12 17.5h.01"/></svg>`,
  power: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v8"/><path d="M6.3 7.2a7.5 7.5 0 1 0 11.4 0"/></svg>`,
  folder: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`,
  cpu: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>`,
  menu: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>`,
  edit: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/></svg>`,
  trash: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/></svg>`,
  users: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M21.5 20a6.5 6.5 0 0 0-4-6"/></svg>`,
  database: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="5.5" rx="7.5" ry="2.5"/><path d="M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-13"/><path d="M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5"/></svg>`,
  message: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/></svg>`,
  list: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg>`,
  star: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 2.8 5.8 6.2.9-4.5 4.4 1 6.2L12 17.4l-5.6 2.9 1-6.2L3 9.7l6.2-.9z"/></svg>`,
  inbox: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 13h5l1.5 3h5L16 13h5"/><path d="M5.5 5h13L21 13v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z"/></svg>`,
  chevron: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>`,
  qr: `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><path d="M14 14h2v2h-2zM18 18h2v2h-2zM18 14h2M14 18v2"/></svg>`
};

// <span data-icon="name"> placeholders in the HTML get the matching SVG from ICON
function paintIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    if (el.dataset.painted) return;
    el.insertAdjacentHTML("afterbegin", ICON[el.dataset.icon] || "");
    el.dataset.painted = "1";
  });
}

const STATE_LABEL = {
  synced: "Synced", pending: "Queued", local_only: "Private", suggested: "Requested",
  redacted_synced: "Synced, redacted", conflict: "Conflict",
};
const DECISION_LABEL = { local: "stays on this node", sync: "syncs to the fleet", sync_redacted: "syncs a redacted copy",
  suggest: "suggested for sharing" };

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kb = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 10240 ? `${Math.round(n / 1024)} KB` : `${(n / 1024).toFixed(1)} KB`);
const secs = (ms) => (ms == null ? "–" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return new Date(ts * 1000).toLocaleTimeString();
};
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

async function api(url, opts = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeout || 10000);
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" }, method: opts.method || "GET", signal: ctl.signal,
      body: opts.body ? JSON.stringify(opts.body) : undefined, credentials: "same-origin",
    });
    if (!res.ok) {
      const err = new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      err.status = res.status;
      throw err;
    }
    return res.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("timed out");
    throw e;
  } finally { clearTimeout(timer); }
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg; t.className = `toast ${kind}`;
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), 3600);
}

// Server-sent events with automatic reconnect. onEvent(obj), onState(connected, mode)
// Some proxies (a Cloudflare tunnel over HTTP/2, corporate proxies) hold a stream's body until it
// ends. The server always sends a first event immediately, so if nothing arrives within 6 s the page
// falls back to polling `poll()` every 2 s, which returns the same events the stream would push.
function liveStream(url, onEvent, onState, poll) {
  let es = null, closed = false, retry = 1000, got = false, timer = null;
  const startPolling = () => {
    if (timer || closed || !poll) return;
    es && es.close();
    const tick = async () => {
      try { for (const ev of await poll()) onEvent(ev); onState && onState(true, "polling"); }
      catch { onState && onState(false); }
    };
    tick();
    timer = setInterval(tick, 2000);
  };
  const open = () => {
    if (closed || timer) return;
    es = new EventSource(url);
    es.onopen = () => { retry = 1000; onState && onState(true); };
    es.onmessage = (m) => { got = true; try { onEvent(JSON.parse(m.data)); } catch (e) { console.warn(e); } };
    es.addEventListener("down", () => onState && onState(false));
    es.onerror = () => {
      if (timer) return;
      onState && onState(false);
      es.close();
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 8000);
    };
  };
  open();
  setTimeout(() => { if (!got) startPolling(); }, 6000);
  return { close() { closed = true; es && es.close(); clearInterval(timer); } };
}

// ------------------------------------------------------------------ renderers
function itemHtml(d, opts = {}) {
  const fleet = d.layer === "mirror";
  const state = `<span class="st st-${esc(d.sync_state)}">${STATE_LABEL[d.sync_state] || esc(d.sync_state)}</span>`;
  const where = `<span class="tag">${fleet ? "Fleet" : "This node"}</span>`;
  const dup = d.similar_to ? `<span class="tag" title="similarity ${d.similar_to.score}">Similar to ${esc(d.similar_to.title)}</span>` : "";
  const contra = d.contradicts?.length ? `<span class="tag warn">${esc(d.contradicts[0].mine)} vs ${esc(d.contradicts[0].theirs)}</span>` : "";
  const by = d.author_device && d.author_device !== "hq-console" ? ` · by ${esc(d.author_device)}` : "";
  const why = opts.why && d.policy && !fleet ? `<div class="why">${d.policy.reasons.map(esc).join("<br>")}</div>` : "";
  const score = opts.score && d.match != null ? ` · match ${d.match.toFixed(2)}` : "";
  const w = d.why || {};
  const whyScores = opts.score && Object.keys(w).length ? `<div class="whyscore" title="Why it ranked: each retriever's own score, fused on the node">
      ${w.meaning != null ? `<span>meaning <b>${w.meaning.toFixed(2)}</b></span>` : ""}
      ${w.keywords != null ? `<span>keywords <b>${w.keywords.toFixed(2)}</b></span>` : ""}
      ${w.photo != null ? `<span>photo <b>${w.photo.toFixed(2)}</b></span>` : ""}
      ${d.score != null ? `<span>fused <b>${d.score.toFixed(3)}</b></span>` : ""}</div>` : "";
  const thumb = d.thumb ? `<img class="thumb" src="${esc(d.thumb)}" alt="photo" loading="lazy" data-photo="${esc(d.doc_id)}"/>` : "";
  const hist = `<button class="btn small ghost" data-hist="${esc(d.doc_id)}" title="Every version this node has seen">History</button>`;
  const actions = opts.actions === false ? "" : `<div class="actions">${hist}<button class="btn small ghost" data-act="edit" data-id="${esc(d.doc_id)}">Edit</button>
      <button class="btn small ghost danger" data-act="del" data-id="${esc(d.doc_id)}">Delete</button></div>`;
  return `<li class="item ${opts.fresh ? "fresh" : ""}">
    <div class="item-top"><span class="item-title">${esc(d.title)}</span><span class="score">${esc(d.kind)} · ${esc(d.site)}${d.version ? " · v" + d.version : ""}${score}</span></div>
    <div class="item-body">${thumb}<div class="item-text">${esc(d.text)}</div></div>${whyScores}
    <div class="meta">${state}${where}${dup}${contra}${d.has_photo ? `<span class="tag">Photo</span>` : ""}<span>${by}</span></div>${why}${actions}</li>`;
}

function answerHtml(a) {
  const text = esc(a.answer).replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>');
  const lang = a.language !== "en" ? '<span class="tag">Hindi/Marathi → English SOPs</span>' : "";
  return `<div class="ai hidden"><div class="ai-head">Answer <span class="muted small">${esc(a.llm_model || "")} on the node</span></div><p class="ai-text"></p></div>
    <div class="instant"><div class="ai-head muted small">From the notes</div><p>${text}</p></div>
    <div class="meta"><span class="tag">search ${a.search_ms} ms</span>
      <span class="tag">answer ${a.ms} ms</span><span class="tag">best match ${a.top_match.toFixed(2)}</span>${lang}
      ${a.online === false ? '<span class="tag warn">offline: answered on the node</span>' : ""}</div>
    <ol class="srcs">${a.sources.map((s) => `<li>${s.thumb ? `<img class="thumb tiny" src="${esc(s.thumb)}" alt=""/>` : ""}${esc(s.title)}</li>`).join("")}</ol>
    <div class="miss"><span class="miss-text">${a.miss ? "Nothing on this node answers that clearly." : "Not what you needed?"}</span>
      <button class="btn small go ask-fleet" type="button">Ask the fleet</button></div>`;
}

// Stream the node's local-LLM answer into an answer box rendered by answerHtml().
async function streamAnswer(box, base, q) {
  const ai = box.querySelector(".ai"), out = box.querySelector(".ai-text");
  ai.classList.remove("hidden");
  out.innerHTML = '<span class="muted">thinking on the node…</span>';
  let text = "", t0 = performance.now(), first = null;
  const render = () => { out.innerHTML = esc(text).replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>') + '<span class="caret"></span>'; };
  try {
    const res = await fetch(`${base}/ask/stream`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }) });
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.t) { if (first == null) first = performance.now() - t0; text += ev.t; render(); }
        if (ev.done) {
          if (ev.error) { ai.classList.add("hidden"); return; }
          if (ev.not_in_notes) {
            out.innerHTML = '<b>Not in this node\'s memory.</b> The AI checked the closest notes and they don\'t answer this. Ask the fleet: a crew that knows may have it privately.';
            box.querySelector(".instant")?.classList.add("dim");
            const mt = box.querySelector(".miss-text"); if (mt) mt.textContent = "The node's AI says: not in memory.";
            box.querySelector(".ask-fleet")?.classList.add("pulse");
          } else {
            out.innerHTML = esc(text.trim()).replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>');
            box.querySelector(".instant")?.classList.add("dim");
          }
          ai.querySelector(".ai-head .muted").textContent += ` · first words ${Math.round(first ?? ev.ms)} ms · done ${(ev.ms / 1000).toFixed(1)} s`;
        }
      }
    }
  } catch { ai.classList.add("hidden"); }
}

function policyHtml(d) {
  const s = d.scores;
  const bar = (label, v) => `<span>${label}</span><span class="bar"><i style="width:${Math.round(v * 100)}%"></i></span><span class="mono">${v.toFixed(2)}</span>`;
  return `<div><span class="decision">${esc(DECISION_LABEL[d.decision])}</span> · queue priority ${d.priority}</div>` +
    `<div class="bars">${bar("team value", s.team_value)}${bar("fleet demand", s.fleet_demand)}${bar("share score", s.share_score)}</div>` +
    `<div>${d.reasons.map((x) => "• " + esc(x)).join("<br>")}</div>` +
    (d.uploaded_text ? `<div class="redacted">fleet copy: ${esc(d.uploaded_text)}</div>` : "");
}

function reviewHtml(rv) {
  const cards = [];
  for (const s of rv.suggestions) {
    const why = (s.policy?.reasons || []).find((x) => x.includes("searched")) || "";
    cards.push(`<div class="rcard suggest"><span class="kind">Another crew needs this</span>
      <div><b>${esc(s.title)}</b><div class="clamp">${esc(s.text)}</div></div>
      <div class="muted small">${esc(why)}</div>
      <div class="actions"><button class="btn small primary" data-share="${esc(s.doc_id)}">Share with fleet</button></div></div>`);
  }
  for (const c of rv.conflicts) {
    cards.push(`<div class="rcard conflict"><span class="kind">Edit conflict</span>
      <div><b>${esc(c.local.title)}</b> <span class="muted small">· edited here and by ${esc(c.cloud.author_device)} (fleet v${c.cloud.version})</span></div>
      <div class="versions"><div><h4>This node</h4>${esc(c.local.text)}</div><div><h4>Fleet · ${esc(c.cloud.author_device)}</h4>${c.cloud.deleted ? "<i>deleted</i>" : esc(c.cloud.text)}</div></div>
      <div class="actions"><button class="btn small" data-s="mine" data-id="${esc(c.doc_id)}">Keep mine</button>
        <button class="btn small" data-s="theirs" data-id="${esc(c.doc_id)}">Keep theirs</button>
        <button class="btn small primary" data-s="merge" data-id="${esc(c.doc_id)}">Merge both</button></div></div>`);
  }
  const hl = (text, raw) => esc(text).replace(esc(raw), `<mark>${esc(raw)}</mark>`);
  for (const c of rv.contradictions) {
    cards.push(`<div class="rcard contra"><span class="kind">Conflicting values: ${esc(c.detail.mine)} vs ${esc(c.detail.theirs)}</span>
      <div class="versions"><div><h4>${esc(c.doc.title)}</h4>${hl(c.doc.text, c.detail.mine)}</div>
        <div><h4>${esc(c.other.title)}</h4>${hl(c.other.text, c.detail.theirs)}</div></div>
      <div class="muted small">Same topic (${esc(c.detail.about)}), different ${esc(c.detail.unit)} value. Check the SOP before work.</div>
      <div class="actions"><button class="btn small" data-dismiss="${esc(c.doc_id)}|${esc(c.other_id)}">Dismiss</button></div></div>`);
  }
  return cards.join("") || `<div class="empty">Nothing to review.</div>`;
}

function bindReview(box, base, after) {
  box.querySelectorAll("[data-share]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api(`${base}/suggestions/${b.dataset.share}/share`, { method: "POST", timeout: 30000 }); toast("Shared fleet-wide"); }
    catch (e) { toast(e.message, "bad"); }
    after();
  }));
  box.querySelectorAll("[data-s]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`${base}/conflicts/${b.dataset.id}/resolve`, { method: "POST", body: { strategy: b.dataset.s } }); toast(`Resolved: ${b.dataset.s}`); }
    catch (e) { toast(e.message, "bad"); }
    after();
  }));
  box.querySelectorAll("[data-dismiss]").forEach((b) => b.addEventListener("click", async () => {
    const [doc_id, other_id] = b.dataset.dismiss.split("|");
    await api(`${base}/contradictions/dismiss`, { method: "POST", body: { doc_id, other_id } }).catch(() => {});
    after();
  }));
}

function logHtml(l, withDevice = true) {
  const dev = l.device || "";
  return `<li class="${esc(l.level)}"><span class="t">${clock(l.ts)}</span>
    ${withDevice ? `<span class="d" style="color:${devColor(dev)}">${esc(window.devName ? devName(dev) : dev.replace("tablet-", "tab "))}</span>` : ""}
    <span><span class="e">${esc(l.event)}</span> ${esc(l.detail)}</span></li>`;
}

const PALETTE = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ec4899", "#0ea5e9", "#84cc16", "#f97316"];
const _colors = {};
function devColor(id) {
  if (id === "cloud") return "#10b981";
  if (!id || id === "hq-console") return "#94a3b8";
  if (!_colors[id]) {
    const m = /^tablet-([A-Z])$/.exec(id);
    const i = m ? m[1].charCodeAt(0) - 65 : [...id].reduce((a, c) => a + c.charCodeAt(0), 0);
    _colors[id] = PALETTE[i % PALETTE.length];
  }
  return _colors[id];
}

// messages worth a pop-up on a crew's screen
function notable(l) {
  const e = l.event;
  if (e === "received") return ["Received " + l.detail, "ok"];
  if (e === "share suggested") return ["Another crew needs a note: " + l.detail, "accent"];
  if (e === "CONFLICT") return ["Edit conflict: " + l.detail, "bad"];
  if (e === "CONTRADICTION") return ["Conflicting values: " + l.detail, "warn"];
  if (e === "network" && l.level === "warn") return [l.detail, "warn"];
  return null;
}

// Camera/gallery file -> resized JPEG data URL (max 1280 px), done on the phone before upload.
function readPhoto(file, max = 1280) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      resolve(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => reject(new Error("not an image"));
    img.src = URL.createObjectURL(file);
  });
}

const OPEN_HISTORY = new Set();  // timelines stay open across live re-renders

function bindHistory(root, base) {
  root.querySelectorAll("[data-hist]").forEach((b) => {
    b.addEventListener("click", () => toggleHistory(b, base));
    if (OPEN_HISTORY.has(b.dataset.hist)) toggleHistory(b, base, true);
  });
}

async function toggleHistory(b, base, reopen = false) {
    const li = b.closest(".item, .row-detail");
    const old = li.querySelector(".timeline");
    if (old && !reopen) { old.remove(); OPEN_HISTORY.delete(b.dataset.hist); return; }
    if (old) return;
    OPEN_HISTORY.add(b.dataset.hist);
    const rows = await api(`${base}/memory/${b.dataset.hist}/history`).catch(() => []);
    const box = document.createElement("ol");
    box.className = "timeline";
    box.innerHTML = rows.length ? rows.map((h, i) => {
      const prev = rows[i - 1];
      const changed = prev && prev.text !== h.text ? " · text changed" : "";
      return `<li><span class="mono">${clock(h.ts)}</span> <b>${esc(h.event)}</b> <span class="muted">v${h.version} · ${esc(h.author)}${changed}</span>
        ${prev && prev.text !== h.text ? `<div class="clamp">${esc(h.text)}</div>` : ""}</li>`;
    }).join("") : `<li class="muted">No recorded versions on this node yet (fleet memory it has not seen change).</li>`;
    if (!li.querySelector(".timeline")) li.appendChild(box);
}

function bindThumbs(root, base) {
  bindHistory(root, base);
  root.querySelectorAll("img[data-photo]").forEach((im) => im.addEventListener("click", () => window.open(`${base}/photos/${im.dataset.photo}`, "_blank")));
}
