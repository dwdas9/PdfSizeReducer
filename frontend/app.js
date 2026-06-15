/* =========================================================================
   Compact — app.js
   -------------------------------------------------------------------------
   All client behaviour for the workbench. Vanilla JS, no build step, so the
   file you read is the file that runs. It talks to the FastAPI backend over a
   small JSON API and renders both PDF previews with PDF.js.

   Mental model
   ------------
   * Each uploaded file is a "session" on the server and an entry in `state.files`
     here, keyed by session_id. Switching tabs swaps which session is active and
     restores that file's own settings and results.
   * "Strategy" has three modes:
       - profile : a named preset (sent as {profile})
       - custom  : the advanced controls drive it (sent as {profile, custom})
       - target  : aim for a size (sent as {target_bytes})
   * Any change re-runs a debounced /api/estimate to update the meter live, BEFORE
     the user commits. /api/optimize then replaces the estimate with real numbers.

   The size meter is the centrepiece: it always reflects the current best
   knowledge — an estimate (slate) until you optimize, then the measured result
   (green).
   ========================================================================= */

"use strict";

/* ----------------------------------------------------------------- config -- */
const API = ""; // same-origin; the server hosts both UI and API.

// PDF.js worker. Matches the library version loaded in index.html.
if (window["pdfjsLib"]) {
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
}

// Display order and one-line descriptions for the profile chooser. Descriptions
// are intentionally about *outcomes* ("smaller, still readable") rather than
// mechanism, matching how a user thinks.
const PROFILE_ORDER = ["lossless", "balanced", "strong", "extreme", "scanned"];
const PROFILE_DESC = {
  lossless: "No pixels touched · safest",
  balanced: "Print-safe · recommended",
  strong: "Smaller · still sharp on screen",
  extreme: "Smallest sensible size",
  scanned: "Tuned for scanned paper",
};

/* ------------------------------------------------------------------ state -- */
const state = {
  ghostscript: false, // from /api/health
  profiles: {}, // from /api/profiles
  files: new Map(), // session_id -> file record (see makeFileRecord)
  activeId: null,
  unit: "MB", // target-size unit toggle
};

// A file record bundles the server analysis with this client's current settings
// and the latest estimate/result so tab switches are instant and stateful.
function makeFileRecord(entry) {
  return {
    id: entry.session_id,
    name: entry.name,
    analysis: entry.analysis,
    recommendation: entry.recommendation,
    // settings
    mode: "profile",
    profile: entry.recommendation.profile, // start on the recommended profile
    custom: defaultCustomFromProfile(entry.recommendation.profile),
    targetBytes: null,
    // live numbers
    estimate: null, // last /api/estimate payload
    result: null, // last /api/optimize payload
    // preview bookkeeping
    optimized: false,
  };
}

/* --------------------------------------------------------------- helpers --- */
const $ = (id) => document.getElementById(id);

/** Human-friendly byte formatter used everywhere a size appears. */
function fmtBytes(n) {
  if (n == null) return "—";
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

/** Debounce: collapses a burst of slider events into one network call. */
function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

/** Minimal toast for errors and confirmations. */
function toast(msg, isError) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " err" : "");
  el.textContent = msg;
  $("toastWrap").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

async function apiJSON(path, options) {
  const res = await fetch(API + path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

/** Seed the advanced-control values from a profile's parameters. */
function defaultCustomFromProfile(profileName) {
  const p = state.profiles[profileName] || {};
  return {
    image_dpi: p.image_dpi ?? 200,
    jpeg_quality: p.jpeg_quality ?? 80,
    to_grayscale: !!p.to_grayscale,
    strip_metadata: !!p.strip_metadata,
    subset_fonts: p.subset_fonts !== false,
    ghostscript_pass: !!p.ghostscript_pass,
  };
}

/* ------------------------------------------------------------- bootstrapping */
async function boot() {
  // Health: enables/greys-out the Ghostscript option and updates the pill.
  try {
    const h = await apiJSON("/api/health");
    state.ghostscript = !!h.ghostscript;
  } catch (_) {
    state.ghostscript = false;
  }
  const pill = $("gsPill");
  $("gsText").textContent = state.ghostscript
    ? "Ghostscript ready"
    : "Ghostscript off";
  pill.classList.toggle("on", state.ghostscript);
  $("gsToggle").disabled = !state.ghostscript;
  if (!state.ghostscript) {
    $("gsSub").textContent = "Install Ghostscript to enable";
  }

  // Profiles power the chooser and seed the sliders.
  try {
    state.profiles = await apiJSON("/api/profiles");
  } catch (e) {
    toast("Could not load profiles: " + e.message, true);
  }

  wireDropzone();
  wireControls();
}

/* --------------------------------------------------------------- dropzone -- */
function wireDropzone() {
  const dz = $("dropzone");
  const input = $("fileInput");

  dz.addEventListener("click", () => input.click());
  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("dragging");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragging"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragging");
    handleFiles(e.dataTransfer.files);
  });
  input.addEventListener("change", () => handleFiles(input.files));
}

/** Upload one or more dropped/selected files and register the results. */
async function handleFiles(fileList) {
  const pdfs = Array.from(fileList).filter(
    (f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
  );
  if (!pdfs.length) {
    toast("Please choose PDF files.", true);
    return;
  }

  const form = new FormData();
  pdfs.forEach((f) => form.append("files", f));

  toast(`Analyzing ${pdfs.length} file${pdfs.length > 1 ? "s" : ""}…`);
  let data;
  try {
    data = await apiJSON("/api/upload", { method: "POST", body: form });
  } catch (e) {
    toast("Upload failed: " + e.message, true);
    return;
  }

  let firstNewId = null;
  for (const entry of data.files) {
    if (entry.error) {
      toast(`${entry.name}: ${entry.error}`, true);
      continue;
    }
    const rec = makeFileRecord(entry);
    state.files.set(rec.id, rec);
    if (!firstNewId) firstNewId = rec.id;
  }

  if (firstNewId) {
    $("dropzone").classList.add("hidden");
    $("workbenchWrap").classList.remove("hidden");
    renderTabs();
    selectFile(firstNewId);
  }
}

/* ------------------------------------------------------------------- tabs -- */
function renderTabs() {
  const wrap = $("fileTabs");
  wrap.innerHTML = "";
  for (const rec of state.files.values()) {
    const tab = document.createElement("div");
    tab.className = "file-tab" + (rec.id === state.activeId ? " active" : "");
    tab.innerHTML = `
      <span class="name" title="${rec.name}">${rec.name}</span>
      <span class="size mono">${fmtBytes(rec.analysis.file_size)}</span>
      <button class="close" title="Remove">×</button>`;
    tab.querySelector(".name").addEventListener("click", () => selectFile(rec.id));
    tab.querySelector(".size").addEventListener("click", () => selectFile(rec.id));
    tab.querySelector(".close").addEventListener("click", (e) => {
      e.stopPropagation();
      removeFile(rec.id);
    });
    wrap.appendChild(tab);
  }
  // trailing "add more" affordance
  const add = document.createElement("div");
  add.className = "file-tab add";
  add.innerHTML = `<span class="name">+ Add PDF</span>`;
  add.addEventListener("click", () => $("fileInput").click());
  wrap.appendChild(add);
}

async function removeFile(id) {
  try {
    await fetch(API + "/api/session/" + id, { method: "DELETE" });
  } catch (_) {}
  state.files.delete(id);
  if (state.activeId === id) state.activeId = null;
  if (state.files.size === 0) {
    $("workbenchWrap").classList.add("hidden");
    $("dropzone").classList.remove("hidden");
    return;
  }
  renderTabs();
  if (!state.activeId) selectFile(state.files.keys().next().value);
}

/* ------------------------------------------------------- active file view -- */
function active() {
  return state.files.get(state.activeId);
}

function selectFile(id) {
  state.activeId = id;
  const rec = active();
  renderTabs();

  // Recommendation chip
  $("reco").classList.remove("hidden");
  $("recoText").innerHTML =
    `Recommended: <b>${rec.recommendation.profile}</b> — ${rec.recommendation.rationale}`;

  renderProfiles();
  syncControlsFromRecord(rec);

  // reset target input display
  $("targetInput").value = "";

  // Render original preview and refresh the meter/estimate.
  renderOriginalPreview(rec);
  // Optimized pane: show prior result if this file was already optimized.
  if (rec.optimized) {
    renderOptimizedPreview(rec);
  } else {
    resetOptimizedPane();
  }
  refreshEstimate();
  refreshProfileBadges();
}

/** Build the profile chooser rows (with a slot for the live % estimate). */
function renderProfiles() {
  const seg = $("profileSeg");
  seg.innerHTML = "";
  for (const name of PROFILE_ORDER) {
    if (!state.profiles[name]) continue;
    const rec = active();
    const btn = document.createElement("button");
    btn.className =
      "seg-btn" +
      (rec.mode === "profile" && rec.profile === name ? " active" : "");
    btn.innerHTML = `
      <span>
        <span class="label">${name}</span>
        <div class="desc">${PROFILE_DESC[name] || ""}</div>
      </span>
      <span class="pct mono" data-pct="${name}">—</span>`;
    btn.addEventListener("click", () => chooseProfile(name));
    seg.appendChild(btn);
  }
}

function chooseProfile(name) {
  const rec = active();
  rec.mode = "profile";
  rec.profile = name;
  rec.targetBytes = null;
  rec.custom = defaultCustomFromProfile(name);
  $("targetInput").value = "";
  renderProfiles();
  syncControlsFromRecord(rec);
  refreshEstimate();
  // Also refresh the per-profile % badges so each row shows its own estimate.
  refreshProfileBadges();
}

/* --------------------------------------------------------------- controls -- */
function wireControls() {
  // Advanced sliders/toggles -> custom mode + debounced estimate.
  const onCustomChange = () => {
    const rec = active();
    if (!rec) return;
    rec.mode = "custom";
    rec.targetBytes = null;
    rec.profile = rec.profile || "balanced"; // baseline for the server merge
    readControlsInto(rec);
    renderProfiles(); // clears the active profile highlight
    $("meterState").textContent = "estimate · custom";
    refreshEstimate();
  };

  $("dpiRange").addEventListener("input", () => {
    $("dpiVal").textContent = $("dpiRange").value + " DPI";
    onCustomChange();
  });
  $("qualityRange").addEventListener("input", () => {
    $("qualityVal").textContent = "Q" + $("qualityRange").value;
    onCustomChange();
  });
  ["grayToggle", "metaToggle", "fontToggle", "gsToggle"].forEach((id) =>
    $(id).addEventListener("change", onCustomChange)
  );

  // Target size
  $("unitKB").addEventListener("click", () => setUnit("KB"));
  $("unitMB").addEventListener("click", () => setUnit("MB"));
  $("targetApply").addEventListener("click", applyTarget);
  $("targetInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") applyTarget();
  });

  // Optimize / download
  $("optimizeBtn").addEventListener("click", runOptimize);
  $("downloadBtn").addEventListener("click", () => {
    const rec = active();
    if (rec) window.location = API + "/api/download/" + rec.id;
  });
}

/** Push a record's settings into the control widgets. */
function syncControlsFromRecord(rec) {
  const p =
    rec.mode === "profile" ? state.profiles[rec.profile] || {} : rec.custom;
  const dpi = p.image_dpi ?? 200;
  const q = p.jpeg_quality ?? 80;
  $("dpiRange").value = dpi;
  $("qualityRange").value = q;
  $("dpiVal").textContent = (p.image_dpi == null ? "keep" : dpi + " DPI");
  $("qualityVal").textContent = (p.jpeg_quality == null ? "lossless" : "Q" + q);
  $("grayToggle").checked = !!p.to_grayscale;
  $("metaToggle").checked = !!p.strip_metadata;
  $("fontToggle").checked = p.subset_fonts !== false;
  $("gsToggle").checked = !!p.ghostscript_pass && state.ghostscript;
  $("meterState").textContent = rec.optimized ? "result" : "estimate";
}

/** Read the current control widgets back into a record's custom settings. */
function readControlsInto(rec) {
  rec.custom = {
    image_dpi: parseInt($("dpiRange").value, 10),
    jpeg_quality: parseInt($("qualityRange").value, 10),
    to_grayscale: $("grayToggle").checked,
    strip_metadata: $("metaToggle").checked,
    subset_fonts: $("fontToggle").checked,
    ghostscript_pass: $("gsToggle").checked,
  };
  $("dpiVal").textContent = rec.custom.image_dpi + " DPI";
  $("qualityVal").textContent = "Q" + rec.custom.jpeg_quality;
}

/* ----------------------------------------------------------- target size -- */
function setUnit(unit) {
  state.unit = unit;
  $("unitKB").classList.toggle("active", unit === "KB");
  $("unitMB").classList.toggle("active", unit === "MB");
}

function targetToBytes() {
  const v = parseFloat($("targetInput").value);
  if (!v || v <= 0) return null;
  return Math.round(v * (state.unit === "MB" ? 1024 * 1024 : 1024));
}

function applyTarget() {
  const bytes = targetToBytes();
  const rec = active();
  if (!rec) return;
  if (!bytes) {
    toast("Enter a target size first.", true);
    return;
  }
  rec.mode = "target";
  rec.targetBytes = bytes;
  renderProfiles(); // clear profile highlight
  $("meterState").textContent = "estimate · target";
  refreshEstimate();
}

/* ----------------------------------------------------------- estimate flow */
function currentRequestBody(rec) {
  // Translate a record's mode into the JSON body the API expects.
  if (rec.mode === "target" && rec.targetBytes) {
    return { session_id: rec.id, target_bytes: rec.targetBytes };
  }
  if (rec.mode === "custom") {
    return { session_id: rec.id, profile: rec.profile, custom: rec.custom };
  }
  return { session_id: rec.id, profile: rec.profile };
}

const refreshEstimate = debounce(async function () {
  const rec = active();
  if (!rec) return;
  try {
    const body = currentRequestBody(rec);
    const data = await apiJSON("/api/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    rec.estimate = data;

    // Target mode returns a plan {estimate, params, achievable}; profile/custom
    // mode returns {estimate, params}. Normalise to a single estimate object.
    const est = data.estimate;
    updateMeter({
      original: est.original_size,
      predicted: est.predicted_size,
      pct: est.saved_pct,
      isResult: false,
      imageBytes: est.predicted_image_bytes,
      fontBytes: est.predicted_font_bytes,
      otherBytes: est.predicted_other_bytes,
    });

    // If we're in target mode, reflect the chosen plan in the controls + banner.
    if (data.mode === "target") {
      const p = data.params;
      $("dpiRange").value = p.image_dpi ?? 200;
      $("qualityRange").value = p.jpeg_quality ?? 80;
      $("dpiVal").textContent = (p.image_dpi ?? 200) + " DPI";
      $("qualityVal").textContent = "Q" + (p.jpeg_quality ?? 80);
      const banner = $("targetBanner");
      banner.classList.remove("hidden", "ok", "miss");
      if (data.achievable) {
        banner.classList.add("ok");
        banner.textContent = `✓ We can reach ${fmtBytes(rec.targetBytes)} — settings chosen automatically.`;
      } else {
        banner.classList.add("miss");
        banner.textContent = `This file can't quite reach ${fmtBytes(rec.targetBytes)}. We'll get as small as possible.`;
      }
    } else {
      $("targetBanner").classList.add("hidden");
    }
  } catch (e) {
    // Estimates are non-critical; fail quietly to a neutral meter.
    console.warn("estimate failed", e);
  }
}, 180);

/** Fetch a quick estimate for each profile to fill the per-row % badges. */
async function refreshProfileBadges() {
  const rec = active();
  if (!rec) return;
  for (const name of PROFILE_ORDER) {
    if (!state.profiles[name]) continue;
    try {
      const data = await apiJSON("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: rec.id, profile: name }),
      });
      const badge = document.querySelector(`[data-pct="${name}"]`);
      if (badge) badge.textContent = "−" + data.estimate.saved_pct + "%";
    } catch (_) {}
  }
}

/* -------------------------------------------------------------- the meter -- */
/**
 * Update the signature size meter. `isResult` switches it from the slate
 * "estimate" presentation to the green "measured result" presentation.
 */
function updateMeter(m) {
  const ratio = m.original > 0 ? m.predicted / m.original : 1;
  const fill = $("meterFill");
  // The fill represents how much of the ORIGINAL remains; smaller = better.
  fill.style.width = Math.max(2, Math.min(100, ratio * 100)) + "%";
  fill.classList.toggle("idle", !m.isResult);

  const pctEl = $("deltaPct");
  pctEl.classList.toggle("idle", !m.isResult);
  pctEl.innerHTML = `<span class="arrow">↓</span>${m.pct.toFixed(1)}%`;

  $("origSize").textContent = fmtBytes(m.original);
  $("newSize").textContent = fmtBytes(m.predicted);
  $("scaleMax").textContent = fmtBytes(m.original);
  $("meterTag").textContent = fmtBytes(m.predicted);
  $("origPaneSize").textContent = fmtBytes(m.original);

  // Breakdown stacked bar (of the predicted/resulting file).
  const total = Math.max(1, m.imageBytes + m.fontBytes + m.otherBytes);
  const ip = (m.imageBytes / total) * 100;
  const fp = (m.fontBytes / total) * 100;
  const op = 100 - ip - fp;
  $("segImg").style.width = ip + "%";
  $("segFont").style.width = fp + "%";
  $("segOther").style.width = op + "%";
  $("legImg").textContent = fmtBytes(m.imageBytes);
  $("legFont").textContent = fmtBytes(m.fontBytes);
  $("legOther").textContent = fmtBytes(m.otherBytes);
}

/* ----------------------------------------------------------- optimize flow */
async function runOptimize() {
  const rec = active();
  if (!rec) return;
  const btn = $("optimizeBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Optimizing…`;
  $("meterState").textContent = "working…";

  try {
    const body = currentRequestBody(rec);
    const data = await apiJSON("/api/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    rec.result = data;
    rec.optimized = true;
    const r = data.result;

    // Swap the meter to measured (green) numbers.
    updateMeter({
      original: r.original_size,
      predicted: r.optimized_size,
      pct: r.saved_pct,
      isResult: true,
      imageBytes: r.optimized_image_bytes,
      fontBytes: r.optimized_font_bytes,
      otherBytes: r.optimized_other_bytes,
    });
    $("meterState").textContent = r.used_ghostscript ? "result · distilled" : "result";

    // Target outcome banner with the real number.
    if (typeof data.target_met !== "undefined") {
      const banner = $("targetBanner");
      banner.classList.remove("hidden", "ok", "miss");
      banner.classList.add(data.target_met ? "ok" : "miss");
      banner.textContent = data.target_met
        ? `✓ Target met — final size ${fmtBytes(r.optimized_size)}.`
        : `Closest achievable size: ${fmtBytes(r.optimized_size)}.`;
    }

    $("optPaneSize").textContent = fmtBytes(r.optimized_size);
    $("downloadBtn").classList.remove("hidden");
    renderOptimizedPreview(rec);
    toast(`Done — ${r.saved_pct}% smaller (${fmtBytes(r.saved_bytes)} saved).`);
  } catch (e) {
    toast("Optimization failed: " + e.message, true);
    $("meterState").textContent = "estimate";
  } finally {
    btn.disabled = false;
    btn.textContent = "Optimize this PDF";
  }
}

/* ---------------------------------------------------------- PDF previews -- */
// We keep the rendered docs + current page per pane at module scope. Only the
// active file's previews are live at any moment, so two references suffice.
let origPdf = null,
  optPdf = null,
  origPage = 1,
  optPage = 1;

function resetOptimizedPane() {
  optPdf = null;
  optPage = 1;
  $("optCanvasWrap").innerHTML =
    `<div class="empty">Optimize to see the result here</div>`;
  $("optPager").innerHTML = "";
  $("optPaneSize").textContent = "—";
  $("downloadBtn").classList.add("hidden");
}

async function renderOriginalPreview(rec) {
  origPage = 1;
  await renderInto({
    url: API + "/api/file/" + rec.id + "/original",
    wrapId: "origCanvasWrap",
    pagerId: "origPager",
    which: "orig",
  });
}

async function renderOptimizedPreview(rec) {
  optPage = 1;
  await renderInto({
    url: API + "/api/file/" + rec.id + "/optimized",
    wrapId: "optCanvasWrap",
    pagerId: "optPager",
    which: "opt",
  });
}

/**
 * Load a PDF by URL and render the current page into a pane, wiring its pager.
 * `which` selects which module-level doc/page pair we update ('orig' | 'opt').
 */
async function renderInto({ url, wrapId, pagerId, which }) {
  if (!window["pdfjsLib"]) return; // offline / CDN blocked: leave empty state
  const wrap = $(wrapId);
  wrap.innerHTML = `<div class="empty">Rendering…</div>`;
  try {
    const pdf = await pdfjsLib.getDocument(url).promise;
    if (which === "orig") origPdf = pdf;
    else optPdf = pdf;
    await drawPage(which, wrapId, pagerId);
  } catch (e) {
    wrap.innerHTML = `<div class="empty">Preview unavailable<br><span class="mono">${e.message || ""}</span></div>`;
  }
}

async function drawPage(which, wrapId, pagerId) {
  const pdf = which === "orig" ? origPdf : optPdf;
  if (!pdf) return;
  let pageNum = which === "orig" ? origPage : optPage;
  pageNum = Math.max(1, Math.min(pdf.numPages, pageNum));
  if (which === "orig") origPage = pageNum;
  else optPage = pageNum;

  const page = await pdf.getPage(pageNum);
  const wrap = $(wrapId);
  // Fit width to the pane for a faithful "what it looks like" preview.
  const targetW = wrap.clientWidth - 24;
  const viewport0 = page.getViewport({ scale: 1 });
  const scale = Math.max(0.3, targetW / viewport0.width);
  const viewport = page.getViewport({ scale });

  const canvas = document.createElement("canvas");
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  const ctx = canvas.getContext("2d");
  wrap.innerHTML = "";
  wrap.appendChild(canvas);
  await page.render({ canvasContext: ctx, viewport }).promise;

  // Pager
  const pager = $(pagerId);
  pager.innerHTML = "";
  if (pdf.numPages > 1) {
    const prev = document.createElement("button");
    prev.textContent = "‹";
    prev.disabled = pageNum <= 1;
    prev.onclick = () => {
      if (which === "orig") origPage--;
      else optPage--;
      drawPage(which, wrapId, pagerId);
    };
    const next = document.createElement("button");
    next.textContent = "›";
    next.disabled = pageNum >= pdf.numPages;
    next.onclick = () => {
      if (which === "orig") origPage++;
      else optPage++;
      drawPage(which, wrapId, pagerId);
    };
    const label = document.createElement("span");
    label.textContent = `page ${pageNum} / ${pdf.numPages}`;
    pager.append(prev, label, next);
  }
}

/* -------------------------------------------------------------------- go --- */
boot();
