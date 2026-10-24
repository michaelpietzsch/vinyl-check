"use strict";
// vinyl-check web interface, plain JavaScript without external libraries.
//
// One page with several modes (overview, record check, turntable check, jobs, settings, documentation). Live state
// (recorder, levels, job queue) arrives over the WebSocket /api/ws, everything else through the JSON API. All texts
// come from the translation catalog via T() (i18n.js, loaded before this file). The helpers at the top are shared by
// all views.

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Display labels for interface values. The keys are the values the API and the database use and stay as they are;
// only the labels are translated. The catalog is loaded before this script and a language switch reloads the page,
// so building the maps once is enough.
const KIND = {
  calibration: T("common.kind_calibration"), before: T("common.kind_before"), after: T("common.kind_after"),
  after_wet: T("common.kind_after_wet"), after_dry: T("common.kind_after_dry"), other: T("common.kind_other"),
  alignment: T("common.kind_alignment"),
};
const CHECK_KINDS = ["before", "after", "after_wet", "after_dry", "other"];   // record-check kinds (no alignment/calibration)
const FINDINGS = {
  scratch: T("common.finding_scratch"), hairline_scratch: T("common.finding_hairline"), warp: T("common.finding_warp"),
  dirt: T("common.finding_dirt"), fingerprints: T("common.finding_fingerprints"),
  off_center: T("common.finding_off_center"),
};
const findingsText = (x) => (x.findings || []).map((f) => FINDINGS[f] || f).join(" · ");
const findingBoxes = (recId, chosen, cls = "r-finding") => Object.entries(FINDINGS).map(([k, l]) => `<label class="chk"><input type="checkbox" class="${cls}" value="${k}" ${recId != null ? `data-rec-finding="${k}" data-rec="${recId}"` : ""} ${(chosen || []).includes(k) ? "checked" : ""}> ${l}</label>`).join(" ");
const EVKIND = { pop: T("common.ev_pop"), click: T("common.ev_click"), tick: T("common.ev_tick"), skip: T("common.ev_skip") };
const REGION = { quiet: T("common.region_quiet"), music: T("common.region_music"), excluded: T("common.region_excluded") };
const STATUS = {
  recording: T("common.status_recording"), recorded: T("common.status_recorded"), queued: T("common.status_queued"),
  analyzing: T("common.status_analyzing"), analyzed: T("common.status_analyzed"), error: T("common.status_error"),
};
const TYPE = {
  tone: T("common.type_tone"), sweep: T("common.type_sweep"), ramp: T("common.type_ramp"),
  two_tone: T("common.type_two_tone"), noise: T("common.type_noise"), silence: T("common.type_silent_groove"),
  unknown: T("common.type_unknown"),
};
const MODE = {
  lateral: T("common.mode_lateral"), vertical: T("common.mode_vertical"), left: T("common.mode_left"),
  right: T("common.mode_right"), mixed: T("common.mode_mixed"),
};
const fmtTime = (s) => { s = Math.max(0, s || 0); return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`; };
const fmtT = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`;
// Numbers and dates in the reader's locale; "–" for missing values
const num = (v, d = 1) => I18N.num(v, d);
const sgn = (v, d = 1) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + num(v, d));
const dateStr = (ts) => I18N.date(ts);
const dayStr = (ts) => I18N.date(ts, { dateStyle: "short" });                 // date only
const andList = (xs) => xs.join(T("common.list_and"));                          // "A and B", "A und B"
const gradeCls = (g) => ({ "M-": "NM", "NM": "NM", "M": "M", "P": "P" }[g] ?? (g || "").replace("+", "p"));   // colour classes: M, NM, VGp, VG, Gp, G, F, P
// Discogs condition "Very Good Plus (VG+)" → "VG+", "Near Mint (NM or M-)" → "NM"
const condShort = (c) => { if (!c) return null; const m = /\(([^)]+)\)/.exec(c); return m ? m[1].split(" or ")[0].trim() : c; };
// Grade chip. The smiley from the sticker sheet appears only in the record and recording views (stickerChip); in the
// record list it broke the layout.
const gradeChip = (g, title = "", recId = null) => g ? `<span class="grade ${gradeCls(g)}" ${recId ? `data-grade-rec="${recId}" tabindex="0"` : ""} title="${esc(title)}">${esc(g)}</span>` : `<span class="grade none">–</span>`;
const ico = (n, sz = 20) => `<svg class="i" width="${sz}" height="${sz}" aria-hidden="true"><use href="icons.svg?v=${window.VC_V || ""}#i-${n}"/></svg>`;
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

let state = { status: null, record: null, selected: new Set(), mode: "record", setups: [], setup: null, setupSel: new Set() };

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

// JSON API call; errors become exceptions with the server's (already localized) message
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" && !(opts.body instanceof Blob) ? JSON.stringify(opts.body) : opts.body });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(msg);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}
// Wraps an async handler: any error ends up in a toast instead of an unhandled rejection
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, 6000); } };

// ─── Grade popup: all grading criteria of a recording on hover ───────────────
const gradeCache = new Map();
const GRADE_SCALE = T("grade.scale_points");
const METRIC_UNIT = { clicks_music_per_min: "/min", clicks_quiet_per_min: "/min", ticks_quiet_per_min: "/min", pops_per_min: "/min", noise_rel_db: " dB" };
// Upper limit of one metric per grade, e.g. "NM ≤ 3.0 · VG+ ≤ 10.0 · …"
const limitsText = (l) => `NM ≤ ${num(l[0], 1)} · VG+ ≤ ${num(l[1], 1)} · VG ≤ ${num(l[2], 1)} · G+ ≤ ${num(l[3], 1)} · G ≤ ${num(l[4], 1)}`;
async function gradePopup(el) {
  const id = +el.dataset.gradeRec, pop = $("#pop");
  let g = gradeCache.get(id);
  if (!g) {
    try { g = (await api(`/api/recordings/${id}`)).analysis?.grading; } catch (_) { return; }
    if (!g) return; gradeCache.set(id, g);
  }
  const lim = state.status?.grading_limits || {};
  const rows = Object.entries(g.per_metric || {}).map(([k, p]) => {
    const l = lim[k];
    const edges = l ? limitsText(l) : "";
    const net = p.raw != null ? `<br><small class="hint">${T("grade.raw_minus_floor", { raw: num(p.raw, 1), floor: num(p.floor, 1) })}</small>` : "";
    return `<tr><td>${esc(p.label)}</td><td class="num">${p.value == null ? "–" : num(p.value, 1) + (METRIC_UNIT[k] || "")}${net}</td><td>${gradeChip(p.grade)}</td><td class="num">${p.score == null ? "–" : num(p.score, 0)}</td><td class="hint">${edges}</td></tr>`;
  }).join("");
  pop.innerHTML = `<div class="pop-head">${gradeChip(g.grade)} <b>${g.score == null ? "–" : num(g.score, 0)} / 100</b> <span class="hint">${T("grade.overall_is_worst")}</span></div>
    <table><tr><th>${T("grade.col_criterion")}</th><th class="num">${T("grade.col_value")}</th><th>${T("grade.col_grade")}</th><th class="num">${T("grade.col_points")}</th><th>${T("grade.col_limits")}</th></tr>${rows}</table>
    <div class="hint">${T("grade.popup_hint", { scale: GRADE_SCALE })} ${g.explanation ? esc(g.explanation) : ""}</div>`;
  placePop(el);
}
function placePop(el) {
  const pop = $("#pop"); pop.classList.remove("hidden");
  const r = el.getBoundingClientRect(), w = Math.min(640, window.innerWidth - 24);
  pop.style.maxWidth = w + "px";
  const ph = pop.offsetHeight, pw = pop.offsetWidth;
  let left = Math.max(12, Math.min(r.left, window.innerWidth - pw - 12));
  let top = r.bottom + 8; if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
  pop.style.left = left + "px"; pop.style.top = top + "px";
}

// ─── Grading scale and criteria: popup at the "In the collection" / "Measured" tiles ──
const GRADE_MEANING = {
  "NM": T("grade.meaning_nm"),
  "VG+": T("grade.meaning_vgp"),
  "VG": T("grade.meaning_vg"),
  "G+": T("grade.meaning_gp"),
  "G": T("grade.meaning_g"),
  "F": T("grade.meaning_f"),
};
const ROLE_TEXT = { graded: T("grade.role_graded"), secondary: T("grade.role_secondary"), info: T("grade.role_info") };
function scalePopup(el) {
  const pop = $("#pop"), st = state.status || {}, g = st.grading || {}, lim = st.grading_limits || {};
  const grades = g.grades || ["NM", "VG+", "VG", "G+", "G", "F"];
  const bands = g.bands || [[100, 90], [90, 75], [75, 55], [55, 35], [35, 15], [15, 0]];
  const cur = el.dataset.gradeCur || "", kind = el.dataset.gradeScale;
  const scaleRows = grades.map((x, i) => `<tr class="${x === cur ? "cur" : ""}"><td>${gradeChip(x)}</td><td class="num">${bands[i][0]}–${bands[i][1]}</td><td>${esc(GRADE_MEANING[x] || "")}</td></tr>`).join("");
  const role = (k) => (g.graded || []).includes(k) ? "graded" : (g.secondary || []).includes(k) ? "secondary" : "info";
  const critRows = Object.keys(lim).map((k) => { const l = lim[k];
    return `<tr><td>${esc((g.labels || {})[k] || k)}</td><td class="hint">${esc(ROLE_TEXT[role(k)])}</td><td class="hint">${limitsText(l)}${METRIC_UNIT[k] || ""}</td></tr>`; }).join("");
  const head = kind === "coll" ? T("grade.scale_head_collection")
                               : el.dataset.gradeSides ? T("grade.scale_head_measured_sides", { sides: esc(el.dataset.gradeSides) }) : T("grade.scale_head_measured");
  const calib = g.calibrated ?? st.grading_calibrated;
  pop.innerHTML = `<div class="pop-head">${cur && cur !== "–" ? gradeChip(cur) : ""} <b>${T("grade.scale_title")}</b> <span class="hint">${head}</span></div>
    <table class="scale"><tr><th>${T("grade.col_grade")}</th><th class="num">${T("grade.col_points")}</th><th>${T("grade.col_meaning")}</th></tr>${scaleRows}</table>
    <table><tr><th>${T("grade.col_metric")}</th><th>${T("grade.col_role")}</th><th>${calib ? T("grade.col_upper_limits") : T("grade.col_upper_limits_uncalibrated")}</th></tr>${critRows}</table>
    <div class="hint">${T("grade.scale_hint", { gap: g.secondary_gap ?? 2, version: String(g.version ?? "–") })}</div>`;
  placePop(el);
}
const popTarget = (t) => { const r = t.closest?.("[data-grade-rec]"); if (r) return [r, gradePopup]; const s = t.closest?.("[data-grade-scale]"); return s ? [s, scalePopup] : null; };
let popTimer = null;
document.addEventListener("mouseover", (e) => { const h = popTarget(e.target); if (!h) return; clearTimeout(popTimer); popTimer = setTimeout(() => h[1](h[0]), 150); });
document.addEventListener("mouseout", (e) => { if (!popTarget(e.target)) return; clearTimeout(popTimer); popTimer = setTimeout(() => $("#pop").classList.add("hidden"), 250); });
document.addEventListener("focusin", (e) => { const h = popTarget(e.target); if (h) h[1](h[0]); });
document.addEventListener("focusout", (e) => { if (popTarget(e.target)) $("#pop").classList.add("hidden"); });
document.addEventListener("click", (e) => { const h = popTarget(e.target); if (h) { if (e.target.closest("button, a, input, select")) return; e.preventDefault(); $("#pop").classList.contains("hidden") ? h[1](h[0]) : $("#pop").classList.add("hidden"); } else if (!e.target.closest("#pop")) $("#pop").classList.add("hidden"); });
$("#pop").addEventListener("mouseenter", () => clearTimeout(popTimer));
$("#pop").addEventListener("mouseleave", () => $("#pop").classList.add("hidden"));

// ─── Browser notifications (Notification API): side ended, run-out groove, analysis done ─────────
// The texts come from the server (status events), already in the reader's language.
let lastEventId = null;
function handleEvents(evs) {
  if (!evs || !evs.length) return;
  if (lastEventId === null) { lastEventId = evs[evs.length - 1].id; return; }   // on page load, don't replay old events
  for (const e of evs) {
    if (e.id <= lastEventId) continue;
    lastEventId = e.id;
    toast(e.text, 8000);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification("vinyl-check", { body: e.text, tag: e.kind, silent: e.kind === "analyzed" || e.kind === "master" }); } catch (_) {}
    }
  }
}
function askNotify() {
  if (!("Notification" in window) || Notification.permission !== "default") return;
  Notification.requestPermission().catch(() => {});
}

// ─── Status over the WebSocket ───────────────────────────────────────────
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  ws.onmessage = (m) => renderStatus(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 2000);
}

let lastWorker = null;
let uiBuild = null;
function checkUiBuild(s) {                                    // the interface files changed on the server: offer a reload
  if (s.ui_build == null) return;
  if (uiBuild == null) { uiBuild = s.ui_build; return; }
  if (s.ui_build !== uiBuild && !$("#reload-hint")) {
    const d = document.createElement("div"); d.id = "reload-hint"; d.className = "warnbox";
    d.innerHTML = `${T("shell.ui_updated")} <button class="link" onclick="location.reload()">${T("shell.reload_now")}</button>`;
    document.body.prepend(d);
  }
}
function checkRestart(s) {                                   // a deploy waits until no recording or analysis is running
  const h = $("#restart-hint");
  if (s.restart_pending && !h) {
    const d = document.createElement("div"); d.id = "restart-hint"; d.className = "warnbox";
    d.innerHTML = T("shell.restart_pending");
    document.body.prepend(d);
  } else if (!s.restart_pending && h) h.remove();
}
function renderStatus(s) {
  checkUiBuild(s); checkRestart(s);
  state.status = s;
  const pills = [];
  const st = s.storage;
  pills.push(st.ok ? `<span class="pill ok">${ico("ok",14)} ${T("shell.pill_storage_free", { gb: st.free_gb ?? "?" })}</span>`
    : `<span class="pill bad" title="${esc(st.reason)}">${ico("bad",14)} ${T("shell.pill_storage_bad", { reason: esc(st.reason) })}</span>`);
  if (s.profile) pills.push(`<span class="pill ${s.profile.accepted ? "ok" : "warn"}" title="${esc(T(s.profile.accepted ? "shell.pill_profile_title" : "shell.pill_profile_title_unset"))}">${ico("tonearm", 14)} ${esc(s.profile.name)}</span>`);
  pills.push(s.calibration ? `<span class="pill ok">${ico("ok",14)} ${T("shell.pill_chain", { db: num(s.calibration.chain_db) })}</span>`
    : `<span class="pill warn" title="${esc(T("shell.pill_uncalibrated_title"))}">${ico("warn",14)} ${T("shell.pill_uncalibrated")}</span>`);
  const w = s.worker.current;
  const job = w ? T(w.kind === "analyze" ? "shell.pill_job_analyze" : "shell.pill_job_compare", { id: w.id }) : "";
  pills.push(w ? `<button class="pill warn as-link" data-go="jobs" title="${esc(T("shell.show_jobs"))}"><span class="spin"></span>${job}${s.worker.queued ? ` · ${T("shell.pill_job_queued", { n: s.worker.queued })}` : ""} ▾</button>` : `<button class="pill as-link" data-go="jobs" title="${esc(T("shell.show_jobs"))}">${T("shell.pill_idle")} ▾</button>`);
  if (s.fake_source) pills.push(`<span class="pill warn">${T("shell.pill_test_mode")}</span>`);
  $("#pills").innerHTML = pills.join("");

  const r = s.recorder;
  document.body.classList.toggle("recording", !!r.active && r.mode === "record");
  $("#btn-stop").disabled = !r.active;
  renderListen();
  // While a record check is recording, the turntable check is locked and vice versa (one station, one recording)
  const kind = r.active ? r.meta?.kind : null, isCheck = kind && CHECK_KINDS.includes(kind), isJust = kind && ["alignment", "calibration"].includes(kind);
  const tj = $('#tabs button[data-mode="alignment"]'), tp = $('#tabs button[data-mode="record"]');
  tj.disabled = !!isCheck; tj.title = isCheck ? T("shell.tab_turntable_locked") : T("shell.tab_turntable_title");
  tp.disabled = !!isJust; tp.title = isJust ? T("shell.tab_record_locked") : T("shell.tab_record_title");
  $("#btn-monitor").disabled = $("#btn-calibrate").disabled = !!r.active;
  document.querySelectorAll(".btn-rec").forEach((b) => (b.disabled = !!r.active || !st.ok));
  $("#rec-label").textContent = r.active ? (r.mode === "record" ? T("shell.rec_label_recording", { kind: KIND[r.meta?.kind] || "" }) : T("shell.rec_label_monitor")) : T("shell.rec_ready");
  $("#rec-time").textContent = fmtTime(r.active ? r.elapsed_s : 0);
  const warn = [];
  // With a fixed level (USB turntable, profile switch) there is nothing to adjust: no hint while recording; the
  // analysis still counts the clipped samples and refuses a grade from 500 samples in the groove on
  if (r.clip_samples && !s.profile?.fixed_gain) {
    const early = r.last_clip_s != null && r.last_clip_s < 3;
    warn.push(early ? T("shell.warn_clip_drop", { n: r.clip_samples })
      : T("shell.warn_clip", { bursts: r.clip_bursts ?? "?", samples: r.clip_samples, last: fmtTime(r.last_clip_s ?? 0) }));
  }
  if (r.xruns) warn.push(T("shell.warn_xruns", { n: r.xruns }));
  for (const note of r.source_notes || []) warn.push(note);       // PortAudio: shared instead of exclusive, silence
  if (r.active && r.mode === "record" && r.music_thr_dbfs != null) {
    if (r.music_start_s == null) warn.push(T("shell.warn_waiting_music"));
    else if (r.expected_end_s) {
      const rem = r.remaining_s ?? (r.expected_end_s - r.elapsed_s);
      warn.push(rem > 0 ? T("shell.warn_side_remaining", { start: fmtTime(r.music_start_s), rem: fmtTime(rem), source: r.meta?.side_length_source_label || "?" })
        : T("shell.warn_side_ended", { t: fmtTime(-rem) }));
    } else warn.push(T("shell.warn_side_unknown", { start: fmtTime(r.music_start_s) }));
  }
  handleEvents(s.events);
  if (!r.active && r.error) warn.push(r.error);
  if (!r.active && r.stopped_reason && r.mode === "record") warn.push(r.stopped_reason);
  $("#rec-warn").textContent = warn.join(" · ");
  renderMeter(r.active ? r.level : null, r.active ? r.peak_hold_dbfs : null);
  renderLive(s);
  renderRta(s);

  const wkey = JSON.stringify(w) + "|" + !!r.active;
  if (wkey !== lastWorker) {
    lastWorker = wkey;
    if (state.record) refreshRecord();
    if (state.setup) refreshSetup(); else if (state.mode === "alignment" && state.profileId) refreshProfile();
  }
}

function pct(db) { return Math.max(0, Math.min(100, ((db + 60) / 60) * 100)); }
function renderMeter(level, hold) {
  document.querySelectorAll("#meter .ch").forEach((ch, i) => {
    const bar = $(".bar", ch);
    if (!level) { $(".rms", ch).style.width = $(".peak", ch).style.width = "0"; $(".hold", ch).style.left = "0"; $(".val", ch).textContent = "–"; bar.classList.remove("clip"); return; }
    const pk = level.peak_dbfs[i] ?? -120, rms = level.rms_dbfs[i] ?? -120;
    $(".rms", ch).style.width = pct(rms) + "%";
    $(".peak", ch).style.width = pct(pk) + "%";
    $(".hold", ch).style.left = pct(hold?.[i] ?? -120) + "%";
    $(".val", ch).textContent = num(pk, 1);
    bar.classList.toggle("clip", level.clip > 0 || pk > -0.5);
  });
}

// ─── Records ─────────────────────────────────────────────────────────────
async function loadRecords() {
  const list = await api("/api/records");
  $("#records").innerHTML = list.map((r) => `
    <li data-id="${r.id}" class="${state.record?.id === r.id ? "sel" : ""}">
      ${r.discogs_release_id || r.cover_local ? `<img class="cover-thumb" data-cover="${r.id}" alt="" loading="lazy">` : noCoverHtml("cover-thumb")}
      <span class="rec-name">${esc(r.artist)} – ${esc(r.title)}<br><small>${T("records.recordings_count", { n: r.recordings })}</small></span>
      <span class="conds" title="${esc(T("records.conds_title"))}">${gradeChip(condShort(r.discogs_media_condition), T("records.collection_cond", { cond: r.discogs_media_condition || T("records.not_set") }))}<span class="arrow">›</span>${copyGrades(r) || (gradeChip(r.last_grade, r.last_grade ? T("records.record_grade_avg") : T("records.not_measured")))}</span>
    </li>`).join("") || `<li class="empty">${T("records.none_yet")}</li>`;
  document.querySelectorAll("#records img[data-cover]").forEach((img) => loadCover(+img.dataset.cover, img, "thumb"));
}
// Cover from Discogs (the server fetches the address once and remembers it) or an uploaded cover; without a cover a
// "No Cover" placeholder is shown. Clicking the cover in the record view uploads one.
const coverCache = new Map();
const noCoverHtml = (cls) => `<span class="${cls} cover-none" role="img" aria-label="${esc(T("records.no_cover_aria"))}">${T("records.no_cover_html")}</span>`;
// Own cover: scaled down in the browser to at most 1200 px and uploaded as JPEG (the server has no image library)
async function shrinkImage(file, max = 1200) {
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error(T("records.image_convert_failed")))), "image/jpeg", 0.85));
}
async function loadCover(id, img, size) {
  try {
    if (!coverCache.has(id)) coverCache.set(id, api(`/api/records/${id}/cover`));
    const c = await coverCache.get(id);
    img.src = size === "thumb" ? c.thumb || c.url : c.url;
    img.classList.remove("hidden");
  } catch (_) { coverCache.delete(id); img.outerHTML = noCoverHtml(size === "thumb" ? "cover-thumb" : "cover-big"); }
}
$("#records").addEventListener("click", guard(async (e) => {
  const li = e.target.closest("li[data-id]"); if (!li) return;
  await openRecord(+li.dataset.id);
}));

let searchT;
$("#nr-search").addEventListener("input", (e) => {
  clearTimeout(searchT);
  const q = e.target.value.trim();
  if (q.length < 3 || !state.status?.discogs) { $("#nr-results").innerHTML = ""; return; }
  searchT = setTimeout(guard(async () => {
    const hits = await api("/api/discogs/search?q=" + encodeURIComponent(q));
    $("#nr-results").innerHTML = hits.map((h, i) => `<div data-i="${i}">${esc(h.artist)} – ${esc(h.title)} <small>${esc(h.year ?? "")} ${esc(h.label)} ${h.media_condition ? "· " + T("records.search_media", { cond: esc(condShort(h.media_condition)) }) : ""}</small></div>`).join("") || `<div class="empty">${T("records.search_none")}</div>`;
    $("#nr-results").onclick = (ev) => {
      const d = ev.target.closest("div[data-i]"); if (!d) return;
      const h = hits[+d.dataset.i];
      $("#nr-artist").value = h.artist; $("#nr-title").value = h.title;
      $("#nr-release").value = h.release_id ?? ""; $("#nr-cond").value = h.media_condition ?? "";
      $("#nr-results").innerHTML = "";
    };
  }), 300);
});
$("#new-record").addEventListener("submit", guard(async (e) => {
  e.preventDefault();
  const body = { artist: $("#nr-artist").value.trim(), title: $("#nr-title").value.trim(),
    discogs_release_id: $("#nr-release").value ? +$("#nr-release").value : null,
    discogs_media_condition: $("#nr-cond").value || null };
  const { id } = await api("/api/records", { method: "POST", body });
  e.target.reset(); await loadRecords(); await openRecord(id);
}));

// Copy labels are free text: "Copy 1" and "copy 1 " mean the same copy
const copyKey = (s) => (s || "").trim().toLowerCase().replace(/\s+/g, " ");

// Cover stickers: a sheet of smileys, six levels, one per grade. [name, colour name, colour]; the name is the text
// printed on the sticker and stays English.
const STICKERS = { NM: ["Excellent", T("grade.color_green"), "#2a9d3a"], "VG+": ["Great", T("grade.color_blue"), "#1f9fe5"],
                   VG: ["Good", T("grade.color_yellow"), "#f2dc00"], "G+": ["Okay", T("grade.color_orange"), "#f08a1d"],
                   G: ["Bad", T("grade.color_pink"), "#ee5fa4"], F: ["Very Bad", T("grade.color_red"), "#e3262c"] };
const stickerFor = (g) => STICKERS[g === "M" || g === "M-" ? "NM" : g === "P" ? "F" : g] || null;
// The smiley itself, as on the sheet: eyes and mouth per level, Very Bad with angry brows
const SMILEY_MOUTH = {
  Excellent: '<path d="M7 13h10a5 5 0 0 1-10 0z" fill="#fff"/>',
  Great: '<path d="M7 13q5 5.5 10 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>',
  Good: '<path d="M7.5 14.5h9" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>',
  Okay: '<path d="M7 15.5q5 -3 10 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>',
  Bad: '<path d="M7 16.5q5 -5.5 10 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>',
  "Very Bad": '<path d="M7.5 16.5q4.5 -4.5 9 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/><path d="M6.5 7.5l4 1.8M17.5 7.5l-4 1.8" stroke="#fff" stroke-width="2" stroke-linecap="round"/>',
};
const smileySvg = (s, size = 22) => `<svg class="smiley" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="${s[2]}"/><circle cx="8.5" cy="9.5" r="1.6" fill="#fff"/><circle cx="15.5" cy="9.5" r="1.6" fill="#fff"/>${SMILEY_MOUTH[s[0]]}</svg>`;
// Only the smiley; name and colour in the tooltip
const stickerChip = (g, extra = "", size = 22) => { const s = stickerFor(g); return s ? `<span class="sticker" title="${esc(T("grade.sticker_title", { name: s[0], color: s[1] }))}">${smileySvg(s, size)}${extra}</span>` : ""; };
const GRADE_ORDER = ["NM", "VG+", "VG", "G+", "G", "F"];
// The measured sides of one copy as text (condition block)
const sidesText = (c) => c.sides.map((s) => `${T("common.side", { side: esc(s.side) })} ${s.grade}`).join(", ") + (c.sides.length === 1 ? ` · ${T("records.only_one_side")}` : "");

// Verdict over several copies: per side the latest analyzed recording of each copy, then compared
function copyVerdict(recs) {
  const plate = recs.filter((x) => x.status === "analyzed" && x.summary && !["alignment", "calibration"].includes(x.kind));
  const names = new Map();                                     // key → first spelling
  for (const x of plate) if (x.copy_label?.trim() && !names.has(copyKey(x.copy_label))) names.set(copyKey(x.copy_label), x.copy_label.trim());
  if (names.size < 2) return "";
  const bySide = new Map();
  for (const x of plate) {
    const k = copyKey(x.copy_label); if (!k) continue;
    const side = String(x.side).trim().toUpperCase();
    if (!bySide.has(side)) bySide.set(side, new Map());
    const cur = bySide.get(side).get(k);
    if (!cur || x.created_at > cur.created_at) bySide.get(side).set(k, x);
  }
  const GR = ["NM", "VG+", "VG", "G+", "G", "F"];
  const better = (a, b) => {                                    // a better than b? grade, then clicks in pauses, then music
    const ga = GR.indexOf(a.grade), gb = GR.indexOf(b.grade);
    if (ga !== gb && ga >= 0 && gb >= 0) return ga < gb;
    const ca = a.summary.clicks_quiet_per_min ?? 0, cb = b.summary.clicks_quiet_per_min ?? 0;
    if (Math.abs(ca - cb) > 0.1 * Math.max(ca, cb, 1)) return ca < cb;
    return (a.summary.clicks_music_per_min ?? 0) < (b.summary.clicks_music_per_min ?? 0);
  };
  const wins = new Map([...names.keys()].map((k) => [k, 0]));
  const rows = [];
  for (const [side, m] of [...bySide.entries()].sort()) {
    const list = [...m.values()];
    if (list.length < 2) continue;
    list.sort((a, b) => (better(a, b) ? -1 : better(b, a) ? 1 : 0));
    const best = list[0];
    wins.set(copyKey(best.copy_label), wins.get(copyKey(best.copy_label)) + 1);
    rows.push(`<tr><td>${T("common.side", { side: esc(side) })}</td><td><b>${esc(names.get(copyKey(best.copy_label)))}</b></td>${list.map((x) => `<td>${esc(names.get(copyKey(x.copy_label)))}: ${gradeChip(x.grade, "", x.id)} <small class="hint">${T("records.copy_row_stats", { quiet: num(x.summary.clicks_quiet_per_min, 0), music: num(x.summary.clicks_music_per_min, 0), pops: num(x.summary.pops_per_min, 2) })}</small></td>`).join("")}</tr>`);
  }
  if (!rows.length) return "";
  const ranked = [...wins.entries()].sort((a, b) => b[1] - a[1]);
  const total = rows.length;
  const verdict = ranked[0][1] === total && total > 0 && ranked.length > 1 && ranked[1][1] < total
    ? T("records.copy_winner", { n: total, name: `<b>${esc(names.get(ranked[0][0]))}</b>`, others: andList(ranked.slice(1).map(([k]) => esc(names.get(k)))) })
    : T("records.copy_no_winner", { list: ranked.map(([k, n]) => `${esc(names.get(k))} ${n}×`).join(", ") });
  return `<h3>${T("records.copies_head")}</h3><p>${verdict} <span class="hint">${T("records.copies_hint")}</span></p>
    <div class="scroll"><table><tr><th>${T("common.col_side")}</th><th>${T("records.col_better")}</th><th colspan="9">${T("records.copies_head")}</th></tr>${rows.join("")}</table></div>`;
}

// History per side (and copy): every analyzed recording as a step, next to it the change from the previous one in percent
function historyTable(recs) {
  const groups = new Map();
  for (const x of [...recs].filter((x) => x.status === "analyzed" && x.summary && !["alignment", "calibration"].includes(x.kind)).sort((a, b) => a.created_at - b.created_at)) {
    const key = `${x.side}|${copyKey(x.copy_label)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(x);
  }
  const multi = [...groups.values()].filter((g) => g.length > 1);
  if (!multi.length) return "";
  const cols = [["clicks_quiet_net_per_min", "clicks_quiet_per_min", T("records.hist_clicks_quiet")], ["ticks_quiet_net_per_min", "ticks_quiet_per_min", T("records.hist_ticks")], ["clicks_music_confirmed_per_min", null, T("records.hist_music_confirmed")], ["clicks_music_per_min", null, T("records.hist_music_raw")], ["pops_per_min", null, T("records.hist_pops")]];
  const val = (x, k, k2) => (k2 ? x.summary[k2] : x.summary[k]) ?? null;      // raw value; the percentages refer to it
  // More than 14 dB below the reference tone: small clicks are missed, so the counts are not comparable
  const tooQuiet = (x) => x.summary?.level_vs_profile_db != null && x.summary.level_vs_profile_db < -14;
  const comparable = (x, prev) => prev && x.profile_id === prev.profile_id && !tooQuiet(x) && !tooQuiet(prev);
  const cell = (x, prev, k, k2) => {
    const v = val(x, k, k2), p = prev ? val(prev, k, k2) : null, net = k2 ? x.summary[k] : null;
    let d = "";
    if (prev && !comparable(x, prev)) d = ` <small class="hint" title="${esc(x.profile_id !== prev.profile_id ? T("records.hist_other_turntable") : T("records.hist_too_quiet_one"))}">${T("records.hist_na")}</small>`;
    else if (!(p == null || v == null || !p)) d = ` <small class="${v < p ? "ok" : v > p ? "bad" : "hint"}">${(v - p) / p > 0 ? "+" : ""}${Math.round((v - p) / p * 100)} %</small>`;
    return `<td class="num">${num(v)}${net != null ? ` <small class="hint" title="${esc(T("records.above_baseline"))}">(${num(net)})</small>` : ""}${d}</td>`;
  };
  return `<h3>${T("records.hist_head")}</h3><p class="hint">${T("records.hist_hint")}</p>
  ${multi.map((g) => `<div class="scroll"><table><tr><th>${T("common.side", { side: esc(g[0].side) })}${g[0].copy_label ? ", " + esc(g[0].copy_label) : ""}</th><th>${T("records.col_step")}</th><th>${T("common.col_grade")}</th>${cols.map((c) => `<th class="num">${c[2]}</th>`).join("")}</tr>
    ${g.map((x, i) => `<tr><td>${dayStr(x.created_at)} <button class="link" data-open-rec-detail="${x.id}">#${x.id}</button></td><td>${KIND[x.kind]}${x.cleaning_method ? ` · ${esc(x.cleaning_method)}` : ""}${profInfo(x) && new Set(g.map((y) => y.profile_id)).size > 1 ? ` <small class="hint">${esc(profInfo(x).name)}</small>` : ""}${tooQuiet(x) ? ` <small class="hint" title="${esc(T("records.too_quiet_title"))}">⚠ ${T("records.too_quiet")}</small>` : ""}</td><td>${gradeChip(x.grade, "", x.id)}</td>${cols.map((c) => cell(x, i ? g[i - 1] : null, c[0], c[1])).join("")}</tr>`).join("")}
  </table></div>`).join("")}`;
}

// ─── Record detail ───────────────────────────────────────────────────────
async function loadProfilesById() {
  if (!state.profilesById) state.profilesById = Object.fromEntries((await api("/api/profiles")).map((p) => [p.id, { name: p.name, fixed_gain: !!p.fixed_gain }]));
  return state.profilesById;
}
const profInfo = (x) => (state.profilesById || {})[x?.profile_id] || null;
async function openRecord(id) {
  state.record = await api(`/api/records/${id}`);
  state.selected.clear();
  try { await loadProfilesById(); } catch (_) {}
  renderRecord(); loadRecords();
}
// Edit side, copy and condition in the recordings table: double-click (double-tap on touch) → confirmation → the cell
// becomes an input; Enter or a selection saves, Esc or clicking elsewhere cancels. A new side re-analyzes the
// recording (the track list is per side), a new condition does not.
const tapState = { el: null, t: 0 };
function doubleTap(el) {
  const now = Date.now(), hit = tapState.el === el && now - tapState.t < 450;
  tapState.el = hit ? null : el; tapState.t = now;
  return hit;
}
function editCell(td, r) {
  const id = +td.dataset.rec, field = td.dataset.edit, x = (r.recordings || []).find((y) => y.id === id);
  if (!x) return;
  const what = field === "side" ? T("records.edit_what_side", { value: x.side })
    : field === "copy_label" ? T("records.edit_what_copy", { value: x.copy_label || T("records.edit_none") })
    : T("records.edit_what_kind", { value: KIND[x.kind] || x.kind });
  if (!confirm(T("records.edit_confirm", { id, what }))) return;
  const old = td.innerHTML;
  const kinds = CHECK_KINDS.filter((k) => k !== "after" || x.kind === "after");          // plain "after" only for old recordings
  const copies = [...new Set((r.recordings || []).map((y) => (y.copy_label || "").trim()).filter(Boolean))];
  td.innerHTML = field === "side"
    ? `<input class="cell-edit" maxlength="8" value="${esc(x.side)}" aria-label="${esc(T("common.col_side"))}">`
    : field === "copy_label"
    ? `<input class="cell-edit" maxlength="40" list="copy-edit-list" value="${esc(x.copy_label || "")}" placeholder="${esc(T("records.copy_placeholder_short"))}" aria-label="${esc(T("common.col_copy"))}"><datalist id="copy-edit-list">${copies.map((c) => `<option value="${esc(c)}">`).join("")}</datalist>`
    : `<select class="cell-edit" aria-label="${esc(T("common.col_condition"))}">${kinds.map((k) => `<option value="${k}" ${k === x.kind ? "selected" : ""}>${KIND[k]}</option>`).join("")}</select>`;
  const inp = td.querySelector(".cell-edit");
  let done = false;
  const cancel = () => { if (done) return; done = true; td.innerHTML = old; };
  const save = async () => {
    if (done) return;
    const v = field === "side" ? inp.value.trim().toUpperCase() : field === "copy_label" ? inp.value.replace(/\s+/g, " ").trim() : inp.value;
    if (!v && field === "side") return toast(T("records.side_empty"));
    if (v === (field === "side" ? String(x.side).trim().toUpperCase() : field === "copy_label" ? (x.copy_label || "").trim() : x.kind)) return cancel();
    done = true;
    try {
      const out = await api(`/api/recordings/${id}`, { method: "PATCH", body: { [field]: v } });
      toast(field === "side" ? T("records.edit_saved_side", { id, side: v }) + (out.reanalyzing ? ` · ${T("records.edit_reanalyzing")}` : "")
        : T("records.edit_saved", { id, value: field === "copy_label" ? v || T("records.edit_no_copy") : KIND[v] }));
    } catch (e) { toast(e.message); td.innerHTML = old; return; }
    refreshRecord();
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } else if (e.key === "Escape") cancel(); });
  if (field === "kind") inp.addEventListener("change", save);
  inp.addEventListener("blur", () => setTimeout(() => { if (!done) (field === "kind" ? cancel() : save()); }, 150));
  inp.focus(); if (field !== "kind") inp.select();
}
async function refreshRecord() {
  if (!state.record) return;
  try { state.record = await api(`/api/records/${state.record.id}`); renderRecord(); } catch (_) {}
  loadRecords().catch(() => {});                                   // keep grades and smileys in the list on the left in step
}

// Record condition: what the Discogs collection says (fetched live) and what was measured last. Only the record
// itself, not the sleeve.
const copyFindingsText = (r, copy) => {                            // findings per side of one copy
  const fx = (r.findings || []).filter((f) => f.copy_key === copyKey(copy) && f.findings.length);
  return fx.length ? `<br><small>${T("common.findings_line", { list: fx.map((f) => `${T("common.side", { side: esc(f.side) })} ${esc(findingsText(f))}`).join(" · ") })}</small>` : "";
};
function conditionBlock(r, coll) {
  const copies = (r.copies || []).filter((c) => c.grade);                      // record grade per copy (from the server)
  const measured = copies.length ? copies.reduce((m, c) => (c.at > m.at ? c : m)) : null;   // most recently measured copy
  const collCond = coll?.media_condition ?? r.discogs_media_condition;
  const collNote = coll ? (coll.in_collection_since ? T("records.coll_since", { date: new Date(coll.in_collection_since).toLocaleDateString(I18N.lang) }) : T("records.coll_from"))
                        : (r.discogs_media_condition ? T("records.coll_taken_over") : T("records.not_set"));
  const a = condShort(collCond), b = measured?.grade;
  const diff = a && b && a !== b;
  const P = r.pressing;
  const dropTxt = (x) => (x.dropped?.length ? T("records.pressing_without", { list: andList(x.dropped.map((d) => `${esc(d.side)} ${d.grade}`)) }) : T("records.pressing_all_counted"));
  const pressing = P && P.copies > 1 ? `<div class="cond" data-grade-scale="measured" data-grade-cur="${esc(P.grade)}" tabindex="0" title="${esc(T("records.pressing_title"))}"><span class="cond-label">${T("records.pressing_label", { n: P.copies })}</span>${gradeChip(P.grade, T("records.pressing_chip_title"))}${stickerChip(P.grade, "", 26)}<span class="cond-note">${T("records.pressing_note", { counted: P.counted, sides: P.sides, dropped: dropTxt(P) })}</span></div>` : "";
  return `${pressing}<div class="cond" data-grade-scale="coll" data-grade-cur="${esc(a || "")}" tabindex="0" title="${esc(T("records.scale_title"))}"><span class="cond-label">${T("records.in_collection")}</span>${gradeChip(a, collCond || "")}${stickerChip(a, "", 26)}<span class="cond-note">${esc(collCond || "–")}<br>${esc(collNote)}</span></div>
    ${copies.map((m) => `<div class="cond" data-grade-scale="measured" data-grade-cur="${esc(m.grade)}" data-grade-sides="${esc(m.sides.map((s) => `${T("common.side", { side: s.side })} ${s.grade}`).join(", "))}" tabindex="0" title="${esc(T("records.scale_title"))}"><span class="cond-label">${T("records.measured")}${m.copy ? ` · ${esc(m.copy)}` : ""}</span>${gradeChip(m.grade, T("records.record_grade_avg"))}${stickerChip(m.grade, "", 26)}<span class="cond-note">${sidesText(m)}<br>${dateStr(m.at)}${copyFindingsText(r, m.copy)}</span></div>`).join("")}
    ${diff ? `<div class="cond-diff">${ico("info", 16)} ${T("records.cond_diff", { from: esc(a), to: esc(b) + (measured.copy ? `, ${esc(measured.copy)}` : "") })}${state.status?.grading_calibrated ? "" : " " + T("records.cond_uncalibrated")}
      ${state.status?.discogs_write && r.discogs_release_id && measured ? `<button class="secondary" id="cond-push" data-copy="${esc(measured.copy)}" data-from="${esc(a || "–")}" data-to="${esc(b)}">${ico("cover", 16)} ${T("records.cond_push", { grade: esc(b) })}</button>` : ""}</div>` : ""}`;
}
async function pushCondition(r, btn) {
  const from = btn.dataset.from, to = btn.dataset.to;
  if (!confirm(T("records.cond_push_confirm", { from, to }))) return;
  const out = await api(`/api/records/${r.id}/condition`, { method: "POST", body: { copy: btn.dataset.copy ?? "" } });
  toast(T("records.cond_pushed", { from: condShort(out.previous) || "–", to: condShort(out.current) }));
  state.record = await api(`/api/records/${r.id}`); renderRecord(); loadRecords();
}
async function loadCondition(r) {
  if (!state.status?.discogs) return;
  try { const c = await api(`/api/records/${r.id}/collection`); if (state.record?.id === r.id) $("#rec-condition").innerHTML = conditionBlock(r, c); } catch (_) {}
  const b = $("#cond-push"); if (b) b.closest(".cond-diff").style.gap = ".6rem";
}
// Recordings table of the record view. Columns sort on click, a second click reverses; without a chosen order the
// rows are grouped by copy (no copy first, "Copy 1" < "Copy 10"), within a copy by date. Empty values always go to
// the bottom. The browser remembers the choice.
const REC_COLS = [["side", T("common.col_side")], ["copy", T("common.col_copy")], ["kind", T("common.col_condition")], ["created_at", T("records.col_date")], ["duration_s", T("records.col_duration"), "num"], ["status", T("records.col_status")], ["grade", T("common.col_grade")],
  ["music", T("records.col_music"), "num", T("records.col_music_tip")],
  ["quiet", T("records.col_quiet"), "num", T("records.col_quiet_tip")],
  ["ticks", T("records.col_ticks"), "num", T("records.col_ticks_tip")],
  ["pops", T("records.col_pops"), "num"], ["noise", T("records.col_noise"), "num", T("records.col_noise_tip")]];
const REC_FIRST_DIR = { side: 1, copy: 1, kind: 1, created_at: -1, duration_s: -1, status: 1, grade: 1, music: -1, quiet: -1, ticks: -1, pops: -1, noise: -1 };
const KIND_ORDER = ["before", "after_wet", "after_dry", "after", "other", "calibration", "alignment"];
const STATUS_ORDER = ["recording", "recorded", "queued", "analyzing", "analyzed", "error"];
const recVal = (x, k) => {
  const sm = x.summary || {};
  switch (k) {
    case "side": return String(x.side || "").trim().toUpperCase();
    case "copy": return copyKey(x.copy_label) || null;
    case "kind": return KIND_ORDER.indexOf(x.kind);
    case "status": return STATUS_ORDER.indexOf(x.status);
    case "grade": return x.grade ? GRADE_ORDER.indexOf(x.grade) : null;
    case "music": return sm.clicks_music_confirmed_per_min ?? sm.clicks_music_per_min ?? null;
    case "quiet": return sm.clicks_quiet_per_min ?? null;
    case "ticks": return sm.ticks_quiet_per_min ?? null;
    case "pops": return sm.pops_per_min ?? null;
    case "noise": return sm.noise_rel_db ?? null;
    default: return x[k] ?? null;
  }
};
state.recSort = (() => { try { const v = JSON.parse(localStorage.getItem("vc-rec-sort") || "null"); return v && REC_FIRST_DIR[v.key] ? v : null; } catch (_) { return null; } })();
const saveRecSort = () => { try { localStorage.setItem("vc-rec-sort", JSON.stringify(state.recSort)); } catch (_) {} };
function sortRecs(recs, sort) {
  if (!sort) return [...recs].sort((a, b) => copyKey(a.copy_label).localeCompare(copyKey(b.copy_label), I18N.lang, { numeric: true }) || a.created_at - b.created_at);
  const cmp = (a, b) => {
    const va = recVal(a, sort.key), vb = recVal(b, sort.key);
    if (va == null || vb == null) return va == null ? (vb == null ? 0 : 1) : -1;          // empty always at the bottom
    return (typeof va === "string" ? va.localeCompare(vb, I18N.lang, { numeric: true }) : va - vb) * sort.dir;
  };
  return [...recs].sort((a, b) => cmp(a, b) || a.created_at - b.created_at);
}
function recTableHtml(r) {
  const recs = r.recordings || [], sort = state.recSort;
  const mixedProfiles = new Set(recs.map((x) => x.profile_id)).size > 1;
  const rows = sortRecs(recs, sort);
  const grouped = !sort && new Set(rows.map((x) => copyKey(x.copy_label))).size > 1;
  const clipMax = state.status?.clip_limits?.max ?? 500, clipMention = state.status?.clip_limits?.mention ?? 200;
  const th = (k, label, cls, tip) => `<th class="${cls || ""} sortable ${sort?.key === k ? "sorted" : ""}" data-rsort="${k}" title="${esc((tip ? tip + " · " : "") + T("records.sort_click"))}">${label}${sort?.key === k ? (sort.dir < 0 ? " ▼" : " ▲") : ""}</th>`;
  return `<div class="scroll"><table>
      <tr><th></th>${REC_COLS.map(([k, l, c, tip]) => th(k, l, c, tip)).join("")}<th>${sort ? `<button class="link" data-rsort-reset title="${esc(T("records.sort_default_title"))}">${T("records.sort_default")}</button>` : ""}</th></tr>
      ${rows.map((x, i) => `<tr${grouped && i && copyKey(x.copy_label) !== copyKey(rows[i - 1].copy_label) ? ' class="grp"' : ""}>
        <td><input type="checkbox" class="sel" data-id="${x.id}" ${x.status === "analyzed" && !x.audio_deleted_at ? "" : "disabled"} ${state.selected.has(x.id) ? "checked" : ""} title="${x.audio_deleted_at ? esc(T("records.audio_deleted_no_compare")) : ""}"></td>
        <td class="edit-cell" data-edit="side" data-rec="${x.id}" title="${esc(T("records.edit_side_title"))}">${esc(x.side)}</td><td class="edit-cell" data-edit="copy_label" data-rec="${x.id}" title="${esc(T("records.edit_copy_title"))}">${x.copy_label ? `<b>${esc(x.copy_label)}</b>` : `<span class="hint">–</span>`}</td><td class="edit-cell" data-edit="kind" data-rec="${x.id}" title="${esc(T("records.edit_kind_title"))}"><span class="kind-label">${KIND[x.kind]}</span>${x.cleaning_method ? `<br><small class="hint">${esc(x.cleaning_method)}</small>` : ""}${mixedProfiles && profInfo(x) ? `<br><small class="hint">${ico("tonearm", 12)} ${esc(profInfo(x).name)}</small>` : ""}${findingsText(x) ? `<br><small class="hint" title="${esc(T("records.findings_seen"))}">${esc(findingsText(x))}</small>` : ""}${x.notes ? `<br><small class="hint">${esc(x.notes)}</small>` : ""}</td>
        <td>${dateStr(x.created_at)}</td>
        <td class="num">${fmtTime(x.duration_s)}</td>
        <td class="status-${x.status}" title="${esc(x.error || "")}">${["queued", "analyzing", "recording"].includes(x.status) ? '<span class="spin"></span>' : ""}${STATUS[x.status] || x.status}${x.xruns ? ` ⚠ ${T("records.dropouts")}` : ""}${(x.summary?.clip_in_groove || 0) > clipMax ? ` ⚠ ${T("records.clip_no_grade", { n: x.summary.clip_in_groove })}` : (x.summary?.clip_in_groove || 0) >= clipMention ? ` <small class="hint" title="${esc(T("records.clip_tight_title"))}">${T("records.clip_samples", { n: x.summary.clip_in_groove })}</small>` : ""}${x.summary?.level_vs_profile_db != null && x.summary.level_vs_profile_db < -14 ? ` ⚠ ${T("records.db_too_quiet", { db: Math.round(-x.summary.level_vs_profile_db) })}` : ""}${x.summary?.skips_back ? ` <button class="link warn-link" data-play-rec="${x.id}" data-t="${x.summary.skips?.[0]?.t ?? 0}" data-before="3" data-after="4" title="${esc(T("records.skip_play_title"))}">⚠ ${T("records.skipped_n", { n: x.summary.skips_back })} ▶</button>` : ""}${x.audio_deleted_at ? `<br><small class="hint">${T("records.audio_deleted")}</small>` : ""}</td>
        <td>${gradeChip(x.grade, "", x.id)}</td>
        <td class="num">${num(x.summary?.clicks_music_confirmed_per_min ?? x.summary?.clicks_music_per_min)}${x.summary?.learned?.clicks_music_per_min != null ? ` <small class="${x.summary.learned.graded ? "ok" : "hint"}" title="${esc(T(x.summary.learned.graded ? "records.learned_title_graded" : "records.learned_title"))}">≈ ${num(x.summary.learned.clicks_music_per_min)}</small>` : ""}</td>
        <td class="num">${rawNet(x.summary, "clicks_quiet_per_min")}</td><td class="num">${rawNet(x.summary, "ticks_quiet_per_min")}</td>
        <td class="num">${num(x.summary?.pops_per_min, 2)}</td><td class="num">${num(x.summary?.noise_rel_db)}</td>
        <td style="white-space:nowrap">${x.status === "analyzed" ? `<button class="link" data-view="${x.id}" title="${esc(T("records.report_title"))}">${ico("spectro", 15)} ${T("records.report")}</button>` : ""}
            ${x.audio_deleted_at ? "" : `<button class="link" data-reana="${x.id}" title="${esc(T("records.reanalyze"))}">↻</button>`}
            <button class="link" data-del="${x.id}" title="${esc(T("records.delete"))}">✕</button></td>
      </tr>`).join("")}
    </table></div>`;
}
function renderRecTable() { const w = $("#rec-table"); if (w && state.record) w.innerHTML = recTableHtml(state.record); }

// Comparison statuses that are not "done"
const CMP_STATUS = { queued: T("common.status_queued"), running: T("records.cmp_running"), error: T("common.status_error") };
function renderRecord() {
  const r = state.record, st = state.status;
  const recs = r.recordings;
  const mixedProfiles = new Set(recs.map((x) => x.profile_id)).size > 1;
  const dis = st?.recorder?.active || !st?.storage?.ok ? "disabled" : "";
  $("#detail").innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:start">
      <button type="button" class="cover-pick" id="cover-up" data-hint="${esc(T("records.cover_change"))}" title="${esc(T(r.cover_local ? "records.cover_replace_title" : "records.cover_upload_title"))}">${r.discogs_release_id || r.cover_local ? `<img class="cover-big hidden" id="rec-cover" alt="${esc(T("records.cover_alt"))}">` : noCoverHtml("cover-big")}</button><input type="file" id="cover-file" accept="image/jpeg,image/png,image/webp" hidden>
      <div style="flex:1"><h2 style="margin:0">${esc(r.artist)} – ${esc(r.title)}</h2>
        <div class="hint">${r.discogs_release_id ? `<a href="https://www.discogs.com/release/${r.discogs_release_id}" target="_blank" rel="noopener">${T("records.discogs_release")}</a>` : ""}${r.cover_local ? ` <button class="link" id="cover-del">${T("records.cover_remove")}</button>` : ""}</div>
        <div class="condition" id="rec-condition">${conditionBlock(r, null)}</div>
        ${Object.keys(r.side_lengths || {}).length ? `<div class="hint" style="margin-top:.4rem">${T("records.side_lengths", { list: Object.entries(r.side_lengths).sort().map(([k, v]) => `${esc(k)} ${fmtTime(v.s)} (${esc(v.source_label || v.source)})`).join(" · ") })}</div>` : ""}
        ${Object.keys(r.tracks || {}).length ? `<div class="hint">${T("records.tracks", { list: Object.entries(r.tracks).sort().map(([s, l]) => `<b>${esc(s)}</b> ${l.map((t) => `${esc(t.title)}${t.dur_s ? " " + fmtTime(t.dur_s) : ""}`).join(", ")}`).join(" · ") })}</div>` : ""}</div>
      <button class="link" id="del-record">${T("records.delete_record")}</button>
    </div>
    <h3>${T("records.new_recording")}</h3>
    <div class="row">
      <div><label>${T("common.col_side")}</label><input id="r-side" value="A" maxlength="8"></div>
      <div><label>${T("common.col_condition")}</label><select id="r-kind">${["before", "after_wet", "after_dry", "other"].map((k) => `<option value="${k}">${KIND[k]}</option>`).join("")}</select></div>
      <div><label>${T("records.speed")}</label><select id="r-rpm"><option value="33.333">33⅓</option><option value="45">45</option></select></div>
      <div><label>${T("records.cleaning_optional")}</label><input id="r-clean" maxlength="80" placeholder="${esc(T("records.cleaning_placeholder"))}" title="${esc(T("records.cleaning_title"))}"></div>
      <div><label>${T("records.copy_optional")}</label><input id="r-copy" list="copy-list" maxlength="40" placeholder="${esc(T("records.copy_placeholder"))}" title="${esc(T("records.copy_title"))}"><datalist id="copy-list">${[...new Set(recs.map((x) => (x.copy_label || "").trim()).filter(Boolean))].map((c) => `<option value="${esc(c)}">`).join("")}</datalist></div>
      <div class="wide"><label>${T("records.note_optional")}</label><input id="r-notes" maxlength="200" placeholder="${esc(T("records.note_placeholder"))}"></div>
      <button class="btn-rec" id="btn-rec" ${dis}>${ico("rec")} ${T("records.start_recording")}</button>
    </div>
    <p class="hint">${T("records.recording_steps")}</p>
    <h3>${T("records.recordings_head")}</h3>
    ${mixedProfiles ? `<p class="hint">${ico("tonearm", 14)} ${T("records.mixed_turntables")}</p>` : ""}
    ${recs.length ? `<div id="rec-table">${recTableHtml(r)}</div>
    <div class="row" style="margin-top:.6rem"><button id="btn-compare" class="secondary" disabled title="${esc(T("records.compare_title"))}">${T("records.compare_selected")}</button></div>`
    : `<p class="empty">${T("records.no_recordings")}</p>`}
    ${copyVerdict(recs)}
    ${historyTable(recs)}
    ${r.comparisons.length ? `<h3>${T("records.comparisons_head")}</h3><ul>${r.comparisons.map((c) => `<li><button class="link" data-cmp="${c.id}">#${c.id}: ${T("common.side", { side: esc(c.side) })}, ${KIND[c.a_kind]}${c.a_copy ? " " + esc(c.a_copy) : ""} → ${KIND[c.b_kind]}${c.b_copy ? " " + esc(c.b_copy) : ""}</button> <span class="hint">${c.status === "done" ? "" : CMP_STATUS[c.status] || esc(c.status)}</span></li>`).join("")}</ul>` : ""}
    ${st?.engineer_mode ? masterSection(r) : ""}
  `;
  $("#btn-rec").onclick = guard(async () => {
    askNotify();
    await api("/api/recordings/start", { method: "POST", body: { record_id: r.id, side: $("#r-side").value, kind: $("#r-kind").value, rpm: +$("#r-rpm").value, cleaning_method: $("#r-clean").value, copy_label: $("#r-copy").value, notes: $("#r-notes").value } });
    toast(T("records.recording_started")); refreshRecord();
  });
  if ($("#rec-cover")) loadCover(r.id, $("#rec-cover"), "big");
  if ($("#cover-up")) {
    $("#cover-up").onclick = () => $("#cover-file").click();
    $("#cover-file").onchange = guard(async (e) => {
      const f = e.target.files[0]; if (!f) return;
      await api(`/api/records/${r.id}/cover`, { method: "PUT", headers: { "Content-Type": "image/jpeg" }, body: await shrinkImage(f) });
      coverCache.delete(r.id); toast(T("records.cover_saved")); await refreshRecord(); loadRecords();
    });
  }
  if (document.querySelector("[data-common-side]")) loadMasterCommon(r).catch((e) => toast(e.message));
  if ($("#master-file")) {
    let side = null;
    $("#detail").querySelectorAll("[data-master-up]").forEach((b) => (b.onclick = () => { side = b.dataset.masterUp; $("#master-file").click(); }));
    $("#detail").querySelectorAll("[data-master-del]").forEach((b) => (b.onclick = guard(async () => {
      if (!confirm(T("records.master_delete_confirm", { side: b.dataset.masterDel }))) return;
      await api(`/api/records/${r.id}/masters/${b.dataset.masterDel}`, { method: "DELETE" });
      refreshRecord();
    })));
    $("#master-file").onchange = guard(async (e) => {
      const f = e.target.files[0]; e.target.value = "";
      if (!f || !side) return;
      const out = await uploadMaster(r.id, side, f);
      toast(out.queued?.length ? T("records.master_saved_queued", { side, n: out.queued.length }) : T("records.master_saved", { side }), 6000);
      refreshRecord();
    });
  }
  if ($("#cover-del")) $("#cover-del").onclick = guard(async () => {
    if (!confirm(T("records.cover_remove_confirm"))) return;
    await api(`/api/records/${r.id}/cover`, { method: "DELETE" });
    coverCache.delete(r.id); await refreshRecord(); loadRecords();
  });
  loadCondition(r);
  $("#rec-condition").onclick = guard(async (e) => { const b = e.target.closest("#cond-push"); if (b) await pushCondition(r, b); });
  $("#del-record").onclick = guard(async () => {
    if (!confirm(T("records.delete_record_confirm"))) return;
    await api(`/api/records/${r.id}`, { method: "DELETE" });
    state.record = null; $("#detail").innerHTML = `<p class="empty">${T("records.select_hint")}</p>`; $("#view").classList.add("hidden"); loadRecords();
  });
  $("#detail").onclick = guard(async (e) => {
    const t = e.target;
    const cp = t.closest?.("[data-cplay]");                       // copy comparison: play this spot on this copy
    if (cp) return play(+cp.dataset.cplay, +cp.dataset.t, 0.8, 1.2);
    if (t.closest?.("#btn-approval")) return showApproval(r.id);
    const rs = t.closest?.("th[data-rsort]");
    if (rs) { const k = rs.dataset.rsort, cur = state.recSort; state.recSort = { key: k, dir: cur?.key === k ? -cur.dir : REC_FIRST_DIR[k] }; saveRecSort(); return renderRecTable(); }
    if (t.closest?.("[data-rsort-reset]")) { state.recSort = null; saveRecSort(); return renderRecTable(); }
    const ec = t.closest?.("td[data-edit]");
    if (ec && !ec.querySelector("input, select")) { if (doubleTap(ec)) editCell(ec, r); return; }
    if (t.dataset.view) return showRecording(+t.dataset.view);
    if (t.dataset.cmp) return showComparison(+t.dataset.cmp);
    if (t.dataset.reana) { await api(`/api/recordings/${t.dataset.reana}/analyze`, { method: "POST" }); return refreshRecord(); }
    if (t.dataset.del) { if (confirm(T("records.delete_recording_confirm"))) { await api(`/api/recordings/${t.dataset.del}`, { method: "DELETE" }); refreshRecord(); } return; }
    if (t.classList.contains("sel")) {
      t.checked ? state.selected.add(+t.dataset.id) : state.selected.delete(+t.dataset.id);
      setCompareBtn();
    }
  });
  const cmpBtn = $("#btn-compare");
  if (cmpBtn) {
    setCompareBtn();
    cmpBtn.onclick = guard(async () => {
      const sel = [...state.selected].filter((i) => recs.some((x) => x.id === i));     // in the order of selection
      const sides = [...new Set(sel.map((i) => String(recs.find((x) => x.id === i)?.side || "").trim().toUpperCase()))];
      if (sides.length > 1) return toast(T("records.compare_sides_mismatch", { sides: andList(sides) }), 8000);
      if (sel.length === 2) {
        const ids = [...sel].sort((a, b) => a - b);
        const { id } = await api("/api/comparisons", { method: "POST", body: { a_id: ids[0], b_id: ids[1] } });
        toast(T("records.comparing")); return pollComparison(id);
      }
      // More than two: each against the first one selected, then summarized
      const [ref, ...others] = sel;
      const pairs = [];
      for (const o of others) pairs.push({ rec: o, cid: (await api("/api/comparisons", { method: "POST", body: { a_id: ref, b_id: o } })).id });
      toast(T("records.multi_cmp_running", { n: pairs.length, ref }));
      showMultiComparison(ref, pairs);
    });
  }
}

// Test pressing approval: per side all copies against the master, check items and a recommendation; the server
// renders the same report as a PDF.
const APPROVAL_ICON = { ok: "✓", warn: "⚠", bad: "✗", info: "·" };
// The overall and per-side verdicts arrive as a code (`verdict`) with its text (`verdict_text`): [colour class, label]
const APPROVAL_VERDICT = {
  approve: ["ok", T("approval.verdict_ok")],
  approve_with_notes: ["warn", T("approval.verdict_notes")],
  reject: ["bad", T("approval.verdict_reject")],
};
async function showApproval(rid) {
  const d = await api(`/api/records/${rid}/approval`);
  const v = openView();
  const vcls = (x) => APPROVAL_VERDICT[x]?.[0] ?? "warn";
  const vtxt = (x, text) => esc(text || APPROVAL_VERDICT[x]?.[1] || x);
  const pol = (x) => (x === "ok" ? T("approval.polarity_same") : POLARITY_TEXT[x] || x || "–");
  v.innerHTML = `<div class="view-bar no-print"><button class="secondary" id="close-view">${ico("back", 16)} ${T("common.back_to_record")}</button>
      <a class="button secondary" id="btn-pdf" href="/api/records/${rid}/approval.pdf" download>${ico("export", 16)} ${T("approval.pdf_download")}</a></div>
    <h2 style="margin:.3rem 0 .2rem">${T("approval.title", { artist: esc(d.record.artist), title: esc(d.record.title) })}</h2>
    <p class="hint">${T("approval.subtitle", { date: dateStr(d.created_at), version: esc(d.version) })}</p>
    ${d.verdict ? `<div class="approval-verdict ${vcls(d.verdict)}">${vtxt(d.verdict, d.verdict_text)}</div>` : `<p class="empty">${T("approval.no_master")}</p>`}
    ${d.sides.map((sd, si) => `<section class="approval-side"><h3>${T("common.side", { side: esc(sd.side) })} <span class="approval-chip ${vcls(sd.verdict)}">${vtxt(sd.verdict, sd.verdict_text)}</span></h3>
      <p class="hint">${T("approval.master_info", { name: esc(sd.master.name), dur: fmtTime(sd.master.duration_s), khz: num(sd.master.samplerate / 1000, 1) })}</p>
      <ul class="approval-checks">${sd.checks.map((c) => `<li class="${c.status}"><b>${APPROVAL_ICON[c.status]} ${esc(c.name)}:</b> ${esc(c.text)}${c.detail ? ` <span class="hint">${esc(c.detail)}</span>` : ""}</li>`).join("")}</ul>
      ${sd.copies.length ? `<div class="scroll"><table><tr><th>${T("common.col_copy")}</th><th>${T("common.col_grade")}</th><th>${T("approval.col_vs_master")}</th><th class="num">${T("approval.col_clicks")}</th><th class="num">${T("approval.col_pops")}</th><th class="num" title="${esc(T("approval.col_loud_title", { n: num(d.audible, 0) }))}">${T("approval.col_loud")}</th><th class="num">${T("approval.col_speed")}</th><th class="num">${T("approval.col_ecc")}</th><th class="num">${T("approval.col_hf_end")}</th><th class="num">${T("approval.col_pre_echo")}</th><th>${T("approval.col_polarity")}</th></tr>
        ${sd.copies.map((c) => `<tr><td>${esc(c.copy)}</td><td>${gradeChip(c.grade)}</td><td>${gradeChip(c.grade_master)}</td><td class="num">${num(c.clicks_vinyl)}</td><td class="num">${num(c.pops_vinyl, 2)}</td><td class="num">${c.audible}</td><td class="num">${sgn(c.cut_pct ?? c.speed_pct, 3)} %</td><td class="num">${num(c.ecc_mm, 2)} mm</td><td class="num">${sgn(c.hf_loss_end_db, 1)} dB</td><td class="num">${c.pre_echo_db == null ? "–" : num(c.pre_echo_db, 0) + " dB"}</td><td>${esc(pol(c.polarity))}${c.swapped ? ` · ${T("approval.swapped")}` : ""}</td></tr>`).join("")}</table></div>
        <p class="hint">${T("approval.table_hint")}</p>` : ""}
      ${sd.loud_common.length ? `<h4>${T("approval.loud_head")}</h4><div class="scroll"><table><tr><th class="num">${T("approval.col_master_time")}</th><th>${T("common.col_type")}</th><th class="num">${T("approval.col_copies")}</th><th class="num">${T("common.col_strength")}</th><th class="no-print">${T("common.listen")}</th></tr>
        ${sd.loud_common.map((x) => { const cs = Object.entries(x.copies); return `<tr><td class="num">${fmtT(x.tm)}</td><td>${EVKIND[x.kind] || esc(x.kind)}</td><td class="num">${x.n} (${cs.map(([n]) => esc(n)).join(", ")})</td><td class="num">${num(x.max_strength, 0)}</td><td class="no-print">${cs.map(([n, e]) => `<button class="link" data-cplay="${e.recording_id}" data-t="${e.t}" title="${esc(n)}">▶ ${esc(n.replace(/^Exemplar\s*/, "Ex. "))}</button>`).join(" ")} <button class="link" data-mplay-rec="${cs[0][1].recording_id}" data-t="${cs[0][1].t}">▶ Master</button></td></tr>`; }).join("")}</table></div>` : ""}      ${sd.copies.length ? `<div class="approval-graphs" id="apg-${si}"><p class="hint">${T("approval.graphs_loading")}</p></div>` : ""}</section>`).join("")}
    <p class="hint">${T("approval.thresholds", { cut_ok: d.criteria.cut_ok, cut_warn: d.criteria.cut_warn, ecc_ok: d.criteria.ecc_ok, ecc_warn: d.criteria.ecc_warn, pre_echo: d.criteria.pre_echo_warn, stamper_max: d.criteria.stamper_hint_max ?? 3 })}</p>`;
  const charts = d.sides.map((sd, si) => (sd.copies.length ? drawApprovalGraphs(rid, sd, si, d.audible).catch((e) => { const el = $(`#apg-${si}`); if (el) el.innerHTML = `<p class="hint">${T("approval.graphs_error", { error: esc(e.message) })}</p>`; }) : Promise.resolve()));
  v.onclick = (e) => {
    if (e.target.closest("#close-view")) return closeView();
    const pdf = e.target.closest("#btn-pdf");
    if (pdf) {                                                  // the server builds the PDF (ReportLab); takes a few seconds
      pdf.classList.add("busy"); toast(T("approval.pdf_building"), 4000);
      setTimeout(() => pdf.classList.remove("busy"), 6000);
      return;
    }
    const p = e.target.closest("[data-cplay]"); if (p) return play(+p.dataset.cplay, +p.dataset.t, 0.8, 1.2);
    const m = e.target.closest("[data-mplay-rec]"); if (m) return playMaster(+m.dataset.mplayRec, +m.dataset.t, 0.8, 1.2);
  };
}
// ─── Approval graphs ─────────────────────────────────────────────────────
// Everything on the master's time axis; SVG prints sharply, spectrograms are canvases. Per side: timeline (stamper
// defects + real clicks per copy), spectrogram with markers, close-ups of the audible stamper defects per copy,
// frequency response, wow, level per track.
const COPY_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#17becf", "#7f7f7f"];   // one per copy, distinguishable on paper and on dark
// Piecewise linear interpolation (extrapolates with the outer segments)
function interp1(x, xs, ys) {
  const n = xs.length; if (!n) return x; if (n === 1) return ys[0] + (x - xs[0]);
  let lo = 0, hi = n - 1;
  if (x <= xs[0]) { lo = 0; hi = 1; } else if (x >= xs[n - 1]) { lo = n - 2; hi = n - 1; }
  else while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  return ys[lo] + ((x - xs[lo]) / ((xs[hi] - xs[lo]) || 1)) * (ys[hi] - ys[lo]);
}
// Time mapping between vinyl and master from the alignment points of a master comparison (m = master, v = vinyl)
function mapFns(res) {
  const m = res?.map?.m || [], v = res?.map?.v || [];
  const iv = v.map((_, i) => i).sort((i, j) => v[i] - v[j]), im = m.map((_, i) => i).sort((i, j) => m[i] - m[j]);
  const vS = iv.map((i) => v[i]), mByV = iv.map((i) => m[i]), mS = im.map((i) => m[i]), vByM = im.map((i) => v[i]);
  return { toM: (t) => interp1(t, vS, mByV), toV: (t) => interp1(t, mS, vByM) };
}
const svgEsc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;");

function approvalTimeline(sd, common, copies, audible, kc) {
  const W = 1000, padL = 104, padR = 8, top = 34, rowH = 26, dur = sd.master.duration_s || 1;
  const X = (t) => padL + (Math.max(0, Math.min(dur, t)) / dur) * (W - padL - padR);
  const rows = 1 + copies.length, H = top + rows * rowH + 30, nCopies = copies.length;
  const out = [`<svg class="ap-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(T("approval.timeline_aria", { side: sd.side }))}">`];
  // Tracks as a band at the top (from the level comparison of the first copy)
  (copies[0]?.res?.levels?.items || []).forEach((it, i) => {
    const [a, b] = it.t_m || [0, 0];
    out.push(`<rect x="${X(a)}" y="2" width="${Math.max(1, X(b) - X(a))}" height="22" fill="currentColor" opacity="${i % 2 ? 0.06 : 0.12}"/>`);
    if (X(b) - X(a) > 40) out.push(`<text x="${X(a) + 3}" y="18" font-size="13" fill="currentColor">${svgEsc(it.pos)} ${svgEsc((it.title || "").slice(0, Math.floor((X(b) - X(a)) / 6)))}</text>`);
  });
  for (let r = 0; r < rows; r++) {
    const y = top + r * rowH;
    out.push(`<line x1="${padL}" y1="${y + rowH / 2}" x2="${W - padR}" y2="${y + rowH / 2}" stroke="currentColor" stroke-opacity=".15"/>`);
    out.push(`<text x="${padL - 6}" y="${y + rowH / 2 + 4}" font-size="14" text-anchor="end" fill="currentColor" font-weight="${r ? 400 : 700}">${r ? svgEsc(copies[r - 1].copy) : svgEsc(T("approval.stamper"))}</text>`);
  }
  // Stamper / cut: the same spot on several copies; red when on all of them, audible ones labelled with their time
  const yS = top + rowH / 2;
  for (const x of common) {
    const all = x.n >= nCopies, loud = x.max_strength >= audible, hgt = 4 + 8 * (x.n / Math.max(nCopies, 1));
    out.push(`<line x1="${X(x.tm)}" y1="${yS - hgt}" x2="${X(x.tm)}" y2="${yS + hgt}" stroke="${all ? kc.bad : kc.warn}" stroke-width="${loud ? 2.5 : 1}" stroke-opacity="${loud ? 1 : 0.55}"/>`);
    if (loud) {
      out.push(`<line x1="${X(x.tm)}" y1="${top}" x2="${X(x.tm)}" y2="${top + rows * rowH}" stroke="${all ? kc.bad : kc.warn}" stroke-dasharray="3 3" stroke-opacity=".7"/>`);
      out.push(`<text x="${X(x.tm) + 3}" y="${top + rows * rowH + 11}" font-size="13" fill="${all ? kc.bad : kc.warn}">${fmtT(x.tm)} (${x.n}/${nCopies})</text>`);
    }
  }
  // Real clicks per copy (only on the vinyl according to the master), size by strength, audible ones outlined
  copies.forEach((c, i) => {
    const y = top + (i + 1) * rowH + rowH / 2;
    for (const e of c.vinyl) {
      const rr = Math.max(1.2, Math.min(5.5, Math.log10(1 + (e.strength || 1)) * 2.2));
      out.push(`<circle cx="${X(e.tm).toFixed(1)}" cy="${y + (e.kind === "tick" ? 5 : e.kind === "pop" ? -5 : 0)}" r="${rr.toFixed(1)}" fill="${kc[e.kind] || kc.click}" fill-opacity=".75"${e.strength >= audible ? ` stroke="currentColor" stroke-width="1"` : ""}/>`);
    }
  });
  // Time axis (master)
  const yA = top + rows * rowH + 22, step = dur > 900 ? 120 : 60;
  out.push(`<line x1="${padL}" y1="${top + rows * rowH}" x2="${W - padR}" y2="${top + rows * rowH}" stroke="currentColor" stroke-opacity=".4"/>`);
  for (let t = 0; t <= dur; t += step) out.push(`<text x="${X(t)}" y="${yA + 6}" font-size="13" text-anchor="middle" fill="currentColor" opacity=".7">${fmtTime(t)}</text>`);
  out.push("</svg>");
  return out.join("");
}

// Frequency response against the master: median over the copies, one line per segment (start/middle/end of the side)
function approvalFreq(copies, kc) {
  const f = copies.map((c) => c.res?.freq).filter((x) => x?.bands && x?.segments?.length);
  if (!f.length) return "";
  const bands = f[0].bands, segs = f[0].segments.map((s) => s.label);
  const med = (xs) => { const a = xs.filter((v) => v != null && isFinite(v)).sort((p, q) => p - q); return a.length ? (a.length % 2 ? a[(a.length - 1) / 2] : (a[a.length / 2 - 1] + a[a.length / 2]) / 2) : null; };
  const lines = segs.map((_, si) => bands.map((_, bi) => med(f.map((x) => x.segments[si]?.mid_db?.[bi]))));
  const vals = lines.flat().filter((v) => v != null), lo = Math.min(-12, Math.floor(Math.min(...vals) / 3) * 3), hi = Math.max(6, Math.ceil(Math.max(...vals) / 3) * 3);
  const W = 480, H = 240, pl = 36, pr = 8, pt = 42, pb = 24, lf0 = Math.log(bands[0]), lf1 = Math.log(bands[bands.length - 1]);
  const X = (hz) => pl + ((Math.log(hz) - lf0) / (lf1 - lf0)) * (W - pl - pr), Y = (db) => pt + ((hi - db) / (hi - lo)) * (H - pt - pb);
  const col = [kc.ok, kc.warn, kc.bad];
  const out = [`<svg class="ap-svg ap-half" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(T("approval.freq_aria"))}">`];
  for (let db = lo; db <= hi; db += 3) out.push(`<line x1="${pl}" y1="${Y(db)}" x2="${W - pr}" y2="${Y(db)}" stroke="currentColor" stroke-opacity="${db === 0 ? 0.5 : 0.12}"/><text x="${pl - 4}" y="${Y(db) + 3}" font-size="12" text-anchor="end" fill="currentColor" opacity=".7">${db}</text>`);
  for (const hz of [50, 100, 200, 500, 1000, 2000, 5000, 10000, 16000]) if (hz >= bands[0] && hz <= bands[bands.length - 1]) out.push(`<text x="${X(hz)}" y="${H - 8}" font-size="12" text-anchor="middle" fill="currentColor" opacity=".7">${hz >= 1000 ? hz / 1000 + "k" : hz}</text>`);
  lines.forEach((ln, si) => {
    const pts = ln.map((v, bi) => (v == null ? null : `${X(bands[bi]).toFixed(1)},${Y(v).toFixed(1)}`)).filter(Boolean).join(" ");
    out.push(`<polyline points="${pts}" fill="none" stroke="${col[si % 3]}" stroke-width="1.8"/><rect x="${pl + si * 90}" y="22" width="10" height="10" fill="${col[si % 3]}"/><text x="${pl + 14 + si * 90}" y="31" font-size="12" fill="currentColor">${svgEsc(segs[si])}</text>`);
  });
  out.push(`<text x="0" y="13" font-size="13" fill="currentColor" font-weight="600">${svgEsc(T("approval.freq_title"))}</text></svg>`);
  return out.join("");
}

// Wow per revolution, one bar per copy and segment
function approvalWow(copies, kc) {
  const rows = copies.map((c) => ({ copy: c.copy, segs: c.res?.wow?.segments || [] })).filter((r) => r.segs.length);
  if (!rows.length) return "";
  const labels = rows[0].segs.map((s) => s.label), mx = Math.max(0.2, ...rows.flatMap((r) => r.segs.map((s) => s.wow_pct || 0)));
  const W = 480, H = 240, pl = 36, pr = 8, pt = 44, pb = 30, gw = (W - pl - pr) / labels.length, bw = Math.min(16, (gw - 12) / rows.length);
  const Y = (v) => pt + (1 - v / mx) * (H - pt - pb), col = COPY_COLORS;
  const out = [`<svg class="ap-svg ap-half" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(T("approval.wow_aria"))}">`];
  for (let v = 0; v <= mx + 1e-9; v += mx > 0.4 ? 0.1 : 0.05) out.push(`<line x1="${pl}" y1="${Y(v)}" x2="${W - pr}" y2="${Y(v)}" stroke="currentColor" stroke-opacity=".12"/><text x="${pl - 4}" y="${Y(v) + 3}" font-size="12" text-anchor="end" fill="currentColor" opacity=".7">${num(v, 2)}</text>`);
  labels.forEach((lb, gi) => {
    rows.forEach((r, ci) => { const v = r.segs[gi]?.wow_pct || 0, x = pl + gi * gw + 6 + ci * bw; out.push(`<rect x="${x}" y="${Y(v)}" width="${bw - 2}" height="${Y(0) - Y(v)}" fill="${col[ci % col.length]}" fill-opacity=".8"><title>${svgEsc(r.copy)} ${svgEsc(lb)}: ${num(v, 3)} %</title></rect>`); });
    out.push(`<text x="${pl + gi * gw + gw / 2}" y="${H - 12}" font-size="13" text-anchor="middle" fill="currentColor">${svgEsc(lb)}</text>`);
  });
  rows.forEach((r, ci) => out.push(`<rect x="${ci * 92}" y="22" width="10" height="10" fill="${col[ci % col.length]}"/><text x="${14 + ci * 92}" y="31" font-size="12" fill="currentColor">${svgEsc(r.copy.replace(/^Exemplar\s*/i, "Ex. "))}</text>`));
  out.push(`<text x="0" y="13" font-size="13" fill="currentColor" font-weight="600">${svgEsc(T("approval.wow_title"))}</text></svg>`);
  return out.join("");
}

// Level per track against the master, relative to the median (the overall loudness depends on the turntable)
function approvalLevels(copies, kc) {
  const items = copies[0]?.res?.levels?.items || [];
  if (!items.length) return "";
  const W = 480, H = 230, pl = 36, pr = 8, pt = 44, pb = 40, gw = (W - pl - pr) / items.length;
  const vals = copies.flatMap((c) => (c.res?.levels?.items || []).map((x) => x.rel_db)).filter((v) => v != null);
  const lim = Math.max(1.5, Math.ceil(Math.max(...vals.map(Math.abs)) * 2) / 2), Y = (v) => pt + ((lim - v) / (2 * lim)) * (H - pt - pb);
  const col = COPY_COLORS;
  const out = [`<svg class="ap-svg ap-half" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(T("approval.levels_aria"))}">`];
  for (const v of [-lim, -lim / 2, 0, lim / 2, lim]) out.push(`<line x1="${pl}" y1="${Y(v)}" x2="${W - pr}" y2="${Y(v)}" stroke="currentColor" stroke-opacity="${v === 0 ? 0.5 : 0.12}"/><text x="${pl - 4}" y="${Y(v) + 3}" font-size="12" text-anchor="end" fill="currentColor" opacity=".7">${sgn(v, 1)}</text>`);
  items.forEach((it, i) => out.push(`<text x="${pl + i * gw + gw / 2}" y="${H - 22}" font-size="13" text-anchor="middle" fill="currentColor">${svgEsc(it.pos)}</text><text x="${pl + i * gw + gw / 2}" y="${H - 10}" font-size="11" text-anchor="middle" fill="currentColor" opacity=".7">${svgEsc((it.title || "").slice(0, 14))}</text>`));
  copies.forEach((c, ci) => (c.res?.levels?.items || []).forEach((x, i) => { if (x.rel_db != null) out.push(`<circle cx="${pl + i * gw + gw / 2 + (ci - (copies.length - 1) / 2) * 6}" cy="${Y(x.rel_db)}" r="3.2" fill="${col[ci % col.length]}"><title>${svgEsc(c.copy)} ${svgEsc(x.pos)}: ${num(x.rel_db, 1)} dB</title></circle>`); }));
  copies.forEach((c, ci) => out.push(`<rect x="${ci * 92}" y="22" width="10" height="10" fill="${col[ci % col.length]}"/><text x="${14 + ci * 92}" y="31" font-size="12" fill="currentColor">${svgEsc(c.copy.replace(/^Exemplar\s*/i, "Ex. "))}</text>`));
  out.push(`<text x="0" y="13" font-size="13" fill="currentColor" font-weight="600">${svgEsc(T("approval.levels_title"))}</text></svg>`);
  return out.join("");
}

async function drawApprovalGraphs(rid, sd, si, audible) {
  const el = $(`#apg-${si}`); if (!el) return;
  const kc = { pop: css("--pop"), click: css("--click"), tick: css("--tick"), ok: css("--ok"), warn: css("--warn"), bad: css("--bad") };
  const [com, ...per] = await Promise.all([api(`/api/records/${rid}/master/common?side=${encodeURIComponent(sd.side)}`),
    ...sd.copies.map((c) => Promise.all([api(`/api/recordings/${c.recording_id}/master`), api(`/api/recordings/${c.recording_id}/events?order=t&limit=20000`)]))]);
  const copies = sd.copies.map((c, i) => {
    const res = per[i][0]?.check?.result || per[i][0]?.result || {}, mf = mapFns(res);
    const byT = new Map(per[i][1].map((e) => [Math.round(e.t * 1000), e]));
    const vinyl = ((res.clicks || {}).events || []).filter((x) => x[1] === "v").map((x) => { const e = byT.get(Math.round(x[0] * 1000)) || {}; return { t: x[0], tm: mf.toM(x[0]), kind: e.kind || "click", strength: e.strength || 0 }; });
    return { copy: c.copy, recording_id: c.recording_id, res, mf, vinyl };
  });
  const common = (com.common || []).filter((x) => x.n >= 2);
  const loud = common.filter((x) => x.max_strength >= audible).sort((a, b) => a.tm - b.tm).slice(0, 6);
  const nV = copies.reduce((a, c) => a + c.vinyl.length, 0);
  const dot = (c) => `<span style="color:${c}">●</span>`;
  el.innerHTML = `<h4>${T("approval.timeline_head")}</h4>
    <div class="ap-fig">${approvalTimeline(sd, common, copies, audible, kc)}</div>
    <p class="hint">${T("approval.timeline_hint", { copies: copies.length, audible: num(audible, 0), real: nV, pop: dot(kc.pop), click: dot(kc.click), tick: dot(kc.tick) })}</p>
    <h4>${T("approval.sg_head", { copy: esc(copies[0].copy) })}</h4>
    <div class="ap-fig"><canvas class="ap-sg" id="apsg-${si}"></canvas></div>
    ${loud.length ? `<h4>${T("approval.zoom_head", { s: 0.25 })}</h4>
      <div class="ap-zooms">${loud.map((x, li) => `<div class="ap-zoomrow"><div class="ap-zoomhead"><b>${fmtT(x.tm)}</b> ${EVKIND[x.kind] || esc(x.kind)} · ${T("approval.zoom_row", { n: x.n, total: copies.length, strength: num(x.max_strength, 0) })}</div>
        <div class="ap-zoomcells" style="grid-template-columns: repeat(${copies.length}, minmax(0, 1fr))">${copies.map((c, ci) => `<figure><canvas class="ap-sgz" id="apz-${si}-${li}-${ci}"></canvas><figcaption>${esc(c.copy)}</figcaption></figure>`).join("")}</div></div>`).join("")}</div>
      <p class="hint">${T("approval.zoom_hint")}</p>` : ""}
    <h4>${T("approval.curves_head")}</h4>
    <div class="ap-row">${approvalFreq(copies, kc)}${approvalWow(copies, kc)}${approvalLevels(copies, kc)}</div>`;
  // Spectrogram of the whole side (first copy), stamper defects as lines
  const c0 = copies[0], cv = $(`#apsg-${si}`);
  const mus = c0.res.music_v || [0, null];
  const sg = await fetchSpectrogram(c0.recording_id, { cols: 1600, rows: 160, t0: Math.max(0, (mus[0] || 0) - 2), t1: mus[1] != null ? mus[1] + 2 : null });
  paintSpectrogram(cv, sg);
  { const g = cv.getContext("2d"), w = cv.clientWidth, h = cv.clientHeight, span = sg.t1 - sg.t0;
    for (const x of common) {
      const tv = x.copies?.[c0.copy]?.t ?? c0.mf.toV(x.tm), xx = ((tv - sg.t0) / span) * w, all = x.n >= copies.length, isLoud = x.max_strength >= audible;
      if (xx < 0 || xx > w || (!isLoud && !all)) continue;
      g.strokeStyle = all ? kc.bad : kc.warn; g.lineWidth = isLoud ? 2 : 1; g.globalAlpha = isLoud ? 0.95 : 0.5;
      g.beginPath(); g.moveTo(xx, 0); g.lineTo(xx, h); g.stroke(); g.globalAlpha = 1;
      if (isLoud) { g.font = "11px system-ui"; g.fillStyle = "#fff"; g.strokeStyle = "rgba(0,0,0,.85)"; g.lineWidth = 3; g.strokeText(fmtT(x.tm), xx + 3, 13); g.fillText(fmtT(x.tm), xx + 3, 13); }
    } }
  // Close-ups of the audible spots per copy
  const jobs = [];
  loud.forEach((x, li) => copies.forEach((c, ci) => {
    const tv = x.copies?.[c.copy]?.t ?? c.mf.toV(x.tm);
    jobs.push(fetchSpectrogram(c.recording_id, { cols: 300, rows: 140, t0: Math.max(0, tv - 0.25), t1: tv + 0.25 }).then((z) => {
      const zc = $(`#apz-${si}-${li}-${ci}`); if (!zc) return;
      paintSpectrogram(zc, z);
      const g = zc.getContext("2d"), xx = ((tv - z.t0) / (z.t1 - z.t0)) * zc.clientWidth, hh = zc.clientHeight;
      g.fillStyle = x.copies?.[c.copy] ? kc.bad : "rgba(255,255,255,.7)";          // triangles at top and bottom, keep the middle clear
      g.beginPath(); g.moveTo(xx - 6, 0); g.lineTo(xx + 6, 0); g.lineTo(xx, 9); g.closePath(); g.fill();
      g.beginPath(); g.moveTo(xx - 6, hh); g.lineTo(xx + 6, hh); g.lineTo(xx, hh - 9); g.closePath(); g.fill();
    }).catch(() => {}));
  }));
  await Promise.all(jobs);
}

function setCompareBtn() {
  const b = $("#btn-compare"); if (!b) return;
  const n = state.selected.size;
  b.disabled = n < 2;
  b.textContent = n < 2 ? T("records.compare_selected") : n === 2 ? T("records.compare_two") : T("records.compare_n", { n });
}
// Several recordings of the same side (e.g. all copies of a pressing): each against the first one selected (the
// reference) with the before/after logic (alignment, remained/gone/new), summarized by what sits at the same spot on all
async function showMultiComparison(refId, pairs) {
  const recs = state.record?.recordings || [];
  const R = (id) => recs.find((x) => x.id === id) || { id };
  const name = (x) => `#${x.id}${x.copy_label ? ` ${esc(x.copy_label)}` : ""}${x.kind ? ` · ${KIND[x.kind]}` : ""}`;
  const v = openView();
  v.onclick = (e) => {
    if (e.target.id === "close-view") return closeView();
    const c = e.target.closest("[data-cmp]"); if (c) return showComparison(+c.dataset.cmp);
    const p = e.target.closest("[data-cplay]"); if (p) return play(+p.dataset.cplay, +p.dataset.t, 0.8, 1.2);
  };
  for (let round = 0; round < 600; round++) {
    const cs = await Promise.all(pairs.map((p) => api(`/api/comparisons/${p.cid}`).catch(() => null)));
    // The view was left (back, single comparison, report opened): don't overwrite it any more
    if (v.classList.contains("hidden") || (round > 0 && !v.querySelector("#multi-cmp"))) return;
    const done = cs.filter((c) => c && (c.status === "done" || c.status === "error")).length;
    const ref = R(refId), sm = (x) => x.summary || {};
    const rows = [{ x: ref, c: null }, ...pairs.map((p, i) => ({ x: R(p.rec), c: cs[i] }))];
    const res = cs.map((c) => (c?.status === "done" ? c.result : null));
    // On all: events of the reference that remained in every finished comparison
    let common = [];
    if (res.length && res.every((r) => r?.comparable && r.match)) {
      const maps = res.map((r) => new Map(r.match.events.persistent.map((e) => [e.t_a.toFixed(4), e])));
      common = res[0].match.events.persistent.filter((e) => maps.every((m) => m.has(e.t_a.toFixed(4))))
        .map((e) => ({ t: e.t_a, kind: e.kind, strength: e.strength, at: maps.map((m) => m.get(e.t_a.toFixed(4)).t_b) }))
        .sort((a, b) => b.strength - a.strength);
    }
    v.innerHTML = `<div class="view-bar"><button class="secondary" id="close-view">${ico("back", 16)} ${T("common.back_to_record")}</button></div>
      <h2 id="multi-cmp" style="margin:.3rem 0 .6rem">${T("records.multi_title", { n: rows.length, side: esc(ref.side || "") })}</h2>
      <p class="hint">${T("records.multi_hint", { ref: name(ref) })} ${done < pairs.length ? T("records.multi_progress", { done, total: pairs.length }) : ""}</p>
      <div class="scroll"><table><tr><th>${T("records.col_recording")}</th><th>${T("common.col_grade")}</th><th class="num">${T("records.col_music")}</th><th class="num">${T("records.col_pops")}</th><th class="num">${T("records.col_noise")}</th><th class="num" title="${esc(T("records.multi_remained_title"))}">${T("records.multi_remained")}</th><th class="num" title="${esc(T("records.multi_gone_title"))}">${T("records.multi_gone")}</th><th class="num" title="${esc(T("records.multi_new_title"))}">${T("records.multi_new")}</th><th></th></tr>
        ${rows.map(({ x, c }) => { const r = c?.result, m = r?.match?.counts;
          return `<tr><td>${name(x)}${c ? "" : ` <small class="hint">${T("records.multi_reference")}</small>`}</td><td>${gradeChip(x.grade)}</td><td class="num">${num(sm(x).clicks_music_confirmed_per_min ?? sm(x).clicks_music_per_min)}</td><td class="num">${num(sm(x).pops_per_min, 2)}</td><td class="num">${num(sm(x).noise_rel_db)}</td>
            ${!c ? `<td class="num">–</td><td class="num">–</td><td class="num">–</td><td></td>` : c.status === "error" ? `<td colspan="3" class="bad">${esc(c.error || T("common.status_error"))}</td><td></td>`
              : c.status !== "done" ? `<td colspan="3" class="hint"><span class="spin"></span> ${c.status === "running" ? T("records.cmp_running") : T("common.status_queued")}</td><td></td>`
              : r.comparable === false ? `<td colspan="3" class="hint">${esc(r.reason || T("records.not_comparable"))}</td><td></td>`
              : `<td class="num">${m.persistent}</td><td class="num">${m.removed}</td><td class="num">${m.new}</td><td><button class="link" data-cmp="${c.id}">${T("records.single_comparison")}</button></td>`}</tr>`; }).join("")}
      </table></div>
      ${done === pairs.length ? `<h3>${T("records.multi_common_head", { n: rows.length, count: common.length })}</h3>
        <p class="hint">${T("records.multi_common_hint")}</p>
        ${common.length ? `<div class="scroll"><table><tr><th class="num">${T("records.col_time_ref")}</th><th>${T("common.col_type")}</th><th class="num">${T("common.col_strength")}</th>${rows.map(({ x }) => `<th class="num">${name(x)}</th>`).join("")}</tr>
          ${common.slice(0, 40).map((e) => `<tr><td class="num">${fmtT(e.t)}</td><td>${EVKIND[e.kind] || esc(e.kind)}</td><td class="num">${num(e.strength, 0)}</td><td class="num"><button class="link" data-cplay="${refId}" data-t="${e.t}">▶</button></td>${e.at.map((tb, i) => `<td class="num"><button class="link" data-cplay="${pairs[i].rec}" data-t="${tb}">▶</button></td>`).join("")}</tr>`).join("")}
        </table></div>` : ""}` : ""}`;
    if (done === pairs.length) { refreshRecord(); return; }
    await new Promise((r) => setTimeout(r, 3000));
  }
}

// ─── Recording view ──────────────────────────────────────────────────────
// Reports open as a page of their own: the main area is hidden, "Back" at the top; closeView() restores the mode
function openView() {
  const v = $("#view"), wasHidden = v.classList.contains("hidden");
  for (const k of MODES) $("#main-" + k).classList.add("hidden");
  v.classList.remove("hidden");
  if (wasHidden) window.scrollTo({ top: 0 });
  return v;
}
function closeView() {
  $("#view").classList.add("hidden");
  for (const k of MODES) $("#main-" + k).classList.toggle("hidden", state.mode !== k);
  if (state.mode === "record" && state.record) $("#detail")?.scrollIntoView({ block: "start" });
}

// Events: the strongest clicks and all skips in one list, by time by default. Clicking a header sorts, a second
// click reverses. Verdicts: click / false alarm / unclear, or run-out groove / no skip for skips.
const EV_COLS = [["t", T("recording.col_time")], ["kind", T("common.col_type")], ["region", T("recording.col_region")], ["strength", T("common.col_strength"), "num"], ["amp_dbfs", T("recording.col_level"), "num"], ["ch", T("recording.col_channel")], ["gc", T("recording.col_ai"), "num"], ["lp", T("recording.col_real"), "num"]];
const EV_DEFAULT_SORT = { key: "t", dir: 1 };
// Verdicts on clicks: [value stored in the database, button text, explanation]. "music" is shown as "false alarm".
const EV_JUDGE = [["defect", T("recording.judge_click"), T("recording.judge_click_tip")],
                  ["music", T("recording.judge_music"), T("recording.judge_music_tip")],
                  ["unsure", T("recording.judge_unsure"), T("recording.judge_unsure_tip")]];
const eventRows = (top, skips) => [...top, ...(skips || []).filter((e) => e.kind === "backward")
  .map((e) => ({ t: e.t, kind: "skip", region: null, strength: e.score, amp_dbfs: null, ch: 0, gc: null, label: e.label, skip: e }))];
// Judged places are hidden so the list only shows what still needs a decision. state.judged remembers verdicts given
// after loading (list, graph), so the row disappears immediately.
state.judged = new Map();
state.showJudged = (() => { try { return localStorage.getItem("vc-show-judged") === "1"; } catch (_) { return false; } })();
const judgedKey = (recId, t) => `${recId}|${Number(t).toFixed(5)}`;
const evLabel = (e, rec) => (e.skip ? e.label : state.judged.has(judgedKey(rec.id, e.t)) ? state.judged.get(judgedKey(rec.id, e.t)) : e.label);
// Region in words: instead of "excluded" lead-in/lead-out, in the groove lead-in groove/run-out groove, the pause
// between tracks, and the track name within the music when Discogs knows it (track bounds from metrics.tracks).
const trackName = (x) => `${x.pos ? x.pos + " " : ""}${x.title || ""}`.trim();
function trackAt(t, m) {
  const tr = m?.tracks || [];
  const cur = tr.find((x) => t >= x.t0 && t < x.t1);
  if (cur) return { cur };
  return { prev: [...tr].reverse().find((x) => x.t1 <= t), next: tr.find((x) => x.t0 > t) };
}
function regionInfo(t, region, m) {
  const g = m?.groove || [], ms = m?.music_start_s, me = m?.music_end_s, base = REGION[region] ?? "–";
  if (region === "excluded") {
    if (g.length && t < g[0]) return { txt: T("recording.region_lead_in"), title: T("recording.region_lead_in_title") };
    if (g.length && t >= g[1]) return { txt: T("recording.region_lead_out"), title: T("recording.region_lead_out_title") };
    return { txt: base, title: T("recording.region_outside_title") };
  }
  if (ms != null && t < ms) return { txt: T("recording.region_in_lead_in", { region: base }), title: T("recording.region_in_lead_in_title") };
  if (me != null && t > me) return { txt: T("recording.region_in_run_out", { region: base }), title: T("recording.region_in_run_out_title") };
  const k = trackAt(t, m);
  if (k.cur) return { txt: `${base} · ${trackName(k.cur)}`, title: T("recording.region_in_track", { region: base, track: trackName(k.cur) }) };
  if (region === "quiet" && k.prev && k.next) return { txt: T("recording.region_pause", { prev: k.prev.pos || "", next: k.next.pos || "" }).trim(), title: T("recording.region_between", { prev: trackName(k.prev), next: trackName(k.next) }) };
  return { txt: base, title: "" };
}
// What-if: "don't count" per detection → the grade is recomputed at once as if it did not exist. View only: nothing
// is stored, verdicts and the learning model stay untouched. The choice is kept per recording while the page is open.
state.whatif = new Map();
const wiGet = (id) => { if (!state.whatif.has(id)) state.whatif.set(id, { ev: new Set(), sk: new Set() }); return state.whatif.get(id); };
let wiSeq = 0;
async function wiCompute(rec) {
  const w = wiGet(rec.id), bar = $("#wi-bar"), card = $("#wi-card");
  if (!bar) return;
  if (!w.ev.size && !w.sk.size) { bar.classList.add("hidden"); bar.innerHTML = ""; if (card) card.innerHTML = ""; return; }
  const my = ++wiSeq;
  bar.classList.remove("hidden"); bar.classList.add("busy");
  const r = await api(`/api/recordings/${rec.id}/whatif`, { method: "POST", body: { ignore_events: [...w.ev], ignore_skips: [...w.sk] } });
  if (my !== wiSeq) return;                                       // clicked on in the meantime: drop the stale answer
  bar.classList.remove("busy");
  const n = r.ignored || {};
  const parts = andList([n.skips ? T("recording.wi_skips", { n: n.skips }) : "", n.events ? T("recording.wi_clicks", { n: n.events }) : ""].filter(Boolean));
  if (!parts) { bar.innerHTML = `<b>${T("recording.wi_title")}</b> <span class="hint">${T("recording.wi_none_found")}</span> <button class="link" data-wi-reset>${T("recording.wi_reset")}</button>`; if (card) card.innerHTML = ""; return; }
  const delta = (k, label, d = 1) => { const a = r.base.metrics[k], b = r.metrics[k]; return a != null && b != null && Math.abs(a - b) > 1e-9 ? `${label} ${num(a, d)} → ${num(b, d)}` : ""; };
  const ch = [delta("skips_back", T("recording.wi_d_skips"), 0), delta("clicks_music_per_min", T("recording.wi_d_music")), delta("clicks_quiet_per_min", T("recording.wi_d_quiet")),
              delta("ticks_quiet_per_min", T("recording.wi_d_ticks")), delta("pops_per_min", T("recording.wi_d_pops"), 2)].filter(Boolean).join(" · ");
  const same = r.grade === r.base.grade;
  const rc = r.record;
  const recLine = !rc ? "" : !rc.decisive ? ` · <small class="hint">${T("recording.wi_newer_counts")}</small>`
    : ` · ${T("recording.wi_record", { copy: rc.copy ? ` (${esc(rc.copy)})` : "", grade: gradeChip(rc.grade) })}${rc.grade === rc.base ? " " + T("recording.wi_unchanged") : " " + T("recording.wi_instead", { grade: gradeChip(rc.base) })}`;
  bar.innerHTML = `<b>${T("recording.wi_title")}</b> <span class="hint">${T("recording.wi_view_only")}</span> <button class="link" data-wi-reset>${T("recording.wi_count_all")}</button><br>
    ${T("recording.wi_without", { what: parts, grade: gradeChip(r.grade), score: num(r.score, 0) })}${same ? " " + T("recording.wi_unchanged") : " " + T("recording.wi_instead", { grade: `${gradeChip(r.base.grade)} ${num(r.base.score, 0)}` })}${recLine}${ch ? `<br><small class="hint">${ch}</small>` : ""}`;
  if (card) card.innerHTML = T("recording.wi_card", { what: parts, grade: gradeChip(r.grade), score: num(r.score, 0) });
}
function eventsTable(rows0, rec, sort) {
  const { key, dir } = sort || EV_DEFAULT_SORT;
  const m = rec.analysis?.metrics || {}, wi = wiGet(rec.id);
  rows0 = rows0.map((e) => (e.skip ? e : { ...e, label: evLabel(e, rec) }));
  const nJudged = rows0.filter((e) => e.label).length;
  if (!state.showJudged) rows0 = rows0.filter((e) => !e.label);
  const cnt = $("#ev-judged-n"); if (cnt) cnt.textContent = nJudged ? T(state.showJudged ? "recording.judged_count" : "recording.judged_count_hidden", { n: nJudged }) : "";
  const KO = { skip: -1, pop: 0, click: 1, tick: 2 }, RO = { quiet: 0, music: 1, excluded: 2 };
  const val = (e) => (key === "kind" ? KO[e.kind] : key === "region" ? (e.region ? RO[e.region] : -1) : e[key] ?? -Infinity);
  const rows = [...rows0].sort((a, b) => ((val(a) < val(b) ? -1 : val(a) > val(b) ? 1 : 0) * dir) || a.t - b.t);
  const th = (k, label, cls) => `<th class="${cls || ""} sortable ${key === k ? "sorted" : ""}" data-sort="${k}" title="${esc(T("recording.sort"))}">${label}${key === k ? (dir < 0 ? " ▼" : " ▲") : ""}</th>`;
  const playBtn = (e) => (rec.audio_available ? `<button class="link" data-play="${e.t}"${e.skip ? ' data-before="3" data-after="4"' : ""} title="${esc(T("common.listen"))}">▶</button>` : "");
  const wiBtn = (kind, t, off) => `<td class="wi-cell"><button class="link wi-toggle ${off ? "chosen" : ""}" data-wi="${kind}" data-t="${t}" title="${esc(T(off ? "recording.wi_btn_on_title" : "recording.wi_btn_off_title"))}">${off ? T("recording.wi_btn_on") : T("recording.wi_btn_off")}</button></td>`;
  const wiCell = (e) => (e.skip ? (e.label ? `<td class="wi-cell hint" title="${esc(T("recording.wi_skip_judged_title"))}">${T("recording.wi_not_counted")}</td>` : wiBtn("skip", e.t, wi.sk.has(e.t)))
    : e.region === "excluded" ? `<td class="wi-cell hint" title="${esc(T("recording.wi_never_title"))}">${T("recording.wi_never")}</td>`
    : e.label === "music" ? `<td class="wi-cell hint" title="${esc(T("recording.wi_false_alarm_title"))}">${T("recording.wi_not_counted")}</td>` : wiBtn("ev", e.t, wi.ev.has(e.t)));
  const off = (e) => (e.skip ? wi.sk.has(e.t) : wi.ev.has(e.t));
  const ri = (e) => regionInfo(e.t, e.region, m);
  const skipTrack = (e) => { const k = trackAt(e.t, m); return k.cur ? ` · ${T("recording.in_track", { track: esc(trackName(k.cur)) })}` : ""; };
  const SKIP_JUDGE = [["run_out", T("recording.judge_run_out"), T("recording.judge_run_out_tip")], ["no_skip", T("recording.judge_no_skip"), T("recording.judge_no_skip_tip")]];
  const row = (e) => e.skip
    ? `<tr class="skip-row ${e.label ? "muted-row" : ""} ${off(e) ? "wi-off" : ""}"><td>${fmtT(e.t)}</td><td><b>${EVKIND.skip}</b></td><td>${T("recording.skip_grooves", { n: e.skip.grooves, repeats: e.skip.repeats })}${e.skip.repeats >= 4 ? " ⚠" : ""} · ${T("recording.skip_at", { pct: num(e.skip.position_pct, 0) })}${e.skip.radius_mm ? ` · r ≈ ${e.skip.radius_mm} mm` : ""}${skipTrack(e)}</td><td class="num" title="${esc(T("recording.confidence"))}">${num(e.strength, 2)}</td><td class="num">–</td><td>–</td><td class="num">–</td><td class="num">–</td><td>${playBtn(e)}</td>
        <td class="audit">${SKIP_JUDGE.map(([l, txt, tip]) => `<button class="link ${e.label === l ? "chosen" : ""}" data-skip-label="${l}" data-t="${e.t}" data-rec="${rec.id}" title="${esc(tip)}">${txt}</button>`).join(" ")}${e.label ? ` <button class="link" data-skip-label="" data-t="${e.t}" data-rec="${rec.id}" title="${esc(T("recording.verdict_withdraw"))}">×</button>` : ""}</td>${wiCell(e)}</tr>`
    : `<tr class="${e.label === "music" ? "muted-row" : ""} ${off(e) ? "wi-off" : ""}"><td>${fmtT(e.t)}</td><td>${EVKIND[e.kind]}</td><td class="region-cell" title="${esc(ri(e).title)}">${esc(ri(e).txt)}</td><td class="num">${num(e.strength)}</td><td class="num">${num(e.amp_dbfs)}</td><td>${["", "L", "R", "L+R"][e.ch]}</td><td class="num">${e.gc == null ? "–" : `<span class="${e.gc >= 0.55 ? "ok" : "bad"}">${Math.round(e.gc * 100)} %</span>`}</td><td class="num" title="${esc(T("recording.col_real_title"))}">${e.lp == null ? "–" : `<span class="${e.lp >= 0.5 ? "ok" : "bad"}">${Math.round(e.lp * 100)} %</span>`}</td><td>${playBtn(e)}</td>
        <td class="audit">${EV_JUDGE.map(([l, txt, tip]) => `<button class="link ${e.label === l ? "chosen" : ""}" data-label="${l}" data-t="${e.t}" data-rec="${rec.id}" title="${esc(tip)}">${txt}</button>`).join(" ")}</td>${wiCell(e)}</tr>`;
  if (!rows.length) return `<p class="empty">${nJudged ? T("recording.all_judged", { n: nJudged }) : T("recording.no_events")}</p>`;
  return `<table><tr>${EV_COLS.map(([k, l, c]) => th(k, l, c)).join("")}<th></th><th>${T("recording.col_verdict")}</th><th title="${esc(T("recording.col_for_grade_title"))}">${T("recording.col_for_grade")}</th></tr>${rows.map(row).join("")}</table>`;
}

async function showRecording(id) {
  const [rec, ov, top] = await Promise.all([api(`/api/recordings/${id}`), api(`/api/recordings/${id}/overview`), api(`/api/recordings/${id}/events?order=strength&limit=60`)]);
  const a = rec.analysis, m = a.metrics, g = a.grading;
  const v = openView();
  const so = m.second_opinion, confirmed = so?.clicks_music_confirmed_per_min;
  v.innerHTML = `
    <div class="view-bar"><button class="secondary" id="close-view">${ico("back", 16)} ${T("common.back_to_record")}</button>
      <a class="button secondary" id="btn-rec-pdf" href="/api/recordings/${rec.id}/report.pdf" download>${ico("export", 16)} ${T("recording.pdf_report")}</a></div>
    <h2 style="margin:.3rem 0 .6rem">${T("recording.title", { id: rec.id, side: esc(rec.side), kind: KIND[rec.kind] })}${rec.copy_label ? `, ${esc(rec.copy_label)}` : ""}</h2>
    ${findingsText(rec) || rec.notes ? `<p class="hint">${findingsText(rec) ? T("common.findings_line", { list: esc(findingsText(rec)) }) : ""}${findingsText(rec) && rec.notes ? " · " : ""}${rec.notes ? esc(rec.notes) : ""}</p>` : ""}
    <div class="cards">
      <div class="stat"><span>${T("recording.card_grade")}</span><b class="big-grade grade ${gradeCls(g.grade)}">${g.grade ?? "–"}</b><span>${num(g.score, 0)} / 100 ${stickerChip(g.grade)}</span><span id="wi-card" class="stat-est" title="${esc(T("recording.card_wi_title"))}"></span></div>
      <div class="stat"><span>${T(confirmed != null ? "recording.card_music_confirmed" : "recording.card_music")}</span><b>${num(confirmed ?? m.clicks_music_per_min)}</b><span>${confirmed != null ? `${T("recording.raw", { v: num(m.clicks_music_per_min) })} · ` : ""}${m.learned?.graded ? T("recording.grade_from_estimate") : T("recording.determines_grade")}</span>${m.learned?.clicks_music_per_min != null ? `<span class="stat-est" title="${esc(T("recording.learned_title"))}">${T("recording.learned_est", { v: num(m.learned.clicks_music_per_min) })}${m.learned.pops_per_min != null ? ` · ${T("recording.learned_pops", { v: num(m.learned.pops_per_min, 2) })}` : ""}</span>` : ""}</div>
      <div class="stat"><span>${T(m.floor ? "recording.card_quiet_net" : "recording.card_quiet")}</span><b>${num(m.clicks_quiet_net_per_min ?? m.clicks_quiet_per_min)}</b>${m.floor ? `<span>${T("recording.raw_floor", { raw: num(m.clicks_quiet_per_min), floor: num(m.floor.clicks_per_min) })}</span>` : ""}</div>
      <div class="stat"><span>${T(m.floor ? "recording.card_ticks_net" : "recording.card_ticks")}</span><b>${num(m.ticks_quiet_net_per_min ?? m.ticks_quiet_per_min)}</b>${m.floor ? `<span>${T("recording.raw_floor", { raw: num(m.ticks_quiet_per_min), floor: num(m.floor.ticks_per_min) })}</span>` : ""}</div>
      <div class="stat"><span>${T("recording.card_pops")}</span><b>${m.counts.pop}</b><span>${num(m.pops_per_min, 2)}/min</span></div>
      <div class="stat"><span>${T("recording.card_noise")}</span><b>${num(m.noise_dbfs)} dBFS</b><span>${T("recording.noise_rel", { db: num(m.noise_rel_db) })}${m.noise_a_dbfs != null ? ` · A: ${num(m.noise_a_dbfs)}` : ""}</span></div>
      <div class="stat"><span>${T("recording.card_quiet_groove")}</span><b>${num(m.quiet_s, 0)} s</b><span>${T("recording.of_groove", { t: fmtTime(m.groove_s) })}</span></div>
    </div>
    ${!rec.audio_available ? `<div class="warnbox">${rec.audio_deleted_at ? T("recording.audio_deleted_on", { date: dayStr(rec.audio_deleted_at) }) : T("recording.audio_missing")}</div>` : ""}
    ${g.explanation ? `<div class="warnbox">${esc(g.explanation)}</div>` : ""}
    ${a.warnings.map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
    <div class="row" style="gap:16px;align-items:center;flex-wrap:wrap">
      ${rec.audio_available ? `<button class="secondary" data-clicks-only="${rec.id}">▶ ${T("recording.clicks_only")}</button>` : ""}
      <label class="hint">${T("recording.sensitivity")} <select data-rec-sens="${rec.id}">${[0.3, 0.4, 0.5, 0.6, 0.7].map((s) => `<option value="${s}" ${Math.abs(s - (rec.sensitivity ?? m.profile?.sensitivity ?? 0.5)) < 0.01 ? "selected" : ""}>${num(s, 1)}${s === 0.5 ? ` ${T("recording.sensitivity_calibrated")}` : ""}</option>`).join("")}</select>
        ${rec.sensitivity == null ? T("recording.sensitivity_from_profile") : `${T("recording.sensitivity_this_only")} · <button class="link" data-rec-sens-reset="${rec.id}">${T("recording.sensitivity_as_profile")}</button>`}</label>
      <label class="hint">${T("common.col_copy")} <input data-rec-copy="${rec.id}" value="${esc(rec.copy_label || "")}" maxlength="40" placeholder="${esc(T("recording.copy_placeholder"))}" style="width:9em"></label>
      <label class="hint">${T("recording.cleaning")} <input data-rec-clean="${rec.id}" value="${esc(rec.cleaning_method || "")}" maxlength="80" placeholder="${esc(T("recording.cleaning_placeholder"))}" style="width:12em"></label>
      <label class="hint">${T("recording.note")} <input data-rec-notes="${rec.id}" value="${esc(rec.notes || "")}" maxlength="200" placeholder="${esc(T("recording.note_placeholder"))}" style="width:18em"></label>
      <span class="hint findings" title="${esc(T("recording.findings_title"))}">${T("recording.findings_label")} ${findingBoxes(rec.id, rec.findings || [], "rec-finding")}</span>
      <span class="hint">${T("recording.diff_track_hint")}</span>
    </div>
    <div id="clicks-audio"></div>
    <div class="legend"><span><i style="background:var(--pop)"></i>${EVKIND.pop}</span><span><i style="background:var(--click)"></i>${EVKIND.click}</span><span><i style="background:var(--tick)"></i>${EVKIND.tick}</span><span><i style="background:var(--quiet);border:1px solid var(--ok)"></i>${T("recording.legend_quiet")}</span><span>${T("recording.legend_click")}</span></div>
    <div class="panel-head"><h3>${T("recording.clicks_head")}</h3><span class="hint">${T("recording.counts", { clicks: m.counts.click + m.counts.pop, ticks: m.counts.tick })}${m.scratch_count ? ` · ${T("recording.scratches", { n: m.scratch_count, revs: m.scratch_clicks })}` : ""}</span></div>
    <div class="chart-wrap"><canvas id="ov-clicks" class="chart small"></canvas><div class="sel"></div><div class="tcursor"><span></span></div></div>
    <p class="hint"><span id="chart-range">${T("recording.whole_side")}</span> · ${T("recording.chart_hint")} <label><input type="checkbox" id="hover-play" ${hoverPlay.on ? "checked" : ""}> ${T("recording.hover_play")}</label></p>
    <div class="panel-head"><h3>${T("recording.level_head")}</h3><span class="hint">${m.music_start_s != null && m.music_end_s != null ? T("recording.music_range", { start: fmtTime(m.music_start_s), end: fmtTime(m.music_end_s) }) : ""}</span></div>
    <div class="chart-wrap"><canvas id="ov-level" class="chart small"></canvas><div class="sel"></div><div class="tcursor"><span></span></div></div>
    <p class="hint">${T("recording.level_hint", { noise: num(m.noise_dbfs), ref: num(m.music_ref_dbfs) })}${m.chain_db != null ? T("recording.level_hint_chain", { db: num(m.chain_db) }) : ""}.</p>
    ${trackTable(m)}
    ${state.status?.engineer_mode && rec.kind !== "calibration" ? `<div id="master-box"></div>` : ""}
    <h3>${T("recording.spectrogram_head")}</h3>
    <div id="sg"></div>
    <h3>${T("recording.grades_head")}</h3>
    <table><tr><th>${T("grade.col_metric")}</th><th class="num">${T("grade.col_value")}</th><th>${T("grade.col_grade")}</th></tr>
      ${Object.values(g.per_metric).map((p) => `<tr><td>${esc(p.label)}</td><td class="num">${num(p.value, 2)}</td><td>${p.grade ? `<span class="grade ${gradeCls(p.grade)}">${p.grade}</span>` : "–"}</td></tr>`).join("")}
    </table>
    ${techTable(m, rec)}
    <h3>${T("recording.events_head")}</h3>
    ${skipNotes(m.skips, rec)}
    ${m.learned ? `<p class="hint">${T("recording.learned_hint", { clicks: num(m.learned.counts?.click ?? 0, 0), clicks_total: m.counts?.click ?? 0, ticks: num(m.learned.counts?.tick ?? 0, 0), ticks_total: m.counts?.tick ?? 0, pops: num(m.learned.counts?.pop ?? 0, 0), pops_total: m.counts?.pop ?? 0 })}${m.learned.graded ? " " + T("recording.learned_graded") : ""}</p>` : ""}
    <p class="hint">${T("recording.events_hint", { n: top.length })} ${so?.available ? `${T("recording.so_summary", { quiet: pct1(so.confirmed_quiet), music: pct1(so.confirmed_music) })}${confirmed != null ? ` · ${T("recording.so_confirmed", { v: num(confirmed) })}` : ""}${so.seconds ? ` · ${num(so.seconds, 0)} s` : ""}.` : T("recording.so_unavailable")}
      ${T("recording.audit_hint")}${m.counts?.labeled_music ? ` ${T("recording.audit_marked", { n: m.counts.labeled_music })}` : ""}.</p>
    <div class="row" style="gap:.6rem;align-items:center"><span class="hint">${T("recording.more_clicks")}</span>
      <button class="link" data-ev-more="200">${T("recording.more_n", { n: 200 })}</button><button class="link" data-ev-more="1000">${T("recording.more_n", { n: 1000 })}</button>
      <label class="hint" style="margin-left:auto" title="${esc(T("recording.show_judged_title"))}"><input type="checkbox" id="ev-show-judged" ${state.showJudged ? "checked" : ""}>${T("recording.show_judged")} <span id="ev-judged-n"></span></label></div>
    <div id="wi-bar" class="wi-bar hidden" aria-live="polite"></div>
    <div class="scroll" id="ev-wrap"></div>`;
  $("#close-view").onclick = closeView;
  let evRows = top;
  const renderEv = () => { const w = $("#ev-wrap"); if (w) w.innerHTML = eventsTable(eventRows(evRows, m.skips), rec, state.evSort); };
  state.renderEv = renderEv; renderEv();
  state.wiRefresh = () => guard(() => wiCompute(rec))();
  state.wiRefresh();                                              // restore this recording's what-if
  $("#ev-show-judged").onchange = (e) => { state.showJudged = e.target.checked; try { localStorage.setItem("vc-show-judged", state.showJudged ? "1" : "0"); } catch (_) {} renderEv(); };
  v.onclick = (e) => {
    if (e.target.dataset.play) return play(rec.id, +e.target.dataset.play, +(e.target.dataset.before || 0.6), +(e.target.dataset.after || 1.2));
    if (e.target.dataset.mplay) return playMaster(rec.id, +e.target.dataset.mplay, +(e.target.dataset.before || 0.6), +(e.target.dataset.after || 1.2));
    if (e.target.closest("[data-master-run]")) return guard(async () => { await api(`/api/recordings/${rec.id}/master`, { method: "POST" }); loadMasterBox(rec); })();
    const wb = e.target.closest("[data-wi]");
    if (wb) { const w = wiGet(rec.id), s = wb.dataset.wi === "skip" ? w.sk : w.ev, t = +wb.dataset.t; s.has(t) ? s.delete(t) : s.add(t); renderEv(); return state.wiRefresh(); }
    if (e.target.closest("[data-wi-reset]")) { const w = wiGet(rec.id); w.ev.clear(); w.sk.clear(); renderEv(); return state.wiRefresh(); }
    const th = e.target.closest("th[data-sort]");
    if (th) { const k = th.dataset.sort, cur = state.evSort || EV_DEFAULT_SORT; state.evSort = { key: k, dir: cur.key === k ? -cur.dir : (k === "t" || k === "kind" || k === "region" ? 1 : -1) }; return renderEv(); }
    const more = e.target.closest("[data-ev-more]");
    if (more) guard(async () => { evRows = await api(`/api/recordings/${rec.id}/events?order=strength&limit=${more.dataset.evMore}`); renderEv(); toast(T("recording.events_loaded", { n: evRows.length })); })();
  };
  const pick = (t) => (!rec.audio_available ? toast(T("recording.audio_deleted_no_listen"))
    : view.zoomed ? playRange(rec.id, t, view.t0, view.t1, true) : playRange(rec.id, t, t, rec.duration_s, false));   // click = from here; zoomed: loop
  const hover = (t) => (rec.audio_available ? play(rec.id, t, 0.25, 0.6, true) : null);
  const durAll = ov.min.length * ov.bucket_s, view = { t0: 0, t1: durAll, zoomed: false };
  // Verdict right in the graph: click a click while zoomed → listen, master, decide
  let masterEv = null;                                           // master comparison classification per event
  if (state.status?.engineer_mode) api(`/api/recordings/${rec.id}/master`).then((d) => {
    const evs = d.check?.result?.clicks?.events; if (evs) masterEv = new Map(evs.map((x) => [x[0].toFixed(4), x]));
  }).catch(() => {});
  const evJudge = (ev, e) => {
    closeEvPop();
    const [t, kind, region, strength, label, gc] = ev;
    const me = masterEv?.get(t.toFixed(4));
    const wrap = e.target.closest(".chart-wrap");
    const p = document.createElement("div");
    p.id = "ev-pop"; p.className = "ev-pop";
    p.innerHTML = `<div><b>${EVKIND[kind] || esc(kind)}</b> ${T("recording.pop_at", { t: fmtT(t) })} · ${esc(regionInfo(t, region, m).txt)} · ${T("recording.pop_strength", { v: num(strength, 1) })}${gc != null ? ` · ${T("recording.pop_ai", { p: Math.round(gc * 100) })}` : ""}${me ? ` · ${me[1] === "v" ? T("recording.pop_master_real") : T("recording.pop_master_music")}${me[2] != null ? ` ${T("recording.pop_master_rest", { p: Math.round(me[2] * 100) })}` : ""}` : ""}</div>
      <div class="row">${rec.audio_available ? `<button class="secondary" data-pop-play="v">${ico("play", 14)} ${T("common.listen")}</button>` : ""}${me ? `<button class="secondary" data-pop-play="m">${ico("wave", 14)} Master</button>` : ""}</div>
      <div class="row">${EV_JUDGE.map(([l, txt, tip]) => `<button class="${label === l ? "" : "secondary"}" data-pop-judge="${l}" title="${esc(tip)}">${txt}</button>`).join("")}${label ? `<button class="link" data-pop-judge="" title="${esc(T("recording.verdict_withdraw"))}">× ${T("recording.undo")}</button>` : ""}</div>`;
    p.style.left = Math.max(0, Math.min(e.offsetX - 20, wrap.clientWidth - 330)) + "px";
    wrap.appendChild(p);
    p.onclick = guard(async (x) => {
      x.stopPropagation();
      const pl = x.target.closest("[data-pop-play]");
      if (pl) return pl.dataset.popPlay === "m" ? playMaster(rec.id, t, 0.8, 1.2) : play(rec.id, t, 0.8, 1.2);
      const j = x.target.closest("[data-pop-judge]"); if (!j) return;
      const lab = j.dataset.popJudge || null;
      const r = await api(`/api/recordings/${rec.id}/events/label`, { method: "PUT", body: { t, label: lab } });
      ev[4] = lab;
      state.judged.set(judgedKey(rec.id, t), lab);
      const row = evRows.find((y) => Math.abs(y.t - t) < 1e-4); if (row) row.label = lab;
      renderEv();
      gradeCache.delete(rec.id); closeEvPop(); redraw(); refreshRecord();
      if ($("#master-box")) loadMasterBox(rec);                    // the grade by ear counts verdicts too
      toast(lab ? T("recording.verdict_saved_pops", { verdict: j.textContent.trim(), grade: r.grade ?? "–", pops: num(r.pops_per_min, 2), music: num(r.clicks_music_per_min) }) : T("recording.verdict_removed"), 5000);
    });
  };
  const redraw = () => {
    drawClicks($("#ov-clicks"), ov, m, pick, hover, view, evJudge);
    drawLevel($("#ov-level"), ov, m, pick, hover, view);
    const r = $("#chart-range"); if (r) r.textContent = view.zoomed ? T("recording.range", { from: fmtTime(view.t0), to: fmtTime(view.t1), s: Math.round(view.t1 - view.t0) }) : T("recording.whole_side");
  };
  setupZoom($("#ov-clicks"), view, durAll, redraw); setupZoom($("#ov-level"), view, durAll, redraw); redraw();
  if ($("#master-box")) loadMasterBox(rec);
  $("#hover-play").onchange = (e) => { hoverPlay.on = e.target.checked; try { localStorage.setItem("vc-hover-play", hoverPlay.on ? "1" : "0"); } catch (_) {} };
  const sgView = spectroView($("#sg"), [{ recId: rec.id, label: `#${rec.id} ${KIND[rec.kind]}`, map: (t) => t }], rec.duration_s, (it, t) => {
    if (!rec.audio_available) return toast(T("recording.audio_deleted_short"));
    const zoomed = sgView.t0 > 0.01 || sgView.t1 < rec.duration_s - 0.01;
    return zoomed ? playRange(rec.id, t, sgView.t0, sgView.t1, true) : playRange(rec.id, t, t, rec.duration_s, false);
  });
  // Time cursor: a line follows the mouse, synchronized across the click, level and spectrogram graphs
  const charts = [$("#ov-clicks"), $("#ov-level")];
  const xOf = (c, t) => CH_PAD.l + ((t - view.t0) / (view.t1 - view.t0)) * (c.clientWidth - CH_PAD.l - CH_PAD.r);
  const tOf = (c, px) => view.t0 + ((px - CH_PAD.l) / (c.clientWidth - CH_PAD.l - CH_PAD.r)) * (view.t1 - view.t0);
  const place = (c, x, on, t) => { const cur = c.parentElement.querySelector(".tcursor"); if (!cur) return; cur.style.display = on ? "block" : "none"; cur.style.left = x + "px"; cur.querySelector("span").textContent = fmtT(t); };
  const showCursor = (t) => {
    for (const c of charts) { const x = xOf(c, t); place(c, x, x >= CH_PAD.l && x <= c.clientWidth - CH_PAD.r, t); }
    for (const c of $("#sg").querySelectorAll(".sg-row canvas")) { const x = ((t - sgView.t0) / (sgView.t1 - sgView.t0)) * c.clientWidth; place(c, x, x >= 0 && x <= c.clientWidth, t); }
  };
  const hideCursor = (e) => { if (e?.relatedTarget?.closest?.("#mini-player")) return; v.querySelectorAll(".tcursor").forEach((c) => { c.style.display = "none"; }); };
  for (const c of charts) { c.addEventListener("mousemove", (e) => showCursor(tOf(c, e.offsetX))); c.addEventListener("mouseleave", hideCursor); }
  $("#sg").addEventListener("mousemove", (e) => { const c = e.target.closest("canvas"); if (c) showCursor(sgView.t0 + (e.offsetX / c.clientWidth) * (sgView.t1 - sgView.t0)); });
  $("#sg").addEventListener("mouseleave", hideCursor);
  if (!state.viewKeepScroll) window.scrollTo({ top: 0 });
}

// ─── Engineer mode: each recording against the digital master of its side ─────────
// One master per record side. Sides come from the track list, existing masters and recordings, else A and B.
function masterSection(r) {
  const sides = [...new Set([...Object.keys(r.tracks || {}), ...(r.masters || []).map((m) => m.side),
    ...r.recordings.filter((x) => x.kind !== "calibration").map((x) => String(x.side || "").trim().toUpperCase().slice(0, 1))])].filter((x) => /^[A-Z]$/.test(x)).sort();
  if (!sides.length) sides.push("A", "B");
  const by = Object.fromEntries((r.masters || []).map((m) => [m.side, m]));
  return `<h3>${ico("wave", 18)} ${T("master.title")}</h3>
    <p class="hint">${T("master.intro")}</p>
    <table class="kv"><tbody>${sides.map((sd) => { const m = by[sd]; return `<tr><th scope="row">${T("master.side", { side: esc(sd) })}</th>
      <td>${m ? `<b>${esc(m.name)}</b> <span class="hint">${fmtTime(m.duration_s)} · ${num(m.samplerate / 1000, 1)} kHz · ${esc(m.subtype || "")}${m.bytes ? ` · ${num(m.bytes / 1e6, 0)} MB` : ""}</span>` : `<span class="hint">${T("master.none")}</span>`}</td>
      <td><button class="link" data-master-up="${esc(sd)}">${m ? T("master.replace") : T("master.upload")}</button>${m ? ` <button class="link" data-master-del="${esc(sd)}">${T("master.remove")}</button>` : ""}</td></tr>`; }).join("")}</tbody></table>
    <input type="file" id="master-file" accept=".wav,.flac,.aif,.aiff,audio/wav,audio/x-wav,audio/flac,audio/aiff" hidden>
    ${(r.masters || []).length ? `<div class="row"><button class="secondary" id="btn-approval" title="${T("master.approval_title")}">${ico("ok", 16)} ${T("master.approval")}</button></div>` : ""}
    ${sides.filter((sd) => by[sd] && r.recordings.filter((x) => String(x.side || "").trim().toUpperCase().startsWith(sd) && r.master_states?.[x.id] === "done").length >= 2)
      .map((sd) => `<div class="master-common" data-common-side="${esc(sd)}"><p class="hint">${T("master.common_loading", { side: esc(sd) })}</p></div>`).join("")}`;
}
// Copies of one side against each other, mapped onto one time axis via the master: spots shared by all copies
// come from the stamper or the cut
async function loadMasterCommon(r) {
  for (const box of document.querySelectorAll("[data-common-side]")) {
    const sd = box.dataset.commonSide;
    let d;
    try { d = await api(`/api/records/${r.id}/master/common?side=${encodeURIComponent(sd)}`); } catch (e) { box.innerHTML = `<div class="warnbox">${esc(e.message)}</div>`; continue; }
    const names = d.copies.map((c) => c.copy);
    box.innerHTML = `<h4>${T("master.common_title", { side: esc(sd) })}</h4>
      <table><tr><th>${T("master.col_copy")}</th><th>${T("master.col_grade")}</th><th>${T("master.col_vs_master")}</th><th class="num" title="${T("master.col_real_title")}">${T("master.col_real")}</th><th class="num" title="${T("master.col_shared_title")}">${T("master.col_shared")}</th><th class="num" title="${T("master.col_loud_title")}">${T("master.col_loud")}</th></tr>
      ${d.copies.map((c) => `<tr><td>${esc(c.copy)}</td><td>${gradeChip(c.grade)}</td><td>${gradeChip(c.grade_master)}</td><td class="num">${c.events}</td><td class="num">${c.shared}</td><td class="num">${c.audible}</td></tr>`).join("")}</table>
      <p class="hint">${T("master.common_hint", { n: d.common_count, tol: num(d.tol_ms, 0), random: num(d.expected_random, 1),
        all: d.copies.length > 2 ? T("master.common_on_all", { n: d.on_all, copies: d.copies.length }) : "" })}</p>
      ${d.common.length ? `<details><summary>${T("master.common_spots", { n: Math.min(40, d.common.length) })}</summary><div class="scroll"><table><tr><th class="num">${T("master.col_master_time")}</th><th>${T("master.col_kind")}</th><th>${T("master.col_channel")}</th>${names.map((n) => `<th class="num">${esc(n)}</th>`).join("")}</tr>
        ${d.common.slice(0, 40).map((c) => `<tr><td class="num">${fmtT(c.tm)}</td><td>${EV_KIND[c.kind] || esc(c.kind)}</td><td>${["", "L", "R", "L+R"][c.ch] || ""}</td>${names.map((n) => { const x = c.copies[n]; return `<td class="num">${x ? `${num(x.strength, 0)} <button class="link" data-cplay="${x.recording_id}" data-t="${x.t}" title="${T("master.listen_copy", { copy: esc(n) })}">▶</button>` : "–"}</td>`; }).join("")}</tr>`).join("")}</table></div></details>` : ""}`;
  }
}
// Upload with progress (a side master is 300–400 MB): XMLHttpRequest, because fetch has no upload progress
function uploadMaster(rid, side, file) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", `/api/records/${rid}/masters/${encodeURIComponent(side)}?name=${encodeURIComponent(file.name)}`);
    x.upload.onprogress = (e) => { const b = $(`[data-master-up="${side}"]`); if (b && e.lengthComputable) b.textContent = T("master.uploading", { pct: Math.round((e.loaded / e.total) * 100) }); };
    x.onload = () => {
      if (x.status < 300) return resolve(JSON.parse(x.responseText));
      let msg = x.statusText; try { const j = JSON.parse(x.responseText); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
      reject(new Error(msg));
    };
    x.onerror = () => reject(new Error(T("master.upload_aborted")));
    x.send(file);
  });
}
function playMaster(recId, t, before = 0.6, after = 1.2) {
  const p = $("#player");
  playback.mode = null; p.loop = false; playback.from = Math.max(0, t - before);
  p.src = `/api/recordings/${recId}/master/snippet?t=${t}&before=${before}&after=${after}`;
  miniPlayer.show(`${ico("wave", 14)} ${T("master.player_label", { id: recId, from: fmtTime(Math.max(0, t - before)), to: fmtTime(t + after) })}`);
  p.play().catch((e) => { miniPlayer.hide(); toast(T("live.play_failed", { error: e?.message || "" }), 6000); });
}
let masterPoll = null;
async function loadMasterBox(rec) {
  clearTimeout(masterPoll);
  const box = $("#master-box"); if (!box) return;
  let d;
  try { d = await api(`/api/recordings/${rec.id}/master`); } catch (e) { box.innerHTML = `<div class="warnbox">${esc(e.message)}</div>`; return; }
  if (!$("#master-box")) return;
  box.innerHTML = masterBoxHtml(rec, d);
  if (d.check && (d.check.status === "queued" || d.check.status === "running")) masterPoll = setTimeout(() => loadMasterBox(rec), 4000);
  if (d.check?.status === "done" && d.check.result?.ok) drawMasterCharts(d.check.result);
}
const EV_KIND = { pop: T("master.ev_pop"), click: T("master.ev_click"), tick: T("master.ev_tick") };
const EV_REGION = { music: T("master.region_music"), quiet: T("master.region_quiet"), lead_in: T("master.region_lead_in"), lead_out: T("master.region_lead_out") };
// Enum values stored by the master check, shown translated; unknown values pass through
const GAP_KIND = { start: T("master.gap_start"), end: T("master.gap_end"), gap: T("master.gap_pause") };
const POLARITY_TEXT = { inverted: T("master.pol_inverted"), right_inverted: T("master.pol_right"), left_inverted: T("master.pol_left") };
const abPlay = (t, before = 0.6, after = 1.2) => `<button class="link" data-play="${t}" data-before="${before}" data-after="${after}" title="${T("master.play_vinyl_title")}">▶ Vinyl</button> <button class="link" data-mplay="${t}" data-before="${before}" data-after="${after}" title="${T("master.play_master_title")}">▶ Master</button>`;
function masterBoxHtml(rec, d) {
  const c = d.check, m = d.master;
  const R = c?.result;
  const head = (extra) => `<div class="panel-head"><h3>${ico("wave", 18)} ${T("master.box_title")}</h3><span class="hint">${extra || ""}</span></div>`;
  const rerun = `<div class="row"><button class="secondary" data-master-run="1">${c ? T("master.rerun") : T("master.run")}</button></div>`;
  const fname = esc(R?.master_file?.name || m?.name || "");
  const file = T("master.file", { name: fname });
  if (!m && !R) return head() + `<p class="hint">${T("master.no_master_for_side", { side: esc(rec.side) })}</p>`;
  if (!c) return head(file) + rerun;
  if (c.status === "queued" || c.status === "running") return head(file) + `<p class="hint">${c.status === "running" ? T("master.measuring") : T("master.waiting")} ${T("master.duration_hint")}</p>`;
  if (c.status === "error") return head(file) + `<div class="warnbox">${T("master.failed", { error: esc(c.error || "") })}</div>` + (m ? rerun : "");
  if (!R.ok) return head(file) + `<div class="warnbox">${esc(R.reason)}</div>` + (m ? rerun : "");
  const w = R.wow || {}, ch = R.channels || {}, f = R.freq || {}, g = R.gaps || {}, k = R.clicks || {}, sp = R.speed || {};
  const segW = (w.segments || []).filter((x) => x.wow_pct != null);
  const mus = k.by_region?.music || { vinyl: 0, master: 0 };
  const stat = (label, val, sub) => `<div class="stat"><span>${esc(label)}</span><b>${val}</b><span>${sub || ""}</span></div>`;
  const pol = R.polarity === "ok" ? (ch.swapped ? T("master.channels_swapped") : T("master.pol_ok")) : esc(POLARITY_TEXT[R.polarity] || R.polarity);
  const echoCell = (e, pre) => !e ? "–" : e.found ? `<b>${num(e.db, 0)} dB</b> <button class="link" data-play="${pre ? e.t_v : e.music_t_v}" data-before="0.3" data-after="${Math.abs(e.music_t_v - e.t_v) + 0.8}" title="${pre ? T("master.echo_pre_title") : T("master.echo_post_title")}">▶</button>` : `<span class="hint">– (${num(e.db, 0)})</span>`;
  const lv = R.levels?.items || [];
  return head(T("master.head_done", { name: fname, anchors: R.alignment?.anchors_used ?? "?", quality: num(R.alignment?.quality, 2), secs: num(R.seconds, 0) }))
    + (R.warnings || []).map((x) => `<div class="warnbox">${esc(x)}</div>`).join("")
    + `<div class="cards">
      ${d.grading_master?.grade ? stat(T("master.stat_grade"), `<span class="grade ${gradeCls(d.grading_master.grade)}">${d.grading_master.grade}</span> ${num(d.grading_master.score, 0)} ${stickerChip(d.grading_master.grade)}`, T("master.stat_grade_sub", { grade: rec.analysis?.grading?.grade ?? "–" })) : ""}
      ${stat(T("master.stat_speed"), `${sgn(sp.vs_master_pct, 3)} %`, sp.cut_pct != null ? T("master.stat_speed_sub", { tt: sgn(sp.turntable_pct, 3), cut: sgn(sp.cut_pct, 3) }) : T("master.stat_speed_unmeasured"))}
      ${stat(T("master.stat_hole"), w.ecc_mm != null ? `≈ ${num(w.ecc_mm, 2)} mm` : "–", segW.length ? T("master.stat_hole_sub", { list: segW.map((x) => num(x.wow_pct, 2)).join(" → ") }) : "")}
      ${stat(T("master.stat_clicks"), `${num(k.vinyl_music_per_min, 1)}/min`, T("master.stat_clicks_sub", { pops: k.vinyl_music_by_kind?.pop ?? 0, master: mus.master, total: mus.vinyl + mus.master, quiet: k.by_region?.quiet?.vinyl || 0 }))}
      ${stat(T("master.stat_treble"), f.hf_loss_end_db != null ? `${sgn(f.hf_loss_end_db, 1)} dB` : "–", T("master.stat_treble_sub"))}
      ${stat(T("master.stat_bass"), f.bass_side_db != null ? `${sgn(f.bass_side_db, 1)} dB` : "–", T("master.stat_bass_sub"))}
      ${stat(T("master.stat_noise"), g.noise_a_median_dbfs != null ? `${num(g.noise_a_median_dbfs, 1)} dB(A)` : "–", g.snr_median_db != null ? T("master.stat_noise_sub", { db: num(g.snr_median_db, 0) }) : "")}
      ${stat(T("master.stat_pre_echo"), g.pre_echo_max_db != null ? `${num(g.pre_echo_max_db, 0)} dB` : T("master.none_found"), T("master.stat_pre_echo_sub", { n: g.pre_echo_count || 0, total: (g.items || []).filter((x) => x.pre_echo).length })
        + (g.post_echo_count ? T("master.stat_post_echo", { n: g.post_echo_count, db: num(g.post_echo_max_db, 0) }) : ""))}
      ${stat(T("master.stat_polarity"), pol, ch.balance_db != null ? T("master.stat_balance", { db: sgn(ch.balance_db, 1) }) : "")}
    </div>
    ${d.loud?.top?.length ? `<details><summary>${T("master.loud_summary", { n: d.loud.audible, threshold: num(d.loud.threshold, 0) })}</summary><div class="scroll"><table><tr><th class="num">${T("master.col_time")}</th><th>${T("master.col_kind")}</th><th>${T("master.col_channel")}</th><th class="num">${T("master.col_strength")}</th><th></th></tr>
      ${d.loud.top.map((x) => `<tr><td class="num">${fmtT(x.t)}</td><td>${EV_KIND[x.kind] || esc(x.kind)}</td><td>${["", "L", "R", "L+R"][x.ch] || ""}</td><td class="num">${num(x.strength, 0)}</td><td>${abPlay(x.t)}</td></tr>`).join("")}</table></div>
      <p class="hint">${T("master.loud_hint")}</p></details>` : ""}
    <h4>${T("master.freq_title")}</h4>
    <div class="chart-wrap"><canvas id="mc-freq" class="chart"></canvas></div>
    <p class="hint">${T("master.freq_hint")}</p>
    ${lv.length ? `<h4>${T("master.levels_title")}</h4><div class="scroll"><table><tr><th>${T("master.col_no")}</th><th>${T("master.col_track")}</th><th class="num">Master</th><th class="num">Vinyl</th><th class="num" title="${T("master.col_relative_title")}">${T("master.col_relative")}</th><th></th></tr>
      ${lv.map((x) => `<tr><td>${esc(x.pos)}</td><td>${esc(x.title)}</td><td class="num">${num(x.master_dbfs, 1)} dBFS</td><td class="num">${num(x.vinyl_dbfs, 1)} dBFS</td><td class="num">${sgn(x.rel_db, 1)} dB</td><td>${abPlay(x.t_v[0] + 3, 0, 8)}</td></tr>`).join("")}</table></div>
      <p class="hint">${T("master.levels_hint", { gain: sgn(R.levels.gain_db, 1), spread: num(R.levels.spread_db, 1) })}</p>` : ""}
    ${segW.length ? `<h4>${T("master.wow_title")}</h4><table><tr><th>${T("master.col_section")}</th><th class="num">${T("master.col_time")}</th><th class="num">${T("master.col_radius")}</th><th class="num">${T("master.col_variation")}</th><th class="num">${T("master.col_hole")}</th></tr>
      ${segW.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${fmtTime(x.t_v[0])}–${fmtTime(x.t_v[1])}</td><td class="num">${num(x.radius_mm, 0)} mm</td><td class="num">±${num(x.wow_pct, 3)} %</td><td class="num">${num(x.ecc_mm, 2)} mm</td></tr>`).join("")}</table>
      <p class="hint">${T("master.wow_hint", { rev: num(w.rev_s, 3), verdict: w.verdict ? T("master.wow_verdict", { verdict: esc(w.verdict) }) : "" })}</p>` : ""}
    ${(g.items || []).length ? `<h4>${T("master.gaps_title")}</h4><div class="scroll"><table><tr><th>${T("master.col_kind")}</th><th class="num">${T("master.col_time")}</th><th class="num">${T("master.col_length")}</th><th class="num">${T("master.col_noise")}</th><th class="num">${T("master.col_a_weighted")}</th><th class="num">${T("master.col_below_music")}</th><th class="num" title="${T("master.col_pre_echo_title")}">${T("master.col_pre_echo")}</th><th class="num" title="${T("master.col_post_echo_title")}">${T("master.col_post_echo")}</th><th></th></tr>
      ${g.items.map((x) => `<tr><td>${esc(GAP_KIND[x.kind] || x.kind)}</td><td class="num">${fmtTime(x.t_v[0])}</td><td class="num">${num(x.len_s, 1)} s</td><td class="num">${num(x.noise_dbfs, 1)} dBFS</td><td class="num">${num(x.noise_a_dbfs, 1)} dB(A)</td><td class="num">${num(x.snr_db, 0)} dB</td><td class="num">${echoCell(x.pre_echo, true)}</td><td class="num">${echoCell(x.post_echo, false)}</td><td>${abPlay(x.t_v[0], 0, Math.min(6, x.len_s + 1))}</td></tr>`).join("")}</table></div>
      <p class="hint">${T("master.gaps_hint")}</p>` : ""}
    ${(k.top || []).length ? `<details><summary>${T("master.clicks_summary", { n: k.top.length })}</summary><div class="scroll"><table><tr><th class="num">${T("master.col_time")}</th><th>${T("master.col_kind")}</th><th>${T("master.col_region")}</th><th class="num">${T("master.col_strength")}</th><th class="num" title="${T("master.col_residual_title")}">${T("master.col_residual")}</th><th></th></tr>
      ${k.top.map((x) => `<tr><td class="num">${fmtT(x.t)}</td><td>${EV_KIND[x.kind] || esc(x.kind)}</td><td>${EV_REGION[x.region] || esc(x.region)}</td><td class="num">${num(x.strength, 1)}</td><td class="num">${x.residual == null ? "–" : pct1(x.residual)}</td><td>${abPlay(x.t)}</td></tr>`).join("")}</table></div>
      <p class="hint">${T("master.clicks_hint")}</p></details>` : ""}
    ${rerun}`;
}
function drawMasterCharts(R) {
  const f = R.freq, c = $("#mc-freq");
  if (!c || !f?.segments?.length) return;
  const cols = [css("--left"), css("--wave"), css("--right")];
  const series = f.segments.map((sg, i) => ({ points: f.bands.map((b, j) => [b, sg.mid_db[j]]), color: cols[i % 3], label: sg.label }));
  const mid = f.segments[Math.floor(f.segments.length / 2)];
  series.push({ points: f.bands.map((b, j) => [b, mid.side_db[j]]), color: css("--muted"), dash: [4, 3], label: T("master.stereo_component") });
  const ys = series.flatMap((x) => x.points.map((p) => p[1])).filter((v) => v != null && isFinite(v));
  chart(c, series, { xlog: true, xmin: 25, xmax: 16000, yunit: " dB", xunit: " Hz", ymin: Math.min(-6, ...ys), ymax: Math.max(3, ...ys),
    hlines: [{ y: 0, color: css("--muted") }] });
}

const pct1 = (v) => (v == null ? "–" : Math.round(v * 100) + " %");
const closeEvPop = () => $("#ev-pop")?.remove();
document.addEventListener("click", (e) => { if (!e.target.closest("#ev-pop") && !e.target.closest("#ov-clicks")) closeEvPop(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeEvPop(); });
// Raw value first, then in small print the value above the turntable floor (with a high floor, the net value
// alone would read "0" almost everywhere)
const rawNet = (sm, key) => {
  const raw = sm?.[key], net = sm?.[key.replace("_per_min", "_net_per_min")];
  if (raw == null) return "–";
  return `${num(raw)}${net != null ? ` <small class="hint" title="${T("skips.above_floor")}">(${num(net)})</small>` : ""}`;
};

// Several copies of the same record: the latest measured grade per copy (else "" → a single grade as before)
function copyGrades(r) {
  let g = [];
  try { g = JSON.parse(r.grades_by_copy || "[]"); } catch (_) { return ""; }
  const copies = g.filter((x) => x.grade);
  if (copies.length < 2) return "";
  return `<span class="copies">${copies.sort((a, b) => (a.copy || "").localeCompare(b.copy || "")).map((x) => `<span class="copy">${gradeChip(x.grade, T("compare.copy_grade_title", { copy: x.copy || T("compare.copy_unnamed"), sides: (x.sides || []).map((s) => `${s.side} ${s.grade}`).join(", ") }))}<small>${esc((x.copy || T("compare.copy_none")).replace(/^(?:Exemplar|Copy)\s*/i, T("compare.copy_short")))}</small></span>`).join("")}</span>`;
}

// Track table: how good each track sounds (boundaries from the Discogs durations, snapped to the gaps)
function trackTable(m) {
  const tr = m.tracks;
  if (!tr?.length) return "";
  const worst = Math.max(...tr.map((x) => x.clicks_per_min ?? 0));
  return `<div class="panel-head"><h3>${T("skips.track_title")}</h3><span class="hint">${T("skips.track_count", { n: tr.length })}${tr.some((x) => !x.snapped) ? T("skips.track_estimated") : ""}</span></div>
    <div class="scroll"><table><tr><th>${T("skips.track_col_no")}</th><th>${T("skips.track_col_title")}</th><th class="num">${T("skips.track_col_duration")}</th><th class="num" title="${T("skips.track_col_clicks_title")}">${T("skips.track_col_clicks")}</th><th class="num">${T("skips.track_col_ticks")}</th><th class="num">${T("skips.track_col_pops")}</th><th class="num">${T("skips.track_col_scratches")}</th><th class="num" title="${T("skips.track_col_confirmed_title")}">${T("skips.track_col_confirmed")}</th><th></th></tr>
    ${tr.map((x) => `<tr><td>${esc(x.pos)}</td><td>${esc(x.title)}</td><td class="num">${fmtTime(x.t1 - x.t0)}${x.dur_discogs_s ? ` <small class="hint">${fmtTime(x.dur_discogs_s)}</small>` : ""}</td><td class="num">${x.clicks_per_min === worst && worst > 0 ? `<b class="bad">${num(x.clicks_per_min)}</b>` : num(x.clicks_per_min)}</td><td class="num">${num(x.ticks_per_min)}</td><td class="num">${x.pops}</td><td class="num">${x.scratches || ""}</td><td class="num">${pct1(x.confirmed_share)}</td><td><button class="link" data-play="${x.t0}" data-before="0" data-after="4" title="${T("skips.track_listen_start")}">▶</button></td></tr>`).join("")}
    </table></div>
    <p class="hint">${T("skips.track_hint")}</p>`;
}

// Play from a spot until stopped (not a fixed 30 s), loaded in 60 s chunks; in a zoomed view loop exactly over the
// visible range. The mini player shows the running time.
const CHUNK_S = 60;
const playback = { recId: null, mode: null, t0: 0, t1: 0, from: 0, start: 0, end: 0 };   // mode: "cont" | "loop" | null
function playRange(recId, from, t0, t1, loop) {
  playback.recId = recId; playback.mode = loop ? "loop" : "cont"; playback.t0 = t0; playback.t1 = t1; playback.start = from;
  $("#player").loop = false;
  playChunk(from);
}
function playChunk(from) {
  const p = $("#player"), end = Math.min(playback.t1, from + CHUNK_S);
  if (end - from < 0.2) {
    if (playback.mode === "loop") return playChunk(playback.t0);
    playback.mode = null; miniPlayer.finish(); return;
  }
  playback.from = from; playback.end = end;
  p.src = `/api/recordings/${playback.recId}/snippet?t=${from}&before=0&after=${end - from}`;
  const range = playback.mode === "loop" ? T("live.player_loop", { from: fmtTime(playback.t0), to: fmtTime(playback.t1) }) : T("live.player_from", { from: fmtTime(playback.start) });
  miniPlayer.show(`${ico("play", 14)} ${T("live.player_recording", { id: playback.recId, range })}`);
  p.play().catch((e) => { playback.mode = null; miniPlayer.hide(); toast(T("live.play_failed", { error: e?.message || "" }), 6000); });
}
function play(recId, t, before = 0.6, after = 1.2, quiet = false) {
  const p = $("#player");
  playback.mode = null; p.loop = false; playback.from = Math.max(0, t - before);
  p.src = `/api/recordings/${recId}/snippet?t=${t}&before=${before}&after=${after}`;
  if (quiet) miniPlayer.hide();                                  // hover snippet: no player (it popped up under the pointer and hid the time cursor)
  else miniPlayer.show(`${ico("play", 14)} ${T("live.player_snippet", { id: recId, from: fmtTime(Math.max(0, t - before)), to: fmtTime(t + after) })}`);
  p.play().catch((e) => { miniPlayer.hide(); if (!quiet) toast(T("live.play_failed", { error: e?.message || "" }), 6000); });
}
// Small player at the bottom: what is playing, progress, replay, stop
const miniPlayer = {
  timer: null,
  init() {
    const p = $("#player"), fill = $("#mini-player .mp-fill"), time = $("#mini-player .mp-time");
    if (!p || !fill) return;                                     // index.html from before an update (cached): no player
    p.ontimeupdate = () => {
      if (playback.mode) { const span = playback.t1 - playback.t0; fill.style.width = `${Math.min(100, ((playback.from + p.currentTime - playback.t0) / (span || 1)) * 100)}%`; }
      else if (p.duration) fill.style.width = `${Math.min(100, (p.currentTime / p.duration) * 100)}%`;
      if (time) time.textContent = fmtTime(playback.from + p.currentTime);
    };
    p.onended = () => { if (playback.mode) return playChunk(playback.end); this.finish(); };
    $("#mini-player .mp-stop").onclick = () => { playback.mode = null; p.pause(); this.hide(); };
    $("#mini-player .mp-again").onclick = () => { if (playback.mode) return playChunk(playback.mode === "loop" ? playback.t0 : playback.start); p.currentTime = 0; this.show(); p.play().catch(() => {}); };
  },
  finish() { $("#mini-player .mp-fill").style.width = "100%"; this.timer = setTimeout(() => this.hide(), 1500); },
  show(label) { clearTimeout(this.timer); if (label) $("#mini-player .mp-label").innerHTML = label; $("#mini-player .mp-fill").style.width = "0"; $("#mini-player").classList.remove("hidden"); },
  hide() { $("#mini-player").classList.add("hidden"); },
};

function setupCanvas(c) {
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext("2d"); g.scale(dpr, dpr);
  return { g, w, h };
}

function drawOverview(c, ov, onPick) {
  const { g, w, h } = setupCanvas(c);
  const n = ov.min.length, dur = n * ov.bucket_s, x = (t) => (t / dur) * w, mid = h * 0.45, amp = h * 0.4;
  g.fillStyle = css("--quiet");
  ov.quiet.forEach(([a, b]) => g.fillRect(x(a), 0, Math.max(1, x(b) - x(a)), h));
  g.fillStyle = css("--line");
  g.fillRect(0, 0, x(ov.groove[0]), h); g.fillRect(x(ov.groove[1]), 0, w - x(ov.groove[1]), h);
  g.strokeStyle = css("--wave"); g.beginPath();
  for (let i = 0; i < n; i++) { const px = (i / n) * w; g.moveTo(px, mid - ov.max[i] * amp); g.lineTo(px, mid - ov.min[i] * amp); }
  g.stroke();
  const col = { pop: css("--pop"), click: css("--click"), tick: css("--tick") };
  for (const [t, k, r] of ov.events) {
    if (r === "excluded") continue;
    g.fillStyle = col[k]; const y = k === "pop" ? h * 0.9 : k === "click" ? h * 0.93 : h * 0.96;
    g.fillRect(x(t), y - (k === "tick" ? 3 : 7), k === "tick" ? 1 : 2, k === "tick" ? 3 : 7);
  }
  g.fillStyle = css("--muted"); g.font = "11px system-ui";
  for (let s = 0; s < dur; s += 60) g.fillText(fmtTime(s), x(s) + 2, 12);
  c.onclick = (e) => {
    const t = ((e.offsetX) / c.clientWidth) * dur;
    let best = null, bd = 0.25;   // nearest event within ±0.25 s, else exactly the clicked spot
    for (const ev of ov.events) { const d = Math.abs(ev[0] - t); if (d < bd) { bd = d; best = ev[0]; } }
    onPick(best ?? t);
  };
}

// Clicks per time bucket as stacked bars (pops red, clicks yellow, crackle grey); lead-in/lead-out below the axis
const hoverPlay = { on: (() => { try { return localStorage.getItem("vc-hover-play") !== "0"; } catch (_) { return true; } })(), last: null, at: 0 };
const CH_PAD = { l: 36, r: 8 };
// Zoom for the click and level charts: drag = select a range, double-click = whole side; both charts share `view`
function setupZoom(c, view, dur, redraw) {
  const wrap = c.parentElement, sel = wrap.querySelector(".sel");
  const tAt = (px) => view.t0 + ((px - CH_PAD.l) / (c.clientWidth - CH_PAD.l - CH_PAD.r)) * (view.t1 - view.t0);
  let drag = null;
  c.onmousedown = (e) => { drag = { x0: e.offsetX, moved: false }; };
  c.addEventListener("mousemove", (e) => {
    if (!drag) return;
    if (Math.abs(e.offsetX - drag.x0) > 3) drag.moved = true;
    if (drag.moved && sel) { sel.style.display = "block"; sel.style.left = Math.min(drag.x0, e.offsetX) + "px"; sel.style.width = Math.abs(e.offsetX - drag.x0) + "px"; }
  });
  const finish = (e) => {
    if (!drag) return;
    const d = drag; drag = null; if (sel) sel.style.display = "none";
    if (!d.moved) return;
    const a = Math.max(0, tAt(Math.min(d.x0, e.offsetX))), b = Math.min(dur, tAt(Math.max(d.x0, e.offsetX)));
    if (b - a < 0.5) return;
    view.t0 = a; view.t1 = b; view.zoomed = true; redraw();
  };
  c.onmouseup = finish;
  c.addEventListener("mouseleave", (e) => { if (drag?.moved) finish(e); else drag = null; });
  c.ondblclick = () => { view.t0 = 0; view.t1 = dur; view.zoomed = false; redraw(); };
}
const niceStep = (span) => [1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((st) => span / st <= 12) || 600;
function nearestEvent(ov, t, tol) { let best = null, bd = tol; for (const ev of ov.events) { const d = Math.abs(ev[0] - t); if (d < bd) { bd = d; best = ev[0]; } } return best; }
function hoverHandler(c, ov, view, onHover) {
  // Hover: briefly play the nearest detected click under the pointer (at most every 450 ms, only when it changes)
  return (e) => {
    if (!hoverPlay.on || !onHover || e.buttons) return;
    const span = view.t1 - view.t0, t = view.t0 + ((e.offsetX - CH_PAD.l) / (c.clientWidth - CH_PAD.l - CH_PAD.r)) * span;
    const best = nearestEvent(ov, t, Math.min(0.6, Math.max(0.03, span / 60)));
    const now = Date.now();
    if (best == null || best === hoverPlay.last || now - hoverPlay.at < 450) return;
    hoverPlay.last = best; hoverPlay.at = now; onHover(best);
  };
}
function drawClicks(c, ov, m, onPick, onHover, view, onEvent) {
  const { g, w, h } = setupCanvas(c);
  const dur = ov.min.length * ov.bucket_s, v = view || { t0: 0, t1: dur }, span = Math.max(0.5, v.t1 - v.t0);
  const pad = { l: CH_PAD.l, r: CH_PAD.r, t: 14, b: 16 }, B = span <= 30 ? 0.1 : span <= 120 ? 0.25 : span <= 600 ? 1.0 : 2.0, nb = Math.ceil(span / B);
  const up = [[], [], [], []], down = new Array(nb).fill(0);                  // tick, click, pop, judged a false alarm
  const order = { tick: 0, click: 1, pop: 2 };
  for (let i = 0; i < 4; i++) up[i] = new Array(nb).fill(0);
  for (const [t, k, r, , lb] of ov.events) { if (t < v.t0 || t >= v.t1) continue; const bi = Math.min(nb - 1, Math.floor((t - v.t0) / B)); if (r === "excluded") down[bi]++; else up[lb === "music" ? 3 : order[k]][bi]++; }
  const tot = up[0].map((_, i) => up[0][i] + up[1][i] + up[2][i] + up[3][i]);
  const maxUp = Math.max(1, ...tot), maxDown = Math.max(1, ...down);
  const axisY = pad.t + (h - pad.t - pad.b) * 0.72, upH = axisY - pad.t, dnH = h - pad.b - axisY;
  const x = (t) => pad.l + ((t - v.t0) / span) * (w - pad.l - pad.r), bw = Math.max(1, (w - pad.l - pad.r) / nb);
  g.fillStyle = css("--quiet"); ov.quiet.forEach(([a, b]) => { const xa = Math.max(pad.l, x(a)), xb = Math.min(w - pad.r, x(b)); if (xb > xa) g.fillRect(xa, pad.t, Math.max(1, xb - xa), h - pad.t - pad.b); });
  const cols = [css("--tick"), css("--click"), css("--pop"), css("--line")];
  for (let i = 0; i < nb; i++) {
    let y = axisY;
    for (let k = 0; k < 4; k++) { const hh = (up[k][i] / maxUp) * upH; if (hh > 0) { g.fillStyle = cols[k]; g.fillRect(x(v.t0 + i * B), y - hh, bw - 0.5, hh); y -= hh; } }
    if (down[i]) { g.fillStyle = css("--muted"); g.globalAlpha = 0.6; g.fillRect(x(v.t0 + i * B), axisY + 1, bw - 0.5, (down[i] / maxDown) * dnH); g.globalAlpha = 1; }
  }
  g.strokeStyle = css("--line"); g.beginPath(); g.moveTo(pad.l, axisY + 0.5); g.lineTo(w - pad.r, axisY + 0.5); g.stroke();
  g.fillStyle = css("--muted"); g.font = "11px system-ui";
  g.fillText(T("charts.per_bucket", { n: maxUp, s: B < 1 ? I18N.num(B, 2) : B }), 2, pad.t + 10);
  const st = niceStep(span);
  for (let s = Math.ceil(v.t0 / st) * st; s < v.t1; s += st) g.fillText(span < 60 ? fmtTime(s) + (s % 1 ? "" : "") : fmtTime(s), x(s) + 2, h - 4);
  c.onclick = (e) => {
    const t = v.t0 + ((e.offsetX - pad.l) / (c.clientWidth - pad.l - pad.r)) * span;
    // Zoomed in (up to 60 s), a click on an event opens its verdict popup; otherwise play from here
    if (onEvent && span <= 60) {
      const tol = Math.max(B, (8 / (c.clientWidth - pad.l - pad.r)) * span);
      let best = null, bd = tol;
      for (const ev of ov.events) { const d = Math.abs(ev[0] - t); if (ev[2] !== "excluded" && d < bd) { bd = d; best = ev; } }
      if (best) return onEvent(best, e);
    }
    const best = nearestEvent(ov, t, Math.min(1.0, Math.max(0.05, span / 40))); onPick(best ?? Math.max(0, t));
  };
  c.onmousemove = hoverHandler(c, ov, v, onHover);
  c.onmouseout = () => { hoverPlay.last = null; };
}

// Level over time (dBFS per bucket) with lines for chain noise, groove silence and music reference, stylus-up
// zones grey; same zoom as the click chart
function drawLevel(c, ov, m, onPick, onHover, view) {
  const { g, w, h } = setupCanvas(c);
  const lv = ov.level_db || [], n = lv.length, dur = n * ov.bucket_s, v = view || { t0: 0, t1: dur }, span = Math.max(0.5, v.t1 - v.t0);
  const pad = { l: CH_PAD.l, r: CH_PAD.r, t: 8, b: 16 }, y0 = -90, y1 = 0;
  const x = (t) => pad.l + ((t - v.t0) / span) * (w - pad.l - pad.r), Y = (val) => pad.t + (1 - (Math.min(Math.max(val, y0), y1) - y0) / (y1 - y0)) * (h - pad.t - pad.b);
  const clipRect = (a, b, col, alpha = 1) => { const xa = Math.max(pad.l, x(a)), xb = Math.min(w - pad.r, x(b)); if (xb > xa) { g.globalAlpha = alpha; g.fillStyle = col; g.fillRect(xa, pad.t, Math.max(1, xb - xa), h - pad.t - pad.b); g.globalAlpha = 1; } };
  clipRect(0, ov.groove[0], css("--line")); clipRect(ov.groove[1], dur, css("--line"));
  ov.quiet.forEach(([a, b]) => clipRect(a, b, css("--quiet")));
  g.strokeStyle = css("--line"); g.fillStyle = css("--muted"); g.font = "11px system-ui"; g.lineWidth = 1;
  for (let val = 0; val >= -80; val -= 20) { g.beginPath(); g.moveTo(pad.l, Y(val)); g.lineTo(w - pad.r, Y(val)); g.stroke(); g.fillText(`${val}`, 4, Y(val) + 4); }
  const line = (val, col, dash) => { if (val == null) return; g.save(); g.setLineDash(dash); g.strokeStyle = col; g.lineWidth = 1.2; g.beginPath(); g.moveTo(pad.l, Y(val)); g.lineTo(w - pad.r, Y(val)); g.stroke(); g.restore(); };
  line(m.chain_db, css("--muted"), [2, 4]);
  line(m.noise_dbfs, css("--ok"), [6, 4]);
  line(m.music_ref_dbfs, css("--muted"), [1, 3]);
  if (n) {
    const i0 = Math.max(0, Math.floor(v.t0 / ov.bucket_s) - 1), i1 = Math.min(n, Math.ceil(v.t1 / ov.bucket_s) + 1);
    g.save(); g.beginPath(); g.rect(pad.l, 0, w - pad.l - pad.r, h); g.clip();
    g.strokeStyle = css("--wave"); g.lineWidth = 1; g.beginPath();
    for (let i = i0; i < i1; i++) { const px = x(i * ov.bucket_s), py = Y(lv[i]); i === i0 ? g.moveTo(px, py) : g.lineTo(px, py); }
    g.stroke(); g.restore();
  }
  const st = niceStep(span);
  for (let s = Math.ceil(v.t0 / st) * st; s < v.t1; s += st) g.fillText(fmtTime(s), x(s) + 2, h - 4);
  c.onclick = (e) => onPick(Math.max(0, v.t0 + ((e.offsetX - pad.l) / (c.clientWidth - pad.l - pad.r)) * span));
  c.onmousemove = hoverHandler(c, ov, v, onHover);
  c.onmouseout = () => { hoverPlay.last = null; };
}

// ─── Comparison ──────────────────────────────────────────────────────────
async function pollComparison(id) {
  for (let i = 0; i < 300; i++) {
    const c = await api(`/api/comparisons/${id}`);
    if (c.status === "done") { refreshRecord(); return showComparison(id); }
    if (c.status === "error") { refreshRecord(); return toast(T("compare.failed", { error: c.error }), 8000); }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function showComparison(id) {
  const c = await api(`/api/comparisons/${id}`);
  if (c.status !== "done") { toast(T("compare.status", { status: c.status })); return pollComparison(id); }
  const r = c.result, al = r.alignment, mt = r.match, cnt = mt?.counts;
  const rep = r.repeatability;
  const LANE = { removed: T("compare.removed"), persistent: T("compare.persistent"), new: T("compare.new") };
  const pct = (k) => { const a = r.metrics_a[k], b = r.metrics_b[k]; return a == null || b == null || !a || k.includes("db") ? "–" : `<b class="${b < a ? "ok" : b > a ? "bad" : ""}">${(b - a) / a > 0 ? "+" : ""}${Math.round((b - a) / a * 100)} %</b>`; };
  const row = (label, k, d = 1) => `<tr><td>${label}</td><td class="num">${num(r.metrics_a[k], d)}</td><td class="num">${num(r.metrics_b[k], d)}</td><td class="num">${r.delta[k] == null ? "–" : (r.delta[k] > 0 ? "+" : "") + num(r.delta[k], d)}</td><td class="num">${pct(k)}</td></tr>`;
  const metricsTable = `<table><tr><th></th><th class="num">${T("compare.before")}</th><th class="num">${T("compare.after")}</th><th class="num">Δ</th><th class="num">%</th></tr>
      ${row(T("compare.m_clicks_quiet"), "clicks_quiet_per_min")}${row(T("compare.m_ticks_quiet"), "ticks_quiet_per_min")}${row(T("compare.m_clicks_music"), "clicks_music_per_min")}
      ${row(T("compare.m_pops"), "pops_per_min", 2)}${row(T("compare.m_noise"), "noise_dbfs")}${row(T("compare.m_noise_a"), "noise_a_dbfs")}${row(T("compare.m_noise_rel"), "noise_rel_db")}
    </table>`;
  const back = `<button class="secondary" id="close-view">${ico("back", 16)} ${T("compare.back")}</button>`;
  const v = openView();
  // Older results have no `comparable`; there the alignment result decides
  if (r.comparable === false || (r.comparable == null && !al.ok)) {
    v.innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">${T("compare.title", { id: c.id, a: c.a_id, b: c.b_id })}</h2>${back}</div>
    <div class="warnbox"><b>${T("compare.not_comparable")}</b> ${esc(r.reason || T("compare.no_common_music"))}<br>
    <span class="hint">${T("compare.alignment_info", { anchors: al.anchors ?? 0, quality: al.quality != null ? T("compare.quality", { q: num(al.quality, 2) }) : "" })}</span></div>
    <h3>${T("compare.metrics_unmatched")}</h3>${metricsTable}`;
    $("#close-view").onclick = closeView;
    if (!state.viewKeepScroll) window.scrollTo({ top: 0 });
    return;
  }
  v.innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">${T("compare.title", { id: c.id, a: c.a_id, b: c.b_id })} ${rep ? T("compare.repeatability") : ""}</h2>${back}</div>
    ${rep ? `<div class="warnbox">${T("compare.repeatability_hint")}</div>` : ""}
    ${r.copy_note ? `<div class="warnbox">${esc(r.copy_note)}</div>` : ""}
    ${!al.ok ? `<div class="warnbox">${T("compare.alignment_uncertain")}</div>` : ""}
    ${(r.skips?.relative || []).map((j) => `<div class="warnbox">${T("compare.skip_relative", { a: fmtTime(j.t_a), b: fmtTime(j.t_b), meaning: esc(j.interpretation), grooves: T("compare.grooves", { n: j.grooves }) })}</div>`).join("")}
    ${(r.skips?.same_place || []).length ? `<div class="warnbox">${T("compare.skip_same_place", { times: r.skips.same_place.map((x) => fmtTime(x.t_a)).join(", ") })}</div>` : ""}
    <div class="cards">
      <div class="stat"><span>${T("compare.grade")}</span><b><span class="grade ${gradeCls(r.grade_a)}">${r.grade_a ?? "–"}</span> → <span class="grade ${gradeCls(r.grade_b)}">${r.grade_b ?? "–"}</span></b></div>
      <div class="stat"><span>${T("compare.removed_dirt")}</span><b style="color:var(--removed)">${cnt.removed}</b><span>${mt.removed_share != null ? T("compare.removed_share", { pct: Math.round(mt.removed_share * 100) }) : ""}</span></div>
      <div class="stat"><span>${T("compare.persistent_scratches")}</span><b style="color:var(--persistent)">${cnt.persistent}</b></div>
      <div class="stat"><span>${LANE.new}</span><b style="color:var(--new)">${cnt.new}</b></div>
      <div class="stat"><span>${T("compare.speed_after")}</span><b>${al.speed_ratio ? num((al.speed_ratio - 1) * 100, 2) + " %" : "–"}</b><span>${T("compare.anchors_quality", { anchors: al.anchors_used ?? 0, quality: num(al.quality, 2) })}</span></div>
    </div>
    <div class="legend"><span><i style="background:var(--removed)"></i>${LANE.removed}</span><span><i style="background:var(--persistent)"></i>${LANE.persistent}</span><span><i style="background:var(--new)"></i>${LANE.new}</span><span>${T("compare.legend_click")}</span></div>
    <canvas id="cmp"></canvas>
    <h3>${T("compare.spectrograms")}</h3>
    <div id="sg"></div>
    <h3>${T("compare.metrics")}</h3>
    ${metricsTable}
    <h3>${T("compare.by_kind")}</h3>
    <table><tr><th></th><th class="num">${T("compare.pops")}</th><th class="num">${T("compare.clicks")}</th><th class="num">${T("compare.ticks")}</th></tr>
      ${["removed", "persistent", "new"].map((k) => `<tr><td>${LANE[k]}</td>${["pop", "click", "tick"].map((x) => `<td class="num">${mt.by_kind[k][x]}</td>`).join("")}</tr>`).join("")}
    </table>`;
  $("#close-view").onclick = closeView;
  drawComparison($("#cmp"), mt.events, c.a_id, c.b_id);
  const sl = al.slope ?? 1, ic = al.intercept ?? al.offset_s ?? 0, durA = Math.max(60, ...[...mt.events.removed, ...mt.events.persistent, ...mt.events.new].map((e) => e.t_a)) + 5;
  api(`/api/recordings/${c.a_id}`).then((ra) => spectroView($("#sg"), [
    { recId: c.a_id, label: `#${c.a_id} ${KIND[r.kinds[0]]}${r.copies?.[0] ? " " + esc(r.copies[0]) : ""}`, map: (t) => t },
    { recId: c.b_id, label: `#${c.b_id} ${KIND[r.kinds[1]]}${r.copies?.[1] ? " " + esc(r.copies[1]) : ""}`, map: (t) => sl * t + ic, mapped: Math.abs(sl - 1) > 1e-6 || Math.abs(ic) > 0.05 },
  ], ra.duration_s || durA, (it, t) => play(it.recId, t))).catch((e) => toast(e.message));
  if (!state.viewKeepScroll) window.scrollTo({ top: 0 });
}

function drawComparison(cv, ev, aId, bId) {
  const { g, w, h } = setupCanvas(cv);
  const all = [...ev.removed, ...ev.persistent, ...ev.new];
  const dur = Math.max(60, ...all.map((e) => e.t_a)) * 1.02, x = (t) => (t / dur) * w;
  const lanes = [["removed", css("--removed"), h * 0.2], ["persistent", css("--persistent"), h * 0.5], ["new", css("--new"), h * 0.8]];
  g.font = "11px system-ui";
  for (const [k, col, y] of lanes) {
    g.fillStyle = css("--muted"); g.fillText(T("compare." + k), 4, y - 14);
    g.fillStyle = col;
    for (const e of ev[k]) { const s = e.kind === "tick" ? 4 : e.kind === "click" ? 8 : 12; g.fillRect(x(e.t_a), y - s / 2, 2, s); }
  }
  g.fillStyle = css("--muted");
  for (let s = 0; s < dur; s += 60) g.fillText(fmtTime(s), x(s) + 2, h - 4);
  cv.onclick = (e) => {
    const t = (e.offsetX / cv.clientWidth) * dur;
    let best = null, bd = 0.3;
    for (const it of all) { const d = Math.abs(it.t_a - t); if (d < bd) { bd = d; best = it; } }
    if (!best) return;
    if (e.shiftKey) { if (best.t_b != null) play(bId, best.t_b); else toast(T("compare.only_before")); }
    else if (ev.new.includes(best)) play(bId, best.t_b); else play(aId, best.t_a);
  };
}

// ─── Groove skips (record check) ─────────────────────────────────────────
// Technical data of the recording (as mastering/restoration tools show it): values with an explanation on hover
function techTable(m, rec) {
  const t = m.technical;
  if (!t) return "";
  if (!t.ok) return `<h3>${T("skips.tech_title")}</h3><p class="hint">${T("skips.tech_missing", { reason: esc(t.reason || "") })}</p>`;
  const lr = (a, u = "", d = 1) => (a ? `L ${num(a[0], d)}${u} · R ${num(a[1], d)}${u}` : "–");
  const cl = (v, warnAt, badAt, higherIsWorse = true) => v == null ? "" : (higherIsWorse ? (v >= badAt ? "bad" : v >= warnAt ? "warn" : "ok") : (v <= badAt ? "bad" : v <= warnAt ? "warn" : "ok"));
  const bal = t.balance_db || {};
  const balTxt = Object.keys(bal).length ? T("skips.tech_balance_value", { bass: num(bal.bass), low_mid: num(bal.low_mid), mid: num(bal.mid), presence: num(bal.presence), treble: num(bal.treble) }) : "–";
  const prof = m.profile || {};
  const rows = [
    [T("skips.tech_peak"), lr(t.peak_dbfs, " dBFS", 1), T("skips.tech_peak_help"), cl(Math.max(...(t.peak_dbfs || [-99])), -1, -0.1)],
    ["True Peak", lr(t.true_peak_dbtp, " dBTP", 1), T("skips.tech_true_peak_help"), cl(Math.max(...(t.true_peak_dbtp || [-99])), -1, 0)],
    [T("skips.tech_rms"), lr(t.rms_dbfs, " dBFS", 1), T("skips.tech_rms_help"), ""],
    [T("skips.tech_crest"), lr(t.crest_db, " dB", 1), T("skips.tech_crest_help"), ""],
    [T("skips.tech_loudness"), t.loudness_lufs == null ? "–" : `${num(t.loudness_lufs, 1)} LUFS`, T("skips.tech_loudness_help"), ""],
    [T("skips.tech_dc"), lr(t.dc_pct, " %", 3), T("skips.tech_dc_help"), cl(Math.max(...(t.dc_pct || [0]).map(Math.abs)), 0.5, 2)],
    [T("skips.tech_correlation"), t.correlation == null ? "–" : num(t.correlation, 2), T("skips.tech_correlation_help"), cl(t.correlation, 0.2, 0, false)],
    [T("skips.tech_width"), t.stereo_width_db == null ? "–" : `${sgn(t.stereo_width_db, 1)} dB`, T("skips.tech_width_help"), ""],
    [T("skips.tech_balance"), balTxt, T("skips.tech_balance_help"), ""],
    [T("skips.tech_centroid"), t.centroid_hz == null ? "–" : `${num(t.centroid_hz, 0)} Hz`, T("skips.tech_centroid_help"), ""],
    [T("skips.tech_rumble"), t.rumble_dbfs == null ? "–" : `${num(t.rumble_dbfs, 1)} dBFS${t.rumble_rel_db != null ? T("skips.tech_rel_music", { db: num(t.rumble_rel_db, 1) }) : ""}`, T("skips.tech_rumble_help"), ""],
    [T("skips.tech_hum"), t.hum_dbfs == null ? "–" : T("skips.tech_hum_value", { db: num(t.hum_dbfs, 1), a: num(t.hum_prominence_db?.["50"], 0), b: num(t.hum_prominence_db?.["100"], 0) }), T("skips.tech_hum_help"), cl(Math.max(t.hum_prominence_db?.["50"] ?? 0, t.hum_prominence_db?.["100"] ?? 0), 10, 20)],
    [T("skips.tech_noise"), T("skips.tech_noise_value", { db: num(m.noise_dbfs), a: num(m.noise_a_dbfs), rel: num(m.noise_rel_db) }), T("skips.tech_noise_help"), ""],
    [T("skips.tech_chain"), m.chain_db == null ? T("skips.tech_not_calibrated") : T("skips.tech_chain_value", { db: num(m.chain_db), above: num(m.noise_above_chain_db) }), T("skips.tech_chain_help"), ""],
    [T("skips.tech_clip"), m.clip_in_groove ? T("skips.tech_clip_groove", { n: m.clip_in_groove, t: fmtTime(m.clip_first_s) }) : m.clip_samples ? T("skips.tech_clip_drop", { n: m.clip_samples }) : T("skips.tech_clip_none"), T("skips.tech_clip_help"), m.clip_in_groove ? "bad" : ""],
    [T("skips.tech_level"), m.level_vs_profile_db == null ? "–" : T("skips.tech_level_value", { db: sgn(m.level_vs_profile_db, 1), ref: num(m.music_ref_dbfs, 1) }), T("skips.tech_level_help"), cl(m.level_vs_profile_db == null ? null : -m.level_vs_profile_db, 14, 20)],
    [T("skips.tech_speed"), prof.speed_pct == null ? "–" : T("skips.tech_speed_value", { pct: sgn(prof.speed_pct, 2), rpm: num(prof.rpm, 3) }), T("skips.tech_speed_help"), ""],
    [T("skips.tech_events"), T("skips.tech_events_value", { pop: m.counts.pop, click: m.counts.click, tick: m.counts.tick, quiet: m.counts.quiet, music: m.counts.music, excluded: m.counts.excluded }), T("skips.tech_events_help"), ""],
    [T("skips.tech_recording"), T("skips.tech_recording_value", { dur: fmtTime(rec.duration_s), groove: fmtTime(m.groove_s), quiet: num(m.quiet_s, 0), rate: rec.samplerate, ch: rec.channels, format: esc(rec.format || ""), device: esc(rec.device || "") }), T("skips.tech_recording_help"), ""],
  ];
  return `<h3>${T("skips.tech_title")}</h3><div class="scroll"><table class="tech">${rows.map(([k, v, h, c]) => `<tr title="${esc(h)}"><th scope="row">${k}</th><td class="${c}">${v}</td><td class="hint">${esc(h)}</td></tr>`).join("")}</table></div>`;
}

function skipNotes(skips, rec) {                                  // notes on backward skips; the skips themselves are in the event list
  const open = (skips || []).filter((e) => e.kind === "backward" && !e.label);
  const end = (skips || []).find((e) => e.kind === "run_out");
  const weak = (skips || []).filter((e) => e.kind === "uncertain");
  const weakNote = weak.length ? `<p class="hint">${T("skips.weak", { n: weak.length })} ${rec.audio_available ? weak.slice(0, 6).map((e) => `<button class="link" data-play="${e.t}" data-before="3" data-after="4">${fmtT(e.t)} ▶</button>`).join(" ") : ""}</p>` : "";
  if (!open.length) return `<p class="hint">${T("skips.none", { end: end ? T("skips.end_groove", { t: fmtTime(end.t) }) : "" })}</p>${weakNote}`;
  return `<div class="warnbox">${T("skips.jumped_back", { n: open.length })}</div>${weakNote}`;
}

// ─── Station (start page) and System (settings) ───────────────────────────
const fmtBytes = (gb) => (gb == null ? "–" : num(gb, 1) + " GB");
async function loadStation() {
  const [st, set, recs, recsAll, setups] = await Promise.all([api("/api/status"), api("/api/settings"), api("/api/recordings"), api("/api/records"), api("/api/setups")]);
  const names = Object.fromEntries(recsAll.map((r) => [r.id, `${r.artist} – ${r.title}`]));
  const sNames = Object.fromEntries(setups.map((s) => [s.id, s.label || T("alignment.run_n", { id: s.id })]));
  const row = (ok, label, value, hint = "") => `<tr><td>${ico(ok === true ? "ok" : ok === false ? "warn" : "info", 16)}</td><th scope="row">${esc(label)}</th><td>${value}</td><td class="hint">${hint}</td></tr>`;
  const cal = st.calibration, rt = set.values.retention_days;
  const last = recs.filter((r) => r.kind !== "calibration").sort((a, b) => b.created_at - a.created_at).slice(0, 6);
  const next = [];
  if (!cal) next.push(`<li>${ico("calibrate", 16)} ${T("station.next_calibrate")} <button class="secondary" id="go-cal">${T("station.start_calibration")}</button></li>`);
  if (!recsAll.length) next.push(`<li>${ico("disc", 16)} ${T("station.next_add_record")} <button class="secondary" data-go="record">${T("station.go_record_check")}</button></li>`);
  const lastRec = last[0];
  const prof = st.profile;
  if (!prof) next.unshift(`<li>${ico("tonearm", 16)} ${T("station.next_measure_turntable")} <button class="secondary" data-go="alignment">${T("station.go_turntable_check")}</button></li>`);
  else if (!prof.accepted) next.unshift(`<li>${ico("tonearm", 16)} ${T("station.next_not_measured", { name: esc(prof.name) })} <button class="secondary" data-go="alignment">${T("station.continue_measuring")}</button></li>`);
  if (lastRec && lastRec.record_id) next.push(`<li>${ico("disc", 16)} ${T("station.next_continue_record", { name: esc(names[lastRec.record_id] || T("station.record")) })} <button class="secondary" data-open-rec="${lastRec.record_id}">${T("station.open_record")}</button></li>`);
  if (lastRec && lastRec.setup_id) next.push(`<li>${ico("sliders", 16)} ${T("station.next_continue_run", { name: esc(sNames[lastRec.setup_id] || T("station.run")) })} <button class="secondary" data-open-setup="${lastRec.setup_id}">${T("station.open_run")}</button></li>`);
  const profHint = st.profile
    ? (st.profile.accepted ? T("station.turntable_measured", { ref: num(st.profile.ref_dbfs), speed: st.profile.speed_pct == null ? "–" : sgn(st.profile.speed_pct, 2) + " %" })
      + (st.profile.floor_clicks_per_min != null ? T("station.turntable_floor", { n: num(st.profile.floor_clicks_per_min, 0) }) : T("station.turntable_floor_missing"))
      : T("station.measurement_pending"))
    : T("station.add_in_turntable_check");
  const dc = st.discogs_collection;
  const discogsHint = st.discogs
    ? (dc.syncing ? T("station.discogs_syncing") : dc.synced_at ? T("station.discogs_as_of", { date: dateStr(dc.synced_at) }) : T("station.discogs_not_synced")) + (dc.error ? " · " + esc(dc.error) : "")
    : T(set.info.desktop ? "station.discogs_setup_hint" : "station.discogs_setup_hint_service");
  $("#station").innerHTML = `
    <h2>${ico("tonearm")} ${T("station.title")}</h2>
    <h3>${T("station.chain")}</h3>
    <table class="kv"><tbody>
      ${row(st.profile ? !!st.profile.accepted : false, T("station.turntable"), st.profile ? esc(st.profile.name) : T("station.none_measured"), profHint)}
      ${row(true, T("station.device"), `<span class="mono">${esc(set.info.device)}</span>`, T("station.device_format", { ch: set.info.channels, format: set.info.format, rate: num(set.info.rate / 1000, 1) }))}
      ${row(!!cal, T("station.calibration"), cal ? `${num(cal.chain_db)} dBFS` : T("station.missing"), cal ? T("station.calibration_hint", { date: dateStr(cal.created_at) }) : T("station.calibrate_once"))}
      ${row(st.storage.ok, T("station.storage"), T("station.free", { size: fmtBytes(st.storage.free_gb) }), st.storage.ok ? T("station.storage_min", { gb: num(set.values.min_free_gb) }) : esc(st.storage.reason))}
      ${row(null, T("station.original_audio"), rt > 0 ? T("station.days", { n: rt }) : T("station.unlimited"), T("station.retention_hint"))}
      ${row(st.discogs, "Discogs", st.discogs ? T("station.discogs_value", { user: esc(dc.username), n: dc.count }) : T("station.not_set_up"), discogsHint)}
    </tbody></table>
    <h3>${T("station.recent")}</h3>
    ${last.length ? `<div class="scroll"><table><tr><th>${T("station.col_recording")}</th><th>${T("station.col_what")}</th><th>${T("station.col_state")}</th><th class="num">${T("station.col_duration")}</th><th>${T("station.col_date")}</th><th>${T("station.col_status")}</th></tr>${last.map((r) => `<tr>
      <td><button class="link" ${r.record_id ? `data-open-rec="${r.record_id}"` : r.setup_id ? `data-open-setup="${r.setup_id}"` : ""}>#${r.id}</button></td>
      <td>${esc(r.record_id ? names[r.record_id] || T("station.record") : r.setup_id ? sNames[r.setup_id] || T("station.run") : "–")} · ${T("station.side", { side: esc(r.side) })}${r.copy_label ? ` · <b>${esc(r.copy_label)}</b>` : ""}</td>
      <td>${KIND[r.kind] || r.kind}${r.cleaning_method ? ` <small class="hint">${esc(r.cleaning_method)}</small>` : ""}</td><td class="num">${r.duration_s ? fmtTime(r.duration_s) : "–"}</td><td>${dateStr(r.created_at)}</td><td class="status-${r.status}">${STATUS[r.status] || r.status}</td></tr>`).join("")}</table></div>` : `<p class="empty">${T("station.no_recordings")}</p>`}
    ${next.length ? `<h3>${T("station.next_steps")}</h3><ul class="next">${next.join("")}</ul>` : ""}`;
  const go = (m) => setMode(m);
  document.querySelectorAll("#station [data-go]").forEach((b) => (b.onclick = () => go(b.dataset.go)));
  document.querySelectorAll("#station [data-open-rec]").forEach((b) => (b.onclick = () => { go("record"); openRecord(+b.dataset.openRec).catch((e) => toast(e.message)); }));
  document.querySelectorAll("#station [data-open-setup]").forEach((b) => (b.onclick = () => { go("alignment"); openSetup(+b.dataset.openSetup).catch((e) => toast(e.message)); }));
  const gc = $("#go-cal"); if (gc) gc.onclick = () => $("#btn-calibrate").click();
}

const THEMES = { system: T("settings.theme_system"), dark: T("settings.theme_dark"), light: T("settings.theme_light") };
function applyTheme(t) {
  const r = document.documentElement;
  if (t === "dark" || t === "light") r.dataset.theme = t; else delete r.dataset.theme;
  try { localStorage.setItem("vc-theme", t); } catch (_) {}
}
function savedTheme() { try { return localStorage.getItem("vc-theme") || "dark"; } catch (_) { return "dark"; } }
applyTheme(savedTheme());

// ─── Target maps (target values for the turntable check) ──────────────────
const KIND_LABEL = { abs: "±", max: "≤", min: "≥", range: T("targets.range") };
function renderTargets(t, mid) {
  const m = t.maps.find((x) => x.id === mid) || t.maps[0];
  const row = ([key, f]) => {
    const r = m.targets[key] || {};
    const n = (v, extra = "") => `<input type="number" step="any" ${extra} value="${v ?? ""}" ${m.builtin ? "disabled" : ""}>`;
    const cells = f.kind === "range"
      ? `<td class="num"><span class="pair">${n(r.ok?.[0], `data-k="${key}" data-f="ok0"`)}${n(r.ok?.[1], `data-k="${key}" data-f="ok1"`)}</span></td>
         <td class="num"><span class="pair">${n(r.warn?.[0], `data-k="${key}" data-f="warn0"`)}${n(r.warn?.[1], `data-k="${key}" data-f="warn1"`)}</span></td>`
      : `<td class="num">${n(r.ok, `data-k="${key}" data-f="ok"`)}</td><td class="num">${n(r.warn, `data-k="${key}" data-f="warn"`)}</td>`;
    return `<tr><td>${esc(f.label)}</td><td class="hint">${KIND_LABEL[f.kind]} ${esc(f.unit)}</td>${cells}<td class="hint">${esc(f.hint)}</td></tr>`;
  };
  $("#targets").innerHTML = `
    <div class="row" style="align-items:end">
      <div><label for="tg-sel">${T("targets.map")}</label><select id="tg-sel">${t.maps.map((x) => `<option value="${esc(x.id)}" ${x.id === m.id ? "selected" : ""}>${esc(x.name)}${x.id === t.active ? T("targets.opt_active") : ""}${x.modified ? T("targets.opt_modified") : ""}</option>`).join("")}</select></div>
      <button type="button" class="secondary" id="tg-activate" ${m.id === t.active ? "disabled" : ""}>${ico("ok", 18)} ${T("targets.activate")}</button>
    </div>
    ${m.builtin ? `<p class="hint">${ico("info", 14)} ${T("targets.builtin_hint")}</p>` : ""}
    <p class="hint">${esc(m.note || "")}${m.builtin ? T("targets.builtin") : T("targets.own")}${T("targets.explain")}</p>
    <div class="scroll"><table class="targets"><tr><th>${T("targets.col_metric")}</th><th>${T("targets.col_rule")}</th><th class="num">${T("targets.col_ok")}</th><th class="num">${T("targets.col_warn")}</th><th>${T("targets.col_hint")}</th></tr>${Object.entries(t.fields).map(row).join("")}</table></div>
    <div class="row" style="margin-top:.6rem">
      <div><label for="tg-name">${T("targets.name")}</label><input id="tg-name" value="${esc(m.name)}" ${m.builtin ? "disabled" : ""}></div>
      <div><label for="tg-note">${T("targets.description")}</label><input id="tg-note" value="${esc(m.note || "")}" ${m.builtin ? "disabled" : ""}></div>
      ${m.builtin ? "" : `<button type="button" id="tg-save">${ico("ok", 18)} ${T("targets.save")}</button>`}
      <button type="button" class="secondary" id="tg-copy">${m.builtin ? T("targets.copy_own") : T("targets.save_as_new")}</button>
      ${m.modified ? `<button type="button" class="secondary" id="tg-reset">${T("targets.reset")}</button>` : ""}
      ${!m.builtin ? `<button type="button" class="danger" id="tg-del">${ico("trash", 18)} ${T("targets.delete")}</button>` : ""}
    </div>`;
  const collect = () => {
    const out = {};
    document.querySelectorAll("#targets input[data-k]").forEach((i) => {
      const k = i.dataset.k, f = i.dataset.f, v = i.value === "" ? null : +i.value;
      out[k] = out[k] || {};
      if (f.length > 2 && /\d$/.test(f)) { const base = f.slice(0, -1), idx = +f.slice(-1); out[k][base] = out[k][base] || [null, null]; out[k][base][idx] = v; }
      else out[k][f] = v;
    });
    return out;
  };
  $("#tg-sel").onchange = (e) => renderTargets(t, e.target.value);
  $("#tg-activate").onclick = guard(async () => { const r = await api("/api/targets/active", { method: "POST", body: { id: m.id } }); toast(T("targets.now_global", { name: m.name })); renderTargets(r, m.id); });
  const sv = $("#tg-save"); if (sv) sv.onclick = guard(async () => {
    const r = await api(`/api/targets/${m.id}`, { method: "PUT", body: { name: $("#tg-name").value, note: $("#tg-note").value, targets: collect() } });
    toast(T("targets.saved")); renderTargets(r, m.id);
  });
  $("#tg-copy").onclick = guard(async () => {
    const name = prompt(T("targets.prompt_name"), T("targets.copy_name", { name: m.name })); if (!name) return;
    const id = name.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "map-" + Date.now();
    const r = await api(`/api/targets/${id}`, { method: "PUT", body: { name, note: $("#tg-note").value, targets: collect() } });
    toast(T("targets.created", { name })); renderTargets(r, id);
  });
  const rs = $("#tg-reset"); if (rs) rs.onclick = guard(async () => { if (!confirm(T("targets.confirm_reset", { name: m.name }))) return; const r = await api(`/api/targets/${m.id}`, { method: "DELETE" }); toast(T("targets.reset_done")); renderTargets(r, m.id); });
  const dl = $("#tg-del"); if (dl) dl.onclick = guard(async () => { if (!confirm(T("targets.confirm_delete", { name: m.name }))) return; const r = await api(`/api/targets/${m.id}`, { method: "DELETE" }); toast(T("targets.deleted")); renderTargets(r, r.active); });
}

// Learning model: state per event type from /api/learn; "Retrain now" trains immediately and recounts all recordings
function renderLearn(d) {
  const box = $("#learn-box"); if (!box) return;
  if (!d.trained) { box.innerHTML = `<p class="hint">${T("settings.learn_no_model")}</p>`; return; }
  const ks = Object.entries(d.kinds || {});
  const pct = (v) => (v == null ? "–" : `±${Math.round(v * 100)} %`);
  box.innerHTML = `<p class="hint">${T("settings.learn_summary", { events: d.data?.events_master ?? 0, recs: d.data?.recordings_master ?? 0, manual: d.data?.events_manual ?? 0, date: d.trained_at ? dateStr(d.trained_at) : "–",
      applied: d.applied_at ? T("settings.learn_applied", { n: d.applied }) : "" })}</p>
    <div class="scroll"><table><tr><th>${T("settings.learn_col_kind")}</th><th class="num">${T("settings.learn_col_real")}</th><th class="num">${T("settings.learn_col_false")}</th><th class="num">${T("settings.learn_col_records")}</th><th class="num">${T("settings.learn_col_estimate")}</th><th class="num">${T("settings.learn_col_raw")}</th><th>${T("settings.learn_col_state")}</th></tr>
    ${ks.map(([k, v]) => `<tr><td>${esc(v.label)}</td><td class="num">${v.n_real ?? 0}</td><td class="num">${v.n_false ?? 0}</td><td class="num">${v.records ?? 0}</td><td class="num ${v.usable ? "ok" : ""}">${pct(v.cv_err_est)}</td><td class="num">${pct(v.cv_err_raw)}</td><td>${v.usable ? `${ico("ok", 14)} ` : ""}${esc(v.reason || "")}</td></tr>`).join("")}</table></div>
    ${ks.some(([, v]) => v.usable && (v.master_records ?? 0) < 2) ? `<p class="hint">${ico("warn", 14)} ${T("settings.learn_one_record")}</p>` : ""}
    ${d.last_error ? `<p class="hint bad">${T("settings.error", { error: esc(d.last_error) })}</p>` : ""}
    <div class="row"><button type="button" class="secondary" id="btn-learn">${ico("calibrate", 16)} ${T("settings.learn_now")}</button></div>`;
  $("#btn-learn").onclick = guard(async () => { const r = await api("/api/learn", { method: "POST" }); toast(T("settings.learn_done", { model: r.model_id, n: r.applied })); renderLearn({ ...(await api("/api/learn")) }); });
}
async function loadSystem() {
  const { values, defaults, spec, info } = await api("/api/settings");
  const onOff = (v) => (v ? T("settings.on") : T("settings.off"));
  const field = ([k, sp]) => {
    const id = "set-" + k, v = values[k];
    const input = sp.type === "bool"
      ? `<label class="check"><input type="checkbox" id="${id}" ${v ? "checked" : ""}> ${onOff(v)}</label>`
      : `<div class="in"><input type="number" id="${id}" value="${v}" min="${sp.min}" max="${sp.max}" step="${sp.type === "float" ? "0.5" : "1"}"><span>${esc(sp.unit || "")}</span></div>`;
    return `<div class="sfield"><label for="${id}">${esc(sp.label)}</label>${input}
      <div class="help">${esc(sp.help)} <span class="def">${T("settings.default", { value: sp.type === "bool" ? onOff(defaults[k]) : defaults[k] + " " + (sp.unit || "") })}</span></div><div class="err" id="${id}-err" role="alert"></div></div>`;
  };
  const ro = (l, v) => `<tr><th scope="row">${l}</th><td class="mono">${esc(String(v))}</td></tr>`;
  const cfgFile = `<span class="mono">${esc(info.config_file)}</span>`;
  $("#system").innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">${ico("nut")} ${T("settings.title")}</h2><button class="secondary" data-go="docs" title="${T("settings.docs_title")}">${ico("info", 18)} ${T("settings.docs")}</button></div>
    <h3>${ico("level", 18)} ${T("audioset.title")}</h3>
    <div id="audio-box"><p class="empty">${T("settings.loading")}</p></div>
    <form id="settings-form">
      <h3>${T("settings.recording")}</h3>
      ${["autostop_end_groove", "max_record_minutes", "min_free_gb"].map((k) => field([k, spec[k]])).join("")}
      <h3>${T("settings.retention")}</h3>
      ${field(["retention_days", spec.retention_days])}
      <div class="row"><button type="button" class="secondary" id="btn-retention">${ico("clock", 18)} ${T("settings.retention_apply")}</button></div>
      <h3>Discogs</h3>
      ${spec.discogs_sync_minutes ? field(["discogs_sync_minutes", spec.discogs_sync_minutes]) : ""}
      <h3>${T("settings.clipping")}</h3>
      <p class="hint">${T("settings.clipping_hint")}</p>
      ${["clip_grace_s", "clip_mention_samples", "clip_max_samples"].filter((k) => spec[k]).map((k) => field([k, spec[k]])).join("")}
      ${spec.engineer_mode ? `<h3>${T("settings.engineer")}</h3>${field(["engineer_mode", spec.engineer_mode])}` : ""}
      ${spec.learn_enabled ? `<h3>${T("settings.learning")}</h3>${field(["learn_enabled", spec.learn_enabled])}${spec.learn_grade ? field(["learn_grade", spec.learn_grade]) : ""}<div id="learn-box"><p class="empty">${T("settings.loading")}</p></div>` : ""}
      <h3>${T("settings.display")}</h3>
      <div class="sfield"><label for="set-lang">${T("settings.language")}</label>
        <select id="set-lang">${Object.entries(I18N.languages).map(([k, n]) => `<option value="${esc(k)}" ${k === I18N.lang ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <div class="help">${T("settings.language_help")}</div></div>
      <div class="sfield"><label for="set-theme">${T("settings.theme")}</label>
        <select id="set-theme">${Object.entries(THEMES).map(([k, n]) => `<option value="${k}" ${savedTheme() === k ? "selected" : ""}>${n}</option>`).join("")}</select>
        <div class="help">${T("settings.theme_help")}</div></div>
      <div class="row"><button type="submit">${ico("ok", 18)} ${T("settings.save")}</button></div>
    </form>
    <h3>${ico("cover", 18)} Discogs</h3>
    <div id="discogs-box"><p class="empty">${T("settings.loading")}</p></div>
    <h3>${ico("sliders", 18)} ${T("settings.targets")}</h3>
    <div id="targets"><p class="empty">${T("settings.targets_loading")}</p></div>
    <div id="network-slot"></div>
    <h3 id="settings-vinylmind">${ico("hub", 18)} ${T("vinylmind.title")}</h3>
    <div id="vm-box"><p class="empty">${T("settings.loading")}</p></div>
    <h3 id="settings-service">${T("settings.device_service")}</h3>
    <p class="hint">${T(info.desktop ? "audioset.service_hint_desktop" : "audioset.service_hint", { file: cfgFile })}</p>
    <table class="kv"><tbody>
      ${ro(T("settings.version"), info.version)}${ro(T("settings.device"), info.device)}${ro(T("settings.format"), T("settings.format_value", { ch: info.channels, format: info.format, rate: info.rate }))}
      ${info.audio_system ? ro(T("audio.backend"), T(`audioset.system_${info.audio_system}`))
        : info.audio_backend ? ro(T("audio.backend"), T(`audio.backend_${info.audio_backend}`)) : ""}
      ${ro(T("settings.recordings_dir"), info.data_dir)}${ro(T("settings.database"), info.state_dir)}${ro("Discogs", info.discogs || T("settings.not_set_up"))}${info.fake_source ? ro(T("settings.test_mode"), T("settings.test_mode_value")) : ""}
    </tbody></table>
    <p class="hint">${T("about.summary")} <a href="/licenses" target="_blank" rel="noopener">${T("about.licenses")}</a></p>`;
  api("/api/targets").then((t) => renderTargets(t, t.active)).catch((e) => toast(e.message));
  if ($("#learn-box")) api("/api/learn").then(renderLearn).catch((e) => { $("#learn-box").innerHTML = `<p class="hint">${esc(e.message)}</p>`; });
  // Discogs: a personal access token entered here (kept on the server, which only ever sends back its last four
  // characters) takes precedence over the configuration file and applies at once; the OAuth connection of a Discogs
  // app stays available where its keys are configured
  const renderDiscogs = (d) => {
    const box = $("#discogs-box"); if (!box) return;
    const saved = d.saved;
    const src = d.source === "ui" || d.source === "config" ? `<span class="def">${T(`settings.discogs_source_${d.source}`)}</span>` : "";
    const link = `<a href="https://www.discogs.com/settings/developers" target="_blank" rel="noopener">discogs.com/settings/developers</a>`;
    const tokenHint = saved ? T("settings.discogs_token_saved", { hint: saved.token_hint }) : d.source === "config" ? d.token_hint : "";
    const form = `<form id="dc-form" autocomplete="off">
      <div class="sfield"><label for="dc-token">${T("settings.discogs_token")}</label>
        <input type="password" id="dc-token" autocomplete="off" spellcheck="false" placeholder="${esc(tokenHint)}">
        <div class="help">${T("settings.discogs_token_help", { link })} ${src}</div><div class="err" id="dc-token-err" role="alert"></div></div>
      <div class="sfield"><label for="dc-username">${T("settings.discogs_username")}</label>
        <input id="dc-username" autocomplete="off" spellcheck="false" value="${esc(saved?.username || (d.enabled ? d.username : ""))}">
        <div class="help">${T("settings.discogs_username_help")}</div><div class="err" id="dc-username-err" role="alert"></div></div>
      <div class="row">
        <button type="submit">${ico("ok", 18)} ${T("settings.discogs_save")}</button>
        <button type="button" class="secondary" id="dc-test" title="${esc(T("settings.discogs_test_hint"))}">${ico("wifi", 18)} ${T("settings.discogs_test")}</button>
        ${saved ? `<button type="button" class="link" id="dc-remove">${T("settings.discogs_remove")}</button>` : ""}
      </div>
      <div id="dc-test-out" role="status"></div>
    </form>`;
    box.innerHTML = (d.enabled ? `
      <p>${T("settings.discogs_account", { user: esc(d.username), n: d.count })}${d.synced_at ? T("settings.discogs_as_of", { date: dateStr(d.synced_at) }) : T("settings.discogs_not_synced")}${d.syncing ? T("settings.discogs_syncing_now") : ""}${d.error ? `<br><span class="st bad">${esc(d.error)}</span>` : ""}</p>
      <p class="hint">${T("settings.discogs_hint")}</p>
      <div class="row"><button type="button" class="secondary" id="btn-discogs-sync" ${d.syncing ? "disabled" : ""}>${ico("cover", 18)} ${T("settings.discogs_sync_now")}</button>
        ${d.via === "oauth" ? `<button type="button" class="link" id="btn-discogs-disconnect">${T("settings.discogs_disconnect")}</button>` : ""}</div>
      <details><summary>${T("settings.discogs_change")}</summary>${form}</details>`
      : `<p class="hint">${T("settings.discogs_off")}</p>${form}`)
      + (!d.enabled && d.oauth_ready ? `<p>${T("settings.discogs_app_ready")}</p>
      <div class="row"><button type="button" class="secondary" id="btn-discogs-connect">${ico("cover", 18)} ${T("settings.discogs_connect")}</button></div>
      <p class="hint">${T("settings.discogs_connect_hint")}</p>` : "")
      + (d.desktop ? "" : `<p class="hint">${T("settings.discogs_config_alt", { file: cfgFile })}</p>`);
    const b = $("#btn-discogs-sync"); if (b) b.onclick = guard(async () => { b.disabled = true; toast(T("settings.discogs_syncing")); const r = await api("/api/discogs/sync", { method: "POST" }); toast(T("settings.discogs_synced", { n: r.count })); renderDiscogs(r); });
    const c = $("#btn-discogs-connect"); if (c) c.onclick = guard(async () => { const r = await api("/api/discogs/connect", { method: "POST" }); window.location.href = r.authorize_url; });
    const x = $("#btn-discogs-disconnect"); if (x) x.onclick = guard(async () => { if (!confirm(T("settings.discogs_confirm_disconnect"))) return; renderDiscogs(await api("/api/discogs/disconnect", { method: "POST" })); });
    const body = () => ({ token: $("#dc-token").value.trim(), username: $("#dc-username").value.trim() });
    const clearErrors = () => box.querySelectorAll(".err").forEach((e) => (e.textContent = ""));
    const fieldErrors = (err) => {
      let fields = {}; try { fields = JSON.parse(err.message).fields || {}; } catch (_) {}
      if (!Object.keys(fields).length) throw err;
      for (const [k, m] of Object.entries(fields)) { const el = $(`#dc-${k}-err`); if (el) el.textContent = m; }
      toast(T("settings.fix_fields"), 5000);
    };
    $("#dc-form").onsubmit = guard(async (ev) => {
      ev.preventDefault(); clearErrors();
      let r;
      try { r = await api("/api/discogs/account", { method: "PUT", body: body() }); } catch (err) { fieldErrors(err); return; }
      toast(T("settings.discogs_saved")); renderDiscogs(r);
    });
    $("#dc-test").onclick = guard(async () => {
      const out = $("#dc-test-out"), btn = $("#dc-test");
      clearErrors(); btn.disabled = true; out.innerHTML = `<p class="hint">${T("settings.discogs_testing")}</p>`;
      let r;
      try { r = await api("/api/discogs/test", { method: "POST", body: body() }); }
      catch (err) { out.innerHTML = ""; fieldErrors(err); return; }
      finally { btn.disabled = false; }
      out.innerHTML = !r.ok ? `<p class="warnbox">${ico("warn", 14)} ${T("settings.discogs_test_failed", { error: esc(r.error) })}</p>`
        : r.matches === false ? `<p class="warnbox">${ico("warn", 14)} ${T("settings.discogs_test_mismatch", { user: esc(r.username), expected: esc(r.expected) })}</p>`
        : `<p>${ico("ok", 14)} ${T("settings.discogs_test_ok", { user: esc(r.username) })}</p>`;
    });
    const rm = $("#dc-remove");
    if (rm) rm.onclick = guard(async () => {
      if (!confirm(T(d.config_token ? "settings.discogs_remove_confirm_config" : "settings.discogs_remove_confirm"))) return;
      renderDiscogs(await api("/api/discogs/account", { method: "DELETE" }));
      toast(T("settings.discogs_removed"));
    });
    // A sync started by saving takes a second or two: show its result, unless someone is typing in the form
    if (d.syncing) setTimeout(() => {
      if ($("#discogs-box") !== box || box.contains(document.activeElement) || $("#dc-token").value) return;
      api("/api/discogs/status").then(renderDiscogs).catch(() => {});
    }, 2000);
  };
  api("/api/discogs/status").then(renderDiscogs).catch((e) => toast(e.message));
  loadAudio();
  loadNetwork();
  loadVinylMind();
  $("#set-lang").onchange = (e) => I18N.setLang(e.target.value);  // stores the choice in a cookie and reloads
  $("#settings-form").onsubmit = guard(async (e) => {
    e.preventDefault();
    document.querySelectorAll("#system .err").forEach((x) => (x.textContent = ""));
    const body = {};
    for (const k of Object.keys(spec)) { const el = $("#set-" + k); body[k] = spec[k].type === "bool" ? el.checked : el.value; }
    applyTheme($("#set-theme").value);
    try {
      await api("/api/settings", { method: "PUT", body });
      toast(T("settings.saved"));
      loadSystem();
    } catch (err) {
      let fields = {}; try { fields = JSON.parse(err.message).fields || {}; } catch (_) {}
      if (!Object.keys(fields).length) throw err;
      for (const [k, m] of Object.entries(fields)) { const el = $("#set-" + k + "-err"); if (el) el.textContent = `${spec[k]?.label || k}: ${m}.`; }
      toast(T("settings.fix_fields"), 5000);
    }
  });
  $("#btn-retention").onclick = guard(async () => {
    const d = values.retention_days;
    if (d <= 0) { toast(T("settings.retention_off")); return; }
    if (!confirm(T("settings.retention_confirm", { n: d }))) return;
    const r = await api("/api/retention/run", { method: "POST" });
    toast(r.deleted.length ? T("settings.retention_deleted", { n: r.deleted.length }) : T("settings.retention_nothing"));
  });
}

// ─── Network access (desktop app): other devices, address, QR code, access code ─────────
// Only the desktop app has this section; a service is configured in its configuration file. The code and the QR code
// (which carries it) are only sent to the computer itself, and only the computer itself can change the setting.
async function loadNetwork() {
  let n;
  try { n = await api("/api/network"); } catch (_) { return; }
  if (!n.desktop || !$("#system")) return;
  if (!$("#network-box")) ($("#network-slot") || $("#system")).insertAdjacentHTML("beforeend", `<h3>${ico("wifi", 18)} ${T("network.title")}</h3><div id="network-box"></div>`);
  const box = $("#network-box");
  const urls = n.urls || [];
  const qr = (i) => (urls[i]?.qr ? `<div class="net-qr">${urls[i].qr}</div><p class="hint">${T("network.qr_hint")}</p>` : (n.local ? `<p class="hint">${T("network.no_qr")}</p>` : ""));
  const pick = urls.length > 1
    ? `<select id="net-addr" aria-label="${T("network.address_pick")}">${urls.map((u, i) => `<option value="${i}">${esc(u.url)}</option>`).join("")}</select>`
    : urls.length ? `<a class="mono" href="${esc(urls[0].url)}" target="_blank" rel="noopener">${esc(urls[0].url)}</a>` : `<span class="hint">${T("network.no_address")}</span>`;
  box.innerHTML = `
    <div class="sfield"><label class="check"><input type="checkbox" id="net-lan" ${n.lan_enabled ? "checked" : ""} ${n.local ? "" : "disabled"}> ${T("network.allow")}</label>
      <div class="help">${T("network.allow_help")}</div></div>
    ${n.local ? "" : `<p class="hint">${T("network.remote_hint")}</p>`}
    ${n.error ? `<p class="hint bad" role="alert">${esc(n.error)}</p>` : ""}
    ${n.running ? `
      <div class="sfield"><label>${T("network.addresses")}</label>${pick}</div>
      <div id="net-qr-box">${qr(0)}</div>
      ${n.code ? `<div class="sfield"><label>${T("network.code")}</label><div class="net-code mono">${esc(n.code)}</div>
        <div class="help">${T("network.code_hint")}</div></div>
      <div class="row"><button type="button" class="secondary" id="btn-net-code">${T("network.new_code")}</button></div>` : ""}
      ${n.local ? `<p class="hint">${T("network.firewall_hint")}</p>` : ""}` : ""}`;
  const sel = $("#net-addr");
  if (sel) sel.onchange = () => { $("#net-qr-box").innerHTML = qr(Number(sel.value)); };
  const lan = $("#net-lan");
  lan.onchange = guard(async () => {
    lan.disabled = true;
    try {
      await api("/api/network", { method: "PUT", body: { lan_enabled: lan.checked } });
      toast(T(lan.checked ? "network.turned_on" : "network.turned_off"));
    } finally { loadNetwork(); }
  });
  const nc = $("#btn-net-code");
  if (nc) nc.onclick = guard(async () => {
    if (!confirm(T("network.new_code_confirm"))) return;
    await api("/api/network/code", { method: "POST" });
    toast(T("network.new_code_done"));
    loadNetwork();
  });
}

// ─── Vinyl-Mind (settings): contribute to and use the model that several stations learn together ─────────────
// Both switches are off until the user turns them on. Joining shows the server key's fingerprint first and stops if
// the server presents another key; "What is sent" shows the exact payloads before anything leaves the station.
const vmKinds = (ks) => (ks || []).map((k) => EVKIND[k] || k).join(", ");
// Pretty-printed JSON with arrays of plain values on one line, so ten weights read as one row
function vmJson(v, ind = "") {
  if (Array.isArray(v)) return v.every((x) => x === null || typeof x !== "object") ? JSON.stringify(v).replace(/,/g, ", ")
    : `[\n${v.map((x) => ind + "  " + vmJson(x, ind + "  ")).join(",\n")}\n${ind}]`;
  if (v && typeof v === "object") return `{\n${Object.entries(v).map(([k, x]) => `${ind}  ${JSON.stringify(k)}: ${vmJson(x, ind + "  ")}`).join(",\n")}\n${ind}}`;
  return JSON.stringify(v);
}
async function loadVinylMind() {
  const box = $("#vm-box"); if (!box) return;
  try { renderVinylMind(await api("/api/vinylmind")); }
  catch (e) { box.innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}
function renderVinylMind(s) {
  const box = $("#vm-box"); if (!box) return;
  const joined = s.state === "pending" || s.state === "approved";
  const stCls = { approved: "ok", pending: "warn", revoked: "bad" }[s.state] || "info";
  const model = !s.model ? T("vinylmind.model_none")
    : s.model.in_use ? T("vinylmind.model_value", { version: esc(s.model.version), kinds: vmKinds(s.model.adopted) })
    : T(s.model.adopted.length ? "vinylmind.model_idle" : "vinylmind.model_unused", { version: esc(s.model.version) });
  const p = s.prior;
  const prior = !p ? T("vinylmind.prior_none")
    : (p.source === "server" ? T("vinylmind.prior_server", { ref: esc(p.ref), kinds: vmKinds(p.kinds) })
      : T("vinylmind.prior_import", { type: T(`vinylmind.type_${p.type}`), ref: esc(p.ref), kinds: vmKinds(p.kinds) }))
      + (p.in_use ? "" : ` · <span class="hint">${T("vinylmind.prior_inactive")}</span>`)
      + ` <button type="button" class="link" id="vm-prior-rm">${T("vinylmind.prior_remove")}</button>`;
  const row = (label, value) => `<tr><th scope="row">${label}</th><td>${value}</td></tr>`;
  box.innerHTML = `
    <p class="hint">${T("vinylmind.intro")}</p>
    ${s.recording ? `<p class="warnbox">${T("vinylmind.waiting_recording")}</p>` : ""}
    <div class="sfield"><label class="check"><input type="checkbox" id="vm-contribute" ${s.contribute ? "checked" : ""}> ${T("vinylmind.contribute")}</label>
      <div class="help">${T("vinylmind.contribute_help")}</div></div>
    <div class="sfield"><label class="check"><input type="checkbox" id="vm-use" ${s.use_model ? "checked" : ""}> ${T("vinylmind.use")}</label>
      <div class="help">${T("vinylmind.use_help")}</div></div>
    <div class="sfield"><label for="vm-url">${T("vinylmind.server")}</label>
      <div class="vm-url"><input type="url" id="vm-url" value="${esc(s.url)}" placeholder="https://" spellcheck="false" autocomplete="off" ${joined ? "disabled" : ""}>
        <button type="button" class="secondary" id="vm-url-save" ${joined ? "disabled" : ""}>${T("vinylmind.save_url")}</button></div>
      <div class="help">${s.preset_url ? T("vinylmind.preset", { url: `<span class="mono">${esc(s.preset_url)}</span>` }) : T("vinylmind.no_default")}
        ${!s.url_is_preset && !joined ? `<button type="button" class="link" id="vm-url-reset">${T("vinylmind.reset_url")}</button>` : ""}</div>
      ${joined ? `<div class="help">${T("vinylmind.url_locked")}</div>` : ""}
      <div class="err" id="vm-url-err" role="alert"></div></div>
    ${s.joined ? `<div class="sfield"><label>${T("vinylmind.server_key")}</label><div class="vm-fp mono">${esc(s.joined.fingerprint)}</div>
      <div class="help">${T("vinylmind.server_key_help")}</div></div>` : ""}
    <table class="kv"><tbody>
      ${row(T("vinylmind.status"), `<span class="st ${stCls}">${T(`vinylmind.state_${s.state}`)}</span>${s.joined?.name ? ` <span class="hint">${esc(s.joined.name)}</span>` : ""}`)}
      ${joined ? row(T("vinylmind.last_contribution"), s.last_contribution ? T("vinylmind.last_contribution_value", { date: dateStr(s.last_contribution.at), n: s.last_contribution.profiles }) : T("vinylmind.never")) : ""}
      ${joined ? row(T("vinylmind.model"), model) : ""}
      ${row(T("vinylmind.prior"), prior)}
      ${s.last_error ? row(T("vinylmind.last_error"), `<span class="bad">${esc(s.last_error)}</span>`) : ""}
    </tbody></table>
    ${joined ? `<div class="row">
        <button type="button" class="secondary" id="vm-sync" title="${T("vinylmind.send_now_hint")}" ${s.contribute || s.use_model ? "" : "disabled"}>${ico("hub", 18)} ${T("vinylmind.send_now")}</button>
        <button type="button" class="link" id="vm-leave">${T("vinylmind.leave")}</button></div>`
      : `<div class="row vm-join"><input id="vm-invite" placeholder="${T("vinylmind.invitation_placeholder")}" aria-label="${T("vinylmind.invitation_placeholder")}" autocomplete="off" spellcheck="false">
        <button type="button" id="vm-join" ${s.url ? "" : "disabled"} title="${s.url ? "" : T("vinylmind.join_no_url")}">${ico("hub", 18)} ${T("vinylmind.join")}</button></div>`}
    <details id="vm-preview"><summary>${T("vinylmind.preview")}</summary><div id="vm-preview-box"><p class="empty">${T("settings.loading")}</p></div></details>
    <h4>${T("vinylmind.files")}</h4>
    <p class="hint">${T("vinylmind.files_hint")}</p>
    <div class="row vm-join"><select id="vm-exp" aria-label="${T("vinylmind.export_profile")}"></select>
      <button type="button" class="secondary" id="vm-export">${ico("export", 18)} ${T("vinylmind.export")}</button>
      <button type="button" class="secondary" id="vm-import">${T("vinylmind.import")}</button>
      <input type="file" id="vm-file" accept=".json,application/json" class="hidden"></div>`;
  const put = async (body, msg) => { renderVinylMind(await api("/api/vinylmind", { method: "PUT", body })); if (msg) toast(msg); };
  for (const [id, key] of [["vm-contribute", "contribute"], ["vm-use", "use_model"]]) {
    const el = $("#" + id);
    el.onchange = guard(async () => {
      el.disabled = true;
      try { await put({ [key]: el.checked }, T(`vinylmind.${key === "contribute" ? "contribute" : "use"}_${el.checked ? "on" : "off"}`)); }
      catch (e) { loadVinylMind(); throw e; }
    });
  }
  const saveUrl = guard(async () => {
    $("#vm-url-err").textContent = "";
    try { await put({ url: $("#vm-url").value.trim() }, T("vinylmind.url_saved")); }
    catch (e) { $("#vm-url-err").textContent = e.message; }
  });
  $("#vm-url-save").onclick = saveUrl;
  $("#vm-url").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); saveUrl(); } };
  const rs = $("#vm-url-reset"); if (rs) rs.onclick = guard(() => put({ reset_url: true }, T("vinylmind.url_reset_done")));
  const pr = $("#vm-prior-rm"); if (pr) pr.onclick = guard(async () => {
    if (!confirm(T("vinylmind.prior_remove_confirm"))) return;
    renderVinylMind(await api("/api/vinylmind/prior", { method: "DELETE" })); toast(T("vinylmind.prior_removed"));
  });
  const jn = $("#vm-join"); if (jn) jn.onclick = guard(async () => {
    jn.disabled = true;
    try {
      const info = await api("/api/vinylmind/server");
      if (!info.supported) toast(T("vinylmind.join_unsupported", { version: s.feature_version }), 6000);
      if (!confirm(T("vinylmind.join_confirm", { name: info.name || info.url, fp: info.fingerprint_display }))) return;
      const r = await api("/api/vinylmind/join", { method: "POST", body: { invitation: $("#vm-invite").value.trim() || null, fingerprint: info.fingerprint } });
      renderVinylMind(r); toast(T(r.state === "approved" ? "vinylmind.joined_approved" : "vinylmind.joined_pending"), 5000);
    } finally { if (document.body.contains(jn)) jn.disabled = false; }
  });
  const sy = $("#vm-sync"); if (sy) sy.onclick = guard(async () => {
    sy.disabled = true;
    try {
      const r = await api("/api/vinylmind/sync", { method: "POST" });
      const res = r.result || {}, n = res.contributions?.sent || 0;
      renderVinylMind(r.status);
      toast(res.status === "pending" ? T("vinylmind.sync_pending") : [n ? T("vinylmind.sync_sent", { n }) : "", res.model === "new" ? T("vinylmind.sync_model") : ""].filter(Boolean).join(" ") || T("vinylmind.sync_done"));
    } catch (e) { loadVinylMind(); throw e; }
  });
  const lv = $("#vm-leave"); if (lv) lv.onclick = guard(async () => {
    if (!confirm(T("vinylmind.leave_confirm"))) return;
    let r;
    try { r = await api("/api/vinylmind/leave", { method: "POST", body: {} }); }
    catch (e) {
      if (!confirm(T("vinylmind.forget_confirm", { error: e.message }))) return;
      r = await api("/api/vinylmind/leave", { method: "POST", body: { forget: true } });
    }
    renderVinylMind(r); toast(T("vinylmind.left"));
  });
  $("#vm-preview").ontoggle = () => { if ($("#vm-preview").open) loadVmPreview(); };
  api("/api/profiles").then((ps) => {
    const sel = $("#vm-exp"); if (!sel) return;
    sel.innerHTML = ps.map((x) => `<option value="${x.id}" ${x.active ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    $("#vm-export").disabled = !ps.length;
  }).catch(() => {});
  $("#vm-export").onclick = guard(async () => {
    const pkg = await api(`/api/vinylmind/export?profile=${encodeURIComponent($("#vm-exp").value)}`);
    const file = `vinyl-mind-contribution-${pkg.profile_id.slice(0, 8)}.json`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(pkg, null, 2) + "\n"], { type: "application/json" }));
    a.download = file; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast(T("vinylmind.export_done", { file }));
  });
  $("#vm-import").onclick = () => $("#vm-file").click();
  $("#vm-file").onchange = guard(async (ev) => {
    const f = ev.target.files[0]; ev.target.value = "";
    if (!f) return;
    const text = await f.text();
    const { package: d } = await api("/api/vinylmind/import", { method: "POST", body: text });
    const kinds = Object.entries(d.kinds).map(([k, v]) => T("vinylmind.import_kind", { kind: EVKIND[k] || k, n: v.examples ?? "–" })).join("\n");
    const ask = d.type === "model"
      ? T("vinylmind.import_confirm_model", { ref: d.ref, version: d.feature_version, signature: T(`vinylmind.signature_${d.signature}`), kinds })
      : T("vinylmind.import_confirm_contribution", { ref: d.ref, version: d.feature_version, kinds });
    if (!confirm(ask)) return;
    const r = await api("/api/vinylmind/import?confirm=true", { method: "POST", body: text });
    renderVinylMind(r.status); toast(T("vinylmind.imported"), 5000);
  });
}
// The payloads of the next contribution, per turntable profile, with the chain class that goes with them
async function loadVmPreview() {
  const box = $("#vm-preview-box"); if (!box) return;
  let d;
  try { d = await api("/api/vinylmind/preview"); } catch (e) { box.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  if (!d.profiles.length) { box.innerHTML = `<p class="hint">${T("vinylmind.preview_none")}</p>`; return; }
  const opts = (kind, values, cur, guess, set) => values.map((v) => {
    const label = T(`vinylmind.${kind}_${v}`);
    return `<option value="${v}" ${v === cur ? "selected" : ""}>${!set && v === guess ? T("vinylmind.guessed", { value: label }) : label}</option>`;
  }).join("");
  box.innerHTML = `<p class="hint">${T("vinylmind.preview_hint")}</p>` + d.profiles.map((x) => `
    <div class="vm-prof">
      <div class="vm-prof-head"><b>${esc(x.name)}</b>${x.active ? ` <span class="st ok">${T("vinylmind.active")}</span>` : ""}
        <span class="hint mono">${T("vinylmind.profile_id", { id: esc(x.vm_id) })}</span>
        ${x.payload ? `<span class="hint">${x.unchanged ? T("vinylmind.preview_sent", { date: dateStr(x.sent_at) }) : x.sent_at ? T("vinylmind.preview_changed") : T("vinylmind.preview_not_sent")}</span>` : ""}</div>
      <div class="vm-chain">
        <label>${T("vinylmind.connection")}<select data-vm-chain="${x.id}" data-part="connection">${opts("connection", ["usb_turntable", "interface", "unknown"], x.chain.connection, x.chain_guess.connection, x.chain_set)}</select></label>
        <label>${T("vinylmind.cartridge")}<select data-vm-chain="${x.id}" data-part="cartridge">${opts("cartridge", ["mm", "mc", "unknown"], x.chain.cartridge, x.chain_guess.cartridge, x.chain_set)}</select></label>
      </div>
      ${x.payload ? `<pre class="vm-json">${esc(vmJson(x.payload))}</pre>` : `<p class="hint">${esc(x.reason || "")}</p>`}
    </div>`).join("");
  box.querySelectorAll("[data-vm-chain]").forEach((el) => el.addEventListener("change", guard(async () => {
    const id = el.dataset.vmChain;
    const part = (n) => box.querySelector(`[data-vm-chain="${id}"][data-part="${n}"]`).value;
    await api("/api/vinylmind", { method: "PUT", body: { chain: { [id]: { connection: part("connection"), cartridge: part("cartridge") } } } });
    toast(T("vinylmind.chain_saved"));
    loadVmPreview();
  })));
}

// ─── Audio interface (settings): device, sample rate, bit depth, inputs, WASAPI/ASIO ───────────────────────────
// What is set here is stored on the server and wins over the configuration file; it applies from the next recording
// or level check. The form shows what applies now and where it comes from, and only offers what the chosen device
// can do (/api/audio?device= answers for a device chosen in the form before it is saved). Saving stores everything the
// form shows, so that later recordings keep the same format even if another program changes the interface's rate.
const AU_BITS = { S16_LE: 16, S24_3LE: 24, S32_LE: 32 };
const auKhz = (r) => num(r / 1000, r % 1000 ? 1 : 0);
const auApi = (h) => String(h || "").replace(/^Windows /, "");      // "Windows WASAPI" → "WASAPI"
const auForm = { dirty: {} };                                        // fields changed in the form since it loaded
async function loadAudio(device) {
  const box = $("#audio-box"); if (!box) return;
  let a;
  try { a = await api("/api/audio" + (device === undefined ? "" : "?device=" + encodeURIComponent(device))); }
  catch (e) { box.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  if (device === undefined) auForm.dirty = {};
  renderAudio(a);
}
function renderAudio(a) {
  const box = $("#audio-box"); if (!box) return;
  const e = a.effective, o = a.options, src = e.sources || {}, dev = a.device;
  const multi = new Set(a.devices.map((d) => d.hostapi).filter(Boolean)).size > 1;   // Windows: ASIO and WASAPI
  const nameOf = (id) => { const d = a.devices.find((x) => x.device === id); return d ? (multi ? auApi(d.hostapi) + ": " : "") + d.name : id; };
  const label = (d) => [(d.hostapi ? (multi ? auApi(d.hostapi) + ": " : "") + d.name : `${d.name} (${d.device})`),
    d.channels ? T("audioset.device_inputs", { n: d.channels }) : "", d.current_rate ? T("audioset.device_rate_now", { rate: auKhz(d.current_rate) }) : "",
    d.kind !== "interface" ? T(`audioset.kind_${d.kind}`) : ""].filter(Boolean).join(" · ");
  const opt = (d) => `<option value="${esc(d.device)}" ${d.device === a.selected ? "selected" : ""}>${esc(label(d))}</option>`;
  const ifaces = a.devices.filter((d) => d.kind === "interface"), others = a.devices.filter((d) => d.kind !== "interface");
  const first = a.config.device ? T("audioset.device_config", { device: esc(a.config.device) })
    : a.fake_source ? T("audioset.device_test_file")
    : a.auto ? T("audioset.device_auto", { name: esc(nameOf(a.auto)) }) : T("audioset.device_auto_none");
  let devOpts = `<option value="" ${a.selected ? "" : "selected"}>${first}</option>`;
  if (a.selected && !a.devices.some((d) => d.device === a.selected)) devOpts += `<option value="${esc(a.selected)}" selected>${T("audioset.not_connected", { device: esc(a.selected) })}</option>`;
  if (ifaces.length) devOpts += `<optgroup label="${T("audioset.group_interfaces")}">${ifaces.map(opt).join("")}</optgroup>`;
  if (others.length) devOpts += `<optgroup label="${T("audioset.group_other")}">${others.map(opt).join("")}</optgroup>`;
  const srcTag = (k) => (src[k] ? `<span class="def">${T(`audioset.source_${src[k]}`)}</span>` : "");
  const native = o.native_formats;
  const sel = (id, items) => `<select id="${id}" ${a.locked ? "disabled" : ""}>${items.join("")}</select>`;
  const rates = o.rates.map((r) => `<option value="${r}" ${r === e.rate ? "selected" : ""}>${auKhz(r)} kHz${r === o.current_rate ? T("audioset.rate_now_tag") : ""}</option>`);
  const bits = o.formats.map((f) => `<option value="${f}" ${f === e.sample_format ? "selected" : ""}>${T("audioset.bits_n", { n: AU_BITS[f] })}${native && !native.includes(f) ? T("audioset.bits_not_native") : ""}</option>`);
  const pairs = o.pairs.map((p) => `<option value="${p.join(",")}" ${p.join(",") === e.inputs.join(",") ? "selected" : ""}>${T("audioset.inputs_pair", { l: p[0], r: p[1] })}</option>`);
  const win = a.windows, asio = a.asio || {};
  box.innerHTML = `
    <p class="hint">${T("audioset.intro")} ${T("audioset.system", { name: T(`audioset.system_${a.system}`) })}</p>
    ${a.locked ? `<p class="warnbox">${T("audioset.locked")}</p>` : ""}
    <form id="audio-form">
      <div class="sfield"><label for="au-dev">${T("audioset.device")}</label>${sel("au-dev", [devOpts])}
        <div class="help">${T("audioset.device_help")} ${srcTag("device")}</div>
        ${a.profile ? `<div class="help">${ico("info", 14)} ${T("audioset.profile_wins", { name: esc(a.profile.name), device: esc(nameOf(a.profile.device)) })}</div>` : ""}
        <div class="err" id="au-dev-err" role="alert"></div></div>
      <div class="sfield"><label for="au-rate">${T("audioset.rate")}</label>${sel("au-rate", rates)}
        <div class="help" id="au-rate-note"></div>
        <div class="help">${T("audioset.rate_help")} ${srcTag("rate")}</div><div class="err" id="au-rate-err" role="alert"></div></div>
      <div class="sfield"><label for="au-bits">${T("audioset.bits")}</label>${sel("au-bits", bits)}
        <div class="help">${T("audioset.bits_help")} ${srcTag("sample_format")}</div><div class="err" id="au-sample_format-err" role="alert"></div></div>
      ${a.channels === 2 && a.fake_source ? `<div class="sfield"><label>${T("audioset.inputs")}</label>
        <div class="help">${ico("info", 14)} ${T("audioset.inputs_test_file")}</div></div>`
      : a.channels === 2 ? `<div class="sfield"><label for="au-inputs">${T("audioset.inputs")}</label>${sel("au-inputs", pairs)}
        <div class="help">${T("audioset.inputs_help")} ${srcTag("inputs")}</div><div class="err" id="au-inputs-err" role="alert"></div></div>` : ""}
      ${win ? `<div class="sfield"><label class="check"><input type="checkbox" id="au-excl" ${e.wasapi_exclusive ? "checked" : ""} ${a.locked ? "disabled" : ""}> ${T("audioset.exclusive")}</label>
        <div class="help">${T("audioset.exclusive_help")} ${srcTag("wasapi_exclusive")}</div><div class="err" id="au-wasapi_exclusive-err" role="alert"></div></div>
      <div class="sfield"><label class="check"><input type="checkbox" id="au-asio" ${e.asio !== false && asio.available ? "checked" : ""} ${asio.available && !a.locked ? "" : "disabled"}> ${T("audioset.asio")}</label>
        <div class="help">${asio.available ? T("audioset.asio_help") : T("audioset.asio_unavailable")} ${srcTag("asio")}</div>
        ${asio.restart ? `<div class="help">${ico("warn", 14)} ${T("audioset.asio_restart")}</div>` : ""}<div class="err" id="au-asio-err" role="alert"></div></div>` : ""}
      <div class="row">
        <button type="submit" ${a.locked ? "disabled" : ""}>${ico("ok", 18)} ${T("audioset.save")}</button>
        <button type="button" class="secondary" id="au-test-btn" ${a.locked ? "disabled" : ""} title="${T("audioset.test_hint")}">${ico("level", 18)} ${T("audioset.test")}</button>
        ${Object.keys(a.stored || {}).length ? `<button type="button" class="link" id="au-reset" ${a.locked ? "disabled" : ""}>${T("audioset.reset")}</button>` : ""}
      </div>
      <div id="au-test" role="status"></div>
    </form>`;
  // Changes made in the form survive switching to another device, where that device offers them
  for (const [id, v] of Object.entries(auForm.dirty)) {
    const el = $("#" + id); if (!el) continue;
    if (el.type === "checkbox") el.checked = v; else if ([...el.options].some((x) => x.value === v)) el.value = v;
  }
  const rateNote = () => {
    const r = +$("#au-rate").value, now = o.current_rate, shared = dev?.hostapi === "Windows WASAPI" && !($("#au-excl")?.checked);
    let t = "";
    if (now && r !== now) t = `${ico("info", 14)} ` + T(shared ? "audioset.rate_shared" : "audioset.rate_switch", { now: auKhz(now), rate: auKhz(r) });
    else if (!o.rates_known && dev) t = T("audioset.rate_unknown");
    $("#au-rate-note").innerHTML = t;
  };
  rateNote();
  box.querySelectorAll("select, input").forEach((el) => el.addEventListener("change", () => {
    auForm.dirty[el.id] = el.type === "checkbox" ? el.checked : el.value;
    if (el.id === "au-dev") { delete auForm.dirty["au-dev"]; loadAudio(el.value); return; }
    rateNote();
  }));
  const body = () => {
    const b = { device: $("#au-dev").value || null, rate: +$("#au-rate").value, sample_format: $("#au-bits").value };
    if ($("#au-inputs")) b.inputs = $("#au-inputs").value.split(",").map(Number);
    if ($("#au-excl")) b.wasapi_exclusive = $("#au-excl").checked;
    if ($("#au-asio") && asio.available) b.asio = $("#au-asio").checked;
    return b;
  };
  const fieldErrors = (err) => {
    let fields = {}; try { fields = JSON.parse(err.message).fields || {}; } catch (_) {}
    if (!Object.keys(fields).length) throw err;
    for (const [k, m] of Object.entries(fields)) { const el = $(`#au-${k === "device" ? "dev" : k === "rate" ? "rate" : k}-err`); if (el) el.textContent = m; }
    toast(T("settings.fix_fields"), 5000);
  };
  $("#audio-form").onsubmit = guard(async (ev) => {
    ev.preventDefault();
    box.querySelectorAll(".err").forEach((x) => (x.textContent = ""));
    try { await api("/api/audio", { method: "PUT", body: body() }); } catch (err) { fieldErrors(err); return; }
    toast(T("audioset.saved"));
    loadSystem();
  });
  $("#au-test-btn").onclick = guard(async () => {
    const out = $("#au-test"), btn = $("#au-test-btn");
    box.querySelectorAll(".err").forEach((x) => (x.textContent = ""));
    btn.disabled = true; out.innerHTML = `<p class="hint">${T("audioset.test_running")}</p>`;
    let r;
    try { r = await api("/api/audio/test", { method: "POST", body: body() }); }
    catch (err) { out.innerHTML = ""; fieldErrors(err); return; }
    finally { btn.disabled = false; }
    if (!r.ok) { out.innerHTML = `<p class="warnbox">${ico("warn", 14)} ${T("audioset.test_failed", { error: esc(r.error) })}</p>`; return; }
    const peaks = r.peak_dbfs || [], names = peaks.length === 2 ? ["L", "R"] : peaks.map((_, i) => String(i + 1));
    const top = Math.max(...peaks), notes = [...(r.notes || [])];
    if (top <= -90) notes.push(T("audioset.test_silent"));
    if (top >= -0.1) notes.push(T("audioset.test_clip"));
    if (r.xruns) notes.push(T("audioset.test_xruns", { n: r.xruns }));
    out.innerHTML = `<p>${ico("ok", 14)} ${T("audioset.test_ok", { device: esc(nameOf(r.device)), rate: auKhz(r.rate), bits: r.bits, inputs: r.inputs.join(" / ") })}<br>
      <span class="hint">${T("audioset.test_peaks", { levels: peaks.map((p, i) => T("audioset.test_channel", { ch: names[i], db: num(p, 1) })).join(", ") })}</span></p>
      ${notes.map((n) => `<p class="warnbox">${esc(n)}</p>`).join("")}`;
  });
  const rs = $("#au-reset");
  if (rs) rs.onclick = guard(async () => {
    if (!confirm(T("audioset.reset_confirm"))) return;
    await api("/api/audio", { method: "DELETE" });
    toast(T("audioset.reset_done"));
    loadSystem();
  });
}

// ─── Mode ────────────────────────────────────────────────────────────────
const MODES = ["station", "record", "alignment", "jobs", "system", "docs"];
// Mode names of versions before 1.0, still found in bookmarks (URL hash) and in the browser's storage
const OLD_MODES = { platte: "record", justage: "alignment", doku: "docs" };
function initialMode() {
  const known = (m) => (MODES.includes(m) ? m : OLD_MODES[m] || null);
  const h = known(location.hash.replace("#", ""));
  if (h) return h;
  try { const m = known(localStorage.getItem("vc-mode")); if (m) return m; } catch (_) {}
  return "station";
}
// Scroll to an element that a view renders asynchronously (e.g. a settings section after loadSystem)
function scrollWhenReady(id, tries = 30) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  else if (tries > 0) setTimeout(() => scrollWhenReady(id, tries - 1), 100);
}

function setMode(m) {
  state.mode = m;
  document.body.classList.toggle("mode-alignment", m === "alignment");
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  for (const k of MODES) $("#main-" + k).classList.toggle("hidden", m !== k);
  $("#view").classList.add("hidden");
  $("#btn-monitor .lbl").textContent = m === "alignment" ? T("live.btn_live_measure") : T("live.btn_check_level");
  try { localStorage.setItem("vc-mode", m); } catch (_) {}
  if (location.hash !== "#" + m) history.replaceState(null, "", "#" + m);
  if (m === "alignment") loadSetups().then(() => { if (!state.setup && state.profileId) return openProfile(state.profileId); }).catch((e) => toast(e.message));
  if (m === "station") loadStation().catch((e) => toast(e.message));
  if (m === "system") loadSystem().catch((e) => toast(e.message));
  if (m === "jobs") loadJobs().catch((e) => toast(e.message));
  if (state.status) renderLive(state.status);
}

// Duration of a job: seconds below 90 s, else m:ss min
const jobDur = (sec) => (sec == null ? "–" : sec < 90 ? `${Math.round(sec)} s` : `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")} min`);

// ─── Drop-down at the "analysis running" pill: what runs, what waits; refreshes every 2 s while open ──
let jobsPopTimer = null;
function closeJobsPop() { const p = $("#jobs-pop"); if (p) p.remove(); clearTimeout(jobsPopTimer); jobsPopTimer = null; }
async function toggleJobsPop(anchor) {
  if ($("#jobs-pop")) return closeJobsPop();
  const pop = document.createElement("div"); pop.id = "jobs-pop"; document.body.appendChild(pop);
  const place = () => { const r = anchor.getBoundingClientRect(); pop.style.top = r.bottom + 6 + "px"; pop.style.right = Math.max(8, window.innerWidth - r.right) + "px"; };
  const fill = async () => {
    if (!document.body.contains(pop)) return;
    try {
      const s = await api("/api/system"); const w = s.worker, cur = w.current;
      pop.innerHTML = `${cur ? `<div class="jp-cur"><span class="spin"></span><b>${esc(cur.label)}</b><div class="hint">${T("jobs.running_for", { dur: jobDur(cur.elapsed_s) })}${cur.duration_s ? T("jobs.recording_len", { t: fmtTime(cur.duration_s) }) : ""} · CPU ${s.load.cpu_percent == null ? "–" : Math.round(s.load.cpu_percent) + " %"}${s.load.temp_c != null ? ` · ${Math.round(s.load.temp_c)} °C` : ""}</div></div>` : `<div class="hint">${T("jobs.nothing")}</div>`}
        ${w.pending.length ? `<div class="jp-list"><div class="hint">${T("jobs.waiting_n", { n: w.pending.length })}</div>${w.pending.map((p, i) => `<div>${i + 1}. ${esc(p.label)}</div>`).join("")}</div>` : ""}
        ${w.history.length ? `<div class="jp-list"><div class="hint">${T("jobs.recent")}</div>${w.history.slice(0, 3).map((h) => `<div>${h.ok ? "✓" : "✗"} ${esc(h.label)} <small class="hint">${jobDur(h.seconds)}</small></div>`).join("")}</div>` : ""}
        <div class="row" style="margin-top:.5rem"><button class="link" data-go-jobs>${T("jobs.all_details")}</button></div>`;
      pop.querySelector("[data-go-jobs]").onclick = () => { closeJobsPop(); setMode("jobs"); };
      place();
      if (!cur && !w.pending.length) { jobsPopTimer = setTimeout(closeJobsPop, 4000); return; }
    } catch (e) { pop.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
    jobsPopTimer = setTimeout(fill, 2000);
  };
  pop.innerHTML = `<div class="hint">${T("jobs.loading")}</div>`; place(); await fill();
}

// ─── "System" tab: jobs and load of the device, refreshed every 2 s while the tab is open ─────────
const jobsHist = [];                                           // CPU history of the last 5 min (kept in the browser)
async function loadJobs() {
  const s = await api("/api/system");
  if (state.mode !== "jobs") return;
  const L = s.load, w = s.worker, cur = w.current;
  if (L.cpu_percent != null) { jobsHist.push({ t: Date.now(), v: L.cpu_percent }); while (jobsHist.length > 150) jobsHist.shift(); }
  const up = L.uptime_s == null ? "–" : L.uptime_s < 86400 ? `${Math.floor(L.uptime_s / 3600)} h ${Math.floor((L.uptime_s % 3600) / 60)} min` : T("jobs.days", { n: Math.floor(L.uptime_s / 86400) });
  const cpuCls = L.cpu_percent == null ? "" : L.cpu_percent > 85 ? "bad" : L.cpu_percent > 50 ? "warn" : "ok";
  const tCls = L.temp_c == null ? "" : L.temp_c > 80 ? "bad" : L.temp_c > 70 ? "warn" : "ok";
  // Rough progress: an analysis takes about a third of the recording's duration (at least 30 s)
  const share = cur?.duration_s && cur.kind === "analyze" ? Math.min(99, Math.round(cur.elapsed_s / Math.max(cur.duration_s * 0.35, 30) * 100)) : null;
  const svc = T("jobs.service_since", { version: s.service.version, date: s.service.started_at ? I18N.date(s.service.started_at) : "–" })
    + (s.service.second_opinion ? T("jobs.second_opinion_on") : T("jobs.without_pytorch"));
  $("#jobs").innerHTML = `
    <h2>${ico("clock")} ${T("jobs.title")}</h2>
    <div class="cards">
      <div class="stat"><span>CPU</span><b class="${cpuCls}">${L.cpu_percent == null ? "–" : num(L.cpu_percent, 0) + " %"}</b><span>${T("jobs.cores", { n: L.cores })}${L.per_core ? " · " + L.per_core.map((v) => (v == null ? "–" : Math.round(v))).join(" / ") + " %" : ""}</span></div>
      <div class="stat"><span>${T("jobs.load")}</span><b>${L.load ? L.load.map((v) => num(v, 2)).join(" / ") : "–"}</b><span>${T("jobs.load_full", { n: L.cores })}</span></div>
      <div class="stat"><span>${T("jobs.memory")}</span><b>${L.mem_used_mb == null ? "–" : num(L.mem_used_mb / 1024, 1) + " GB"}</b><span>${T("jobs.memory_sub", { total: L.mem_total_mb == null ? "–" : num(L.mem_total_mb / 1024, 1), rss: L.rss_mb == null ? "–" : num(L.rss_mb / 1024, 2) })}</span></div>
      <div class="stat"><span>${T("jobs.temperature")}</span><b class="${tCls}">${L.temp_c == null ? "–" : num(L.temp_c, 0) + " °C"}</b><span>${T("jobs.throttle")}</span></div>
      <div class="stat"><span>${T("jobs.uptime")}</span><b>${up}</b><span>${svc}</span></div>
      <div class="stat"><span>${T("jobs.storage")}</span><b>${T("jobs.free", { size: fmtBytes(s.storage.free_gb) })}</b><span>${s.recorder.active ? T("jobs.recording_active", { mode: s.recorder.mode, t: fmtTime(s.recorder.elapsed_s || 0) }) : T("jobs.no_recording")}</span></div>
    </div>
    <canvas id="cpu-hist" class="chart small"></canvas>
    <p class="hint">${T("jobs.cpu_hint")}</p>
    <h3>${T("jobs.jobs")}</h3>
    ${cur ? `<div class="warnbox">${T("jobs.running", { label: esc(cur.label), dur: jobDur(cur.elapsed_s) })}${share != null ? T("jobs.about_pct", { pct: share }) : ""}${cur.duration_s ? T("jobs.recording_long", { t: fmtTime(cur.duration_s) }) : ""}</div>` : `<p class="hint">${T("jobs.nothing")}</p>`}
    ${w.pending.length ? `<table><tr><th>#</th><th>${T("jobs.col_waiting")}</th></tr>${w.pending.map((p, i) => `<tr><td>${i + 1}</td><td>${esc(p.label)}</td></tr>`).join("")}</table>` : `<p class="hint">${T("jobs.queue_empty")}</p>`}
    ${w.history.length ? `<h3>${T("jobs.recently_done")}</h3><div class="scroll"><table><tr><th>${T("jobs.col_when")}</th><th>${T("jobs.col_job")}</th><th class="num">${T("jobs.col_duration")}</th><th></th></tr>
      ${w.history.map((h) => `<tr><td>${I18N.date(h.finished_at, { timeStyle: "short" })}</td><td>${esc(h.label)}</td><td class="num">${jobDur(h.seconds)}</td><td>${h.ok ? `<span class="ok">✓</span>` : `<span class="bad" title="${esc(h.error || "")}">${T("jobs.error")}</span>`}</td></tr>`).join("")}</table></div>` : ""}`;
  drawCpuHist($("#cpu-hist"));
  clearTimeout(loadJobs.timer); loadJobs.timer = setTimeout(() => { if (state.mode === "jobs") loadJobs().catch(() => {}); }, 2000);
}
function drawCpuHist(c) {
  if (!c) return;
  const { g, w, h } = setupCanvas(c);
  if (jobsHist.length < 2) { g.fillStyle = css("--muted"); g.font = "11px system-ui"; g.fillText(T("jobs.history_building"), 6, 14); return; }
  const t0 = Date.now() - 300000, x = (t) => Math.max(0, (t - t0) / 300000) * w, y = (v) => h - 4 - (v / 100) * (h - 18);
  g.strokeStyle = css("--line"); g.beginPath(); for (const p of [25, 50, 75, 100]) { g.moveTo(0, y(p)); g.lineTo(w, y(p)); } g.stroke();
  g.fillStyle = css("--muted"); g.font = "11px system-ui"; for (const p of [50, 100]) g.fillText(p + " %", 2, y(p) - 2);
  g.strokeStyle = css("--accent"); g.lineWidth = 1.5; g.beginPath();
  jobsHist.forEach((p, i) => (i ? g.lineTo(x(p.t), y(p.v)) : g.moveTo(x(p.t), y(p.v)))); g.stroke();
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-mode]"); if (b) setMode(b.dataset.mode); });
miniPlayer.init();

// ─── 31-band analyzer (both modes, during recording and level check) ──────────
const RTA_HZ = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
  1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000];
const rta = { seq: null, peak: null, cur: null };
function renderRta(s) {
  const r = s.recorder, c = $("#rta");
  c.classList.toggle("hidden", !r.active);
  if (!r.active) { rta.peak = rta.cur = null; return; }
  const L = r.live;
  if (L?.rta_db && L.seq !== rta.seq) {
    rta.seq = L.seq;
    // RTA ballistics: instant rise, fall ≈ 15 dB/s (3 dB per update at 5/s); without this every bar jumps to the
    // value of the 0.5 s window every 0.2 s and the display flickers
    rta.cur = L.rta_db.map((ch, i) => ch.map((v, k) => { const d = rta.cur?.[i]?.[k]; return d === undefined || v >= d ? v : Math.max(v, d - 3); }));
    // Peak hold, falling slowly (≈ 6 dB/s)
    rta.peak = L.rta_db.map((ch, i) => ch.map((v, k) => Math.max(v, (rta.peak?.[i]?.[k] ?? -120) - 1.2)));
  }
  drawRta(c, rta.cur, rta.peak);
}
function drawRta(c, cur, peak) {
  const { g, w, h } = setupCanvas(c);
  const pad = { l: 40, r: 8, t: 18, b: 20 }, y0 = -90, y1 = 0;
  const Y = (v) => pad.t + (1 - (Math.min(Math.max(v, y0), y1) - y0) / (y1 - y0)) * (h - pad.t - pad.b);
  const n = RTA_HZ.length, cw = (w - pad.l - pad.r) / n;
  g.font = "11px system-ui"; g.strokeStyle = css("--line"); g.fillStyle = css("--muted"); g.lineWidth = 1;
  for (let v = y1; v >= y0; v -= 20) { g.beginPath(); g.moveTo(pad.l, Y(v)); g.lineTo(w - pad.r, Y(v)); g.stroke(); g.fillText(`${v} dB`, 2, Y(v) + 4); }
  RTA_HZ.forEach((f, k) => { if ([20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].includes(f)) g.fillText(fmtTick(f), pad.l + k * cw + 1, h - 5); });
  if (!cur) { g.fillText(T("live.rta_waiting"), pad.l + 8, h / 2); return; }
  const cols = [css("--left"), css("--right")], nch = cur.length, bw = Math.max(1, (cw - 2) / nch);
  for (let i = 0; i < nch; i++) {
    for (let k = 0; k < n; k++) {
      const x = pad.l + k * cw + 1 + i * bw;
      g.fillStyle = cols[i]; g.globalAlpha = 0.85;
      g.fillRect(x, Y(cur[i][k]), bw - 1, Y(y0) - Y(cur[i][k]));
      g.globalAlpha = 1;
      if (peak) { g.fillRect(x, Y(peak[i][k]) - 1, bw - 1, 2); }
    }
  }
  let lx = pad.l;
  [T("live.left"), T("live.right")].slice(0, nch).forEach((lab, i) => { g.fillStyle = cols[i]; g.fillRect(lx, 6, 12, 3); g.fillStyle = css("--ink"); g.fillText(lab, lx + 16, 11); lx += 28 + g.measureText(lab).width; });
  g.fillStyle = css("--muted"); g.fillText(T("live.rta_legend"), lx + 6, 11);
}

// ─── Live meter (turntable check) ─────────────────────────────────────────
const live = { session: null, hist: [], fr: [], seq: null, lastTone: null };
const TONE_HOLD_S = 2;   // show "no test tone" only after 2 s without a tone, otherwise the display flickers in short gaps
const thdCls = (v) => (v > 3 ? "bad" : v > 1 ? "warn" : "ok");
function renderLive(s) {
  const r = s.recorder, box = $("#live");
  const show = state.mode === "alignment" && !!r.active;
  box.classList.toggle("hidden", !show);
  if (!show) return;
  if (live.session !== r.started) { live.session = r.started; live.hist = []; live.fr = []; live.seq = null; live.lastTone = null; }
  const raw = r.live;
  if (raw?.tonal) live.lastTone = raw;
  // Display: bridge short gaps without a tone; histories: always the real measurements (raw)
  const hold = raw && !raw.error && !raw.tonal && live.lastTone && raw.t - live.lastTone.t < TONE_HOLD_S;
  const L = hold ? { ...live.lastTone, seq: raw.seq, t: raw.t } : raw;
  if (raw && raw.seq !== undefined && raw.seq !== live.seq) {
    live.seq = raw.seq;
    // [t, THD L, THD R (harmonics, without hum/noise), test tone frequency, level L, level R]; level = RMS of the
    // recording, also without a test tone. The frequency also for a moving tone (sweep: fundamental share < 0.7 but
    // clear), not for noise/music
    const rms = r.level?.rms_dbfs || [], f = raw.tonal || (raw.share ?? 0) >= 0.3 ? raw.freq_hz : null;
    live.hist.push(raw.tonal ? [raw.t, raw.thd_pct[0], raw.thd_pct[1] ?? null, f, rms[0] ?? null, rms[1] ?? null]
                             : [raw.t, null, null, f ?? null, rms[0] ?? null, rms[1] ?? null]);
    while (live.hist.length && live.hist[0][0] < raw.t - 60) live.hist.shift();
    // Live frequency response: test tone level over its frequency for the whole measurement (sweeps draw the curve)
    if (f && raw.level_db) {
      live.fr.push([f, raw.level_db[0], raw.level_db[1] ?? null]);
      if (live.fr.length > 4000) live.fr.shift();
    }
  }
  let html;
  if (!L || L.error) html = `<p class="hint">${L?.error ? T("live.meter_error", { error: esc(L.error) }) : T("live.waiting")}</p>`;
  else if (!L.tonal) html = `<p class="hint">${T("live.no_tone")}</p>`;
  else {
    const two = L.level_db.length > 1;
    const thdTitle = (i) => T("live.thd_title", { pct: num(L.thdn_pct[i], 2) });
    html = `<div class="cards">
      <div class="stat"><span>${T("live.test_tone")}</span><b>${num(L.freq_hz, 0)} Hz</b><span>${MODE[L.mode] || ""}</span></div>
      <div class="stat"><span>${T("live.left_cap")}</span><b>${num(L.level_db[0])} dBFS</b><span class="st ${thdCls(L.thd_pct[0])}" title="${thdTitle(0)}">${T("live.thd", { pct: num(L.thd_pct[0], 2) })}</span></div>
      ${two ? `<div class="stat"><span>${T("live.right_cap")}</span><b>${num(L.level_db[1])} dBFS</b><span class="st ${thdCls(L.thd_pct[1])}" title="${thdTitle(1)}">${T("live.thd", { pct: num(L.thd_pct[1], 2) })}</span></div>` : ""}
      ${L.balance_db != null && !["left", "right"].includes(L.mode) ? `<div class="stat"><span>Balance L − R</span><b>${sgn(L.balance_db)} dB</b></div>` : ""}
      ${L.crosstalk_db != null ? `<div class="stat"><span>${T("live.crosstalk", { dir: L.mode === "left" ? "L → R" : "R → L" })}</span><b>${num(L.crosstalk_db)} dB</b></div>` : ""}
      ${L.mono_cancel_db != null ? `<div class="stat"><span>${T("live.mono_cancel")}</span><b>${num(L.mono_cancel_db)} dB</b></div>` : ""}
      ${levelHint(L, s.level_target)}
    </div>`;
  }
  $("#live-values").innerHTML = html;
  chart($("#live-fr"), [
    { points: live.fr.map((p) => [p[0], p[1]]), color: css("--left"), label: T("live.left"), dots: true },
    { points: live.fr.map((p) => [p[0], p[2]]), color: css("--right"), label: T("live.right"), dots: true },
  ], { xlog: true, xmin: 10, xmax: 20000, xunit: " Hz", ymin: -70, ymax: 0, yunit: " dB",
       empty: T("live.fr_empty") });
  const tEnd = live.hist.length ? live.hist[live.hist.length - 1][0] : 0;
  const win = { xunit: " s", xmin: Math.max(0, tEnd - 60), xmax: Math.max(60, tEnd) };
  chart($("#live-freq"), [
    { points: live.hist.map((h) => [h[0], h[3]]), color: css("--ink"), label: T("live.tone_freq") },
  ], { ...win, ylog: true, ymin: 10, ymax: 20000, yunit: " Hz", empty: T("live.tone_freq_empty") });
  chart($("#live-level"), [
    { points: live.hist.map((h) => [h[0], h[4]]), color: css("--left"), label: T("live.level_left") },
    { points: live.hist.map((h) => [h[0], h[5]]), color: css("--right"), label: T("live.level_right") },
  ], { ...win, ymin: -90, ymax: 0, yunit: " dB", hlines: [{ y: -1, color: css("--bad"), label: "−1 dBFS" }],
       empty: T("live.level_empty") });
  chart($("#live-chart"), [
    { points: live.hist.map((h) => [h[0], h[1]]), color: css("--left"), label: T("live.thd_left") },
    { points: live.hist.map((h) => [h[0], h[2]]), color: css("--right"), label: T("live.thd_right") },
  ], { ...win, ylog: true, ymin: 0.05, ymax: 50, yunit: " %", hlines: [{ y: 3, color: css("--bad"), label: "3 %" }],
       empty: T("live.history_empty") });
}
function levelHint(L, lt) {
  // Only on the 1 kHz reference tone (lateral, near the target level; not the −20 dB band)
  if (!lt || L.mode !== "lateral" || Math.abs(L.freq_hz / 1000 - 1) > 0.05) return "";
  const lv = Math.max(...L.level_db), d = lv - lt.ref_dbfs;
  if (d < -12) return "";
  const peak = lv + lt.headroom_db, ok = Math.abs(d) <= lt.tol_db && peak < -1;
  const last = lt.last ? T("live.level_last_run", { level: num(lt.last.ref_level_dbfs), delta: sgn(lv - lt.last.ref_level_dbfs) }) : "";
  return `<div class="stat wide"><span>${T("live.level_setting")}</span><b class="st ${ok ? "ok" : "warn"}">${ok ? T("live.level_ok") : peak >= -1 || d > 0 ? T("live.level_too_loud") : T("live.level_too_quiet")}</b>
    <span>${T("live.level_target", { target: num(lt.ref_dbfs, 0), tol: num(lt.tol_db, 0), headroom: num(lt.headroom_db, 0), peak: num(peak) })}${last}</span></div>`;
}

// ─── Charts ──────────────────────────────────────────────────────────────
function ticks(a, b, log) {
  if (log) {
    const out = [];
    for (let e = Math.floor(Math.log10(a)); e <= Math.ceil(Math.log10(b)); e++) for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= a * 0.999 && v <= b * 1.001) out.push(v); }
    return out.length > 8 ? out.filter((v) => Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9) : out;
  }
  const span = b - a || 1, base = 10 ** Math.floor(Math.log10(span / 5));
  const step = [1, 2, 5, 10].map((m) => m * base).find((st) => span / st <= 6) || span / 5;
  const out = [];
  for (let v = Math.ceil(a / step) * step; v <= b + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
// Axis labels: three significant digits in the reader's number format, thousands as "k" (12.5k)
const tickFmt = new Intl.NumberFormat(I18N.lang, { maximumSignificantDigits: 3, useGrouping: false });
const fmtTick = (v) => (Math.abs(v) >= 1000 ? tickFmt.format(v / 1000) + "k" : tickFmt.format(v));
function chart(c, series, o = {}) {
  const { g, w, h } = setupCanvas(c);
  const pad = { l: 46, r: 10, t: 22, b: 22 };
  const pts = series.flatMap((s) => s.points.filter((p) => p[1] != null && isFinite(p[1]) && (!o.xlog || p[0] > 0)));
  g.font = "11px system-ui";
  if (!pts.length) { g.fillStyle = css("--muted"); g.fillText(o.empty || T("charts.no_data"), pad.l, h / 2); return; }
  const tx = (v) => (o.xlog ? Math.log10(v) : v), ty = (v) => (o.ylog ? Math.log10(Math.max(v, 1e-9)) : v);
  const x0 = o.xmin ?? Math.min(...pts.map((p) => p[0])), x1 = o.xmax ?? Math.max(...pts.map((p) => p[0]));
  let y0 = o.ymin ?? Math.min(...pts.map((p) => p[1])), y1 = o.ymax ?? Math.max(...pts.map((p) => p[1]));
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const X = (v) => pad.l + ((tx(v) - tx(x0)) / (tx(x1) - tx(x0) || 1)) * (w - pad.l - pad.r);
  const Y = (v) => h - pad.b - ((ty(Math.min(Math.max(v, y0), y1)) - ty(y0)) / (ty(y1) - ty(y0))) * (h - pad.t - pad.b);
  for (const [a, b, col] of o.bands || []) { g.fillStyle = col; g.fillRect(X(a), pad.t, X(b) - X(a), h - pad.t - pad.b); }
  g.strokeStyle = css("--line"); g.fillStyle = css("--muted"); g.lineWidth = 1;
  for (const v of ticks(x0, x1, o.xlog)) {
    const px = X(v), lab = fmtTick(v) + (o.xunit || "");
    g.beginPath(); g.moveTo(px, pad.t); g.lineTo(px, h - pad.b); g.stroke();
    if (px + 2 + g.measureText(lab).width < w) g.fillText(lab, px + 2, h - 7);
  }
  for (const v of ticks(y0, y1, o.ylog)) { const py = Y(v); g.beginPath(); g.moveTo(pad.l, py); g.lineTo(w - pad.r, py); g.stroke(); g.fillText(fmtTick(v) + (o.yunit || ""), 2, py + 4); }
  for (const l of o.hlines || []) {
    g.strokeStyle = l.color; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(pad.l, Y(l.y)); g.lineTo(w - pad.r, Y(l.y)); g.stroke(); g.setLineDash([]);
    if (l.label) { g.fillStyle = l.color; g.fillText(l.label, w - pad.r - g.measureText(l.label).width - 2, Y(l.y) - 3); }
  }
  for (const l of o.vlines || []) {
    g.strokeStyle = l.color; g.setLineDash([4, 3]); g.beginPath(); g.moveTo(X(l.x), pad.t); g.lineTo(X(l.x), h - pad.b); g.stroke(); g.setLineDash([]);
    if (l.label) { g.fillStyle = l.color; g.fillText(l.label, X(l.x) + 3, pad.t + 22); }
  }
  for (const s of series) {
    if (s.dots) {   // scatter instead of a line (e.g. live frequency response: points do not arrive sorted by frequency)
      g.fillStyle = s.color;
      for (const [x, y] of s.points) if (y != null && isFinite(y) && (!o.xlog || x > 0)) g.fillRect(X(x) - 1.5, Y(y) - 1.5, 3, 3);
      continue;
    }
    g.strokeStyle = s.color; g.lineWidth = s.width || 1.6; g.setLineDash(s.dash || []); g.beginPath();
    let on = false;
    for (const [x, y] of s.points) {
      if (y == null || !isFinite(y) || (o.xlog && x <= 0)) { on = false; continue; }
      const px = X(x), py = Y(y); on ? g.lineTo(px, py) : g.moveTo(px, py); on = true;
    }
    g.stroke(); g.setLineDash([]);
  }
  let lx = pad.l;
  for (const s of series) if (s.label) { g.fillStyle = s.color; g.fillRect(lx, 8, 12, 3); g.fillStyle = css("--ink"); g.fillText(s.label, lx + 16, 13); lx += 28 + g.measureText(s.label).width; }
}

// ─── Alignment runs ──────────────────────────────────────────────────────
const fmtG = (v) => (v == null ? "–" : num(v, 2) + " g");
const chips = (c) => (!c ? "" : `<span class="chips">${c.bad ? `<i class="st bad" title="${T("alignment.st_bad")}">${c.bad}</i>` : ""}${c.warn ? `<i class="st warn" title="${T("alignment.st_warn")}">${c.warn}</i>` : ""}${c.ok ? `<i class="st ok" title="${T("alignment.st_ok_long")}">${c.ok}</i>` : ""}</span>`);
const STLABEL = { ok: T("alignment.st_ok"), warn: T("alignment.st_warn"), bad: T("alignment.st_bad"), info: T("alignment.st_info") };

async function loadSetups() {
  await loadProfiles();
  const pid = state.profileId;
  const list = await api(pid ? `/api/setups?profile_id=${pid}` : "/api/setups");
  const prof = state.profiles?.find((p) => p.id === pid);
  $("#runs-head").textContent = prof ? T("alignment.runs_of", { name: prof.name }) : T("alignment.runs");
  state.setups = list;
  $("#setups").innerHTML = list.map((s) => `
    <li data-id="${s.id}" class="${state.setup?.id === s.id ? "sel" : ""}">
      <input type="checkbox" class="cmp" data-id="${s.id}" ${state.setupSel.has(s.id) ? "checked" : ""} title="${T("alignment.mark_compare")}">
      <span class="grow">${esc(s.label || T("alignment.run_short", { id: s.id }))}<br><small>${T("alignment.run_meta", { date: dateStr(s.created_at), force: fmtG(s.tracking_force_g), bias: esc(s.bias || "–"),
        hours: s.needle_hours != null ? " · " + num(s.needle_hours, 0) + " h" : "", n: s.recordings })}</small></span>
      ${chips(s.status_counts)}
    </li>`).join("") || `<li class="empty">${T("alignment.no_runs")}</li>`;
  $("#btn-cmp-setups").disabled = state.setupSel.size !== 2;
  const f = $("#new-setup"), last = list[0];
  if (last && !f.dataset.filled) {
    f.dataset.filled = "1";
    $("#ns-force").value = last.tracking_force_g ?? ""; $("#ns-bias").value = last.bias || ""; $("#ns-vta").value = last.vta || "";
    $("#ns-hours").value = last.needle_hours ?? ""; $("#ns-input").value = last.input_level || "";
  }
}
$("#setups").addEventListener("click", guard(async (e) => {
  if (e.target.classList.contains("cmp")) {
    const id = +e.target.dataset.id;
    e.target.checked ? state.setupSel.add(id) : state.setupSel.delete(id);
    $("#btn-cmp-setups").disabled = state.setupSel.size !== 2;
    return;
  }
  const li = e.target.closest("li[data-id]"); if (li) await openSetup(+li.dataset.id);
}));
$("#new-setup").addEventListener("submit", guard(async (e) => {
  e.preventDefault();
  const n = (v) => (v === "" ? null : +v);
  const body = { tracking_force_g: n($("#ns-force").value), bias: $("#ns-bias").value.trim(), vta: $("#ns-vta").value.trim(),
    needle_hours: n($("#ns-hours").value), input_level: $("#ns-input").value.trim(), label: $("#ns-label").value.trim(),
    profile_id: state.profileId ?? null };
  const { id } = await api("/api/setups", { method: "POST", body });
  $("#ns-label").value = "";
  await loadSetups(); await openSetup(id);
}));
$("#btn-cmp-setups").onclick = guard(async () => { const [a, b] = [...state.setupSel].sort((x, y) => x - y); await showSetupComparison(a, b); });

async function openSetup(id) { state.setup = await api(`/api/setups/${id}`); renderSetup(); loadSetups(); }

// ─── Turntable profiles and setup wizard ───────────────────────────────────
async function loadProfiles() {
  const list = await api("/api/profiles");
  state.profiles = list;
  if (!state.profileId || !list.some((p) => p.id === state.profileId)) state.profileId = (list.find((p) => p.active) || list[0])?.id ?? null;
  $("#profiles").innerHTML = list.map((p) => `
    <li data-pid="${p.id}" class="${p.id === state.profileId && !state.setup ? "sel" : ""}">
      <span class="grow"><b>${esc(p.name)}</b>${p.active ? ` <span class="st ok">${T("profiles.active")}</span>` : ""}<br>
      <small>${esc(p.cartridge || T("profiles.no_cartridge"))} · ${p.progress.next === "done" ? T("profiles.measured_on", { date: dateStr(p.accepted_at) }) : T("profiles.pending")}${p.connected ? "" : T("profiles.not_connected_suffix")}</small></span>
    </li>`).join("") || `<li class="empty">${T("profiles.none")}</li>`;
}
$("#profiles").addEventListener("click", guard(async (e) => {
  const li = e.target.closest("li[data-pid]"); if (!li) return;
  state.profileId = +li.dataset.pid; state.setup = null; state.setupSel.clear(); $("#view").classList.add("hidden");
  await loadSetups(); await openProfile(state.profileId);
}));
$("#btn-new-profile").onclick = () => { state.setup = null; renderProfileForm(null); };

async function openProfile(id) { state.profile = await api(`/api/profiles/${id}`); renderProfile(); }
async function refreshProfile() {
  if (!state.profileId || state.setup) return;
  try { state.profile = await api(`/api/profiles/${state.profileId}`); renderProfile(); loadProfiles(); } catch (_) {}
}

// Recording devices (/api/devices): `device` is the value to store; older servers only sent the ALSA name. ALSA entries
// show their hw: name, PortAudio entries (macOS, Windows) the host API, the input channels and the default rate.
const deviceId = (d) => d.device ?? d.alsa;
const deviceLabel = (d) => d.hostapi
  ? `${esc(d.name)} (${T("audio.device_detail", { hostapi: esc(d.hostapi), n: d.channels, rate: num(d.default_rate / 1000, 1) })})`
  : `${esc(d.name)} (${esc(deviceId(d))})`;

function renderProfileForm(p) {
  const v = (k) => esc(p?.[k] ?? "");
  $("#setup-detail").innerHTML = `
    <h2>${ico("tonearm")} ${p ? T("profiles.edit_title") : T("profiles.add_title")}</h2>
    <p class="hint">${T("profiles.form_hint")}</p>
    <form id="profile-form" class="setup-form">
      <div class="wide"><label for="pf-name">${T("profiles.name")}</label><input id="pf-name" name="name" required value="${v("name")}" placeholder="${T("profiles.name_placeholder")}"></div>
      <div><label for="pf-cart">${T("profiles.cartridge")}</label><input id="pf-cart" name="cartridge" value="${v("cartridge")}" placeholder="${T("profiles.cartridge_placeholder")}"></div>
      <div><label for="pf-dev">${T("profiles.device")}</label><select id="pf-dev" name="device"><option value="">${T("profiles.device_searching")}</option></select></div>
      <div><label for="pf-force">${T("profiles.force")}</label><input id="pf-force" name="tracking_force_g" type="number" step="0.01" min="0" value="${v("tracking_force_g")}"></div>
      <div><label for="pf-anti">${T("profiles.antiskating")}</label><input id="pf-anti" name="antiskating" value="${v("antiskating")}" placeholder="${T("profiles.antiskating_placeholder")}"></div>
      <div><label for="pf-weight">${T("profiles.weight")}</label><input id="pf-weight" name="weight" value="${v("weight")}" placeholder="${T("profiles.weight_placeholder")}"></div>
      <div class="wide"><label><input type="checkbox" id="pf-fixed" ${p?.fixed_gain ? "checked" : ""}> ${T("profiles.fixed_gain")}</label>
        <div class="hint">${T("profiles.fixed_gain_hint")}</div></div>
      <div class="wide"><label for="pf-map">${T("profiles.targets")}</label><select id="pf-map" name="target_map"><option value="">${T("profiles.targets_default")}</option></select>
        <div class="hint">${T("profiles.targets_hint")}</div></div>
      <div class="wide"><label for="pf-notes">${T("profiles.notes")}</label><input id="pf-notes" name="notes" value="${v("notes")}"></div>
      <div class="wide row"><button type="submit">${ico("ok", 18)} ${p ? T("profiles.save") : T("profiles.create")}</button><button type="button" class="secondary" id="pf-cancel">${T("profiles.cancel")}</button></div>
    </form>`;
  api("/api/targets").then((t) => {
    const sel = $("#pf-map"); if (!sel) return;
    sel.innerHTML = `<option value="">${T("profiles.targets_default_named", { name: esc(t.maps.find((m) => m.id === t.active)?.name || t.active) })}</option>`
      + t.maps.map((m) => `<option value="${esc(m.id)}" ${p?.target_map === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("");
  }).catch(() => {});
  api("/api/devices").then((devs) => {
    const sel = $("#pf-dev"); if (!sel) return;
    const cur = p?.device || "";
    const opts = devs.map((d) => `<option value="${esc(deviceId(d))}" ${deviceId(d) === cur ? "selected" : ""}>${deviceLabel(d)}</option>`);
    if (cur && !devs.some((d) => deviceId(d) === cur)) opts.unshift(`<option value="${esc(cur)}" selected>${T("profiles.device_not_connected", { device: esc(cur) })}</option>`);
    sel.innerHTML = opts.join("") || `<option value="">${T("profiles.no_device")}</option>`;
  }).catch(() => {});
  $("#pf-cancel").onclick = () => (p ? renderProfile() : ($("#setup-detail").innerHTML = `<p class="empty">${T("profiles.select_or_add")}</p>`));
  $("#profile-form").onsubmit = guard(async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target), body = {};
    for (const [k, val] of fd.entries()) body[k] = k === "tracking_force_g" ? (val === "" ? null : +val) : String(val).trim();
    if (p && body.target_map === "") body.target_map = null;
    body.fixed_gain = $("#pf-fixed").checked;
    const out = p ? await api(`/api/profiles/${p.id}`, { method: "PATCH", body }) : await api("/api/profiles", { method: "POST", body });
    state.profileId = out.id; state.profile = out; toast(p ? T("profiles.saved") : T("profiles.added")); await loadSetups(); renderProfile();
  });
}

const STEPS = [
  ["profile", T("profiles.step_profile")], ["calibration", T("profiles.step_calibration")], ["side1", T("profiles.step_side1")],
  ["side2", T("profiles.step_side2")], ["accept", T("profiles.step_accept")]];
function stepState(pr, key) {
  const order = STEPS.map((x) => x[0]), cur = pr.next === "done" ? 99 : order.indexOf(pr.next), i = order.indexOf(key);
  const rec = pr[key];
  if (key === "side1" || key === "side2") {
    if (rec?.status === "error") return "bad";
    if (rec && rec.status !== "analyzed") return "run";
  }
  return i < cur ? "ok" : i === cur ? "cur" : "todo";
}
function renderProfile() {
  const p = state.profile; if (!p) return;
  const pr = p.progress, st = state.status, busy = !!st?.recorder?.active, mine = st?.recorder?.meta?.profile_id === p.id;
  const dis = busy || !st?.storage?.ok ? "disabled" : "";
  const b = p.baseline, sum = b?.summary || {};
  const stepRow = ([key, label], i) => {
    const s = stepState(pr, key);
    const icon = s === "ok" ? ico("ok", 18) : s === "bad" ? ico("bad", 18) : s === "run" ? ico("rec", 18) : `<span class="stepno">${i + 1}</span>`;
    return `<li class="step ${s}">${icon}<span>${label}</span></li>`;
  };
  let action = "";
  const recInfo = (k) => pr[k]?.status === "error" ? `<div class="warnbox">${T("profiles.last_failed", { error: esc(pr[k].error || "") })}</div>` : pr[k] && pr[k].status !== "analyzed" ? `<p class="hint">${ico("clock", 16)} ${STATUS[pr[k].status] || pr[k].status} …</p>` : "";
  if (pr.next === "calibration") action = `
    <h3>${T("profiles.cal_title")}</h3>
    <p>${T("profiles.cal_text")}</p>
    <button class="btn-rec" data-wiz="calibration" ${dis}>${ico("calibrate")} ${T("profiles.cal_start")}</button>`;
  else if (pr.next === "side1" || pr.next === "side2") {
    const n = pr.next === "side1" ? 1 : 2;
    action = `
    <h3>${T("profiles.side_title", { step: n + 2, n })}</h3>
    <p>${T("profiles.side_put", { n })} ${n === 1 ? T("profiles.side1_content") : T("profiles.side2_content")}
      ${T("profiles.side_go")}</p>
    ${recInfo(pr.next)}
    <button class="btn-rec" data-wiz="${pr.next}" ${dis}>${ico("rec")} ${T("profiles.side_record", { n })}</button>`;
  } else if (pr.next === "accept") action = `
    <h3>${T("profiles.accept_title")}</h3>
    <p>${T("profiles.accept_text")}</p>
    <div class="row"><button class="btn-rec" data-wiz="accept" ${busy ? "disabled" : ""}>${ico("ok")} ${T("profiles.step_accept")}</button>
      <button class="secondary" data-wiz="side1" ${dis}>${T("profiles.redo_side", { n: 1 })}</button><button class="secondary" data-wiz="side2" ${dis}>${T("profiles.redo_side", { n: 2 })}</button></div>
    <div id="wiz-results"></div>`;
  const trims = p.accepted_at ? `
    <h3>${T("profiles.trims_title")}</h3>
    <table class="kv"><tbody>
      <tr><th scope="row">${T("profiles.ref_level")}</th><td>${num(p.ref_dbfs)} dBFS</td><td class="hint">${T("profiles.ref_level_hint")}</td></tr>
      <tr><th scope="row">${T("profiles.speed")}</th><td>${p.speed_pct == null ? "–" : sgn(p.speed_pct, 2) + " %"}</td><td class="hint">${T("profiles.speed_hint")}</td></tr>
      <tr><th scope="row">${T("profiles.floor")}</th><td>${p.floor_clicks_per_min == null ? T("profiles.not_measured") : T("profiles.floor_value", { clicks: num(p.floor_clicks_per_min, 0), ticks: num(p.floor_ticks_per_min, 0) })}</td><td class="hint">${T("profiles.floor_hint")}${p.floor_clicks_per_min == null ? T("profiles.floor_missing") : ""}</td></tr>
      <tr><th scope="row">${T("profiles.sensitivity")}</th><td><select data-prof-sens="${p.id}">${[0.3, 0.4, 0.5, 0.6, 0.7].map((s) => `<option value="${s}" ${Math.abs(s - (p.sensitivity ?? 0.5)) < 0.01 ? "selected" : ""}>${I18N.num(s, 1)}${s === 0.5 ? T("profiles.sensitivity_default") : ""}</option>`).join("")}</select></td><td class="hint">${T("profiles.sensitivity_hint")}</td></tr>
      <tr><th scope="row">${T("profiles.chain_noise")}</th><td>${p.chain_db == null ? T("profiles.not_calibrated") : num(p.chain_db) + " dBFS"}</td><td class="hint">${T("profiles.chain_noise_hint")}</td></tr>
      <tr><th scope="row">${T("profiles.device")}</th><td class="mono">${esc(p.device || T("profiles.automatic"))}</td><td class="hint">${p.connected ? T("profiles.connected") : T("profiles.not_connected")}</td></tr>
    </tbody></table>
    ${b?.results?.length ? `<h3>${T("profiles.baseline_findings")}</h3>${resultsTable(b.results.map((r) => ({ ...r, text: "" })))}` : ""}` : "";
  $("#setup-detail").innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:start">
      <div><h2 style="margin:0">${ico("tonearm")} ${esc(p.name)}</h2>
        <div class="hint">${T("profiles.summary", { cartridge: esc(p.cartridge || T("profiles.no_cartridge")), force: fmtG(p.tracking_force_g), anti: esc(p.antiskating || "–"), weight: esc(p.weight || "–") })}<br>${T("profiles.targets_used", { name: esc(p.targets_used?.name || T("profiles.targets_standard")) })}${p.targets_used?.own ? "" : T("profiles.targets_global")}</div></div>
      <div class="row" style="flex:0 0 auto">${p.active ? `<span class="st ok">${ico("ok", 14)} ${T("profiles.active")}</span>` : `<button data-wiz="activate" ${busy ? "disabled" : ""}>${T("profiles.use")}</button>`}
        <button class="secondary" data-wiz="edit">${T("profiles.edit")}</button><button class="link" data-wiz="delete">${T("profiles.delete")}</button></div>
    </div>
    ${p.connected ? "" : `<div class="warnbox">${ico("offline", 16)} ${T("profiles.device_missing", { device: esc(p.device) })}</div>`}
    <h3>${T("profiles.measurement")} ${pr.next === "done" ? `<span class="st ok">${ico("ok", 14)} ${T("profiles.done_on", { date: dateStr(p.accepted_at) })}</span>` : pr.remeasure ? `<span class="st warn">${T("profiles.remeasure_running")}</span>` : ""}</h3>
    <ol class="steps">${STEPS.map(stepRow).join("")}</ol>
    ${mine ? `<div class="go">${ico("tonearm", 24)} <b>GO</b>: ${st.recorder.meta?.kind === "calibration" ? T("profiles.go_calibration") : T("profiles.go_side")}</div>` : ""}
    ${action}
    ${pr.next === "done" ? `<div class="row"><button class="secondary" data-wiz="remeasure" ${dis}>${ico("sliders", 18)} ${T("profiles.remeasure")}</button><button class="secondary" data-wiz="calibration" ${dis}>${ico("calibrate", 18)} ${T("profiles.recalibrate")}</button></div>` : ""}
    ${trims}`;
  if (pr.next === "accept" && pr.run_id) api(`/api/setups/${pr.run_id}`).then((s) => { const el = $("#wiz-results"); if (el) el.innerHTML = resultsTable(s.results); }).catch(() => {});
}
$("#setup-detail").addEventListener("click", guard(async (e) => {
  const t = e.target.closest("[data-wiz]"); if (!t || state.setup) return;
  const p = state.profile, w = t.dataset.wiz;
  if (w === "edit") return renderProfileForm(p);
  if (w === "activate") { await api(`/api/profiles/${p.id}/activate`, { method: "POST" }); toast(T("profiles.now_active", { name: p.name })); return refreshProfile(); }
  if (w === "delete") {
    if (!confirm(T("profiles.confirm_delete", { name: p.name }))) return;
    await api(`/api/profiles/${p.id}`, { method: "DELETE" }); state.profileId = null; state.profile = null;
    $("#setup-detail").innerHTML = `<p class="empty">${T("profiles.select_or_add")}</p>`; return loadSetups();
  }
  if (w === "calibration") {
    await api("/api/recordings/start", { method: "POST", body: { kind: "calibration", side: "-", profile_id: p.id, max_s: 25 } });
    toast(T("profiles.toast_calibrating")); return refreshProfile();
  }
  if (w === "side1" || w === "side2" || w === "remeasure") {
    const { id } = await api(`/api/profiles/${p.id}/run${w === "remeasure" ? "?new=true" : ""}`, { method: "POST" });
    if (w === "remeasure") { await loadSetups(); return refreshProfile(); }
    await api("/api/recordings/start", { method: "POST", body: { kind: "alignment", setup_id: id, side: w === "side1" ? "1" : "2", profile_id: p.id } });
    toast(T("profiles.toast_go")); await loadSetups(); return refreshProfile();
  }
  if (w === "accept") { state.profile = await api(`/api/profiles/${p.id}/accept`, { method: "POST", body: {} }); toast(T("profiles.toast_accepted")); renderProfile(); return loadProfiles(); }
}));
async function refreshSetup() {
  if (!state.setup || state.mode !== "alignment") return;
  try { state.setup = await api(`/api/setups/${state.setup.id}`); renderSetup(); loadSetups(); } catch (_) {}
}

// Verdict of the anti-skating ramp (a stored code) in words
const ANTISKATING = { ok: T("alignment.as_verdict_ok"), too_strong: T("alignment.as_verdict_too_strong"),
                      too_weak: T("alignment.as_verdict_too_weak"), balanced: T("alignment.as_verdict_balanced") };
function fmtVal(v, unit) {
  if (v === null || v === undefined) return "–";
  if (typeof v !== "number") return esc(ANTISKATING[v] || v);
  const d = unit === "%" ? (Math.abs(v) < 1 ? 3 : 2) : unit === "Hz" ? 1 : 1;
  return (["dB"].includes(unit) ? sgn(v, d) : num(v, d)) + (unit ? " " + unit : "");
}
function resultsTable(items) {
  if (!items?.length) return `<p class="empty">${T("alignment.no_findings")}</p>`;
  return `<div class="scroll"><table class="results"><tr><th></th><th>${T("alignment.col_metric")}</th><th class="num">${T("alignment.col_value")}</th><th>${T("alignment.col_target")}</th><th>${T("alignment.col_assessment")}</th></tr>
    ${items.map((it) => `<tr><td><span class="st ${it.status}">${STLABEL[it.status] || it.status}</span></td><td>${esc(it.label)}</td>
      <td class="num">${fmtVal(it.value, it.unit)}</td><td class="hint">${esc(it.target || "")}</td><td>${esc(it.text)}</td></tr>`).join("")}
  </table></div>`;
}
const fld = (k, label, v, type = "text", step = "") => `<div><label>${label}</label><input name="${k}" type="${type}" ${step ? `step="${step}"` : ""} value="${esc(v ?? "")}"></div>`;

function renderSetup() {
  const s = state.setup, st = state.status, recs = s.recordings;
  const dis = st?.recorder?.active || !st?.storage?.ok ? "disabled" : "";
  const done = new Set(recs.filter((r) => r.status === "analyzed").map((r) => r.side));
  $("#setup-detail").innerHTML = `
    <div class="row" style="justify-content:space-between;align-items:start">
      <div><h2 style="margin:0">${esc(s.label || T("alignment.run_n", { id: s.id }))}</h2><div class="hint">${T("alignment.run_date_record", { date: dateStr(s.created_at), record: esc(s.test_record) })}</div></div>
      <button class="link" id="del-setup">${T("alignment.delete_run")}</button>
    </div>
    <h3>${T("alignment.settings_title")}</h3>
    <form id="setup-form" class="setup-form">
      ${fld("tracking_force_g", T("alignment.force_g"), s.tracking_force_g, "number", "0.01")}${fld("bias", T("alignment.bias_anti"), s.bias)}
      ${fld("vta", T("alignment.vta_long"), s.vta)}${fld("needle_hours", T("alignment.stylus_hours"), s.needle_hours, "number", "1")}
      ${fld("input_level", T("alignment.input_level_long"), s.input_level)}${fld("label", T("alignment.label"), s.label)}
      <div class="wide">${fld("notes", T("alignment.notes"), s.notes)}</div>
      <button type="submit" class="secondary">${T("alignment.save")}</button>
    </form>
    <h3>${T("alignment.record_title")}</h3>
    <div class="row">
      <div><label>${T("alignment.side")}</label><select id="j-side"><option value="1">${T("alignment.side1_option")}</option>
        <option value="2" ${done.has("1") && !done.has("2") ? "selected" : ""}>${T("alignment.side2_option")}</option></select></div>
      <button class="btn-rec" id="btn-jrec" ${dis}>${ico("rec")} ${T("alignment.record_side")}</button>
    </div>
    <p class="hint">${T("alignment.procedure")}</p>
    <h3>${T("alignment.recordings")}</h3>
    ${recs.length ? `<div class="scroll"><table><tr><th>${T("alignment.side")}</th><th>${T("alignment.col_date")}</th><th class="num">${T("alignment.col_duration")}</th><th>${T("alignment.col_status")}</th><th class="num">${T("alignment.col_bands")}</th><th>${T("alignment.col_findings")}</th><th></th></tr>
      ${recs.map((x) => `<tr><td>${esc(x.side)}</td><td>${dateStr(x.created_at)}</td><td class="num">${fmtTime(x.duration_s)}</td>
        <td class="status-${x.status}" title="${esc(x.error || "")}">${STATUS[x.status] || x.status}${x.xruns ? ` ⚠ ${T("alignment.dropouts")}` : ""}${x.clip_in_bands ? ` ⚠ ${T("alignment.clipped")}` : x.clip_samples ? ` <small class="hint" title="${T("alignment.drop_clip_title", { n: x.clip_samples })}">${T("alignment.drop_clip")}</small>` : ""}${x.audio_deleted_at ? `<br><small class="hint">${T("alignment.audio_deleted")}</small>` : ""}${x.status === "error" && x.error ? `<br><small class="hint">${esc(x.error)}</small>` : ""}</td>
        <td class="num">${x.status === "analyzed" ? x.bands : ""}</td><td>${x.status === "analyzed" ? chips(x.status_counts) : ""}</td>
        <td style="white-space:nowrap">${x.status === "analyzed" ? `<button class="link" data-jview="${x.id}" title="${T("alignment.report_title")}">${ico("spectro", 15)} ${T("alignment.report")}</button>` : ""}${x.audio_available ? `<button class="link" data-reana="${x.id}" title="${T("alignment.reanalyze")}">↻</button>` : ""}<button class="link" data-del="${x.id}" title="${T("alignment.delete")}">✕</button></td></tr>`).join("")}
      </table></div>` : `<p class="empty">${T("alignment.no_recordings")}</p>`}
    <h3>${T("alignment.run_findings")}</h3>
    ${resultsTable(s.results)}`;
  $("#setup-form").onsubmit = guard(async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target), body = {};
    for (const [k, v] of fd.entries()) body[k] = ["tracking_force_g", "needle_hours"].includes(k) ? (v === "" ? null : +v) : v;
    state.setup = await api(`/api/setups/${s.id}`, { method: "PATCH", body }); toast(T("alignment.saved")); renderSetup(); loadSetups();
  });
  $("#btn-jrec").onclick = guard(async () => {
    askNotify();
    await api("/api/recordings/start", { method: "POST", body: { kind: "alignment", setup_id: s.id, side: $("#j-side").value } });
    toast(T("alignment.toast_recording")); refreshSetup();
  });
  $("#del-setup").onclick = guard(async () => {
    if (!confirm(T("alignment.confirm_delete_run"))) return;
    await api(`/api/setups/${s.id}`, { method: "DELETE" });
    state.setup = null; state.setupSel.delete(s.id); $("#view").classList.add("hidden");
    $("#setup-detail").innerHTML = `<p class="empty">${T("alignment.select_or_create")}</p>`; loadSetups();
  });
  $("#setup-detail").onclick = guard(async (e) => {
    const t = e.target;
    if (t.dataset.jview) return showAlignment(+t.dataset.jview);
    if (t.dataset.reana) { await api(`/api/recordings/${t.dataset.reana}/analyze`, { method: "POST" }); return refreshSetup(); }
    if (t.dataset.del) { if (confirm(T("alignment.confirm_delete_recording"))) { await api(`/api/recordings/${t.dataset.del}`, { method: "DELETE" }); refreshSetup(); } }
  });
}

function bandValue(b) {
  const m = b.m || {};
  if (m.error) return `<span class="hint">${T("alignment.error", { error: esc(m.error) })}</span>`;
  switch (b.role) {
    case "ref_1k": case "ref_1k_m20": case "hf_10k": case "lf_100": return `L ${num(m.level_db?.[0])} / R ${num(m.level_db?.[1])} dBFS`;
    case "xt_left": case "xt_right": return T("alignment.bv_crosstalk", { db: num(m.crosstalk_db) });
    case "vert_1k": return T("alignment.bv_mono_cancel", { db: num(m.mono_cancel_db) });
    case "antiskating": return m.ok ? T("alignment.bv_antiskating", { l: m.break_db[0] == null ? T("alignment.bv_clean") : sgn(m.break_db[0]) + " dB", r: m.break_db[1] == null ? T("alignment.bv_clean") : sgn(m.break_db[1]) + " dB" }) : "";
    case "imd": return `IMD L ${num(m.imd_pct?.[0], 2)} / R ${num(m.imd_pct?.[1], 2)} %`;
    case "wow_flutter": return `${sgn(m.speed_pct, 2)} % · W&F ${num(m.wf_weighted_2s_pct, 3)} %`;
    case "resonance": return m.fit ? `${num(m.fit.f0_hz)} Hz, +${num(m.fit.peak_db)} dB` : "";
    case "sweep_high": case "sweep_low": return T("alignment.bv_points", { n: (m.curve || []).length });
    case "silent_groove": return m.rumble_db != null ? T("alignment.bv_rumble", { db: num(m.rumble_db) }) : `${num(m.rumble_dbfs)} dBFS`;
    case "pink_lateral": case "pink_vertical": return T("alignment.bv_balance", { db: sgn(m.balance_db) });
    default: return "";
  }
}

const BANDCOL = ["#3d5a80", "#2e7d4f", "#b7791f", "#8e44ad", "#c0392b", "#16a085"];
function drawBands(c, ov, onPick) {
  const { g, w, h } = setupCanvas(c);
  const n = ov.min.length, dur = n * ov.bucket_s, x = (t) => (t / dur) * w, mid = h * 0.58, amp = h * 0.38;
  g.font = "11px system-ui";
  ov.bands.forEach(([a, b, i, role], k) => {
    g.fillStyle = role ? BANDCOL[k % BANDCOL.length] + "22" : "rgba(128,128,128,.12)"; g.fillRect(x(a), 0, x(b) - x(a), h);
    g.fillStyle = css("--muted"); g.fillText(String(i), x(a) + 3, 12);
  });
  g.strokeStyle = css("--wave"); g.beginPath();
  for (let i = 0; i < n; i++) { const px = (i / n) * w; g.moveTo(px, mid - ov.max[i] * amp); g.lineTo(px, mid - ov.min[i] * amp); }
  g.stroke();
  c.onclick = (e) => onPick((e.offsetX / c.clientWidth) * dur);
}

async function showAlignment(id) {
  const [rec, ov] = await Promise.all([api(`/api/recordings/${id}`), api(`/api/recordings/${id}/overview`)]);
  const m = rec.analysis.metrics, v = openView();
  const by = Object.fromEntries(m.bands.filter((b) => b.role).map((b) => [b.role, b]));
  const fr = [by.sweep_high?.m?.curve, by.sweep_low?.m?.curve].filter(Boolean);
  v.innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">${T("alignment.recording_title", { id: rec.id, side: esc(rec.side) })}</h2><button class="secondary" id="close-view">${ico("back", 16)} ${T("alignment.back")}</button></div>
    ${!rec.audio_available ? `<div class="warnbox">${rec.audio_deleted_at ? T("alignment.audio_retention_deleted") : T("alignment.audio_missing")}</div>` : ""}
    ${rec.analysis.warnings.map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
    <h3>${T("alignment.side_findings")}</h3>${resultsTable(m.results)}
    <h3>${T("alignment.detected_bands")}</h3>
    <div class="legend"><span>${T("alignment.click_to_listen")}</span></div>
    <canvas id="ov"></canvas>
    <div id="sg"></div>
    <div class="scroll"><table><tr><th>#</th><th>${T("alignment.col_time")}</th><th>${T("alignment.col_detected_as")}</th><th>${T("alignment.col_channel_mode")}</th><th class="num">${T("alignment.col_frequency")}</th><th class="num">${T("alignment.col_level")}</th><th>${T("alignment.col_measurement")}</th><th></th></tr>
      ${m.bands.map((b) => `<tr><td>${b.i}</td><td>${fmtTime(b.t0)}–${fmtTime(b.t1)}</td><td>${esc(b.label)}${b.role ? "" : ` <small class="hint">${T("alignment.not_assigned")}</small>`}</td><td>${esc(MODE[b.mode] || b.mode)}</td>
        <td class="num">${b.type === "sweep" ? `${num(b.f_start_hz, 0)} → ${num(b.f_end_hz, 0)} Hz` : b.freq_hz ? num(b.freq_hz, b.freq_hz < 200 ? 1 : 0) + " Hz" + (b.freq2_hz ? ` + ${num(b.freq2_hz, 0)} Hz` : "") : ""}</td>
        <td class="num">${num(b.level_db)} dBFS</td><td>${bandValue(b)}</td>
        <td>${rec.audio_available ? `<button class="link" data-play="${(b.t0 + Math.min(b.t1, b.t0 + 4)) / 2}" data-before="1" data-after="3">▶</button>` : ""}</td></tr>`).join("")}
    </table></div>
    ${by.antiskating?.m?.curve ? `<h3>${T("alignment.as_title")}</h3><canvas id="ch-as" class="chart"></canvas>
      <p class="hint">${T("alignment.as_hint")}</p>` : ""}
    ${by.resonance?.m?.curve ? `<h3>${T("alignment.res_title")}</h3><canvas id="ch-res" class="chart"></canvas>
      <p class="hint">${T("alignment.res_hint", { lo: num(8, 0), hi: num(12, 0) })}</p>` : ""}
    ${fr.length ? `<h3>${T("alignment.fr_title")}</h3><canvas id="ch-fr" class="chart"></canvas>` : ""}
    ${by.wow_flutter?.m?.trace ? `<h3>${T("alignment.wf_title")}</h3><canvas id="ch-wf" class="chart"></canvas>` : ""}`;
  $("#close-view").onclick = closeView;
  v.onclick = (e) => { if (e.target.dataset.play) play(rec.id, +e.target.dataset.play, +(e.target.dataset.before || 1), +(e.target.dataset.after || 3)); };
  drawBands($("#ov"), ov, (t) => (rec.audio_available ? play(rec.id, t, 0.5, 3) : toast(T("alignment.audio_deleted_toast"))));
  spectroView($("#sg"), [{ recId: rec.id, label: T("alignment.side_n", { side: rec.side }), map: (t) => t }], rec.duration_s, (it, t) => (rec.audio_available ? play(rec.id, t, 0.5, 3) : toast(T("alignment.audio_deleted_toast"))));
  const as = by.antiskating?.m;
  if (as?.curve) chart($("#ch-as"), [
    { points: as.curve.map((p) => [p[0], p[1]]), color: css("--left"), label: T("live.left") },
    { points: as.curve.map((p) => [p[0], p[2]]), color: css("--right"), label: T("live.right") },
  ], { ylog: true, ymin: 0.05, ymax: 50, yunit: " %", xunit: " dB", hlines: [{ y: as.limit_pct || 3, color: css("--bad"), label: T("alignment.threshold", { pct: num(as.limit_pct || 3, 0) }) }],
       vlines: as.break_db.map((bv, i) => (bv == null ? null : { x: bv, color: i ? css("--right") : css("--left"), label: (i ? "R " : "L ") + sgn(bv) })).filter(Boolean) });
  const rs = by.resonance?.m;
  if (rs?.curve) {
    const f = rs.fit, model = [];
    // Model curve: 2nd-order high-pass with resonance f0 and quality q, plus the fitted gain
    if (f) for (let lf = Math.log10(5); lf <= Math.log10(1000); lf += 0.02) { const fq = 10 ** lf, u = (fq / f.f0_hz) ** 2; model.push([fq, 20 * Math.log10(u / Math.sqrt((1 - u) ** 2 + u / f.q ** 2)) + f.gain_db]); }
    chart($("#ch-res"), [{ points: rs.curve.map((p) => [p[0], p[1]]), color: css("--wave"), label: T("alignment.measured_vertical") },
      ...(f ? [{ points: model, color: css("--muted"), dash: [5, 4], label: T("alignment.model") }] : [])],
      { xlog: true, xmin: 5, xmax: 1000, yunit: " dB", xunit: " Hz", ymin: Math.min(-15, ...rs.curve.map((p) => p[1])), ymax: Math.max(6, ...rs.curve.map((p) => p[1])) + 2,
        bands: [[8, 12, "rgba(46,125,79,.12)"]], vlines: f ? [{ x: f.f0_hz, color: css("--ok"), label: num(f.f0_hz) + " Hz" }] : [] });
  }
  if (fr.length) {
    const all = fr.flat();
    chart($("#ch-fr"), [{ points: all.map((p) => [p[0], p[1]]).sort((a, b) => a[0] - b[0]), color: css("--left"), label: T("live.left") },
      { points: all.map((p) => [p[0], p[2]]).sort((a, b) => a[0] - b[0]), color: css("--right"), label: T("live.right") }],
      { xlog: true, xmin: 20, xmax: 20000, yunit: " dB", xunit: " Hz", ymin: Math.min(-6, ...all.map((p) => Math.min(p[1], p[2]))), ymax: Math.max(6, ...all.map((p) => Math.max(p[1], p[2]))),
        hlines: [{ y: 0, color: css("--muted") }] });
  }
  const wf = by.wow_flutter?.m?.trace;
  if (wf) chart($("#ch-wf"), [{ points: wf.dev_pct.map((d, i) => [i / wf.rate, d]), color: css("--wave"), width: 1.2 }], { yunit: " %", xunit: " s" });
  if (!state.viewKeepScroll) window.scrollTo({ top: 0 });
}

async function showSetupComparison(a, b) {
  const c = await api(`/api/setups/compare?a=${a}&b=${b}`);
  const v = openView();
  const lab = (s) => esc(s.label || T("alignment.run_short", { id: s.id }));
  const setRow = (label, k, f = (x) => esc(x ?? "–")) => `<tr><td>${label}</td><td class="num">${f(c.a[k])}</td><td class="num">${f(c.b[k])}</td><td></td></tr>`;
  v.innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">${T("alignment.compare_title", { a: lab(c.a), b: lab(c.b) })}</h2><button class="secondary" id="close-view">${ico("back", 16)} ${T("alignment.back")}</button></div>
    <p class="hint">${T("alignment.compare_hint")}</p>
    ${c.notes.map((n) => `<div class="warnbox">${esc(n)}</div>`).join("")}
    <div class="scroll"><table><tr><th></th><th class="num">A: ${lab(c.a)}<br><small class="hint">${dateStr(c.a.created_at)}</small></th><th class="num">B: ${lab(c.b)}<br><small class="hint">${dateStr(c.b.created_at)}</small></th><th class="num">Δ</th></tr>
      ${setRow(T("alignment.force"), "tracking_force_g", fmtG)}${setRow("Bias", "bias")}${setRow(T("alignment.vta"), "vta")}${setRow(T("alignment.stylus_hours"), "needle_hours", (x) => (x == null ? "–" : num(x, 0) + " h"))}${setRow(T("alignment.input_level"), "input_level")}
      ${c.rows.map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${fmtVal(r.a, r.unit)}${r.better === "a" ? ' <span class="st ok">▲</span>' : ""}</td>
        <td class="num">${fmtVal(r.b, r.unit)}${r.better === "b" ? ' <span class="st ok">▲</span>' : ""}</td><td class="num">${r.delta == null ? "–" : sgn(r.delta, r.unit === "%" ? (Math.abs(r.delta) < 1 ? 3 : 2) : 1)}</td></tr>`).join("")}
    </table></div>`;
  $("#close-view").onclick = closeView;
  if (!state.viewKeepScroll) window.scrollTo({ top: 0 });
}


// ─── Spectrogram ─────────────────────────────────────────────────────────
// x = time, y = frequency (log), color/brightness = level (dark = floor, bright = peak). The server sends the
// image as a uint8 matrix; the color scale is applied in the browser (color or grayscale, switchable). Several
// recordings are stacked: the first one is the time axis, the others are mapped onto it through their time
// mapping (comparison: offset + speed), so the same spots line up exactly.
const SG_SCALES = {
  color: [[0, 0, 4], [40, 12, 70], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 253, 191]],
  gray: [[0, 0, 0], [255, 255, 255]],
};
const sgLut = (name) => {
  const st = SG_SCALES[name] || SG_SCALES.color, lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    const x = (i / 255) * (st.length - 1), k = Math.min(st.length - 2, Math.floor(x)), f = x - k;
    for (let c = 0; c < 3; c++) lut[i * 4 + c] = st[k][c] * (1 - f) + st[k + 1][c] * f;
    lut[i * 4 + 3] = 255;
  }
  return lut;
};
let sgScale = "color";
try {
  const saved = { farbe: "color", grau: "gray" }[localStorage.getItem("vc-sg-scale")] || localStorage.getItem("vc-sg-scale");   // values stored by older versions
  if (SG_SCALES[saved]) sgScale = saved;
} catch (_) {}
let SG_LUT = sgLut(sgScale);

async function fetchSpectrogram(recId, q) {
  const r = await fetch(`/api/recordings/${recId}/spectrogram?` + new URLSearchParams(Object.fromEntries(Object.entries(q).filter(([, v]) => v != null))));
  if (!r.ok) { let msg = r.statusText; try { msg = (await r.json()).detail; } catch (_) {} throw new Error(msg); }
  const h = (k) => +r.headers.get(k);
  return { data: new Uint8Array(await r.arrayBuffer()), rows: h("x-rows"), cols: h("x-cols"), t0: h("x-t0"), t1: h("x-t1"),
    fmin: h("x-fmin"), fmax: h("x-fmax"), top: h("x-top-db"), floor: h("x-floor-db") };
}

function paintSpectrogram(c, sg) {
  const { g, w, h } = setupCanvas(c);
  const off = document.createElement("canvas"); off.width = sg.cols; off.height = sg.rows;
  const img = off.getContext("2d").createImageData(sg.cols, sg.rows);
  for (let r = 0; r < sg.rows; r++) for (let x = 0; x < sg.cols; x++) {          // row 0 (fmin) at the bottom
    const v = sg.data[r * sg.cols + x] * 4, o = ((sg.rows - 1 - r) * sg.cols + x) * 4;
    img.data[o] = SG_LUT[v]; img.data[o + 1] = SG_LUT[v + 1]; img.data[o + 2] = SG_LUT[v + 2]; img.data[o + 3] = 255;
  }
  off.getContext("2d").putImageData(img, 0, 0);
  g.imageSmoothingEnabled = false; g.drawImage(off, 0, 0, w, h);
  // White labels with a dark outline: readable on black and on white
  g.font = "11px system-ui"; g.fillStyle = "#fff"; g.strokeStyle = "rgba(0,0,0,.8)"; g.lineWidth = 3; g.lineJoin = "round";
  const label = (txt, x, y) => { g.strokeText(txt, x, y); g.fillText(txt, x, y); };
  const tick = (x0, y0, x1, y1) => { g.strokeStyle = "rgba(0,0,0,.8)"; g.lineWidth = 3; g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke(); g.strokeStyle = "#fff"; g.lineWidth = 1; g.stroke(); g.strokeStyle = "rgba(0,0,0,.8)"; g.lineWidth = 3; };
  const ly = Math.log(sg.fmax / sg.fmin);
  for (const f of [50, 100, 200, 500, 1000, 2000, 5000, 10000]) {
    if (f < sg.fmin || f > sg.fmax) continue;
    const y = h - (Math.log(f / sg.fmin) / ly) * h;
    tick(0, y, 6, y); label(f >= 1000 ? f / 1000 + "k" : String(f), 8, y + 4);
  }
  const span = sg.t1 - sg.t0, step = span > 600 ? 120 : span > 240 ? 60 : span > 60 ? 10 : span > 10 ? 2 : 0.5;
  for (let t = Math.ceil(sg.t0 / step) * step; t <= sg.t1; t += step) {
    const x = ((t - sg.t0) / span) * w;
    tick(x, h - 6, x, h); label(step < 1 ? fmtT(t) : fmtTime(t), x + 2, h - 8);
  }
}

// items: [{recId, label, map: tA → t of this recording}]; the first recording is the time axis.
function spectroView(el, items, dur, onPlay) {
  const scaleLabel = () => (sgScale === "color" ? T("spectrogram.grayscale") : T("spectrogram.color"));
  el.classList.add("sg");
  el.innerHTML = `<div class="sg-zoom"><span class="sg-text"></span> <button class="link sg-scale">${scaleLabel()}</button></div>` + items.map((it, i) => `<div class="sg-row"><canvas data-i="${i}"></canvas><div class="tcursor"><span></span></div><span class="sg-label">${esc(it.label)}</span><div class="sg-sel"></div></div>`).join("");
  const rows = [...el.querySelectorAll(".sg-row")];
  const view = { t0: 0, t1: dur, top: null, busy: false, again: false };
  const cols = 1600;   // default size: the whole recording is stored precomputed at this size in the database
  async function render() {
    if (view.busy) { view.again = true; return; }
    view.busy = true;
    el.querySelector(".sg-text").textContent = T("spectrogram.range", { from: fmtTime(view.t0), to: fmtTime(view.t1), s: Math.round(view.t1 - view.t0) });
    let top = null;
    for (const [i, it] of items.entries()) {
      const c = rows[i].querySelector("canvas");
      try {
        // The precomputed image of the whole recording only fits if the time axis is not shifted: the after
        // recording of a comparison is always computed from the audio in the mapped window
        const full = view.t0 === 0 && view.t1 === dur && !it.mapped;
        const q = { cols, rows: 160, t0: full ? 0 : it.map(view.t0), t1: full ? null : it.map(view.t1), top };
        const sg = await fetchSpectrogram(it.recId, q);
        if (top == null) top = sg.top;                 // same color scale for all rows
        rows[i].sg = sg; paintSpectrogram(c, sg);
      } catch (e) {
        const { g, h } = setupCanvas(c); g.fillStyle = css("--muted"); g.font = "12px system-ui"; g.fillText(`${it.label}: ${e.message}`, 10, h / 2);
      }
    }
    view.busy = false;
    if (view.again) { view.again = false; render(); }
  }
  for (const [i, row] of rows.entries()) {
    const c = row.querySelector("canvas"), sel = row.querySelector(".sg-sel");
    let drag = null;
    const tAt = (e) => view.t0 + (e.offsetX / c.clientWidth) * (view.t1 - view.t0);
    c.onmousedown = (e) => { drag = { x0: e.offsetX, moved: false }; };
    c.onmousemove = (e) => {
      if (!drag) return;
      if (Math.abs(e.offsetX - drag.x0) > 3) drag.moved = true;
      sel.style.display = "block"; sel.style.left = Math.min(drag.x0, e.offsetX) + "px"; sel.style.width = Math.abs(e.offsetX - drag.x0) + "px";
    };
    c.onmouseup = (e) => {
      if (!drag) return;
      sel.style.display = "none";
      if (drag.moved) {
        const a = view.t0 + (Math.min(drag.x0, e.offsetX) / c.clientWidth) * (view.t1 - view.t0);
        const b = view.t0 + (Math.max(drag.x0, e.offsetX) / c.clientWidth) * (view.t1 - view.t0);
        if (b - a >= 0.5) { view.t0 = a; view.t1 = b; render(); }
      } else if (onPlay) onPlay(items[i], items[i].map(tAt(e)), e);
      drag = null;
    };
    c.onmouseleave = () => { if (drag) { drag = null; sel.style.display = "none"; } };
    c.ondblclick = () => { view.t0 = 0; view.t1 = dur; render(); };
  }
  el.querySelector(".sg-scale").onclick = (e) => {             // switch the color scale without reloading
    sgScale = sgScale === "color" ? "gray" : "color"; SG_LUT = sgLut(sgScale);
    try { localStorage.setItem("vc-sg-scale", sgScale); } catch (_) {}
    e.target.textContent = scaleLabel();
    rows.forEach((row) => { if (row.sg) paintSpectrogram(row.querySelector("canvas"), row.sg); });
  };
  render();
  return view;
}

// ─── Live listening: sound of the recording/level check via the Web Audio API ────────
// The server streams raw 16-bit PCM (/api/live/pcm); here it is filled block by block into AudioBuffers and
// scheduled back to back without gaps. 0.4 s of lead time absorb Wi-Fi jitter. With `start` (second of the
// recording) the sound comes delayed from the file ("−30 s") and then continues live.
const listen = { on: false, id: 0, ctx: null, reader: null, base: 0, t0: null, autoMonitor: false };
const listenPos = () => (listen.t0 === null || !listen.ctx ? listen.base : listen.base + Math.max(0, listen.ctx.currentTime - listen.t0));
async function listenStart(start = null) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.resume();
  const myId = ++listen.id;
  Object.assign(listen, { on: true, ctx, reader: null, base: start ?? (state.status?.recorder?.elapsed_s ?? 0), t0: null });
  renderListen();
  let resp;
  try {
    resp = await fetch(start === null ? "/api/live/pcm" : `/api/live/pcm?start=${start.toFixed(2)}`, { cache: "no-store" });
    if (!resp.ok) { let m = resp.statusText; try { m = (await resp.json()).detail; } catch (_) {} throw new Error(m); }
  } catch (e) { if (listen.id === myId) listenStop(); throw e; }
  const rate = +resp.headers.get("X-Rate") || 48000, ch = +resp.headers.get("X-Channels") || 2, frameB = 2 * ch;
  const reader = resp.body.getReader();
  listen.reader = reader;
  let rest = new Uint8Array(0), next = ctx.currentTime + 0.4;
  try {
    while (listen.on && listen.id === myId) {
      const { value, done } = await reader.read();
      if (done) break;
      const all = new Uint8Array(rest.length + value.length); all.set(rest); all.set(value, rest.length);
      const usable = all.length - (all.length % frameB);
      rest = all.slice(usable);
      const n = usable / frameB;
      if (!n) continue;
      const i16 = new Int16Array(all.slice(0, usable).buffer);
      const buf = ctx.createBuffer(ch, n, rate);
      for (let c = 0; c < ch; c++) { const d = buf.getChannelData(c); for (let i = 0; i < n; i++) d[i] = i16[i * ch + c] / 32768; }
      const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
      if (next < ctx.currentTime) { listen.base = listenPos(); listen.t0 = null; next = ctx.currentTime + 0.2; }   // underrun: restart the schedule
      if (listen.t0 === null) listen.t0 = next;
      src.start(next); next += buf.duration;
    }
  } finally { if (listen.id === myId) listenStop(); }
}
function listenStop() {
  if (!listen.on && !listen.ctx) return;
  listen.on = false;
  try { listen.reader?.cancel(); } catch (_) {}
  try { listen.ctx?.close(); } catch (_) {}
  listen.ctx = listen.reader = null;
  if (listen.autoMonitor) {                                   // we started the level check just for listening: stop it again
    listen.autoMonitor = false;
    if (state.status?.recorder?.mode === "monitor") api("/api/monitor/stop", { method: "POST" }).catch(() => {});
  }
  renderListen();
}
function listenRestart(start) { listenStop(); guard(listenStart)(start); }
function renderListen() {
  const b = $("#btn-listen"), r = state.status?.recorder, act = !!r?.active;
  b.disabled = false;                                         // without a recording, listening starts the level check itself
  b.title = act ? T("live.listen_title") : T("live.preview_title");
  $(".lbl", b).textContent = listen.on ? T("live.listen_off") : (act ? T("live.listen") : T("live.preview"));
  b.classList.toggle("on", listen.on);
  const ctl = $("#listen-ctl"), show = listen.on && r?.mode === "record";
  ctl.classList.toggle("hidden", !show);
  if (!show) return;
  const pos = listenPos(), behind = Math.max(0, (r.elapsed_s ?? pos) - pos);
  $("#listen-pos").textContent = behind < 2 ? T("live.pos_live", { t: fmtTime(pos) }) : T("live.pos_behind", { t: fmtTime(pos), s: Math.round(behind) });
  $("#listen-live").disabled = behind < 2;
}
$("#btn-listen").onclick = guard(async () => {
  if (listen.on) return listenStop();
  if (!state.status?.recorder?.active) {                      // nothing running: start the level check, then listen
    await api("/api/monitor/start", { method: "POST" });
    listen.autoMonitor = true;
    for (let i = 0; i < 40 && !state.status?.recorder?.active; i++) await new Promise((res) => setTimeout(res, 150));
  }
  await listenStart();
});
document.querySelectorAll("#listen-ctl [data-seek]").forEach((b) => (b.onclick = () => {
  const live = state.status?.recorder?.elapsed_s ?? Infinity, to = listenPos() + (+b.dataset.seek);
  if (to >= live - 1.5) return listenRestart(null);              // seeking past live = just live
  listenRestart(Math.max(0, to));
}));
$("#listen-live").onclick = () => listenRestart(null);

// ─── Recording buttons at the top ────────────────────────────────────────
$("#btn-monitor").onclick = guard(async () => { await api("/api/monitor/start", { method: "POST" }); });
$("#btn-stop").onclick = guard(async () => { await api("/api/recordings/stop", { method: "POST" }); setTimeout(() => { refreshRecord(); refreshSetup(); }, 500); });
$("#btn-calibrate").onclick = guard(async () => {
  if (!confirm(T("shell.calibrate_confirm"))) return;
  await api("/api/recordings/start", { method: "POST", body: { kind: "calibration", side: "-" } });
});

// ─── Verdicts, difference track and per-recording fields (delegated: the views are rebuilt often) ──────────
document.addEventListener("click", (e) => {
  const sl = e.target.closest("[data-skip-label]");
  if (sl) {
    guard(async () => {
      const r = await api(`/api/recordings/${sl.dataset.rec}/skips/label`, { method: "PUT", body: { t: +sl.dataset.t, label: sl.dataset.skipLabel || null } });
      gradeCache.delete(+sl.dataset.rec);
      toast(T("recording.skip_verdict_toast", { verdict: T(sl.dataset.skipLabel ? "recording.verdict_saved" : "recording.verdict_removed"), n: r.skips_back, grade: r.grade ?? "–" }), 5000);
      const y = window.scrollY; await showRecording(+sl.dataset.rec); window.scrollTo({ top: y });
    })();
    return;
  }
  const pr = e.target.closest("[data-play-rec]");
  if (pr) { e.stopPropagation(); play(+pr.dataset.playRec, +pr.dataset.t, +(pr.dataset.before || 1), +(pr.dataset.after || 3)); return; }
  const g = e.target.closest("#pills [data-go]");
  if (g) { toggleJobsPop(g); return; }
  const gg = e.target.closest("[data-go]");
  if (gg && !gg.closest("#station")) {
    e.preventDefault(); setMode(gg.dataset.go);
    if (gg.dataset.goTo) scrollWhenReady(gg.dataset.goTo); else if (gg.dataset.go === "docs") window.scrollTo(0, 0);
    return;
  }
  if (!e.target.closest("#jobs-pop")) closeJobsPop();
  const lb = e.target.closest("[data-label]");
  if (lb) {
    guard(async () => {
      const cur = lb.classList.contains("chosen");
      const r = await api(`/api/recordings/${lb.dataset.rec}/events/label`, { method: "PUT", body: { t: +lb.dataset.t, label: cur ? null : lb.dataset.label } });
      for (const b of lb.parentElement.querySelectorAll("[data-label]")) b.classList.toggle("chosen", !cur && b === lb);
      lb.closest("tr")?.classList.toggle("muted-row", !cur && lb.dataset.label === "music");
      state.judged.set(judgedKey(+lb.dataset.rec, +lb.dataset.t), cur ? null : lb.dataset.label);
      if (lb.closest("#ev-wrap") && !state.showJudged && !cur) {                     // fade the row out, then redraw
        const tr = lb.closest("tr"); tr.style.transition = "opacity .25s"; tr.style.opacity = "0";
        setTimeout(() => state.renderEv?.(), 260);
      } else state.renderEv?.();
      gradeCache.delete(+lb.dataset.rec);
      refreshRecord();
      state.wiRefresh?.();
      toast(cur ? T("recording.verdict_removed") : T("recording.verdict_saved_quiet", { verdict: lb.textContent.trim(), grade: r.grade ?? "–", quiet: num(r.clicks_quiet_per_min), music: num(r.clicks_music_per_min) }), 5000);
    })();
    return;
  }
  const o = e.target.closest("[data-open-rec-detail]");
  if (o) { showRecording(+o.dataset.openRecDetail).catch((x) => toast(x.message)); return; }
  const b = e.target.closest("[data-clicks-only]");
  if (b) {
    const id = b.dataset.clicksOnly, box = $("#clicks-audio");
    box.innerHTML = `<audio controls autoplay src="/api/recordings/${id}/clicks.wav" style="width:100%;max-width:640px"></audio><p class="hint">${T("recording.clicks_only_hint")}</p>`;
    return;
  }
  const r = e.target.closest("[data-rec-sens-reset]");
  if (r) guard(async () => {
    await api(`/api/recordings/${r.dataset.recSensReset}`, { method: "PATCH", body: { sensitivity: null } });
    toast(T("recording.sensitivity_reset"), 6000);
  })();
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset?.recSens) guard(async () => {
    await api(`/api/recordings/${t.dataset.recSens}`, { method: "PATCH", body: { sensitivity: +t.value } });
    toast(T("recording.sensitivity_changed"), 8000);
  })();
  if (t.dataset?.recClean) guard(async () => {
    await api(`/api/recordings/${t.dataset.recClean}`, { method: "PATCH", body: { cleaning_method: t.value.trim() } });
    toast(t.value.trim() ? T("recording.cleaning_saved") : T("recording.cleaning_removed"));
  })();
  if (t.dataset?.recNotes) guard(async () => {
    await api(`/api/recordings/${t.dataset.recNotes}`, { method: "PATCH", body: { notes: t.value.trim() } });
    toast(t.value.trim() ? T("recording.note_saved") : T("recording.note_removed"));
  })();
  if (t.dataset?.recFinding) guard(async () => {
    const boxes = [...t.closest(".findings").querySelectorAll("input[data-rec-finding]:checked")].map((c) => c.value);
    await api(`/api/recordings/${t.dataset.rec}`, { method: "PATCH", body: { findings: boxes } });
    toast(boxes.length ? T("recording.findings_saved", { list: boxes.map((f) => FINDINGS[f]).join(", ") }) : T("recording.findings_cleared"));
    if (state.record) state.record = await api(`/api/records/${state.record.id}`);   // sibling recordings show them too
  })();
  if (t.dataset?.recCopy) guard(async () => {
    await api(`/api/recordings/${t.dataset.recCopy}`, { method: "PATCH", body: { copy_label: t.value.trim() } });
    toast(t.value.trim() ? T("recording.copy_saved", { copy: t.value.trim() }) : T("recording.copy_removed"));
  })();
  if (t.dataset?.profSens) guard(async () => {
    await api(`/api/profiles/${t.dataset.profSens}`, { method: "PATCH", body: { sensitivity: +t.value } });
    toast(T("common.profile_sensitivity_saved"), 8000);
  })();
});

// Start once the static texts are translated: i18n.js applies them on DOMContentLoaded with a listener registered
// before this one, so labels that setMode() and the status updates change are not overwritten afterwards.
function startApp() {
  connect();
  loadRecords().catch((e) => toast(e.message));
  setMode(initialMode());
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startApp); else startApp();
