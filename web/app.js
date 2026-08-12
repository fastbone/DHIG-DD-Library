"use strict";

/* ── helpers ─────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const qs = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nfmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
const bytes = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), u.length - 1);
  return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
};
const money = (v) => {
  v = Number(v || 0);
  return `$${v.toFixed(v < 0.01 ? 4 : v < 1 ? 3 : 2)}`;
};
const compact = (n) => {
  n = Number(n || 0);
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
};
const clock = (ts) => new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString();

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 4200);
}

const state = {
  status: null,
  user: null,
  csrf: "",
  docOffset: 0,
  docLimit: 60,
  docTotal: 0,
  history: [],
  running: false,
  pickerPath: null,
};

function authHeaders(extra = {}) {
  const h = { ...extra };
  if (state.csrf) h["X-CSRF-Token"] = state.csrf;
  return h;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: authHeaders({ "Content-Type": "application/json", ...(opts.headers || {}) }),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.replace("/login"); throw new Error("session expired"); }
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || data.detail || res.statusText);
  return data;
}

/* ── theme ───────────────────────────────────────────────────────────── */
const savedTheme = localStorage.getItem("dd-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("themeToggle").onclick = () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
  if (next) document.documentElement.dataset.theme = next;
  else delete document.documentElement.dataset.theme;
  localStorage.setItem("dd-theme", next);
};

/* ── tabs ────────────────────────────────────────────────────────────── */
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  for (const b of $("tabs").children) b.classList.toggle("active", b === btn);
  for (const s of document.querySelectorAll(".tab")) s.classList.toggle("active", s.id === "tab-" + btn.dataset.tab);
  if (btn.dataset.tab === "deliverables") { loadArtifacts(); loadQaLog(); }
  if (btn.dataset.tab === "sweep") loadManifestPreview();
  if (btn.dataset.tab === "admin") loadAdmin();
  // Land in the box, since arriving on this tab means you came to type in it.
  if (btn.dataset.tab === "search") searchMounts.full?.focus();
});

/* ── status ──────────────────────────────────────────────────────────── */
async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); } catch (e) { toast("status: " + e.message, true); return; }
  state.status = s;
  state.user = s.user;
  state.csrf = s.user?.csrf || state.csrf;
  renderUserChip(s.user);
  $("adminTabBtn").classList.toggle("hidden", s.user?.role !== "admin");
  $("accessQuickBtn").classList.toggle("hidden", s.user?.role !== "admin");
  // Reading and exporting the log is for anyone signed in — the point is that an
  // analyst who hits a failure can report it. Deleting it is destructive, so it
  // matches the route's admin dependency rather than offering a button that 403s.
  $("logClear").classList.toggle("hidden", s.user?.role !== "admin");
  $("corpusRoot").textContent = s.corpus_root || "not set";
  if (s.corpus_root && !$("ingestPath").value) $("ingestPath").value = s.corpus_root;
  $("carderModel").textContent = s.models.carder;

  const st = s.stats;
  const pills = [
    ["docs", nfmt(st.documents)],
    ["indexed", nfmt(st.by_status?.carded || 0)],
    ["map", `${nfmt(s.manifest.approx_tokens)} tok`],
    ["map/turn", money(s.manifest_cost_per_turn_usd)],
    ["spent", money(s.lifetime_usage.cost_usd)],
  ];
  // Your own remaining allowance, next to the lifetime total. Shown only when a
  // cap actually applies: a pill reading "unlimited" is noise, and being told the
  // number only at the moment you are refused is the wrong time to learn it.
  const mine = s.budget?.ask;
  let budgetPill = "";
  if (mine && !mine.unlimited) {
    const resets = new Date(s.budget.resets_at * 1000);
    const cls = mine.exhausted ? "warn" : mine.remaining_usd < mine.limit_usd * 0.2 ? "warn" : "";
    budgetPill =
      `<span class="pill ${cls}" title="Your weekly question budget. Resets ${resets.toLocaleString()}` +
      `${s.budget.grace_available ? " · one-time overrun still available" : " · overrun used this week"}">` +
      `<span class="muted">this week</span><b>${money(mine.spent_usd)} / ${money(mine.limit_usd)}</b></span>`;
  }
  $("headPills").innerHTML =
    pills.map(([k, v]) => `<span class="pill"><span class="muted">${k}</span><b>${v}</b></span>`).join("") +
    budgetPill +
    (s.has_api_key
      ? `<span class="pill ok" title="credentials: ${esc(s.credentials)}">${esc(s.models.analyst)}</span>`
      : `<span class="pill warn">no API key</span>`);

  $("statGrid").innerHTML = [
    ["files seen", nfmt(st.files_seen)],
    ["unique docs", nfmt(st.documents)],
    ["indexed", nfmt(st.by_status?.carded || 0)],
    ["citable units", nfmt(st.units)],
    ["extracted chars", compact(st.chars)],
    ["on disk", bytes(st.bytes)],
    ["exact copies", nfmt(st.exact_duplicates)],
    ["version families", nfmt(st.near_dupe_groups)],
    ["flagged", nfmt(st.flagged)],
  ].map(([k, v]) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

  renderBars("workstreamBars", st.by_workstream.map((r) => [r.workstream, r.n]));
  renderBars("familyBars", st.by_family.map((r) => [r.family, r.n]));

  const wsSel = $("docWorkstream");
  if (wsSel.options.length <= 1) {
    for (const w of s.workstreams) wsSel.append(new Option(w, w));
  }
  // Same refill-if-empty rule for the search filters. They are mounted after the
  // first status call, but that call can fail — and then the taxonomy has to
  // arrive with a later poll or the filter stays permanently empty.
  for (const panel of Object.values(searchMounts)) panel?.fillWorkstreams?.();

  $("manifestStats").innerHTML = [
    ["mode", s.manifest.mode],
    ["indexed docs", nfmt(s.manifest.n_indexed)],
    ["not indexed", nfmt(s.manifest.n_unindexed)],
    ["map size", `${nfmt(s.manifest.approx_tokens)} tok`],
    ["cached read / turn", money(s.manifest_cost_per_turn_usd)],
    ["uncached", money((s.manifest.approx_tokens * 5) / 1e6)],
  ].map(([k, v]) => `<div class="stat"><div class="v">${esc(String(v))}</div><div class="k">${k}</div></div>`).join("");
  const modeNote =
    s.manifest.mode === "rollup"
      ? "The catalogue is too large to list inline, so the analyst gets a coverage rollup and pages through it with list_documents."
      : s.manifest.mode === "compact"
        ? "Card lines without summaries, to stay inside the context budget."
        : "Full card lines with summaries — the analyst sees a one-line description of every document.";
  // Below the model's minimum cacheable prefix, cache_control is a silent no-op.
  const tooSmall = s.manifest.approx_tokens > 0 && s.manifest.approx_tokens < 512;
  $("manifestNote").textContent =
    modeNote +
    (tooSmall
      ? "  Note: the map is under ~512 tokens, which is below the minimum cacheable prefix — prompt caching will not engage until the catalogue is larger."
      : "");

  const jobs = Object.keys(s.jobs_running || {});
  if (!jobs.some((j) => j.startsWith("ingest-"))) hideProgress("ingest");
  if (!jobs.some((j) => j.startsWith("sweep-"))) hideProgress("sweep");
  $("sweepStats").innerHTML = (s.recent_jobs || [])
    .filter((j) => j.kind === "sweep")
    .slice(0, 2)
    .map((j) => `<div class="stat"><div class="v">${j.done}/${j.total}</div><div class="k">${esc(j.status)} · ${esc((j.message || "").slice(0, 46))}</div></div>`)
    .join("") || `<div class="stat"><div class="v">—</div><div class="k">no sweep yet</div></div>`;
}

function renderUserChip(user) {
  if (!user) return;
  const chip = $("userChip");
  chip.innerHTML =
    `<span>${esc(user.username)}</span><span class="role">${esc(user.role)}</span>` +
    `<button id="logoutBtn" title="Sign out">⏻</button>`;
  $("logoutBtn").onclick = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ }
    location.replace("/login");
  };
  if (user.must_change_password && !renderUserChip._warned) {
    renderUserChip._warned = true;
    toast("Your password was set by an administrator — change it under Admin.", true);
  }
}

function renderBars(target, pairs) {
  const max = Math.max(1, ...pairs.map((p) => p[1]));
  $(target).innerHTML = pairs.length
    ? pairs.map(([k, n]) => `
      <div class="barrow">
        <span class="muted">${esc(k)}</span>
        <span class="track"><span class="fill" style="width:${(n / max) * 100}%"></span></span>
        <span class="n">${nfmt(n)}</span>
      </div>`).join("")
    : `<span class="muted small">nothing ingested yet</span>`;
}

/* ── live events ─────────────────────────────────────────────────────── */
let statsTimer = null;
let eventsOpenedOnce = false;
function connectEvents() {
  const src = new EventSource("/api/events");
  src.onopen = () => {
    // Log lines are not replayed on the stream — history is a query — so a
    // reconnect has to fetch whatever was written while it was down. The first
    // open needs nothing: boot already loaded the log.
    if (eventsOpenedOnce) catchUpLog();
    eventsOpenedOnce = true;
  };
  src.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.kind === "log") appendLog(ev);
    else if (ev.kind === "job") onJob(ev);
    else if (ev.kind === "stats_dirty") {
      clearTimeout(statsTimer);
      statsTimer = setTimeout(() => { refreshStatus(); loadDocs(); }, 400);
    }
    else if (ev.kind === "archives_dirty") loadArchives();
    else if (ev.kind === "sync_dirty") loadConnections();
  };
  src.onerror = () => { src.close(); setTimeout(connectEvents, 2500); };
}

/* ── activity log ────────────────────────────────────────────────────── */
/* The log is persisted server-side, so this view is a query over history rather
   than a transcript of what happened while the tab was open. Changing a filter
   re-queries: "errors only" has to reach failures that scrolled past hours ago,
   which a client-side filter over the live stream cannot do. */
const logState = { levels: [], source: "", q: "", entries: [], oldestId: null, counts: null };
const LOG_MAX = 3000;

function logMatches(ev) {
  if (logState.levels.length && !logState.levels.includes(ev.level || "info")) return false;
  if (logState.source && (ev.source || "") !== logState.source) return false;
  if (logState.q) {
    const hay = `${ev.message || ""} ${ev.context ? JSON.stringify(ev.context) : ""}`;
    if (!hay.toLowerCase().includes(logState.q.toLowerCase())) return false;
  }
  return true;
}

function logQueryString(extra = {}) {
  const p = new URLSearchParams();
  if (logState.levels.length) p.set("levels", logState.levels.join(","));
  if (logState.source) p.set("source", logState.source);
  if (logState.q) p.set("q", logState.q);
  for (const [k, v] of Object.entries(extra)) if (v != null) p.set(k, v);
  return p.toString();
}

function contextText(ctx) {
  // Traceback last and unquoted: it is the part someone reads, and JSON-escaped
  // newlines make it unreadable in exactly the case that matters.
  const lines = [];
  let tb = null;
  for (const [k, v] of Object.entries(ctx)) {
    if (k === "traceback") { tb = v; continue; }
    if (Array.isArray(v)) {
      lines.push(`${k}: (${v.length})`);
      v.slice(0, 50).forEach((item) => lines.push(`  - ${item}`));
      if (v.length > 50) lines.push(`  … ${v.length - 50} more`);
    } else if (v && typeof v === "object") lines.push(`${k}: ${JSON.stringify(v)}`);
    else lines.push(`${k}: ${v}`);
  }
  if (tb) lines.push("", String(tb).trimEnd());
  return lines.join("\n");
}

function logRow(ev) {
  const level = ev.level || "info";
  const head = [el("span", "t", clock(ev.ts)), el("span", "m", ev.message)];
  if (ev.source) head.splice(1, 0, el("span", "src", ev.source));
  const ctx = ev.context && Object.keys(ev.context).length ? ev.context : null;
  if (!ctx) {
    const row = el("div", level);
    row.append(...head);
    return row;
  }
  const box = el("details", level);
  const sum = el("summary");
  sum.append(...head);
  box.append(sum, el("pre", "ctx", contextText(ctx)));
  return box;
}

function renderLog({ keepScroll = false } = {}) {
  const log = $("log");
  const wasAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const top = log.scrollTop;
  log.textContent = "";
  if (!logState.entries.length) {
    log.append(el("div", "muted", logState.levels.length || logState.q || logState.source
      ? "nothing matches this filter"
      : "no activity recorded yet"));
    return;
  }
  for (const ev of logState.entries) log.append(logRow(ev));
  if (keepScroll) log.scrollTop = top;
  else if ($("logAutoscroll").checked || wasAtBottom) log.scrollTop = log.scrollHeight;
}

function renderLogCounts() {
  const c = logState.counts;
  if (!c) return ($("logCounts").textContent = "");
  const parts = [];
  if (c.error) parts.push(`${nfmt(c.error)} error${c.error === 1 ? "" : "s"}`);
  if (c.warn) parts.push(`${nfmt(c.warn)} warning${c.warn === 1 ? "" : "s"}`);
  parts.push(`${nfmt(c.total)} kept`);
  $("logCounts").textContent = parts.join(" · ");
  const sel = $("logSource");
  const want = ["", ...(c.sources || [])];
  if (sel.options.length !== want.length) {
    const current = sel.value;
    sel.textContent = "";
    for (const s of want) {
      const o = el("option", null, s || "every source");
      o.value = s;
      sel.append(o);
    }
    sel.value = want.includes(current) ? current : "";
  }
}

const logNewestId = () =>
  logState.entries.reduce((n, e) => (e.id != null && e.id > n ? e.id : n), 0);

/* The paging cursor is derived from what is actually on screen, never from what
   was fetched. Trimming to LOG_MAX can discard part of a page, and a cursor that
   advanced past discarded rows would make every further click page over history
   nobody ever saw. */
function syncOldestId() {
  const ids = logState.entries.map((e) => e.id).filter((n) => n != null);
  logState.oldestId = ids.length ? Math.min(...ids) : null;
}

async function loadLog({ older = false } = {}) {
  try {
    const qs = logQueryString(older ? { before_id: logState.oldestId, limit: 200 } : { limit: 300 });
    const r = await api(`/api/logs?${qs}`);
    const fetched = (r.entries || []).slice().reverse();   // server sends newest first
    if (older) {
      // Trim from the newest end, not the oldest: the click asked to see further
      // back, so that is the end worth keeping.
      logState.entries = fetched.concat(logState.entries).slice(0, LOG_MAX);
    } else {
      // A line can arrive over SSE while this request is in flight. Every stored
      // line carries its row id, so anything already held that the query did not
      // return is newer, not a duplicate — keep it rather than dropping a failure
      // the operator just watched happen.
      const seen = new Set(fetched.map((e) => e.id).filter((n) => n != null));
      const newest = fetched.length ? Math.max(...fetched.map((e) => e.id ?? 0)) : 0;
      const live = logState.entries.filter((e) => (e.id ?? Infinity) > newest && !seen.has(e.id));
      logState.entries = fetched.concat(live);
    }
    syncOldestId();
    logState.counts = r.counts;
    renderLogCounts();
    renderLog({ keepScroll: older });
    $("logMore").disabled = !logState.oldestId || fetched.length === 0;
    $("logHint").textContent = fetched.length === 0 && older
      ? "no older lines"
      : `keeping the last ${nfmt(r.retention)} lines`;
  } catch (e) {
    $("logHint").textContent = `could not load the log: ${e.message}`;
  }
}

/* Called when the event stream reconnects. Lines written while it was down were
   never streamed and are not replayed, so without this they would stay invisible
   until a filter change or a reload — a failure disappearing from a live view is
   the one thing this panel exists to prevent. Only what is newer than the newest
   line held is fetched, so the view (including any "load older" pages) survives. */
async function catchUpLog() {
  const after = logNewestId();
  if (!after) return loadLog();
  try {
    const LIMIT = 300;
    const r = await api(`/api/logs?${logQueryString({ after_id: after, limit: LIMIT })}`);
    const fetched = (r.entries || []).slice().reverse();
    // A full page back means the gap may be longer than one page; a plain reload
    // is then both simpler and correct.
    if (fetched.length >= LIMIT) return loadLog();
    for (const ev of fetched) appendLog(ev);
    // After the appends, not before: appendLog adjusts the counts itself, so
    // overwriting them first would count the caught-up lines twice.
    logState.counts = r.counts;
    renderLogCounts();
  } catch { /* the next reconnect tries again */ }
}

function appendLog(ev) {
  if (logState.counts) {
    const lv = ev.level || "info";
    if (lv in logState.counts) logState.counts[lv] += 1;
    logState.counts.total += 1;
    renderLogCounts();
  }
  if (!logMatches(ev)) return;
  if (ev.id != null && logState.entries.some((e) => e.id === ev.id)) return;
  logState.entries.push(ev);
  if (logState.entries.length > LOG_MAX) logState.entries.shift();
  const log = $("log");
  if (log.children.length === 1 && log.firstChild.classList?.contains("muted")) log.textContent = "";
  log.append(logRow(ev));
  while (log.children.length > LOG_MAX) log.firstChild.remove();
  if ($("logAutoscroll").checked) log.scrollTop = log.scrollHeight;
}

let logSearchTimer = null;
$("logLevelFilter").onclick = (e) => {
  const btn = e.target.closest("button[data-levels]");
  if (!btn) return;
  for (const b of $("logLevelFilter").querySelectorAll("button")) b.classList.toggle("on", b === btn);
  logState.levels = btn.dataset.levels ? btn.dataset.levels.split(",") : [];
  loadLog();
};
$("logSource").onchange = () => { logState.source = $("logSource").value; loadLog(); };
$("logSearch").oninput = () => {
  clearTimeout(logSearchTimer);
  logSearchTimer = setTimeout(() => { logState.q = $("logSearch").value.trim(); loadLog(); }, 250);
};
$("logMore").onclick = () => loadLog({ older: true });

async function logExportText() {
  const res = await fetch(`/api/logs/export?${logQueryString({ limit: 2000 })}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) { location.replace("/login"); throw new Error("session expired"); }
  if (!res.ok) throw new Error(await res.text());
  return res.text();
}

$("logCopy").onclick = async () => {
  try {
    const text = await logExportText();
    // The clipboard API needs a secure context, and this app is often reached over
    // plain HTTP on a LAN address. Fall back to a hidden textarea rather than
    // failing with a permissions error the operator can do nothing about.
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const ta = el("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast(`Copied ${nfmt(text.split("\n").length)} lines — paste into the bug report`);
  } catch (e) { toast(e.message, true); }
};

$("logDownload").onclick = async () => {
  try {
    const text = await logExportText();
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = el("a");
    a.href = url;
    a.download = `dd-library-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.txt`;
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { toast(e.message, true); }
};

$("logClear").onclick = async () => {
  const scope = logState.levels.length ? logState.levels.join(" and ") + " lines" : "the whole log";
  if (!confirm(`Delete ${scope}? Exported or copied reports are unaffected.`)) return;
  try {
    const r = await api("/api/logs/clear", { method: "POST", body: { levels: logState.levels } });
    toast(`Removed ${nfmt(r.removed)} line(s)`);
    await loadLog();
  } catch (e) { toast(e.message, true); }
};

const JOB_UI = { ingest: "ingest", sweep: "sweep", extract: "extract", sync: "sync" };

function onJob(ev) {
  const which = JOB_UI[ev.job_kind] || "sweep";
  const wrap = $(which + "Progress");
  wrap.classList.remove("hidden");
  const pct = ev.total ? Math.round((ev.done / ev.total) * 100) : 0;
  $(which + "Bar").style.width = pct + "%";
  const bits = [];
  if (ev.status === "running") bits.push(`${nfmt(ev.done)} / ${nfmt(ev.total)}`, `${pct}%`);
  // "skipped" means unchanged files during ingest, but refused members during
  // extraction — same field, different meaning.
  if (ev.skipped) bits.push(`${ev.skipped} ${which === "extract" ? "refused" : "unchanged"}`);
  if (ev.job_kind === "sync" && ev.deleted) bits.push(`${ev.deleted} deleted`);
  if (ev.failed) bits.push(`${ev.failed} failed`);
  if (ev.bytes_done && ev.status === "running") bits.push(bytes(ev.bytes_done));
  if (ev.usage?.cost_usd != null) bits.push(money(ev.usage.cost_usd));
  if (ev.message) bits.push(ev.message);
  $(which + "Meta").textContent = bits.join(" · ");
  const cancelBtn = $(which + "Cancel");
  if (cancelBtn) cancelBtn.dataset.job = ev.job_id;
  if (ev.job_kind === "sync") onSyncJobDetail(ev);
  // The ingest chained onto a sync writes the indexed and dropped counts onto the
  // run row after the sync's own terminal event, so an open detail view needs a
  // second look once that finishes.
  if (ev.job_kind === "ingest" && ev.status !== "running" && runState.connId) {
    setTimeout(() => loadRunList({ select: runState.runId }), 400);
  }
  if (ev.status !== "running") {
    hideProgress(which);
    setTimeout(() => { refreshStatus(); loadDocs(); loadArchives(); loadConnections(); }, 300);
  }
}

/* Keep an open detail modal live from the event stream rather than polling: the
   event already carries the figures, so a watched sync updates on rclone's own
   2-second cadence at no request cost. */
function onSyncJobDetail(ev) {
  if (!runState.connId || ev.conn_id !== runState.connId) return;
  if (ev.status === "running") {
    if (runState.runId && runState.runId !== ev.job_id) {
      // A different run started while this was open. Follow it only if what is on
      // screen has already finished — otherwise the reader deliberately opened
      // something else and should keep it.
      if (runState.current && runState.current.status === "running") return;
      runState.current = null;
      loadRunList({ listOnly: true });
    }
    runState.runId = ev.job_id;
    // Only carry forward what belongs to *this* run. Merging whatever was last
    // rendered would paint a previous run's changed-file list and start time onto
    // a new live one, and it would stick.
    const cur = runState.current && runState.current.id === ev.job_id
      ? runState.current : {};
    renderRunDetail({
      ...cur,
      id: ev.job_id, status: "running", live: true,
      started_at: cur.started_at || Date.now() / 1000 - (ev.elapsed_s || 0),
      transferred: ev.done, unchanged: ev.skipped, deleted: ev.deleted, errors: ev.failed,
      bytes: ev.bytes_done, transferring: ev.transferring || [],
      speed_bps: ev.speed_bps, eta_s: ev.eta_s, elapsed_s: ev.elapsed_s,
      library_bytes: ev.library_bytes ?? cur.library_bytes,
      message: ev.message,
    });
  } else {
    // Finished: re-read *this* run explicitly, because loadRunList keeps whatever
    // is already selected and the pane would otherwise sit on its last live frame
    // — still labelled running, with no change list — which is exactly the answer
    // someone watched the sync to get.
    //
    // But only if it is still the run on screen when the timer fires. Four hundred
    // milliseconds is long enough to click another run in the history, and
    // overwriting that choice is the same hijack the live path refuses to do.
    setTimeout(() => {
      if (runState.runId === ev.job_id) loadRunList({ select: ev.job_id });
      else loadRunList({ listOnly: true });   // its row still needs its final status
    }, 400);
  }
}
function hideProgress(which) {
  const btn = { ingest: "ingestBtn", sweep: "sweepBtn" }[which];
  if (btn && $(btn)) $(btn).disabled = false;
}

for (const which of ["ingest", "sweep", "extract", "sync"]) {
  const btn = $(which + "Cancel");
  if (!btn) continue;
  btn.onclick = async (e) => {
    const job = e.target.dataset.job;
    if (!job) return;
    try { await api(`/api/jobs/${job}/cancel`, { method: "POST" }); } catch (err) { toast(err.message, true); }
  };
}

/* ── ingest ──────────────────────────────────────────────────────────── */
$("ingestBtn").onclick = async () => {
  const path = $("ingestPath").value.trim();
  if (!path) return toast("Pick a folder first", true);
  $("ingestBtn").disabled = true;
  $("ingestProgress").classList.remove("hidden");
  $("ingestMeta").textContent = "starting…";
  try {
    await api("/api/ingest", { method: "POST", body: { path, ocr: $("ocrToggle").checked } });
  } catch (e) {
    toast(e.message, true);
    $("ingestBtn").disabled = false;
  }
};
$("dedupeBtn").onclick = async () => {
  try {
    const r = await api("/api/dedupe", { method: "POST" });
    toast(`${r.exact_duplicates} exact, ${r.near_duplicate_extras} near duplicates`);
    refreshStatus(); loadDocs();
  } catch (e) { toast(e.message, true); }
};
$("sweepBtn").onclick = async () => {
  $("sweepBtn").disabled = true;
  $("sweepProgress").classList.remove("hidden");
  $("sweepMeta").textContent = "starting…";
  try {
    const r = await api("/api/sweep", { method: "POST", body: { redo: $("sweepRedo").checked } });
    if (!r.pending) toast("Nothing to index");
  } catch (e) {
    toast(e.message, true);
    $("sweepBtn").disabled = false;
  }
};

async function loadManifestPreview() {
  try {
    const m = await api("/api/manifest?full=true");
    $("manifestPreview").textContent = (m.text || "—").slice(0, 20000);
  } catch { /* ignore */ }
}

/* ── archive upload ──────────────────────────────────────────────────── */
const dz = $("dropzone");
$("fileInput").onchange = (e) => { if (e.target.files[0]) uploadArchive(e.target.files[0]); };
dz.onclick = () => $("fileInput").click();
for (const ev of ["dragenter", "dragover"]) {
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); });
}
for (const ev of ["dragleave", "drop"]) {
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); });
}
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer?.files?.[0];
  if (f) uploadArchive(f);
});

function uploadArchive(file) {
  const limit = (state.status?.max_upload_mb || 4096) * 1024 * 1024;
  if (file.size > limit) {
    return toast(`${file.name} is larger than the ${state.status.max_upload_mb} MB limit`, true);
  }
  const form = new FormData();
  form.append("file", file);
  form.append("auto_extract", $("autoExtract").checked ? "true" : "false");
  form.append("auto_ingest", $("autoIngest").checked ? "true" : "false");

  $("uploadProgress").classList.remove("hidden");
  $("uploadMeta").textContent = `uploading ${file.name} …`;

  // XHR rather than fetch: upload progress events are not available on fetch.
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/archives");
  if (state.csrf) xhr.setRequestHeader("X-CSRF-Token", state.csrf);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    $("uploadBar").style.width = pct + "%";
    $("uploadMeta").textContent = `uploading ${file.name} · ${bytes(e.loaded)} / ${bytes(e.total)} · ${pct}%`;
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
    if (xhr.status === 401) return location.replace("/login");
    if (xhr.status >= 400) {
      $("uploadMeta").textContent = "";
      $("uploadProgress").classList.add("hidden");
      return toast(data.error || data.detail || `upload failed (${xhr.status})`, true);
    }
    $("uploadBar").style.width = "100%";
    $("uploadMeta").textContent = `uploaded ${file.name} (${bytes(data.archive?.size_bytes)})`;
    setTimeout(() => $("uploadProgress").classList.add("hidden"), 4000);
    $("fileInput").value = "";
    loadArchives();
  };
  xhr.onerror = () => {
    $("uploadProgress").classList.add("hidden");
    toast("upload failed — connection error", true);
  };
  xhr.send(form);
}

async function loadArchives() {
  let r;
  try { r = await api("/api/archives"); } catch { return; }
  $("dropHint").textContent =
    `${r.accepted.join(" · ")} — up to ${r.max_upload_mb} MB, extracted into ${r.extract_root}`;
  const box = $("archiveList");
  if (!r.archives.length) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  for (const a of r.archives) {
    const row = el("div", "rowitem");
    const state_tag =
      a.status === "extracted" ? `<span class="tag">${a.n_files} files</span>`
      : a.status === "failed" ? `<span class="tag bad">failed</span>`
      : `<span class="tag dupe">${esc(a.status)}</span>`;
    const meta = el("div", "meta");
    meta.innerHTML = `<div class="t">${esc(a.filename)} ${state_tag}</div>
      <div class="muted small">${bytes(a.size_bytes)} · ${new Date(a.created_at * 1000).toLocaleString()}
      ${a.uploaded_by ? "· " + esc(a.uploaded_by) : ""}
      ${a.n_skipped ? `· <span class="tag flag">${a.n_skipped} skipped</span>` : ""}
      ${a.error ? "· " + esc(a.error.slice(0, 120)) : ""}</div>
      ${a.extract_dir ? `<div class="doc-path">${esc(a.extract_dir)}</div>` : ""}`;
    const actions = el("div", "actions");

    if (a.extract_dir) {
      const useBtn = el("button", "ghost", "use as corpus");
      useBtn.onclick = async () => {
        try {
          await api("/api/corpus-root", { method: "POST", body: { path: a.extract_dir } });
          $("ingestPath").value = a.extract_dir;
          toast("Corpus root set — press Ingest");
          refreshStatus();
        } catch (e) { toast(e.message, true); }
      };
      actions.append(useBtn);
    }
    const exBtn = el("button", "ghost", a.extract_dir ? "re-extract" : "extract");
    exBtn.onclick = async () => {
      try {
        await api(`/api/archives/${a.id}/extract`, {
          method: "POST", body: { auto_ingest: $("autoIngest").checked },
        });
        $("extractProgress").classList.remove("hidden");
        $("extractMeta").textContent = "starting …";
      } catch (e) { toast(e.message, true); }
    };
    const delBtn = el("button", "danger", "delete");
    delBtn.onclick = async () => {
      const withDir = a.extract_dir
        ? confirm(`Delete ${a.filename} AND its extracted folder?\n\n${a.extract_dir}\n\n` +
                  "OK = remove both, Cancel = keep the extracted folder.")
        : false;
      try {
        await api(`/api/archives/${a.id}?drop_extracted=${withDir}`, { method: "DELETE" });
        loadArchives(); refreshStatus();
      } catch (e) { toast(e.message, true); }
    };
    actions.append(exBtn, delBtn);
    row.append(meta, actions);
    box.append(row);
  }
}

/* ── connected SharePoint libraries ──────────────────────────────────── */
const INTERVALS = { 0: "manual", 60: "hourly", 240: "every 4h", 720: "every 12h", 1440: "daily" };

async function loadConnections() {
  // Admin-only route: for an analyst this 403s, and the section stays hidden.
  let r;
  try { r = await api("/api/sync/connections"); } catch { return; }
  $("syncSection").classList.remove("hidden");
  const engine = r.rclone || {};
  $("syncHint").innerHTML = engine.available
    ? `Mirrored into <code>${esc(r.sync_root)}</code>, then indexed like any other folder ·
       up to ${r.max_sync_gb} GB`
    : `<span class="tag bad">rclone not installed</span> the sync engine is missing
       (<code>${esc(engine.bin || "rclone")}</code>), so syncing will fail — rebuild the image`;

  const box = $("syncList");
  box.innerHTML = "";
  if (!r.connections.length) {
    box.innerHTML = `<span class="muted small">no libraries connected yet</span>`;
    return;
  }
  for (const c of r.connections) {
    const row = el("div", "rowitem");
    const tag =
      c.status === "syncing" ? `<span class="tag dupe">syncing</span>`
      : c.status === "failed" ? `<span class="tag bad">failed</span>`
      : c.last_test_ok === false ? `<span class="tag flag">unreachable</span>`
      : c.last_sync_at ? `<span class="tag">${nfmt(c.n_files)} files</span>`
      : `<span class="tag dupe">never synced</span>`;
    const meta = el("div", "meta");
    meta.innerHTML = `<div class="t">${esc(c.label)} ${tag}</div>
      <div class="muted small">
        ${esc(c.drive_name || c.library || "default library")} ·
        secret …${esc(c.secret_last4)} ·
        ${INTERVALS[c.interval_minutes] || `every ${c.interval_minutes} min`}
        ${c.last_sync_at ? "· synced " + new Date(c.last_sync_at * 1000).toLocaleString() : ""}
        ${c.bytes_total ? "· " + bytes(c.bytes_total) : ""}
        ${c.n_deleted ? `· <span class="tag flag">${c.n_deleted} removed remotely</span>` : ""}
      </div>
      <div class="doc-path">${esc(c.site_url)}</div>
      ${c.error ? `<div class="muted small">${esc(c.error.slice(0, 160))}</div>` : ""}
      ${c.last_test_note && c.last_test_ok === false
        ? `<div class="muted small">${esc(c.last_test_note.slice(0, 160))}</div>` : ""}`;

    const actions = el("div", "actions");
    const syncBtn = el("button", "primary", "sync now");
    syncBtn.onclick = async () => {
      try {
        await api(`/api/sync/connections/${c.id}/sync`, { method: "POST" });
        $("syncProgress").classList.remove("hidden");
        $("syncMeta").textContent = "starting …";
      } catch (e) { toast(e.message, true); }
    };
    const detailBtn = el("button", "ghost", c.status === "syncing" ? "watch" : "detail");
    detailBtn.onclick = () => openRunModal(c);
    const testBtn = el("button", "ghost", "test");
    testBtn.onclick = async () => {
      testBtn.disabled = true;
      try {
        const out = await api(`/api/sync/connections/${c.id}/test`, { method: "POST" });
        toast(out.ok ? out.note : out.note, !out.ok);
        loadConnections();
      } catch (e) { toast(e.message, true); } finally { testBtn.disabled = false; }
    };
    const editBtn = el("button", "ghost", "edit");
    editBtn.onclick = () => openConnectModal(c);
    const useBtn = el("button", "ghost", "use as corpus");
    useBtn.onclick = async () => {
      try {
        await api("/api/corpus-root", { method: "POST", body: { path: c.mirror_dir } });
        $("ingestPath").value = c.mirror_dir;
        toast("Corpus root set — press Ingest");
        refreshStatus();
      } catch (e) { toast(e.message, true); }
    };
    const delBtn = el("button", "danger", "remove");
    delBtn.onclick = async () => {
      const withMirror = confirm(
        `Remove the connection to ${c.label}?\n\n` +
        `OK = also delete the mirrored files at ${c.mirror_dir}\n` +
        `Cancel = keep the mirrored files on disk`);
      try {
        await api(`/api/sync/connections/${c.id}?drop_mirror=${withMirror}`, { method: "DELETE" });
        loadConnections(); refreshStatus();
      } catch (e) { toast(e.message, true); }
    };
    actions.append(syncBtn, detailBtn, testBtn, editBtn, useBtn, delBtn);
    row.append(meta, actions);
    box.append(row);
  }
}

/* ── sync run detail ─────────────────────────────────────────────────── */
/* "Did the sync work" is answered by the row. "What did it change" is not, and
   that is the question someone has on a Monday. The modal shows one run — live if
   it is still going, otherwise as it finished — with the history beside it. */
const runState = { connId: null, runId: null, label: "" };

const secs = (s) =>
  s == null ? "—" : s < 60 ? `${Math.round(s)}s`
  : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
  : `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;

const RUN_STATUS = { ok: "ok", running: "dupe", failed: "bad", cancelled: "flag" };

function runRow(run, active) {
  const item = el("div", "rowitem" + (active ? " on" : ""));
  const when = new Date(run.started_at * 1000);
  const tag = RUN_STATUS[run.status] || "dupe";
  item.innerHTML = `<div class="meta">
      <div class="t"><span class="tag ${tag}">${esc(run.status)}</span>
        ${when.toLocaleDateString([], { day: "numeric", month: "short" })}
        ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>
      <div class="muted small">${nfmt(run.transferred)} transferred${
        run.deleted ? ` · ${nfmt(run.deleted)} deleted` : ""}${
        run.errors ? ` · ${nfmt(run.errors)} errors` : ""}
        ${run.actor ? "· " + esc(run.actor) : ""}</div>
    </div>`;
  item.onclick = () => loadRun(run.id);
  return item;
}

function renderRunDetail(run) {
  runState.current = run;
  const box = $("runDetail");
  const dur = run.finished_at ? run.finished_at - run.started_at : run.elapsed_s;
  const stats = [
    ["transferred", nfmt(run.transferred)],
    ["unchanged", nfmt(run.unchanged)],
    ["deleted", nfmt(run.deleted)],
    ["errors", nfmt(run.errors)],
    ["moved", bytes(run.bytes)],
    ["duration", secs(dur)],
  ];
  if (run.live) {
    stats.push(["rate", run.speed_bps ? `${bytes(run.speed_bps)}/s` : "—"]);
    stats.push(["eta", secs(run.eta_s)]);
  }
  // Indexing is a separate step chained onto the sync, and "12 files copied" is
  // not the same fact as "9 new documents indexed" — both get asked about.
  if (run.indexed_new != null) stats.push(["indexed", nfmt(run.indexed_new)]);
  if (run.purged) stats.push(["dropped", nfmt(run.purged)]);

  const changes = run.changes || [];
  const errs = run.error_lines || [];
  box.innerHTML = `
    <div class="row between wrap">
      <div><span class="tag ${RUN_STATUS[run.status] || "dupe"}">${esc(run.status)}</span>
        <span class="muted small">${new Date(run.started_at * 1000).toLocaleString()}
        ${run.actor ? "· started by " + esc(run.actor) : ""}</span></div>
      ${run.live ? '<span class="muted small">updating live</span>' : ""}
    </div>
    ${run.message ? `<p class="muted small">${esc(run.message)}</p>` : ""}
    ${run.error ? `<div class="notice err">${esc(run.error)}</div>` : ""}
    <div class="stat-grid tight">${stats.map(([k, v]) =>
      `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("")}</div>
    ${run.library_files != null
      ? `<p class="muted small">Library holds ${nfmt(run.library_files)} matching file(s),
         ${bytes(run.library_bytes)}.</p>` : ""}
    ${run.live && (run.transferring || []).length ? `
      <h3>In flight</h3>
      <div class="bars">${run.transferring.map((t) => `
        <div class="barrow">
          <span class="doc-path" title="${esc(t.name)}">${esc(t.name.split("/").pop())}</span>
          <span class="track"><span class="fill" style="width:${t.percentage}%"></span></span>
          <span class="n">${t.percentage}%</span>
        </div>`).join("")}</div>` : ""}
    ${errs.length ? `
      <h3>Errors (${errs.length})</h3>
      <pre class="pre-scroll">${esc(errs.join("\n"))}</pre>` : ""}
    <h3>Changed files ${changes.length ? `(${changes.length}${
      changes.length >= 500 ? "+, capped" : ""})` : ""}</h3>
    ${changes.length ? `
      <div class="table-scroll"><table><thead><tr><th>What</th><th>Path</th></tr></thead>
        <tbody>${changes.map((c) => `<tr>
          <td><span class="tag ${c.op === "deleted" ? "bad" : "dupe"}">${esc(c.op)}</span></td>
          <td class="doc-path">${esc(c.path)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted small">${run.status === "running"
          ? "Nothing moved yet."
          : "Nothing changed — every file was already mirrored."}</p>`}`;
}

async function loadRun(runId) {
  runState.runId = runId;
  try {
    const r = await api(`/api/sync/runs/${runId}`);
    renderRunDetail(r.run);
    for (const item of $("runList").children) item.classList.remove("on");
    const idx = [...$("runList").children].findIndex((n) => n.dataset.run === runId);
    if (idx >= 0) $("runList").children[idx].classList.add("on");
  } catch (e) {
    $("runDetail").innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
  }
}

async function loadRunList({ select = null, listOnly = false } = {}) {
  if (!runState.connId) return;
  try {
    const r = await api(`/api/sync/connections/${runState.connId}/runs`);
    const box = $("runList");
    box.innerHTML = "";
    if (!r.runs.length) {
      box.innerHTML = `<span class="muted small">this library has not synced yet</span>`;
      $("runDetail").innerHTML =
        `<p class="muted small">Press <strong>sync now</strong> on the connection to mirror it
         for the first time. The detail of every run appears here afterwards.</p>`;
      return;
    }
    for (const run of r.runs) {
      const node = runRow(run, run.id === runState.runId);
      node.dataset.run = run.id;
      box.append(node);
    }
    // A caller may name the run to show — a run that has just finished, whose
    // stored detail now differs from what is on screen. Otherwise default to
    // whatever is running, else the most recent, which is what the person opening
    // this is asking about in both cases.
    // listOnly refreshes the navigation and leaves the pane alone — used when a
    // live event is already driving it. Without that, this would helpfully load
    // "the newest run" over the top of the running one the event is painting.
    if (listOnly) return;
    if (select) {
      await loadRun(select);
    } else if (!runState.runId || !r.runs.some((x) => x.id === runState.runId)) {
      await loadRun(r.running_id || r.runs[0].id);
    }
  } catch (e) {
    $("runDetail").innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
  }
}

function openRunModal(conn) {
  runState.connId = conn.id;
  runState.runId = null;
  runState.current = null;
  runState.label = conn.label;
  $("runTitle").textContent = `${conn.label} — sync detail`;
  $("runList").innerHTML = "";
  $("runDetail").innerHTML = `<span class="muted small">loading…</span>`;
  $("runModal").classList.add("open");
  loadRunList();
}

function closeRunModal() {
  $("runModal").classList.remove("open");
  runState.connId = null;
  runState.runId = null;
  runState.current = null;
}

$("runClose").onclick = closeRunModal;
$("runModal").onclick = (e) => { if (e.target.id === "runModal") closeRunModal(); };

let editingConnection = null;

function openConnectModal(conn = null) {
  editingConnection = conn;
  $("connectTitle").textContent = conn ? `Edit ${conn.label}` : "Connect a SharePoint library";
  $("connectSave").textContent = conn ? "Save" : "Connect";
  // On edit the stored secret is not retrievable, so blank means "keep it".
  $("cxSecretHint").textContent = conn ? "leave blank to keep the stored one" : "";
  $("cxLabel").value = conn?.label || "";
  $("cxSite").value = conn?.site_url || "";
  $("cxLibrary").value = conn?.library || "";
  $("cxTenant").value = conn?.tenant || "";
  $("cxClientId").value = conn?.client_id || "";
  $("cxSecret").value = "";
  $("cxSupportedOnly").checked = conn ? conn.only_supported_types : true;
  $("cxMirrorDeletions").checked = conn ? conn.mirror_deletions : true;
  $("cxInterval").value = String(conn?.interval_minutes ?? 0);
  $("connectStatus").textContent = "";
  $("connectModal").classList.add("open");
}

function closeConnectModal() {
  $("connectModal").classList.remove("open");
  $("cxSecret").value = "";
  editingConnection = null;
}

$("addConnectionBtn").onclick = () => openConnectModal();
$("connectClose").onclick = closeConnectModal;

$("connectSave").onclick = async () => {
  const body = {
    label: $("cxLabel").value.trim(),
    site_url: $("cxSite").value.trim(),
    // Empty string, never null: the patch route drops nulls so that untouched
    // fields keep their stored value, which would make clearing this impossible.
    // The server reads "" as "back to the site's default library".
    library: $("cxLibrary").value.trim(),
    tenant: $("cxTenant").value.trim(),
    client_id: $("cxClientId").value.trim(),
    only_supported_types: $("cxSupportedOnly").checked,
    mirror_deletions: $("cxMirrorDeletions").checked,
    interval_minutes: Number($("cxInterval").value),
  };
  const secret = $("cxSecret").value;
  if (secret) body.secret = secret;
  if (!editingConnection && !secret) {
    $("connectStatus").textContent = "a client secret is required";
    return;
  }
  $("connectSave").disabled = true;
  $("connectStatus").textContent = editingConnection ? "saving …" : "connecting and testing …";
  try {
    if (editingConnection) {
      await api(`/api/sync/connections/${editingConnection.id}`, { method: "POST", body });
      toast("Connection updated");
    } else {
      const out = await api("/api/sync/connections", { method: "POST", body });
      toast(out.test?.ok ? `Connected — ${out.test.note}` : `Stored, but ${out.test?.note}`,
            !out.test?.ok);
    }
    closeConnectModal();
    loadConnections();
  } catch (e) {
    $("connectStatus").textContent = e.message;
  } finally {
    $("connectSave").disabled = false;
  }
};

/* ── folder picker ───────────────────────────────────────────────────── */
$("browseBtn").onclick = () => openPicker($("ingestPath").value || "");
$("pickerClose").onclick = () => $("picker").classList.remove("open");
$("pickerUp").onclick = () => openPicker(state.pickerParent || "");
$("pickerUse").onclick = () => {
  if (!state.pickerPath) return toast("Pick a folder first", true);
  $("ingestPath").value = state.pickerPath;
  $("picker").classList.remove("open");
};
async function openPicker(path) {
  try {
    const r = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
    state.pickerPath = r.path;
    state.pickerParent = r.parent;
    $("pickerPath").textContent = r.path || "permitted roots";
    const counts = Object.entries(r.supported_files_here || {});
    $("pickerHere").textContent = !r.path
      ? "Browsing is limited to these roots (DD_BROWSE_ROOTS)."
      : counts.length
        ? "here: " + counts.map(([k, v]) => `${v}× ${k}`).join(", ")
        : "no supported files directly in this folder (subfolders are still scanned)";
    $("pickerList").innerHTML = r.dirs.length
      ? r.dirs.map((d) => `<div data-path="${esc(d.path)}">${esc(d.name)}${r.path ? "/" : ""}</div>`).join("")
      : `<div class="muted">no subfolders</div>`;
    for (const d of $("pickerList").children) {
      if (d.dataset.path) d.onclick = () => openPicker(d.dataset.path);
    }
    $("pickerUp").classList.toggle("hidden", !r.parent);
    $("picker").classList.add("open");
  } catch (e) { toast(e.message, true); }
}

/* ── documents table ─────────────────────────────────────────────────── */
let docDebounce;
for (const id of ["docQuery", "docWorkstream", "docStatus", "docDupes", "docFlagged"]) {
  $(id).addEventListener("input", () => {
    clearTimeout(docDebounce);
    docDebounce = setTimeout(() => { state.docOffset = 0; loadDocs(); }, 220);
  });
}
$("docPrev").onclick = () => { state.docOffset = Math.max(0, state.docOffset - state.docLimit); loadDocs(); };
$("docNext").onclick = () => {
  if (state.docOffset + state.docLimit < state.docTotal) { state.docOffset += state.docLimit; loadDocs(); }
};

async function loadDocs() {
  const p = new URLSearchParams({
    limit: state.docLimit, offset: state.docOffset,
    duplicates: $("docDupes").value,
  });
  if ($("docQuery").value.trim()) p.set("query", $("docQuery").value.trim());
  if ($("docWorkstream").value) p.set("workstream", $("docWorkstream").value);
  if ($("docStatus").value) p.set("status", $("docStatus").value);
  if ($("docFlagged").checked) p.set("flagged", "true");

  let r;
  try { r = await api("/api/documents?" + p); } catch (e) { return toast(e.message, true); }
  state.docTotal = r.total;
  const tb = qs("#docTable tbody");
  tb.innerHTML = "";
  for (const d of r.documents) {
    const tr = el("tr");
    tr.onclick = () => openDoc(d.id);
    const badge =
      d.status === "carded" ? "" :
      d.status === "extracted" ? `<span class="tag dupe">not indexed</span>` :
      `<span class="tag bad">${esc(d.status)}</span>`;
    tr.innerHTML = `
      <td><div class="doc-title">${esc(d.title || d.filename)}</div>
          <div class="doc-path">${esc(d.rel_path)}</div>
          ${d.summary ? `<div class="muted small">${esc(d.summary.slice(0, 150))}</div>` : ""}</td>
      <td>${d.workstream ? `<span class="tag">${esc(d.workstream)}</span>` : badge}</td>
      <td class="small">${esc(d.doc_type || d.family)}</td>
      <td class="small">${esc(d.period_covered || "—")}</td>
      <td>${(d.card_flags || []).map((f) => `<span class="tag flag">${esc(f)}</span>`).join(" ")}
          ${d.dupe_group ? `<span class="tag dupe">version family</span>` : ""}
          ${d.n_paths > 1 ? `<span class="tag dupe">${d.n_paths}× filed</span>` : ""}
          ${d.workstream ? badge : ""}</td>
      <td class="num small">${nfmt(d.n_units)}</td>
      <td class="num small">${bytes(d.size_bytes)}</td>`;
    tb.append(tr);
  }
  const from = r.total ? state.docOffset + 1 : 0;
  $("docCount").textContent = `${nfmt(from)}–${nfmt(Math.min(state.docOffset + state.docLimit, r.total))} of ${nfmt(r.total)}`;
  $("docPrev").disabled = state.docOffset === 0;
  $("docNext").disabled = state.docOffset + state.docLimit >= r.total;
}

/* ── document drawer ─────────────────────────────────────────────────── */
$("drawerClose").onclick = closeDrawer;
$("scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); $("picker").classList.remove("open"); } });
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("scrim").classList.remove("open");
}

async function openDoc(docId, anchor) {
  let card;
  try { card = await api(`/api/documents/${docId}`); } catch (e) { return toast(e.message, true); }
  $("drawerTitle").textContent = card.title || card.filename;
  $("drawerPath").textContent = card.rel_path;
  $("drawerOriginal").href = `/api/documents/${docId}/original`;
  const meta = [
    ["workstream", card.workstream], ["type", card.doc_type], ["period", card.period_covered],
    ["parties", (card.parties || []).join(", ")],
    ["key figures", (card.key_figures || []).join(" · ")],
    ["flags", (card.card_flags || []).join(", ")],
    ["status", card.status], ["units", card.n_units], ["size", bytes(card.size_bytes)],
  ].filter(([, v]) => v != null && v !== "" && v !== "[]");
  $("drawerMeta").innerHTML =
    (card.summary ? `<div>${esc(card.summary)}</div>` : "") +
    meta.map(([k, v]) => `<div><span class="muted">${k}:</span> ${esc(String(v))}</div>`).join("") +
    ((card.identical_copies_at || []).length
      ? `<div><span class="muted">identical copies at:</span> ${card.identical_copies_at
          .map((p) => `<code>${esc(p)}</code>`).join(", ")}</div>` : "") +
    ((card.near_duplicates || []).length
      ? `<div><span class="muted">near-duplicate versions:</span> ${card.near_duplicates
          .map((d) => `<code data-doc="${esc(d.doc_id)}">${esc(d.rel_path)}</code>`).join(", ")}</div>`
      : "");
  $("drawerAnchors").innerHTML = (card.anchors || [])
    .map((a) => `<span class="anchor" data-a="${esc(a.anchor)}">${esc(a.anchor)}</span>`).join("");
  for (const a of $("drawerAnchors").children) a.onclick = () => loadDocText(docId, a.dataset.a);
  $("drawer").classList.add("open");
  $("scrim").classList.add("open");
  await loadDocText(docId, anchor);
}

async function loadDocText(docId, anchor) {
  const p = new URLSearchParams({ chars: 30000 });
  if (anchor) p.set("anchor", anchor);
  try {
    const t = await api(`/api/documents/${docId}/text?` + p);
    $("drawerText").textContent = t.text || "(no extracted text)";
    for (const a of $("drawerAnchors").children) a.classList.toggle("active", a.dataset.a === anchor);
    $("drawerText").scrollIntoView({ block: "nearest" });
  } catch (e) { toast(e.message, true); }
}

/* ── markdown-lite with citation chips ──────────────────────────────── */
const CITE = /\[\[([0-9a-f]{6,32}):([^\]\s]{1,120})\]\]/g;

function inline(s) {
  let out = esc(s);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(CITE, (_m, id, anchor) =>
    `<button class="cite" data-doc="${id}" data-anchor="${esc(anchor)}">${esc(anchor)}</button>`);
  return out;
}

function renderMarkdown(text) {
  const lines = String(text || "").split("\n");
  const html = [];
  let list = null, table = null;
  const closeList = () => { if (list) { html.push(`</${list}>`); list = null; } };
  const closeTable = () => {
    if (table) {
      html.push("<table><thead><tr>" + table.header.map((h) => `<th>${inline(h)}</th>`).join("") + "</tr></thead><tbody>");
      for (const r of table.rows) html.push("<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
      html.push("</tbody></table>");
      table = null;
    }
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    const cells = line.trim().match(/^\|(.+)\|$/);
    if (cells) {
      const parts = cells[1].split("|").map((c) => c.trim());
      if (!table) { table = { header: parts, rows: [] }; continue; }
      if (parts.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      table.rows.push(parts);
      continue;
    }
    closeTable();
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); html.push(`<h4>${inline(h[2])}</h4>`); continue; }
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    if (ul) { if (list !== "ul") { closeList(); html.push("<ul>"); list = "ul"; } html.push(`<li>${inline(ul[1])}</li>`); continue; }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) { if (list !== "ol") { closeList(); html.push("<ol>"); list = "ol"; } html.push(`<li>${inline(ol[1])}</li>`); continue; }
    closeList();
    if (!line.trim()) { html.push("<br>"); continue; }
    html.push(`<div>${inline(line)}</div>`);
  }
  closeList(); closeTable();
  return html.join("");
}

document.addEventListener("click", (e) => {
  const c = e.target.closest(".cite");
  if (c) openDoc(c.dataset.doc, c.dataset.anchor);
});

/* ── ask ─────────────────────────────────────────────────────────────── */
const SUGGESTIONS = [
  "What does the data room contain, by workstream, and what is conspicuously missing for a standard buy-side DD?",
  "Pull revenue and EBITDA for every year available, compute the CAGR from the source workbook, and reconcile the audited accounts against the management deck.",
  "List every contract with a change-of-control or termination-for-convenience clause, with the exact clause text.",
  "Produce a red-flag memo as a Word document: findings, evidence, severity, and the open questions for management.",
];
$("suggestions").innerHTML = SUGGESTIONS.map((s) => `<div class="suggestion">${esc(s)}</div>`).join("");
for (const s of $("suggestions").children) {
  s.onclick = () => { $("question").value = s.textContent; $("question").focus(); };
}

$("newThread").onclick = () => {
  state.history = [];
  $("thread").innerHTML = `<div class="empty"><h2>New thread</h2><p class="muted">Previous turns cleared.</p></div>`;
  $("trace").innerHTML = ""; $("citations").innerHTML = ""; $("runMeta").textContent = "idle";
};

$("question").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") $("askForm").requestSubmit();
});

/* ── question scope: which folders the assistant may see ──────────────── */
// Sticky for the browser session, not the account: a scope belongs to the piece
// of work in front of you, and inheriting yesterday's on a fresh sign-in would
// silently narrow a question nobody meant to narrow.
const SCOPE_KEY = "dd.scope";
state.scope = (() => {
  try { return JSON.parse(sessionStorage.getItem(SCOPE_KEY) || "[]"); } catch { return []; }
})();
let scopeDraft = [];
let scopeFolders = [];
let scopeEstimateTimer = null;
let scopeEstimateIssued = 0;

// A stored scope outlives the corpus it names. Delete an extracted folder, or
// re-point the app, and the saved prefixes are no longer inside any known root —
// at which point the server rejects them and *every* question 400s until someone
// thinks to clear the chip by hand. So the restored scope is checked against the
// roots the server will check it against, and anything gone is dropped.
function pruneScope(roots) {
  if (!state.scope.length || !Array.isArray(roots) || !roots.length) return 0;
  const inside = (p) => roots.some((r) => {
    const root = r.replace(/\/$/, "");
    return p === root || p.startsWith(root + "/");
  });
  const kept = state.scope.filter(inside);
  if (kept.length === state.scope.length) return 0;
  const lost = state.scope.length - kept.length;
  setScope(kept);
  // Said out loud, never silently: the alternative is a question quietly
  // answered against more of the library than the reader chose.
  toast(
    `${lost} folder${lost === 1 ? "" : "s"} in your saved scope no longer exist — ` +
    (kept.length ? "scope narrowed to what is left." : "back to the whole corpus."),
    true,
  );
  return lost;
}

function setScope(paths) {
  state.scope = paths;
  sessionStorage.setItem(SCOPE_KEY, JSON.stringify(paths));
  renderScopeChip();
}

// Re-check the saved scope after the server has refused it, and return what to
// tell the reader — one sentence per outcome, each of them true. The three cases
// are genuinely different: pruned, nothing to prune, and could not look.
async function recheckScope() {
  let roots;
  try {
    const r = await api("/api/corpus/folders");
    scopeFolders = r.folders || [];
    roots = r.roots;
  } catch {
    return "the folder list could not be re-read, so the scope is unchanged;"
      + " clear the scope chip to ask against the whole corpus.";
  }
  return pruneScope(roots)
    ? "the scope has been narrowed to the folders that still exist; ask again."
    : "the folder list still offers those folders, so this may be a server-side"
      + " change; clear the scope chip to ask against the whole corpus.";
}

function scopeCount(paths) {
  // Nested selections would double-count, and a parent already covers its
  // children, so count only the outermost picks.
  const outer = paths.filter((p) => !paths.some((q) => q !== p && p.startsWith(q.replace(/\/$/, "") + "/")));
  return outer.reduce(
    (n, p) => n + (scopeFolders.find((f) => f.path === p)?.n_indexed || 0), 0);
}

function renderScopeChip() {
  const chip = $("scopeChip");
  if (!chip) return;
  const n = state.scope.length;
  chip.classList.toggle("on", n > 0);
  if (!n) {
    chip.textContent = "scope: whole corpus";
    chip.title = "Limit this question to part of the library";
    return;
  }
  const docs = scopeCount(state.scope);
  chip.textContent = `scope: ${n} folder${n === 1 ? "" : "s"}${docs ? ` · ${nfmt(docs)} docs` : ""}`;
  chip.title = state.scope.join("\n");
}

function scopeDraftPaths() {
  return [...document.querySelectorAll("#scopeTree input:checked")].map((i) => i.value);
}

async function updateScopeEstimate() {
  // Debouncing only delays the starts; the replies still race. Ticking a third
  // folder while the two-folder estimate is in flight must not leave the price of
  // two folders under a selection of three.
  const mine = ++scopeEstimateIssued;
  const paths = scopeDraftPaths();
  const box = $("scopeEstimate");
  box.textContent = "estimating…";
  try {
    const q = paths.length ? `?scope=${encodeURIComponent(JSON.stringify(paths))}` : "";
    const m = await api(`/api/manifest${q}`);
    if (mine !== scopeEstimateIssued) return;
    const total = m.n_indexed_total ?? m.n_indexed;
    box.innerHTML = paths.length
      ? `${nfmt(m.n_indexed)} of ${nfmt(total)} documents · map ~${nfmt(m.approx_tokens)} tokens ·
         ${money(m.cost_per_turn_usd)} per turn`
      : `whole corpus: ${nfmt(m.n_indexed)} documents · map ~${nfmt(m.approx_tokens)} tokens ·
         ${money(m.cost_per_turn_usd)} per turn`;
  } catch (e) {
    if (mine !== scopeEstimateIssued) return;
    box.textContent = e.message;
  }
}

function renderScopeTree() {
  const tree = $("scopeTree");
  if (!scopeFolders.length) {
    tree.innerHTML = `<span class="muted small">No indexed folders yet.</span>`;
    return;
  }
  tree.innerHTML = "";
  let currentRoot = null;
  for (const f of scopeFolders) {
    if (f.root !== currentRoot) {
      currentRoot = f.root;
      if (f.depth > 0) tree.append(el("div", "scope-root muted small", currentRoot));
    }
    const row = el("label", "scope-row");
    row.style.paddingLeft = `${f.depth * 18}px`;
    const cb = el("input");
    cb.type = "checkbox";
    cb.value = f.path;
    cb.checked = scopeDraft.includes(f.path);
    cb.onchange = () => {
      clearTimeout(scopeEstimateTimer);
      scopeEstimateTimer = setTimeout(updateScopeEstimate, 250);
    };
    row.append(cb);
    row.append(el("span", "n", f.name || f.path));
    row.append(el("span", "tag dupe", `${nfmt(f.n_indexed)} indexed`));
    if (f.n_documents > f.n_indexed) {
      row.append(el("span", "tag", `${nfmt(f.n_documents - f.n_indexed)} not indexed`));
    }
    tree.append(row);
  }
}

async function openScopeModal() {
  scopeDraft = [...state.scope];
  $("scopeModal").classList.add("open");
  $("scopeTree").innerHTML = `<span class="muted small">loading…</span>`;
  try {
    const r = await api("/api/corpus/folders");
    scopeFolders = r.folders || [];
    pruneScope(r.roots);
    scopeDraft = [...state.scope];
  } catch (e) {
    $("scopeTree").innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
    return;
  }
  renderScopeTree();
  updateScopeEstimate();
}

function closeScopeModal() {
  $("scopeModal").classList.remove("open");
}

$("scopeChip").onclick = openScopeModal;
$("scopeClose").onclick = closeScopeModal;
$("scopeModal").onclick = (e) => { if (e.target.id === "scopeModal") closeScopeModal(); };
$("scopeClear").onclick = () => {
  for (const cb of document.querySelectorAll("#scopeTree input")) cb.checked = false;
  updateScopeEstimate();
};
$("scopeApply").onclick = () => {
  setScope(scopeDraftPaths());
  closeScopeModal();
};
renderScopeChip();

$("askForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.running) return;
  const question = $("question").value.trim();
  if (!question) return;
  if (!state.status?.stats?.by_status?.carded) {
    return toast("Nothing is indexed yet — run Ingest then Indexing first.", true);
  }

  state.running = true;
  $("askBtn").disabled = true;
  $("askBtn").textContent = "Working…";
  $("question").value = "";
  const thread = $("thread");
  if (qs(".empty", thread)) thread.innerHTML = "";
  $("trace").innerHTML = "";
  $("citations").innerHTML = "";

  thread.append(msgBlock("you", esc(question).replace(/\n/g, "<br>"), "user"));

  const assistant = msgBlock("analyst", "", "assistant");
  const think = el("div", "think hidden");
  think.append(el("div", "who", "reasoning"));
  const thinkBody = el("div", "b");
  think.append(thinkBody);
  const body = qs(".body", assistant);
  assistant.insertBefore(think, body);
  // Notices live beside the answer, not inside it: `body` is replaced wholesale
  // on every text_delta and again on done, so a notice appended into it would be
  // wiped by the next token — which is the opposite of the point.
  const notices = el("div", "notices");
  assistant.append(notices);
  // Stated on the answer itself, not only on the chip: scrolled back to weeks
  // later, "not in the data room" has to carry what the room was at the time.
  if (state.scope.length) {
    const names = state.scope.map((p) => p.replace(/\/$/, "").split("/").pop());
    assistant.insertBefore(
      el("div", "scope-note muted small",
         `Scoped to ${names.join(", ")} — documents elsewhere were not readable.`),
      think,
    );
  }
  thread.append(assistant);
  thread.scrollTop = thread.scrollHeight;

  let answer = "";
  const t0 = Date.now();
  const tick = setInterval(() => {
    if (!state.running) return clearInterval(tick);
    const cur = $("runMeta").dataset.base || "";
    $("runMeta").innerHTML = `<span class="spin">◐</span> ${((Date.now() - t0) / 1000).toFixed(0)}s ${cur}`;
  }, 500);

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        question, history: state.history,
        verify: $("verifyToggle").checked, effort: $("effort").value,
        scope: state.scope,
      }),
    });
    if (!res.ok) {
      const detail = (await res.text()) || res.statusText;
      // The corpus can be re-pointed or an extracted folder deleted between
      // choosing a scope and asking. Recover here rather than leaving every
      // question failing on a chip nobody suspects.
      if (res.status === 400 && detail.includes("outside every known corpus root")) {
        // Awaited, and the message says what actually happened. Reporting a
        // re-check that is still in flight — or that failed — would send the
        // reader straight back into the same refusal believing it was fixed.
        // Awaiting also closes the retry window: `state.running` is still set,
        // so nothing can post the stale prefixes while this resolves.
        throw new Error(`${detail} — ${await recheckScope()}`);
      }
      throw new Error(detail);
    }

    for await (const ev of sseStream(res)) {
      switch (ev.type) {
        case "status":
          if (ev.reason === "budget") {
            // A spending stop is a decision the reader has to see, not a line in
            // the meta strip that the next status message overwrites.
            notices.insertAdjacentHTML("beforeend",
              `<div class="notice warn">${esc(ev.message)}</div>`);
            refreshStatus();
          } else {
            $("runMeta").dataset.base = esc(ev.message || "");
          }
          break;
        case "thinking_delta":
          think.classList.remove("hidden");
          thinkBody.textContent += ev.text;
          think.scrollTop = think.scrollHeight;
          break;
        case "text_delta":
          answer += ev.text;
          body.innerHTML = renderMarkdown(answer);
          thread.scrollTop = thread.scrollHeight;
          break;
        case "tool_use":
          addTrace(ev.id, ev.name, ev.label, "running");
          break;
        case "tool_result":
          updateTrace(ev.id, ev.summary, ev.ok ? "ok" : "err");
          break;
        case "usage": {
          const u = ev.cumulative || {};
          $("runMeta").dataset.base =
            `· ${money(u.cost_usd)} · ${nfmt(ev.cache_read)} cached / ${nfmt(ev.input)} fresh in, ${nfmt(ev.output)} out`;
          break;
        }
        case "citations":
          renderCitations(ev.citations, []);
          break;
        case "verdict":
          markVerdict(ev);
          break;
        case "artifact":
          body.appendChild(artifactRow(ev));
          loadArtifacts();
          break;
        case "error":
          // A budget refusal is a policy outcome, not a fault — say so in the
          // thread and leave it there, rather than flashing a red failure tag.
          if (ev.reason === "budget") {
            notices.insertAdjacentHTML("beforeend",
              `<div class="notice warn">${esc(ev.message)}</div>`);
            refreshStatus();
          } else {
            toast(ev.message, true);
            body.innerHTML += `<div class="tag bad">${esc(ev.message)}</div>`;
          }
          break;
        case "done":
          answer = ev.answer || answer;
          body.innerHTML = renderMarkdown(answer);
          if (ev.stopped_on_budget) assistant.classList.add("stopped-early");
          for (const a of ev.artifacts || []) body.appendChild(artifactRow(a));
          renderCitations(ev.citations, ev.verdicts);
          state.history.push({ role: "user", content: question });
          state.history.push({ role: "assistant", content: answer });
          refreshStatus();
          $("runMeta").innerHTML =
            `done in ${ev.duration_s}s · ${money(ev.usage.cost_usd)} · ` +
            `${(ev.citations || []).length} citations · ${(ev.verdicts || []).length} checked`;
          $("runMeta").dataset.base = "";
          loadQaLog();
          break;
      }
    }
  } catch (err) {
    toast(err.message, true);
    body.innerHTML += `<div class="tag bad">${esc(err.message)}</div>`;
  } finally {
    clearInterval(tick);
    state.running = false;
    $("askBtn").disabled = false;
    $("askBtn").textContent = "Ask";
    refreshStatus();
  }
});

function msgBlock(who, html, cls) {
  const m = el("div", "msg " + cls);
  m.append(el("div", "who", who));
  const b = el("div", "body");
  b.innerHTML = html;
  m.append(b);
  return m;
}

async function* sseStream(res) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try { yield JSON.parse(line.slice(5).trim()); } catch { /* ignore */ }
      }
    }
  }
}

function addTrace(id, name, label, cls) {
  const step = el("div", "tstep " + cls);
  step.id = "t-" + id;
  step.append(el("span", "n", name));
  step.append(el("div", "d", typeof label === "string" ? label.slice(0, 160) : ""));
  $("trace").append(step);
  $("trace").scrollTop = $("trace").scrollHeight;
}
function updateTrace(id, summary, cls) {
  const step = $("t-" + id);
  if (!step) return;
  step.className = "tstep " + cls;
  const d = qs(".d", step);
  d.textContent = (d.textContent ? d.textContent + " → " : "") + summary;
}

function renderCitations(citations, verdicts) {
  const byCite = {};
  for (const v of verdicts || []) for (const c of v.citations || []) byCite[c] = v;

  // Inline chips only have room for the anchor, and two documents can share an
  // anchor name — put the document on the hover title once we know it.
  const titles = {};
  for (const c of citations || []) titles[c.citation] = c.title || c.rel_path || c.doc_id;
  for (const chip of document.querySelectorAll(".msg .cite")) {
    const key = `${chip.dataset.doc}:${chip.dataset.anchor}`;
    if (titles[key]) chip.title = `${titles[key]} · ${chip.dataset.anchor}`;
  }

  const box = $("citations");
  box.innerHTML = (citations || []).length
    ? citations.map((c) => {
        const v = byCite[c.citation];
        return `<div class="citation" data-doc="${c.doc_id}" data-anchor="${esc(c.anchor)}">
          ${v ? `<span class="v ${v.verdict}">${v.verdict}</span>` : ""}
          <div class="c">${esc(c.anchor)}${c.count > 1 ? ` ×${c.count}` : ""}</div>
          <div>${esc((c.title || "").slice(0, 90))}</div>
          ${c.resolved ? "" : `<div class="tag bad">unresolved</div>`}
        </div>`;
      }).join("")
    : `<span class="muted small">no citations yet</span>`;
  for (const n of box.children) {
    if (n.dataset?.doc) n.onclick = () => openDoc(n.dataset.doc, n.dataset.anchor);
  }
}

function markVerdict(v) {
  for (const c of v.citations || []) {
    for (const n of $("citations").children) {
      if (!n.dataset) continue;
      if (`${n.dataset.doc}:${n.dataset.anchor}` !== c) continue;
      if (qs(".v", n)) continue;
      const badge = el("span", "v " + v.verdict, v.verdict);
      n.prepend(badge);
      if (v.verdict !== "supported" && v.note) n.append(el("div", "muted small", v.note));
    }
  }
}

function artifactRow(a) {
  const row = el("div", "artifact");
  const left = el("div");
  left.innerHTML = `<span class="kindchip">${esc(a.kind)}</span> <strong>${esc(a.filename)}</strong>
    <span class="muted small"> ${bytes(a.size_bytes)}</span>`;
  const link = el("a", "btn ghost", "download");
  link.href = a.download_url;
  link.setAttribute("download", a.filename);
  row.append(left, link);
  return row;
}

/* ── deliverables ────────────────────────────────────────────────────── */
async function loadArtifacts() {
  try {
    const r = await api("/api/artifacts");
    $("artifacts").innerHTML = "";
    if (!r.artifacts.length) {
      $("artifacts").innerHTML = `<span class="muted small">Nothing generated yet. Ask for a memo, a findings table or a deck.</span>`;
      return;
    }
    for (const a of r.artifacts) $("artifacts").append(artifactRow(a));
  } catch (e) { /* ignore */ }
}
$("refreshArtifacts").onclick = loadArtifacts;

async function loadQaLog() {
  try {
    const r = await api("/api/qa-log?limit=40");
    const box = $("qaLog");
    box.innerHTML = r.entries.length ? "" : `<span class="muted small">No questions asked yet.</span>`;
    for (const e of r.entries) {
      const bad = (e.verdicts || []).filter((v) => v.verdict === "unsupported" || v.verdict === "partial").length;
      const n = el("div", "qa-entry");
      // The scope belongs on the log line: an answer that found nothing means
      // something different depending on how much of the library it could open.
      const scope = e.scope || [];
      const scopeTag = scope.length
        ? `<span class="tag" title="${esc(scope.join("\n"))}">scoped: ${
            esc(scope.map((p) => p.replace(/\/$/, "").split("/").pop()).join(", ").slice(0, 60))}</span> · `
        : "";
      n.innerHTML = `<div class="q">${esc(e.question.slice(0, 190))}</div>
        <div class="muted small">${new Date(e.created_at * 1000).toLocaleString()} ·
        ${scopeTag}${(e.citations || []).length} citations · ${bad ? `<span class="tag flag">${bad} weak</span>` : "all checked citations held"} ·
        ${money(e.usage?.cost_usd)} · ${Number(e.duration_s || 0).toFixed(0)}s</div>`;
      const detail = el("div", "body hidden");
      detail.innerHTML = renderMarkdown(e.answer || "");
      n.append(detail);
      n.onclick = (ev) => { if (!ev.target.closest(".cite")) detail.classList.toggle("hidden"); };
      box.append(n);
    }
  } catch (e) { /* ignore */ }
}

/* ── admin ───────────────────────────────────────────────────────────── */
const AREA_COLOURS = ["#0f766e", "#2dd4bf", "#a5610a", "#7b848e", "#196b3c"];

async function loadAdmin() {
  await Promise.all([loadKeys(), loadUsers(), loadStorage(), loadAudit()]);
}

/* ── corpus access ───────────────────────────────────────────────────── */
function renderAccess(r) {
  const id = r.identity;
  $("accessIdentity").textContent =
    `running as ${id.user} (uid ${id.uid}, gid ${id.gid}) · umask ${id.umask}`;
  $("accessRepairBtn").classList.toggle("hidden", !r.fixable);

  const box = $("accessReport");
  box.innerHTML = "";

  if (r.repaired) {
    const n = r.repaired.length;
    const rest = r.fixable
      ? `${r.fixable} more can still be repaired here — run it again.`
      : r.blocked ? `${r.blocked} still need a fix on the host.` : "Everything is readable now.";
    box.append(el("div", n ? "notice" : "notice warn",
      n ? `Repaired ${n} path(s). ${rest}`
        : "Nothing could be repaired from inside the container."));
    if (r.repair_incomplete) {
      box.append(el("div", "notice warn",
        "Stopped before finishing — the tree was still yielding repairable paths. Run it again."));
    }
  }

  if (r.ok) {
    box.append(el("div", "notice", `All ${nfmt(r.supported_files)} supported file(s) across ${r.roots.length} root(s) are readable.`));
  } else if (r.blocked) {
    box.append(el("div", "notice warn",
      `${nfmt(r.blocked)} path(s) unreadable — their documents are missing from every ingest.`));
  }

  for (const root of r.roots) {
    const card = el("div", "rowitem" + (root.blocked ? "" : " active"));
    const meta = el("div", "meta");
    const tags = [];
    if (root.read_only_mount) tags.push('<span class="tag dupe">read-only mount</span>');
    if (!root.in_browse_roots) tags.push('<span class="tag bad">outside DD_BROWSE_ROOTS</span>');
    if (!root.exists) tags.push('<span class="tag bad">missing</span>');
    if (root.truncated) tags.push('<span class="tag dupe">partial scan</span>');
    meta.innerHTML = `<div class="t">${esc(root.root)} ${tags.join(" ")}</div>
      <div class="muted small">${nfmt(root.supported_files)} supported files ·
        ${root.blocked ? `<b>${nfmt(root.blocked)} unreadable</b>` : "all readable"}
        ${root.fixable ? ` · ${nfmt(root.fixable)} repairable here` : ""}</div>` +
      (root.source_path && root.source_path !== root.root
        ? `<div class="doc-path">mounted from ${esc(root.mount?.source || "?")} at
             ${esc(root.source_path)} — likely the host path, verify before using</div>` : "");
    for (const i of root.issues) {
      const line = el("div", "muted small");
      line.innerHTML = `&nbsp;&nbsp;${esc(i.kind)} <code>${esc(i.mode)}</code>
        ${esc(i.owner)} — ${esc(i.problem)}
        ${i.fixable ? '<span class="tag">fixable</span>' : ""}
        <div class="doc-path">${esc(i.path)}${i.detail ? " — " + esc(i.detail) : ""}</div>`;
      meta.append(line);
    }
    card.append(meta);
    box.append(card);
  }

  if (r.host_commands?.length) {
    box.append(el("h3", null, "Run on the Docker host"));
    const note = el("p", "muted small",
      "The container drops every capability and mounts the corpus read-only, so it cannot chown "
      + "or chmod paths it does not own. These commands do it from outside:");
    const pre = el("pre", "pre-scroll");
    pre.textContent = r.host_commands.join("\n");
    box.append(note, pre);
  }
}

async function runAccess(method) {
  const btns = [$("accessCheckBtn"), $("accessRepairBtn"), $("accessQuickBtn")];
  for (const b of btns) if (b) b.disabled = true;
  try {
    const path = $("ingestPath").value.trim();
    const r = method === "repair"
      ? await api("/api/access-repair", { method: "POST", body: { path } })
      : await api(`/api/access-check?path=${encodeURIComponent(path)}`);
    renderAccess(r);
    if (method === "repair" && r.repaired.length) { refreshStatus(); }
  } catch (e) {
    $("accessReport").innerHTML = `<div class="notice warn">${esc(e.message)}</div>`;
  }
  for (const b of btns) if (b) b.disabled = false;
}

$("accessCheckBtn").onclick = () => runAccess("check");
$("accessRepairBtn").onclick = () => runAccess("repair");
$("accessQuickBtn").onclick = () => {
  document.querySelector('[data-tab="admin"]').click();
  runAccess("check");
};

async function loadKeys() {
  let r;
  try { r = await api("/api/keys"); } catch (e) { return toast(e.message, true); }
  $("keySource").innerHTML =
    `resolving from <b>${esc(r.source)}</b>${r.env_key_present ? " · env key present" : ""}` +
    ` · secret: ${esc(r.secret_key_source)}`;
  const box = $("keyList");
  box.innerHTML = "";
  if (!r.keys.length) {
    box.innerHTML = r.env_key_present
      ? `<div class="notice">No stored key — using <code>ANTHROPIC_API_KEY</code> from the environment.</div>`
      : `<div class="notice warn">No API key configured. Indexing and Ask are disabled until you add one.</div>`;
  }
  for (const k of r.keys) {
    const row = el("div", "rowitem" + (k.is_active ? " active" : ""));
    const test = k.last_test_at
      ? `<span class="tag ${k.last_test_ok ? "" : "bad"}">${k.last_test_ok ? "verified" : "failed"}</span>`
      : "";
    const meta = el("div", "meta");
    meta.innerHTML = `<div class="t">${esc(k.label)} ${k.is_active ? '<span class="tag">active</span>' : ""} ${test}</div>
      <div class="keymask">sk-ant-…${esc(k.last4)}</div>
      <div class="muted small">added ${new Date(k.created_at * 1000).toLocaleDateString()}
        ${k.created_by ? "by " + esc(k.created_by) : ""}
        ${k.last_used_at ? "· last used " + new Date(k.last_used_at * 1000).toLocaleString() : ""}
        ${k.last_test_note ? "· " + esc(k.last_test_note.slice(0, 110)) : ""}</div>`;
    const actions = el("div", "actions");
    const testBtn = el("button", "ghost", "test");
    testBtn.onclick = async () => {
      testBtn.textContent = "testing…";
      try {
        const res = await api(`/api/keys/${k.id}/test`, { method: "POST" });
        toast(res.ok ? `OK — ${res.note}` : res.note, !res.ok);
      } catch (e) { toast(e.message, true); }
      loadKeys();
    };
    actions.append(testBtn);
    if (!k.is_active) {
      const act = el("button", "ghost", "make active");
      act.onclick = async () => {
        try { await api(`/api/keys/${k.id}/activate`, { method: "POST" }); loadKeys(); refreshStatus(); }
        catch (e) { toast(e.message, true); }
      };
      actions.append(act);
    }
    const del = el("button", "danger", "delete");
    del.onclick = async () => {
      if (!confirm(`Delete key "${k.label}"?`)) return;
      try { await api(`/api/keys/${k.id}`, { method: "DELETE" }); loadKeys(); refreshStatus(); }
      catch (e) { toast(e.message, true); }
    };
    actions.append(del);
    row.append(meta, actions);
    box.append(row);
  }
}

$("addKeyBtn").onclick = async () => {
  const key = $("keyValue").value.trim();
  if (!key) return toast("Paste an API key first", true);
  try {
    await api("/api/keys", {
      method: "POST",
      body: { label: $("keyLabel").value.trim() || "default", key, activate: true },
    });
    $("keyValue").value = ""; $("keyLabel").value = "";
    toast("Key stored and activated");
    loadKeys(); refreshStatus();
  } catch (e) { toast(e.message, true); }
};

/* A weekly cap is one of three things, and the difference matters: a number, no
   cap at all, or "whatever the instance default is". Rendered as words rather
   than a sentinel number so nobody has to decode -1. */
function budgetLabel(b) {
  if (!b) return "—";
  const amount = b.unlimited ? "unlimited" : `$${b.limit_usd.toFixed(2)}`;
  // `inherited` first, always. An account on an unlimited instance default reports
  // both unlimited and inherited, and reading the value first loses the difference
  // between "follows the default" and "was set to unlimited here".
  return b.inherited ? `default (${amount})` : amount;
}

function budgetBar(b) {
  if (!b || b.unlimited || !b.limit_usd) return "";
  const pct = Math.min(100, (b.spent_usd / b.limit_usd) * 100);
  const cls = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "";
  return `<span class="budget-track"><span class="budget-fill ${cls}"
            style="width:${pct.toFixed(1)}%"></span></span>`;
}

async function loadUsers() {
  let r;
  try { r = await api("/api/users"); } catch (e) { return; }
  $("accountCount").textContent = `${r.users.length} account${r.users.length === 1 ? "" : "s"}`;
  // The next reset as an instant, in the reader's own timezone. The boundary is
  // Monday midnight on the server, which is not necessarily Monday midnight where
  // the reader is sitting — so show when it actually happens for them.
  const resets = new Date(r.resets_at * 1000).toLocaleString([], {
    weekday: "long", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
  const dflt = (v) => (v < 0 ? "unlimited" : `$${v.toFixed(2)}`);
  $("budgetNote").innerHTML =
    `Weekly budgets reset <strong>${esc(resets)}</strong>, then every Monday
     · instance defaults: questions ${dflt(r.budget_defaults.ask)},
     indexing ${dflt(r.budget_defaults.index)}
     · one-time overrun ${r.budget_defaults.grace_pct}% of the question budget`;
  const box = $("userList");
  box.innerHTML = "";
  for (const u of r.users) {
    const row = el("div", "rowitem");
    const meta = el("div", "meta");
    const ask = u.budget?.ask, idx = u.budget?.index;
    meta.innerHTML = `<div class="t">${esc(u.username)}
      <span class="tag">${esc(u.role)}</span>
      ${u.disabled ? '<span class="tag bad">disabled</span>' : ""}
      ${u.must_change_password ? '<span class="tag flag">must change password</span>' : ""}
      ${ask?.exhausted ? '<span class="tag bad">question budget spent</span>' : ""}
      ${idx?.exhausted ? '<span class="tag bad">indexing budget spent</span>' : ""}</div>
      <div class="muted small">created ${new Date(u.created_at * 1000).toLocaleDateString()}
        ${u.last_login_at ? "· last sign-in " + new Date(u.last_login_at * 1000).toLocaleString() : "· never signed in"}</div>
      <div class="budget-line">
        <span>questions <strong>$${ask ? ask.spent_usd.toFixed(2) : "0.00"}</strong>
          of ${budgetLabel(ask)}</span>${budgetBar(ask)}
        <span>indexing <strong>$${idx ? idx.spent_usd.toFixed(2) : "0.00"}</strong>
          of ${budgetLabel(idx)}</span>${budgetBar(idx)}
        ${u.budget && !u.budget.grace_available
          ? '<span class="muted">overrun used this week</span>' : ""}
      </div>`;
    const actions = el("div", "actions");

    const budgetBtn = el("button", "ghost", "budgets");
    budgetBtn.onclick = () => editBudgets(u);
    actions.append(budgetBtn);

    const roleBtn = el("button", "ghost", u.role === "admin" ? "make analyst" : "make admin");
    roleBtn.onclick = () => patchUser(u.username, { role: u.role === "admin" ? "analyst" : "admin" });
    const disBtn = el("button", "ghost", u.disabled ? "enable" : "disable");
    disBtn.onclick = () => patchUser(u.username, { disabled: !u.disabled });
    const pwBtn = el("button", "ghost", "set password");
    pwBtn.onclick = () => {
      const pw = prompt(`New password for ${u.username} (min 8 characters):`);
      if (pw) patchUser(u.username, { password: pw });
    };
    actions.append(roleBtn, disBtn, pwBtn);
    if (u.username !== state.user?.username) {
      const del = el("button", "danger", "delete");
      del.onclick = async () => {
        if (!confirm(`Delete account "${u.username}"? Their sessions end immediately.`)) return;
        try { await api(`/api/users/${u.username}`, { method: "DELETE" }); loadUsers(); }
        catch (e) { toast(e.message, true); }
      };
      actions.append(del);
    }
    row.append(meta, actions);
    box.append(row);
  }
}

async function patchUser(username, patch) {
  try {
    await api(`/api/users/${username}`, { method: "POST", body: patch });
    toast(`Updated ${username}`);
    loadUsers();
    if (username === state.user?.username) refreshStatus();
  } catch (e) { toast(e.message, true); }
}

/* Both caps in one dialog, because they are set together and the interesting
   question is the pair: an admin who sweeps needs an indexing budget an analyst
   never touches. `default` and `unlimited` are typed as words, matching what the
   API accepts, so there is no sentinel number to remember. */
function editBudgets(u) {
  const asCurrent = (b) => (!b || b.inherited ? "default"
                            : b.unlimited ? "unlimited" : String(b.limit_usd));
  const ask = prompt(
    `Weekly QUESTION budget for ${u.username}.\n\n` +
    `A dollar amount, "unlimited", or "default" to follow the instance setting.\n` +
    `0 means this account cannot ask questions at all.`,
    asCurrent(u.budget?.ask)
  );
  if (ask === null) return;
  const index = prompt(
    `Weekly INDEXING budget for ${u.username}.\n\n` +
    `Covers sweeps — the catalogue cards. A full library is typically $10–25, so ` +
    `an account that never sweeps can be left at 0.\n\n` +
    `A dollar amount, "unlimited", or "default".`,
    asCurrent(u.budget?.index)
  );
  if (index === null) return;
  patchUser(u.username, { budget_ask: ask.trim(), budget_index: index.trim() });
}

$("addUserBtn").onclick = async () => {
  const username = $("newUsername").value.trim();
  const password = $("newPassword").value;
  if (!username || password.length < 8) return toast("Username and a password of 8+ characters", true);
  try {
    await api("/api/users", { method: "POST", body: { username, password, role: $("newRole").value } });
    $("newUsername").value = ""; $("newPassword").value = "";
    toast(`Created ${username}`);
    loadUsers();
  } catch (e) { toast(e.message, true); }
};

$("changePwBtn").onclick = async () => {
  const current_password = $("curPassword").value;
  const new_password = $("nextPassword").value;
  if (new_password.length < 8) return toast("New password must be 8+ characters", true);
  try {
    await api("/api/me/password", { method: "POST", body: { current_password, new_password } });
    toast("Password changed — signing out");
    setTimeout(() => location.replace("/login"), 1200);
  } catch (e) { toast(e.message, true); }
};

async function loadStorage() {
  let r;
  try { r = await api("/api/storage"); } catch (e) { return; }
  $("dataDirPath").textContent = r.data_dir;
  const total = Math.max(1, r.total_bytes);
  $("usageBar").innerHTML = r.areas
    .map((a, i) => `<span style="width:${(a.bytes / total) * 100}%;background:${AREA_COLOURS[i % AREA_COLOURS.length]}"
       title="${esc(a.label)}: ${bytes(a.bytes)}"></span>`).join("");
  $("usageLegend").innerHTML = r.areas
    .map((a, i) => `<span><i style="background:${AREA_COLOURS[i % AREA_COLOURS.length]}"></i>
       ${esc(a.label)} <b>${bytes(a.bytes)}</b>
       <span class="muted">${a.files ? a.files + " files" : ""}</span></span>`).join("")
    + `<span class="muted">total <b>${bytes(r.total_bytes)}</b></span>`;

  const box = $("rootList");
  box.innerHTML = "";
  if (!r.known_roots.length) {
    box.innerHTML = `<span class="muted small">No corpus folder yet — upload an archive or set a folder above.</span>`;
  }
  for (const root of r.known_roots) {
    const row = el("div", "rowitem" + (root.active ? " active" : ""));
    const meta = el("div", "meta");
    meta.innerHTML = `<div class="t">${esc(root.name)}
        ${root.active ? '<span class="tag">active</span>' : ""}
        <span class="tag dupe">${esc(root.source)}</span></div>
      <div class="doc-path">${esc(root.path)}</div>
      <div class="muted small">${bytes(root.bytes)} · ${nfmt(root.files)} files ·
        ${nfmt(root.indexed_documents)} indexed documents</div>`;
    const actions = el("div", "actions");
    if (!root.active) {
      const use = el("button", "ghost", "make active");
      use.onclick = async () => {
        try {
          await api("/api/corpus-root", { method: "POST", body: { path: root.path } });
          $("ingestPath").value = root.path;
          toast("Corpus root set"); loadStorage(); refreshStatus();
        } catch (e) { toast(e.message, true); }
      };
      actions.append(use);
    }
    if (root.source === "extracted") {
      const del = el("button", "danger", "delete folder");
      del.onclick = async () => {
        if (!confirm(`Permanently delete ${root.path}?\n\nIts documents stay in the index until you run "Purge missing files".`)) return;
        try {
          const res = await api("/api/storage/extracted/delete", { method: "POST", body: { path: root.path } });
          toast(`Deleted ${res.files} files (${bytes(res.reclaimed_bytes)})`);
          loadStorage(); refreshStatus();
        } catch (e) { toast(e.message, true); }
      };
      actions.append(del);
    }
    row.append(meta, actions);
    box.append(row);
  }

  $("storageNote").dataset.orphans = r.orphan_occurrences;
}

for (const btn of document.querySelectorAll("[data-op]")) {
  btn.onclick = async () => {
    const op = btn.dataset.op;
    const scary = { clear_cards: "Clear every catalogue card? The next sweep re-indexes and re-bills.",
                    reset_index: "Drop the whole index and text mirror? Originals are untouched but a full re-ingest is needed." };
    if (scary[op] && !confirm(scary[op])) return;
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "working…";
    try {
      const res = await api(`/api/storage/${op}`, { method: "POST" });
      toast(Object.entries(res).map(([k, v]) =>
        `${k.replace(/_/g, " ")}: ${typeof v === "number" && k.includes("bytes") ? bytes(v) : v}`).join(" · "));
      loadStorage(); refreshStatus(); loadDocs();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false; btn.textContent = label;
  };
}

async function loadAudit() {
  let r;
  try { r = await api("/api/audit?limit=200"); } catch (e) { return; }
  const tb = qs("#auditTable tbody");
  tb.innerHTML = r.entries.map((a) => `<tr>
      <td class="muted">${new Date(a.ts * 1000).toLocaleString()}</td>
      <td>${esc(a.actor || "—")}</td>
      <td><code>${esc(a.action)}</code></td>
      <td class="muted">${esc((a.detail || "").slice(0, 160))}</td>
    </tr>`).join("");
}
$("refreshAudit").onclick = loadAudit;

/* ── full-text search ────────────────────────────────────────────────── */
/* The index has always been there — FTS5 over every page, slide and sheet, which
   is what the analyst's search_corpus tool runs. Only the agent could reach it, so
   finding a word cost a question. This is the same index, for people.

   One component, mounted three times. Three copies of one endpoint is how a small
   feature turns into three sets of bugs. `compact` drops the filters and shortens
   the list; everything else is shared. */

const SEARCH_LIMITS = { compact: 10, full: 40 };

function searchPanel(mount, { compact = false } = {}) {
  const box = $(mount);
  if (!box) return null;
  box.innerHTML = `
    <div class="row gap wrap">
      <input type="search" class="search-q" placeholder="a word or a &quot;quoted phrase&quot;"
             autocomplete="off" spellcheck="false">
      ${compact ? "" : `
        <select class="search-ws"><option value="">all workstreams</option></select>
        <select class="search-family">
          <option value="">any file type</option>
          <option value="pdf">PDF</option>
          <option value="xlsx">spreadsheet</option>
          <option value="pptx">deck</option>
          <option value="docx">Word</option>
          <option value="text">text / CSV</option>
        </select>`}
    </div>
    <div class="search-note muted small"></div>
    <div class="search-hits"></div>`;

  const input = qs(".search-q", box);
  const note = qs(".search-note", box);
  const hits = qs(".search-hits", box);
  const ws = qs(".search-ws", box);
  const fam = qs(".search-family", box);

  // Populated from whatever the corpus actually contains, like the docs filter.
  // Re-callable, and a no-op once filled, because the first status call can fail.
  function fillWorkstreams() {
    if (!ws || ws.options.length > 1) return;
    for (const w of state.status?.workstreams || []) {
      const o = el("option", null, w);
      o.value = w;
      ws.append(o);
    }
  }
  fillWorkstreams();

  // Every keystroke starts a request and they do not come back in order. Only the
  // newest one may write to the DOM, or a slow response for "ind" lands on top of
  // the hits for "indemnity" — or on top of the cleared box.
  let issued = 0;

  async function run() {
    const mine = ++issued;
    const q = input.value.trim();
    if (q.length < 2) {
      // "Nothing typed yet" and "nothing matched" are different states, and telling
      // them apart is the difference between a working box and a broken-looking one.
      note.textContent = "";
      hits.innerHTML = `<span class="muted small">Type at least two characters.</span>`;
      return;
    }
    const p = new URLSearchParams({ q, limit: String(compact ? SEARCH_LIMITS.compact : SEARCH_LIMITS.full) });
    if (ws?.value) p.set("workstream", ws.value);
    if (fam?.value) p.set("family", fam.value);
    note.textContent = "searching…";
    try {
      const r = await api(`/api/search?${p}`);
      if (mine !== issued) return;
      const first = r.hits[0];
      if (first?.error) {
        note.textContent = "";
        hits.innerHTML = `<div class="notice err">${esc(first.error)}</div>`;
        return;
      }
      note.textContent = r.n_hits
        ? `${nfmt(r.n_hits)} passage${r.n_hits === 1 ? "" : "s"} in ${nfmt(r.n_documents)} document${r.n_documents === 1 ? "" : "s"}`
        : "";
      hits.innerHTML = "";
      if (!r.n_hits) {
        hits.innerHTML = `<span class="muted small">No passage matches “${esc(q)}”.</span>`;
        return;
      }
      for (const h of r.hits) hits.append(searchHit(h, compact));
    } catch (e) {
      if (mine !== issued) return;
      note.textContent = "";
      hits.innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
    }
  }

  let timer = null;
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
  input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); run(); } };
  if (ws) ws.onchange = run;
  if (fam) fam.onchange = run;
  run();
  return { run, focus: () => input.focus(), fillWorkstreams };
}

function searchHit(h, compact) {
  const row = el("div", "search-hit");
  // FTS5 marks the matched terms with «…»; turn them into <mark> after escaping,
  // never before — the snippet is document text and must not become markup.
  const snippet = esc(h.snippet || "")
    .replaceAll("«", '<mark>').replaceAll("»", "</mark>");
  row.innerHTML = `
    <div class="t">${esc(h.title || h.rel_path || h.doc_id)}
      <span class="tag dupe">${esc(h.anchor)}</span>
      ${!compact && h.workstream ? `<span class="tag">${esc(h.workstream)}</span>` : ""}
      ${!compact && h.doc_type ? `<span class="tag dupe">${esc(h.doc_type)}</span>` : ""}</div>
    ${compact ? "" : `<div class="doc-path">${esc(h.rel_path || "")}</div>`}
    <div class="snip">${snippet}</div>`;
  // The same drawer a citation opens, at the same anchor. Reading a hit is a
  // solved problem; this only has to hand it the right two values.
  row.onclick = () => openDoc(h.doc_id, h.anchor);
  return row;
}

const searchMounts = {};

/* ── boot ────────────────────────────────────────────────────────────── */
// Mounted after the first status load, because the workstream filter is populated
// from the taxonomy that call returns.
refreshStatus().then(() => {
  searchMounts.full = searchPanel("searchFullMount");
  searchMounts.corpus = searchPanel("searchCorpusMount", { compact: true });
  searchMounts.ask = searchPanel("searchAskMount", { compact: true });
});
// A scope restored from the session has paths but no counts; the chip needs the
// counts to say how much of the library the next question can actually see.
if (state.scope.length) {
  api("/api/corpus/folders")
    .then((r) => { scopeFolders = r.folders || []; pruneScope(r.roots); renderScopeChip(); })
    .catch(() => {});
}
loadDocs();
loadArchives();
loadConnections();
loadLog();
connectEvents();
setInterval(refreshStatus, 20000);
