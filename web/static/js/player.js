"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const meta    = document.getElementById("episode-meta");
  const dateStr = meta?.dataset.date;
  if (!dateStr) return;

  const videoId   = meta?.dataset.videoId || "";
  const isYoutube = !!videoId;   // static: whether this episode has a YouTube video
  let   useYoutube = isYoutube;  // mutable: current playback mode

  const audio        = document.getElementById("audio-player");
  const transcriptEl = document.getElementById("transcript");
  const loadingEl    = document.getElementById("transcript-loading");
  const tooltip      = document.getElementById("tooltip");

  const panelVocab        = document.getElementById("panel-vocab");
  const panelGrammar      = document.getElementById("panel-grammar");
  const panelExpr         = document.getElementById("panel-expressions");
  const panelContext      = document.getElementById("panel-context");
  const modalTranscriptEl = document.getElementById("modal-transcript");
  const jumpPill          = document.getElementById("jump-now-pill");
  const modalJumpPill     = document.getElementById("modal-jump-pill");
  const transcriptCard    = document.getElementById("transcript-card");

  let segments   = [];
  let highlights = [];
  let currentIdx = -1;
  let showEn     = false;
  let showZh     = false;
  let showFurigana = true;
  let sidebarCollapsed = localStorage.getItem('mimichan.sidebarCollapsed') === 'true';
  let ytPlayer   = null;

  const isTouch = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  let isDesktopLayout = window.innerWidth >= 1024 || (!isTouch && window.innerWidth >= 768);
  let modalProgrammaticScroll = false;
  let modalProgrammaticScrollTimer = null;

  // ── Resume playback position ────────────────────────────────────────────
  // Position is keyed by episode + shared across audio/video toggle since
  // both represent the same underlying timeline.
  const RESUME_KEY        = `mimichan.resume.${dateStr}`;
  const RESUME_MIN_T       = 5;   // don't bother resuming inside the first 5s
  const RESUME_END_MARGIN  = 15;  // within 15s of the end counts as "finished"
  const RESUME_SAVE_EVERY  = 5000; // ms between periodic saves
  let resumeApplied  = false;
  // Start the throttle window at load time (not 0) so the first periodic tick
  // doesn't fire immediately — that race can capture a stale/zero position
  // (e.g. YouTube's getCurrentTime() before a seekTo() has settled) and
  // clobber a resume point that was just restored.
  let lastResumeSave  = Date.now();

  function loadResumeT() {
    try {
      const raw = localStorage.getItem(RESUME_KEY);
      if (!raw) return null;
      const t = JSON.parse(raw).t;
      return typeof t === "number" && t > 0 ? t : null;
    } catch { return null; }
  }

  function saveResumeT(t, force) {
    if (!force) {
      const now = Date.now();
      if (now - lastResumeSave < RESUME_SAVE_EVERY) return;
      lastResumeSave = now;
    }
    try {
      if (!(t > RESUME_MIN_T)) { localStorage.removeItem(RESUME_KEY); return; }
      localStorage.setItem(RESUME_KEY, JSON.stringify({ t, savedAt: Date.now() }));
    } catch { /* localStorage unavailable (private mode, quota) — resume just won't persist */ }
  }

  function clearResumeT() {
    try { localStorage.removeItem(RESUME_KEY); } catch {}
  }

  function formatResumeTime(t) {
    t = Math.max(0, Math.floor(t));
    const m = Math.floor(t / 60), s = t % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function showResumeToast(t) {
    const el = document.createElement("div");
    el.className = "fixed bottom-20 md:bottom-6 left-1/2 -translate-x-1/2 z-40 bg-gray-900/95 " +
      "border border-gray-700 text-gray-300 text-xs px-4 py-2 rounded-full shadow-lg " +
      "pointer-events-none transition-opacity duration-500";
    el.textContent = `Resumed at ${formatResumeTime(t)}`;
    document.body.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 500); }, 3000);
  }

  // Try to restore position once the given duration is known; skips episodes
  // resumed near the very end (treat as finished, don't loop back to the tail).
  function maybeApplyResume(duration, seekFn) {
    if (resumeApplied) return;
    const t = loadResumeT();
    if (t == null) { resumeApplied = true; return; }
    if (duration > 0 && t >= duration - RESUME_END_MARGIN) {
      clearResumeT();
      resumeApplied = true;
      return;
    }
    resumeApplied = true;
    seekFn(t);
    showResumeToast(t);
  }

  window.addEventListener("pagehide", () => {
    const t = useYoutube
      ? (ytPlayer?.getCurrentTime?.() || 0)
      : (audio?.currentTime || 0);
    if (t > 0) saveResumeT(t, true);
  });

  // ── Layout initialization ─────────────────────────────────────────────────
  // Tabbed sidebar with transcript tab only applies to desktop/iPad YouTube.
  // Audio and mobile always use the original layout (transcript in left col).

  if (isYoutube && isDesktopLayout) {
    // Move transcript elements from left-col card into sidebar panel-transcript
    const panelTranscript = document.getElementById("panel-transcript");
    if (panelTranscript) {
      [loadingEl, transcriptEl, jumpPill].forEach(el => {
        if (el) panelTranscript.appendChild(el);
      });
      transcriptEl.style.height = "";
      transcriptEl.classList.add("flex-1");
    }
    transcriptCard?.classList.add("hidden");

    // Default to Transcript tab on Desktop Video mode
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("tab-active"));
    document.querySelector('.tab-btn[data-tab="transcript"]')?.classList.add("tab-active");
    document.querySelectorAll("[data-panel]").forEach(p => p.classList.add("hidden"));
    if (panelTranscript) panelTranscript.classList.remove("hidden");
  } else if (isYoutube) {
    // Mobile YouTube: hide Transcript tab, activate Vocab instead
    const transcriptTab = document.querySelector('.tab-btn[data-tab="transcript"]');
    if (transcriptTab) {
      transcriptTab.classList.add("hidden");
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("tab-active"));
      document.querySelector('.tab-btn[data-tab="vocab"]')?.classList.add("tab-active");
      document.querySelectorAll("[data-panel]").forEach(p => p.classList.add("hidden"));
      document.getElementById("panel-vocab")?.classList.remove("hidden");
    }
  }

  // ── Vocab Storage Integration ───────────────────────────────────────────

  async function handleSaveVocab(e) {
    const btn = e.target.closest(".btn-anki");
    if (!btn) return;
    e.stopPropagation();
    
    const cardData = JSON.parse(btn.dataset.card);
    cardData.source_episode = dateStr;
    
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin h-3 w-3" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>';

    try {
      const resp = await fetch("/api/vocab", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cardData)
      });
      const result = await resp.json();
      
      if (result.status === "exists") {
        btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
        btn.title = "Already saved";
      } else {
        savedWords.add(cardData.front);  // keep in-session state in sync
        btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        btn.title = "Saved!";
      }
    } catch (err) {
      console.error(err);
      showToast("Error saving to vocab: " + err.message);
      btn.innerHTML = originalHTML;
      btn.disabled = false;
    }
  }

  // ── Helpers (Hoisted or defined before use) ────────────────────────────────

  /**
   * Shows a toast notification.
   * @param {string} message The message to display.
   * @param {"error"|"success"} type The type of toast.
   */
  function showToast(message, type = "error") {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className = "fixed bottom-4 right-4 z-50 flex flex-col gap-2";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    const bgColor = type === "error" ? "bg-red-500" : "bg-green-500";
    toast.className = `${bgColor} text-white px-4 py-2 rounded shadow-lg transition-opacity duration-300`;
    toast.innerText = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.classList.add("opacity-0");
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  function esc(str) {
    return String(str || "")
      .replace(/&/g,  "&amp;")
      .replace(/</g,  "&lt;")
      .replace(/>/g,  "&gt;")
      .replace(/"/g,  "&quot;");
  }

  function annotateWithTokens(tokens, hls) {
    if (!tokens || !tokens.length) return "";
    const text = tokens.map(t => t.w).join("");
    const intervals = [];
    for (const h of hls) {
      let pos = 0;
      while (pos < text.length) {
        const idx = text.indexOf(h.word, pos);
        if (idx === -1) break;
        intervals.push({ start: idx, end: idx + h.word.length, hl: h });
        pos = idx + h.word.length;
      }
    }
    intervals.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
    const kept = [];
    let cursor = 0;
    for (const iv of intervals) {
      if (iv.start >= cursor) { kept.push(iv); cursor = iv.end; }
    }
    let html = "";
    let charIdx = 0;
    for (const token of tokens) {
      const tokenStart = charIdx;
      const tokenEnd = charIdx + token.w.length;
      charIdx = tokenEnd;
      const hlInterval = kept.find(iv => (tokenStart < iv.end && tokenEnd > iv.start));
      let tokenHtml = "";
      if (token.kanji) {
        tokenHtml = `<ruby>${esc(token.w)}<rt>${esc(token.r)}</rt></ruby>`;
      } else {
        tokenHtml = esc(token.w);
      }
      if (hlInterval) {
        const hl = hlInterval.hl;
        const typeCls  = hl.type === "vocab" ? "hl-vocab" : "hl-grammar";
        const levelCls = "hl-" + (hl.level || "n2").toLowerCase();
        const data = JSON.stringify(hl).replace(/'/g, "&#39;");
        html += `<span class="${typeCls} ${levelCls}" data-hl='${data}'>${tokenHtml}</span>`;
      } else {
        html += tokenHtml;
      }
    }
    return html;
  }

  function annotateSegment(seg, hls) {
    if (seg.tokens && seg.tokens.length) {
      return annotateWithTokens(seg.tokens, hls);
    }
    return annotate(seg.ja, hls);
  }

  function annotate(text, hls) {
    if (!hls.length) return esc(text);
    const intervals = [];
    for (const h of hls) {
      let pos = 0;
      while (pos < text.length) {
        const idx = text.indexOf(h.word, pos);
        if (idx === -1) break;
        intervals.push({ start: idx, end: idx + h.word.length, hl: h });
        pos = idx + h.word.length;
      }
    }
    intervals.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
    const kept = [];
    let cursor = 0;
    for (const iv of intervals) {
      if (iv.start >= cursor) { kept.push(iv); cursor = iv.end; }
    }
    let html = "";
    let pos  = 0;
    for (const { start, end, hl } of kept) {
      html += esc(text.slice(pos, start));
      const typeCls  = hl.type === "vocab" ? "hl-vocab" : "hl-grammar";
      const levelCls = "hl-" + (hl.level || "n2").toLowerCase();
      const data = JSON.stringify(hl).replace(/'/g, "&#39;");
      html += `<span class="${typeCls} ${levelCls}" data-hl='${data}'>${esc(text.slice(start, end))}</span>`;
      pos = end;
    }
    html += esc(text.slice(pos));
    return html;
  }

  function renderTranscript() {
    transcriptEl.innerHTML = segments.map((seg, i) => {
      const jaHtml = annotateSegment(seg, allHighlights);
      return `<div class="segment" id="seg-${i}" data-start="${seg.start}" data-end="${seg.end}">
        <span class="segment-time">${esc(seg.time || "")}</span>
        <div class="segment-body">
          <div class="flex items-start justify-between">
            <div class="segment-ja">${jaHtml}</div>
            <button class="btn-explain ml-2 text-gray-700 hover:text-blue-400 transition-colors p-1" title="Explain this sentence">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            </button>
          </div>
          <div class="translation-en hidden"><span class="trans-tag">EN</span>${esc(seg.en || "")}</div>
          <div class="translation-zh hidden"><span class="trans-tag">ZH</span>${esc(seg.zh || "")}</div>
          <div class="explanation-box hidden mt-2 p-3 bg-gray-800/40 rounded-lg border border-gray-700 text-xs text-gray-300 leading-relaxed"></div>
        </div>
      </div>`;
    }).join("");
  }

  function renderModalTranscript() {
    if (!modalTranscriptEl) return;
    modalTranscriptEl.innerHTML = segments.map((seg, i) => {
      const jaHtml = annotateSegment(seg, allHighlights);
      return `<div class="modal-seg" id="modal-seg-${i}" data-start="${seg.start}" data-end="${seg.end}">
        <span class="modal-seg-time">${esc(seg.time || "")}</span>
        <div class="modal-seg-body">
          <div class="flex items-start justify-between">
            <div class="modal-seg-ja">${jaHtml}</div>
            <button class="btn-explain ml-2 text-gray-700 hover:text-blue-400 transition-colors p-1" title="Explain this sentence">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            </button>
          </div>
          ${seg.en ? `<div class="translation-en hidden"><span class="trans-tag">EN</span>${esc(seg.en)}</div>` : ""}
          ${seg.zh ? `<div class="translation-zh hidden"><span class="trans-tag">ZH</span>${esc(seg.zh)}</div>` : ""}
          <div class="explanation-box hidden mt-2 p-3 bg-gray-800/40 rounded-lg border border-gray-700 text-xs text-gray-300 leading-relaxed"></div>
        </div>
      </div>`;
    }).join("");
  }

  const COMPACT_NEAR = 2;
  function updateNearbySegments(idx) {
    if (isDesktopLayout) return; // Tabbed sidebar doesn't need compact mode
    const activeIdx = idx < 0 ? 0 : idx;
    const near = COMPACT_NEAR;
    segments.forEach((_, i) => {
      const el = document.getElementById(`seg-${i}`);
      if (!el) return;
      const dist = Math.abs(i - activeIdx);
      el.classList.toggle("seg-nb-hidden", dist > near);
      el.classList.toggle("seg-nb-near",   dist > 0 && dist <= near);
    });
  }

  function setActive(idx) {
    document.querySelectorAll(".segment.segment-active")
      .forEach(el => el.classList.remove("segment-active"));
    if (idx >= 0) {
      const el = document.getElementById(`seg-${idx}`);
      if (el) { el.classList.add("segment-active"); scrollActiveIntoView(idx, false); }
    }

    if (useYoutube && !isDesktopLayout) updateNearbySegments(idx);

    document.querySelectorAll(".modal-seg.modal-seg-active")
      .forEach(el => el.classList.remove("modal-seg-active"));
    if (idx >= 0) {
      const modalEl = document.getElementById(`modal-seg-${idx}`);
      if (modalEl) {
        modalEl.classList.add("modal-seg-active");
        if (transcriptModal && !transcriptModal.classList.contains("hidden")) {
          scrollModalToActive();
        }
      }
    }
    currentIdx = idx;
  }

  function scrollActiveIntoView(idx, force) {
    // If we are in compact mode (mobile video), don't scroll the container
    if (useYoutube && !isDesktopLayout) return;

    const el = document.getElementById(`seg-${idx}`);
    if (!el || (!force && !autoFollow)) return;

    // Use computed overflow style to distinguish container scroll from page scroll.
    // Checking scrollHeight alone is misleading because overflow:visible elements
    // can have scrollHeight > clientHeight without actually being scrollable.
    const overflowY = window.getComputedStyle(transcriptEl).overflowY;
    const isContainerScrollable = (overflowY === "auto" || overflowY === "scroll") &&
                                   transcriptEl.scrollHeight > transcriptEl.clientHeight + 1;
    const scrollTarget = isContainerScrollable ? transcriptEl : window;

    setProgrammaticScroll(scrollTarget);
    el.scrollIntoView({ behavior: "smooth", block: force ? "center" : "nearest" });
  }

  function scrollModalToActive() {
    if (!modalTranscriptEl || currentIdx < 0) return;
    const el = document.getElementById(`modal-seg-${currentIdx}`);
    if (!el) return;
    modalProgrammaticScroll = true;
    if (modalProgrammaticScrollTimer) clearTimeout(modalProgrammaticScrollTimer);
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    if ("onscrollend" in window) {
      modalTranscriptEl.addEventListener("scrollend", () => { modalProgrammaticScroll = false; }, { once: true });
    } else {
      const onScroll = () => {
        clearTimeout(modalProgrammaticScrollTimer);
        modalProgrammaticScrollTimer = setTimeout(() => {
          modalProgrammaticScroll = false;
          modalTranscriptEl.removeEventListener("scroll", onScroll);
        }, 100);
      };
      modalTranscriptEl.addEventListener("scroll", onScroll, { passive: true });
      modalProgrammaticScrollTimer = setTimeout(() => {
        modalProgrammaticScroll = false;
        modalTranscriptEl.removeEventListener("scroll", onScroll);
      }, 300);
    }
  }

  function seekTo(t) {
    if (useYoutube) {
      ytPlayer?.seekTo(t, true);
      ytPlayer?.playVideo();
    } else {
      if (audio) { audio.currentTime = t; audio.play().catch(() => {}); }
    }
    autoFollow = true;
    jumpPill?.classList.add("hidden");
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
  }

  // ── YouTube IFrame API ────────────────────────────────────────────────────

  async function waitForYTAPI() {
    if (window.YT?.Player || window._ytAPILoaded) return;
    await new Promise(resolve => {
      const timeout = setTimeout(resolve, 5000);
      window._ytAPIReady = () => { clearTimeout(timeout); resolve(); };
    });
  }

  function createYTPlayer(vid) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("YT Player timeout")), 8000);
      try {
        const player = new YT.Player("yt-player", {
          videoId: vid,
          width:   "100%",
          height:  "100%",
          playerVars: { rel: 0, modestbranding: 1, playsinline: 1 },
          events: {
            onReady: e => { clearTimeout(timeout); resolve(e.target); },
            onError: e => { clearTimeout(timeout); reject(new Error("YT Player error " + e.data)); }
          },
        });
      } catch (err) {
        clearTimeout(timeout);
        reject(err);
      }
    });
  }

  // ── Fetch data ────────────────────────────────────────────────────────────
  // Start YouTube player init immediately so it loads in parallel with data
  // fetches — the player doesn't need the transcript to begin initialising.
  const ytInitPromise = isYoutube
    ? waitForYTAPI().then(() => createYTPlayer(videoId))
    : Promise.resolve(null);

  // Vocab is only needed to mark already-saved words in sidebar cards, so
  // fetch it in parallel but don't block transcript rendering on it.
  const vocabPromise = fetch("/api/vocab").then(r => r.json()).catch(() => []);

  let transcriptData = { segments: [] };
  let analysisData   = { highlights: [], vocab: [], grammar: [], expressions: [] };
  let savedWords     = new Set();  // words already in the global vocab bank

  // Use presigned R2 URLs embedded at render time (one round trip, direct
  // from R2).  Fall back to the /api/ proxy route if the URL is absent or
  // returns 403 (expired after the 1-hour presigned window).
  async function fetchJson(presignedUrl, apiUrl) {
    if (presignedUrl) {
      const r = await fetch(presignedUrl);
      if (r.ok) return r.json();
      if (r.status !== 403 && r.status !== 400) throw r;
      // Presigned URL expired — fall through to the API route below.
    }
    const r = await fetch(apiUrl);
    if (!r.ok) throw r;
    return r.json();
  }

  const [transcriptResult, analysisResult] = await Promise.allSettled([
    fetchJson(meta?.dataset.transcriptUrl, `/api/episode/${dateStr}/transcript`),
    fetchJson(meta?.dataset.analysisUrl,   `/api/episode/${dateStr}/analysis`),
  ]);

  if (transcriptResult.status === "fulfilled") {
    transcriptData = transcriptResult.value;
  } else {
    loadingEl.textContent = "Could not load transcript (pipeline may still be running).";
    return;
  }

  if (analysisResult.status === "fulfilled") {
    analysisData = analysisResult.value;
  }

  // Resolve vocab (likely already done, given transcript takes longer)
  const vocabResult = await vocabPromise;
  if (Array.isArray(vocabResult)) {
    vocabResult.forEach(item => { if (item.word) savedWords.add(item.word); });
  }

  segments   = transcriptData.segments || [];
  highlights = analysisData.highlights || [];

  const ctxVocab      = (analysisData.vocab    || []).filter(v => v.level === "context-specific");
  const ctxGrammar    = (analysisData.grammar  || []).filter(g => g.level === "context-specific");
  const ctxHighlights = highlights.filter(h => h.level === "context-specific");

  const LEVEL_TIERS = {
    "beginner":              ["N5"],
    "beginner-intermediate": ["N4", "N3"],
    "intermediate":          ["N3"],
    "intermediate-advanced": ["N2"],
    "advanced":              ["N2", "N1"],
  };
  const episodeLevel = meta?.dataset.level || "";
  const allowedTiers = LEVEL_TIERS[episodeLevel] || null;

  if (allowedTiers) {
    highlights             = highlights.filter(h => allowedTiers.includes(h.level));
    analysisData.vocab     = (analysisData.vocab   || []).filter(v => allowedTiers.includes(v.level));
    analysisData.grammar   = (analysisData.grammar || []).filter(g => allowedTiers.includes(g.level));
  }

  const allHighlights = [...highlights, ...ctxHighlights];

  const panelTranscript = document.getElementById("panel-transcript");
  const transcriptCardBody = document.getElementById("transcript-card-body");
  const tabBtnTranscript = document.getElementById("tab-btn-transcript");

  function repositionTranscript() {
    // 1. Determine where the transcript belongs
    // - Desktop Video: Sidebar (panel-transcript)
    // - Desktop Audio: Main Area (transcript-card-body)
    // - Mobile: Main Area (transcript-card-body)
    
    const targetInSidebar = isDesktopLayout && useYoutube && !sidebarCollapsed;
    const targetParent = targetInSidebar ? panelTranscript : transcriptCardBody;

    if (transcriptEl.parentElement !== targetParent) {
      targetParent.appendChild(transcriptEl);
    }

    // 2. Manage visibility of UI elements
    // - Transcript tab button only visible in Desktop Video
    if (tabBtnTranscript) {
      tabBtnTranscript.classList.toggle("hidden", !targetInSidebar);
    }
    
    // - Transcript card only visible if NOT in sidebar
    if (transcriptCard) {
      transcriptCard.classList.toggle("hidden", targetInSidebar);
    }

    // - If it moved into the sidebar, make sure the panel is visible if the tab is active
    if (targetInSidebar) {
      const activeTab = document.querySelector(".tab-btn.tab-active")?.dataset.tab;
      panelTranscript.classList.toggle("hidden", activeTab !== "transcript");
    } else {
      // If moved to left col, ensure the sidebar switches to a card tab (like Vocab)
      const currentTab = document.querySelector(".tab-btn.tab-active");
      if (currentTab?.dataset.tab === "transcript") {
        document.querySelector('.tab-btn[data-tab="vocab"]')?.click();
      }
    }
    
    // 3. Update scroll logic targets
    updatePillPosition();
  }

  // ── Initial Render ────────────────────────────────────────────────────────

  renderTranscript();
  renderModalTranscript();
  renderVocab(analysisData.vocab        || [], savedWords);
  renderGrammar(analysisData.grammar    || [], savedWords);
  renderExpressions(analysisData.expressions || [], savedWords);
  renderContext([...ctxVocab, ...ctxGrammar],    savedWords);
  repositionTranscript();
  syncFuriganaUI();

  // Stats bar
  const vocabCount  = (analysisData.vocab || []).length + ctxVocab.length;
  const grammarCount = (analysisData.grammar || []).length + ctxGrammar.length;
  const exprCount   = (analysisData.expressions || []).length;
  const ctxCount    = ctxVocab.length + ctxGrammar.length;
  if (vocabCount + grammarCount + exprCount > 0) {
    const statsEl = document.createElement("div");
    statsEl.className = "px-3 py-2 border-b border-gray-800 flex items-center gap-2 flex-wrap";
    const chips = [];
    if (vocabCount)   chips.push(`<span class="stat-chip"><span class="text-gray-200 font-medium">${vocabCount}</span> vocab</span>`);
    if (grammarCount) chips.push(`<span class="stat-chip"><span class="text-gray-200 font-medium">${grammarCount}</span> grammar</span>`);
    if (exprCount)    chips.push(`<span class="stat-chip"><span class="text-gray-200 font-medium">${exprCount}</span> phrases</span>`);
    if (ctxCount)     chips.push(`<span class="stat-chip stat-chip-ctx"><span class="font-medium">${ctxCount}</span> ctx</span>`);
    statsEl.innerHTML = chips.join("");
    const panelHeader = transcriptCard?.querySelector(".border-b.border-gray-800");
    if (panelHeader) panelHeader.after(statsEl);
  }

  loadingEl.classList.add("hidden");
  transcriptEl.classList.remove("hidden");

  if (isYoutube) {
    if (!isDesktopLayout) transcriptEl.classList.add("compact-mode");
    updateNearbySegments(-1);
  }

  if (isDesktopLayout) {
    const nav = document.querySelector("nav");
    if (nav) document.documentElement.style.setProperty("--nav-h", nav.getBoundingClientRect().height + "px");
    document.body.classList.add("ep-desktop");
    if (isYoutube) document.body.classList.add("yt-mode");
    if (sidebarCollapsed) {
      document.body.classList.add("sidebar-collapsed");
      const lbl = document.getElementById('sidebar-btn-label');
      const ico = document.getElementById('sidebar-btn-icon');
      if (lbl) lbl.textContent = 'Show panel';
      if (ico) ico.querySelector('path')?.setAttribute('d', 'M15 18l-6-6 6-6');
    }
  }

  function checkLayout() {
    const newIsDesktopLayout = window.innerWidth >= 1024 || (!isTouch && window.innerWidth >= 768);
    if (newIsDesktopLayout !== isDesktopLayout) {
      isDesktopLayout = newIsDesktopLayout;
      repositionTranscript();
      if (isYoutube) {
        if (!isDesktopLayout && useYoutube) {
          transcriptEl.classList.add("compact-mode");
          transcriptEl.style.height = "35vh";
          updateNearbySegments(currentIdx);
        } else {
          transcriptEl.classList.remove("compact-mode");
          transcriptEl.style.height = "";
          segments.forEach((_, i) => {
            const el = document.getElementById(`seg-${i}`);
            if (el) el.classList.remove("seg-nb-hidden", "seg-nb-near");
          });
        }
      }
      if (isDesktopLayout) {
        document.body.classList.add("ep-desktop");
        if (useYoutube) document.body.classList.add("yt-mode");
      } else {
        document.body.classList.remove("ep-desktop");
        document.body.classList.remove("yt-mode");
      }
    }
  }

  window.addEventListener("resize", checkLayout, { passive: true });

  updatePillPosition();

  // ── Playback Speed ────────────────────────────────────────────────────────

  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
  let speedIdx = 2;
  const speedDisplay = document.getElementById("speed-display");

  function applySpeed(idx) {
    speedIdx = idx;
    const s = SPEEDS[speedIdx];
    if (useYoutube) { if (ytPlayer) ytPlayer.setPlaybackRate(s); }
    else if (audio) audio.playbackRate = s;
    if (speedDisplay) speedDisplay.textContent = s + "×";
    document.querySelectorAll(".speed-btn[data-speed]").forEach(b =>
      b.classList.toggle("speed-active", parseFloat(b.dataset.speed) === s)
    );
  }

  document.getElementById("speed-btns")?.addEventListener("click", e => {
    const btn = e.target.closest(".speed-btn[data-speed]");
    if (btn) applySpeed(SPEEDS.indexOf(parseFloat(btn.dataset.speed)));
  });
  document.getElementById("speed-down")?.addEventListener("click", () => { if (speedIdx > 0) applySpeed(speedIdx - 1); });
  document.getElementById("speed-up")?.addEventListener("click",   () => { if (speedIdx < SPEEDS.length - 1) applySpeed(speedIdx + 1); });

  // ── Auto-follow Logic ─────────────────────────────────────────────────────

  const AUTO_FOLLOW_INACTIVITY_MS = 8_000;
  let autoFollow = true;
  let autoFollowTimer = null;
  let programmaticScroll = false;
  let programmaticScrollTimer = null;

  function setProgrammaticScroll(scrollTarget) {
    programmaticScroll = true;
    if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer);
    if ("onscrollend" in window) {
      scrollTarget.addEventListener("scrollend", () => { programmaticScroll = false; }, { once: true });
    } else {
      const onScroll = () => {
        clearTimeout(programmaticScrollTimer);
        programmaticScrollTimer = setTimeout(() => {
          programmaticScroll = false;
          scrollTarget.removeEventListener("scroll", onScroll);
        }, 100);
      };
      scrollTarget.addEventListener("scroll", onScroll, { passive: true });
      programmaticScrollTimer = setTimeout(() => {
        programmaticScroll = false;
        scrollTarget.removeEventListener("scroll", onScroll);
      }, 300);
    }
  }

  function markUserNavigation() {
    if (programmaticScroll) return;
    autoFollow = false;
    jumpPill?.classList.remove("hidden");
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
    autoFollowTimer = setTimeout(() => {
      autoFollow = true;
      jumpPill?.classList.add("hidden");
      if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true);
    }, AUTO_FOLLOW_INACTIVITY_MS);
  }

  transcriptEl.addEventListener("scroll", () => { if (!programmaticScroll) markUserNavigation(); }, { passive: true });
  window.addEventListener("scroll",    () => { if (!isDesktopLayout && !useYoutube) markUserNavigation(); }, { passive: true });
  window.addEventListener("wheel",     () => { if (!useYoutube) markUserNavigation(); },            { passive: true });

  jumpPill?.addEventListener("click", () => {
    autoFollow = true;
    jumpPill.classList.add("hidden");
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
    if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true);
  });

  function updatePillPosition() {
    // Page-scroll mode (mobile, not YouTube video): pill must be fixed so it
    // stays visible as the page scrolls. Compact/desktop: absolute inside card.
    const pageScroll = !isDesktopLayout && !useYoutube;
    jumpPill?.classList.toggle("pill-fixed", pageScroll);
  }

  async function handleExplain(e) {
    const btn = e.target.closest(".btn-explain");
    if (!btn) return;
    e.stopPropagation();

    const segEl = btn.closest("[data-start]");
    const box = segEl.querySelector(".explanation-box");
    
    if (!box.classList.contains("hidden")) {
      box.classList.add("hidden");
      return;
    }

    if (box.innerHTML && !box.querySelector(".animate-pulse")) {
      box.classList.remove("hidden");
      return;
    }

    const jaText = segEl.querySelector(".segment-ja, .modal-seg-ja").innerText;
    box.innerHTML = '<span class="animate-pulse flex items-center gap-2"><svg class="animate-spin h-3 w-3 text-blue-400" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>Analyzing grammar...</span>';
    box.classList.remove("hidden");

    try {
      const resp = await fetch("/api/explain", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: jaText, episode: dateStr })
      });
      const data = await resp.json();
      if (data.explanation) {
        box.innerHTML = marked.parse(data.explanation);
        // Ensure links in explanation open in new tab
        box.querySelectorAll("a").forEach(a => a.target = "_blank");
      } else {
        box.innerHTML = '<span class="text-red-400">Error: ' + esc(data.error || "Could not explain") + "</span>";
      }
    } catch (err) {
      box.innerHTML = '<span class="text-red-400">Error: ' + esc(err.message) + "</span>";
    }
  }

  transcriptEl.addEventListener("click", e => {
    if (e.target.closest(".btn-explain")) {
      handleExplain(e);
      return;
    }
    if (e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (seg) seekTo(parseFloat(seg.dataset.start));
  });

  // ── Sync & Poll ───────────────────────────────────────────────────────────

  if (audio) {
    audio.addEventListener("loadedmetadata", () => {
      if (useYoutube) return;  // video is the active initial mode — YT branch resumes instead
      maybeApplyResume(audio.duration, t => { audio.currentTime = t; });
    });
    audio.addEventListener("timeupdate", () => {
      if (useYoutube) return;
      const t   = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
      saveResumeT(t);
    });
    audio.addEventListener("seeked", () => {
      if (useYoutube) return;
      const t   = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx >= 0) { setActive(idx); scrollActiveIntoView(idx, true); }
    });
    audio.addEventListener("pause", () => { if (!useYoutube) saveResumeT(audio.currentTime, true); });
    audio.addEventListener("ended", () => { if (!useYoutube) clearResumeT(); });
  }

  if (isYoutube) {
    // ytInitPromise was started before the data fetches; resolve it now.
    ytInitPromise.then(player => {
      ytPlayer = player;
      if (speedIdx !== 2) ytPlayer?.setPlaybackRate(SPEEDS[speedIdx]);
      if (useYoutube) maybeApplyResume(player.getDuration?.() || 0, t => player.seekTo(t, true));
    }).catch(err => {
      console.error("YouTube Player failed:", err);
      const p = document.getElementById("yt-player");
      if (p) p.innerHTML = `<div class="p-6 text-center text-gray-500">Video failed to load.</div>`;
    });

    setInterval(() => {
      if (!useYoutube || !ytPlayer || typeof ytPlayer.getCurrentTime !== "function") return;
      const t   = ytPlayer.getCurrentTime();
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
      saveResumeT(t);
    }, 250);
  }

  // ── Toggle & UI ───────────────────────────────────────────────────────────

  const btnTogglePlayer = document.getElementById("btn-toggle-player");
  const ytPlayerWrap    = document.getElementById("yt-player-wrap");
  const audioPlayerWrap = document.getElementById("audio-player-wrap");

  btnTogglePlayer?.addEventListener("click", () => {
    const currentTime = useYoutube ? (ytPlayer?.getCurrentTime() || 0) : (audio?.currentTime || 0);
    useYoutube = !useYoutube;
    if (useYoutube) {
      ytPlayerWrap?.classList.remove("hidden");
      audioPlayerWrap?.classList.add("hidden");
      audio?.pause();
      ytPlayer?.seekTo(currentTime, true);
      if (!isDesktopLayout) {
        transcriptEl.classList.add("compact-mode");
        transcriptEl.style.height = "35vh";
      }
      btnTogglePlayer.textContent = "Audio only";
      updateNearbySegments(currentIdx);
      if (isDesktopLayout) document.body.classList.add("yt-mode");
    } else {
      ytPlayerWrap?.classList.add("hidden");
      audioPlayerWrap?.classList.remove("hidden");
      ytPlayer?.pauseVideo();
      if (audio) audio.currentTime = currentTime;
      if (!isDesktopLayout) {
        transcriptEl.classList.remove("compact-mode");
        transcriptEl.style.height = "";
      }
      btnTogglePlayer.textContent = "Video";
      document.body.classList.remove("yt-mode");
      segments.forEach((_, i) => {
        const el = document.getElementById(`seg-${i}`);
        if (el) el.classList.remove("seg-nb-hidden", "seg-nb-near");
      });
    }
    repositionTranscript();
    updatePillPosition();
    if (!useYoutube && currentIdx >= 0) setTimeout(() => scrollActiveIntoView(currentIdx, true), 50);
  });

  // ── Sidebar collapse toggle (desktop only) ────────────────────────────────────
  const btnToggleSidebar = document.getElementById('btn-toggle-sidebar');
  btnToggleSidebar?.addEventListener('click', () => {
    sidebarCollapsed = !sidebarCollapsed;
    localStorage.setItem('mimichan.sidebarCollapsed', sidebarCollapsed);
    document.body.classList.toggle('sidebar-collapsed', sidebarCollapsed);
    const lbl = document.getElementById('sidebar-btn-label');
    const ico = document.getElementById('sidebar-btn-icon');
    if (lbl) lbl.textContent = sidebarCollapsed ? 'Show panel' : 'Hide panel';
    if (ico) ico.querySelector('path')?.setAttribute('d', sidebarCollapsed ? 'M15 18l-6-6 6-6' : 'M9 18l6-6-6-6');
    repositionTranscript();
    updatePillPosition();
  });

  function syncTranslationUI() {
    document.getElementById("toggle-en")?.classList.toggle("active", showEn);
    document.getElementById("toggle-zh")?.classList.toggle("active", showZh);
    const fabEn   = document.getElementById("fab-en-btn");
    const fabZh   = document.getElementById("fab-zh-btn");
    const fabMain = document.getElementById("fab-main");
    if (fabEn)   fabEn.classList.toggle("active", showEn);
    if (fabZh)   fabZh.classList.toggle("active", showZh);
    if (fabMain) fabMain.classList.toggle("fab-on", showEn || showZh || !showFurigana);
    [transcriptEl, modalTranscriptEl].forEach(container => {
      if (!container) return;
      container.querySelectorAll(".translation-en").forEach(el => el.classList.toggle("hidden", !showEn));
      container.querySelectorAll(".translation-zh").forEach(el => el.classList.toggle("hidden", !showZh));
    });
  }

  function syncFuriganaUI() {
    document.getElementById("toggle-furigana")?.classList.toggle("active", showFurigana);
    const fabFurigana = document.getElementById("fab-furigana-btn");
    if (fabFurigana) fabFurigana.classList.toggle("active", showFurigana);
    const fabMain = document.getElementById("fab-main");
    if (fabMain) fabMain.classList.toggle("fab-on", showEn || showZh || !showFurigana);
    [transcriptEl, modalTranscriptEl].forEach(container => {
      if (!container) return;
      container.classList.toggle("hide-furigana", !showFurigana);
    });
  }

  document.getElementById("toggle-en")?.addEventListener("click", () => { showEn = !showEn; syncTranslationUI(); });
  document.getElementById("toggle-zh")?.addEventListener("click", () => { showZh = !showZh; syncTranslationUI(); });
  document.getElementById("toggle-furigana")?.addEventListener("click", () => { showFurigana = !showFurigana; syncFuriganaUI(); });

  const fabMain       = document.getElementById("fab-main");
  const fabTray       = document.getElementById("fab-tray");
  const fabEnBtn      = document.getElementById("fab-en-btn");
  const fabZhBtn      = document.getElementById("fab-zh-btn");
  const fabFuriganaBtn = document.getElementById("fab-furigana-btn");
  let fabOpen = false;
  fabMain?.addEventListener("click",       e => { e.stopPropagation(); fabOpen ? closeTray() : openTray(); });
  fabEnBtn?.addEventListener("click",      e => { e.stopPropagation(); showEn = !showEn; syncTranslationUI(); });
  fabZhBtn?.addEventListener("click",      e => { e.stopPropagation(); showZh = !showZh; syncTranslationUI(); });
  fabFuriganaBtn?.addEventListener("click", e => { e.stopPropagation(); showFurigana = !showFurigana; syncFuriganaUI(); });
  function openTray()  { fabOpen = true;  fabTray?.classList.replace("fab-tray-hidden", "fab-tray-visible"); }
  function closeTray() { fabOpen = false; fabTray?.classList.replace("fab-tray-visible", "fab-tray-hidden"); }
  document.addEventListener("click", closeTray);

  // ── Keyboard ──────────────────────────────────────────────────────────────

  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.isContentEditable) return;
    switch (e.key) {
      case " ": e.preventDefault();
        if (useYoutube) { if (ytPlayer?.getPlayerState() === 1) ytPlayer.pauseVideo(); else ytPlayer?.playVideo(); }
        else if (audio) { if (audio.paused) audio.play().catch(()=>{}); else audio.pause(); }
        break;
      case "ArrowLeft":  if (useYoutube) ytPlayer?.seekTo(Math.max(0, ytPlayer.getCurrentTime() - 5), true); else if (audio) audio.currentTime -= 5; break;
      case "ArrowRight": if (useYoutube) ytPlayer?.seekTo(ytPlayer.getCurrentTime() + 5, true); else if (audio) audio.currentTime += 5; break;
      case "ArrowUp":    if (currentIdx > 0) seekTo(segments[currentIdx - 1].start); break;
      case "ArrowDown":  if (currentIdx < segments.length - 1) seekTo(segments[currentIdx + 1].start); break;
      case "e": showEn = !showEn; syncTranslationUI(); break;
      case "c": showZh = !showZh; syncTranslationUI(); break;
      case "f": showFurigana = !showFurigana; syncFuriganaUI(); break;
      case "[": if (speedIdx > 0) applySpeed(speedIdx - 1); break;
      case "]": if (speedIdx < SPEEDS.length - 1) applySpeed(speedIdx + 1); break;
    }
  });

  // ── Tabs & Drawer ─────────────────────────────────────────────────────────

  const drawerLabel = document.getElementById("drawer-label");
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("tab-active"));
      btn.classList.add("tab-active");
      document.querySelectorAll("[data-panel]").forEach(p => p.classList.add("hidden"));
      document.querySelector(`[data-panel="${btn.dataset.tab}"]`).classList.remove("hidden");
      if (drawerLabel) drawerLabel.textContent = btn.textContent.trim();
      if (btn.dataset.tab === "transcript" && currentIdx >= 0) {
        requestAnimationFrame(() => scrollActiveIntoView(currentIdx, true));
      }
    });
  });

  const sidePanel = document.getElementById("side-panel");
  sidePanel?.addEventListener("click", handleSaveVocab);
  const drawerOverlay = document.getElementById("drawer-overlay");
  const btnOpenDrawer = document.getElementById("btn-open-drawer");
  const drawerHandle  = document.getElementById("drawer-handle");
  btnOpenDrawer?.addEventListener("click", () => { sidePanel.classList.add("drawer-open"); drawerOverlay.classList.remove("hidden"); });
  drawerOverlay?.addEventListener("click", () => { sidePanel.classList.remove("drawer-open"); drawerOverlay.classList.add("hidden"); });
  drawerHandle?.addEventListener("click",  () => { sidePanel.classList.remove("drawer-open"); drawerOverlay.classList.add("hidden"); });

  // ── Tooltips & Modals ─────────────────────────────────────────────────────

  function showTooltip(hl, x, y) {
    const lvl = (hl.level || "").toLowerCase();
    tooltip.innerHTML = `<div><span class="tt-word">${esc(hl.word)}</span><span class="tt-reading">【${esc(hl.reading)}】</span><span class="tt-badge tt-badge-${lvl}">${esc(hl.level)}</span></div><div class="tt-register">${esc(hl.register)}</div><div class="tt-en">${esc(hl.en)}</div><div class="tt-zh">${esc(hl.zh)}</div>`;
    tooltip.style.left = x + "px"; tooltip.style.top = y + "px"; tooltip.classList.remove("hidden");
  }

  function setupTouchTooltips(container) {
    let activeSpan = null;
    container.addEventListener("click", e => {
      const span = e.target.closest("[data-hl]");
      if (!span || span === activeSpan) { tooltip.classList.add("hidden"); activeSpan = null; return; }
      let hl = JSON.parse(span.dataset.hl);
      const rect = span.getBoundingClientRect();
      const db = document.getElementById("drawer-trigger-bar");
      const dbRect = db ? db.getBoundingClientRect() : null;
      const bc = (dbRect && dbRect.height > 0) ? (window.innerHeight - dbRect.top + 8) : 0;
      showTooltip(hl, Math.max(8, Math.min(rect.left, window.innerWidth - 268)), Math.min(rect.bottom + 8, window.innerHeight - 160 - bc));
      activeSpan = span;
    });
  }

  if (isTouch) { setupTouchTooltips(transcriptEl); document.addEventListener("scroll", () => tooltip.classList.add("hidden"), { passive: true }); }
  else {
    let hoveredSpan = null;
    document.addEventListener("mouseover", e => {
      const span = e.target.closest("[data-hl]");
      if (span && span !== hoveredSpan) {
        showTooltip(JSON.parse(span.dataset.hl), e.clientX + 14, e.clientY + 14);
        hoveredSpan = span;
      }
    });
    document.addEventListener("mouseout", e => {
      const to = e.relatedTarget;
      if (!to || !to.closest?.("[data-hl]")) {
        tooltip.classList.add("hidden");
        hoveredSpan = null;
      }
    });
  }

  tooltip.addEventListener("click", () => tooltip.classList.add("hidden"));

  const transcriptModal = document.getElementById("transcript-modal");
  const modalClose = document.getElementById("modal-close");
  const btnFullTranscriptNodes = document.querySelectorAll(".btn-full-transcript");
  let bodyScrollPos = 0;
  
  function lockBodyScroll() {
    bodyScrollPos = window.scrollY;
    document.body.style.position = "fixed";
    document.body.style.top = `-${bodyScrollPos}px`;
    document.body.style.width = "100%";
  }
  
  function unlockBodyScroll() {
    document.body.style.position = "";
    document.body.style.top = "";
    document.body.style.width = "";
    window.scrollTo(0, bodyScrollPos);
  }

  btnFullTranscriptNodes.forEach(btn => btn.addEventListener("click", () => { transcriptModal.classList.remove("hidden"); lockBodyScroll(); setTimeout(scrollModalToActive, 50); }));
  modalClose?.addEventListener("click", () => { transcriptModal.classList.add("hidden"); unlockBodyScroll(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !transcriptModal?.classList.contains("hidden")) { transcriptModal?.classList.add("hidden"); unlockBodyScroll(); } });
  modalTranscriptEl?.addEventListener("scroll", () => { if (!modalProgrammaticScroll) modalJumpPill?.classList.remove("hidden"); }, { passive: true });
  modalJumpPill?.addEventListener("click", () => { modalJumpPill.classList.add("hidden"); scrollModalToActive(); });
  modalTranscriptEl?.addEventListener("click", e => {
    if (e.target.closest(".btn-explain")) {
      handleExplain(e);
      return;
    }
    if (e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (seg) { seekTo(parseFloat(seg.dataset.start)); transcriptModal.classList.add("hidden"); unlockBodyScroll(); }
  });
  if (isTouch && modalTranscriptEl) setupTouchTooltips(modalTranscriptEl);

  function jishoLinkHTML(term) {
    const url = `https://jisho.org/search/${encodeURIComponent(term)}`;
    return `<a href="${url}" target="_blank" rel="noopener" class="text-[10px] text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1">Jisho ↗</a>`;
  }

  function savedBtnHTML(isSaved, cardJson) {
    if (isSaved) {
      return `<button class="btn-anki text-gray-400 p-1" title="Already saved" disabled><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></button>`;
    }
    return `<button class="btn-anki text-gray-500 hover:text-blue-400 p-1" title="Save to vocab" data-card='${cardJson}'><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button>`;
  }

  /**
   * Renders the vocabulary cards.
   * @param {Array<{word: string, reading: string, en: string, zh: string, level: string}>} v Vocabulary array
   * @param {Set<string>} saved Already-saved word set
   */
  function renderVocab(v, saved) {
    saved = saved || savedWords;
    panelVocab.innerHTML = v.length ? v.map(item => {
      const isSaved = saved.has(item.word);
      const cardJson = JSON.stringify({
        type: "vocab",
        front: item.word,
        reading: item.reading,
        en: item.en,
        zh: item.zh,
        example: item.example,
        level: item.level,
        tags: `japanese vocab ${item.level || ""}`.trim()
      }).replace(/'/g, "&#39;");
      return `<div class="card"><div class="card-front">${esc(item.word)}<span class="card-reading">【${esc(item.reading)}】</span><span class="card-level card-level-${(item.level||"").toLowerCase()} ml-auto">${esc(item.level)}</span></div><div class="card-body"><div class="card-en">${esc(item.en)}</div><div class="card-zh">${esc(item.zh)}</div><div class="flex items-center justify-between mt-1">${jishoLinkHTML(item.word)}${isSaved ? savedBtnHTML(true) : savedBtnHTML(false, cardJson)}</div></div></div>`;
    }).join("") : `<p class="panel-empty">No vocab</p>`;
  }

  /**
   * Renders the grammar cards.
   * @param {Array<{pattern: string, reading: string, meaning_en: string, meaning_zh: string, level: string}>} g Grammar array
   * @param {Set<string>} saved Already-saved word set
   */
  function renderGrammar(g, saved) {
    saved = saved || savedWords;
    panelGrammar.innerHTML = g.length ? g.map(item => {
      const isSaved = saved.has(item.pattern);
      const cardJson = JSON.stringify({
        type: "grammar",
        front: item.pattern,
        reading: item.reading,
        en: item.meaning_en,
        zh: item.meaning_zh,
        example: item.example,
        level: item.level,
        tags: `japanese grammar ${item.level || ""}`.trim()
      }).replace(/'/g, "&#39;");
      return `<div class="card"><div class="card-front">${esc(item.pattern)}<span class="card-level card-level-${(item.level||"").toLowerCase()} ml-auto">${esc(item.level)}</span></div><div class="card-body"><div class="card-en">${esc(item.meaning_en)}</div><div class="card-zh">${esc(item.meaning_zh)}</div><div class="flex items-center justify-between mt-1">${jishoLinkHTML(item.pattern)}${isSaved ? savedBtnHTML(true) : savedBtnHTML(false, cardJson)}</div></div></div>`;
    }).join("") : `<p class="panel-empty">No grammar</p>`;
  }

  /**
   * Renders the expressions cards.
   * @param {Array<{expression: string, reading: string, en: string, zh: string}>} e Expressions array
   * @param {Set<string>} saved Already-saved word set
   */
  function renderExpressions(e, saved) {
    saved = saved || savedWords;
    panelExpr.innerHTML = e.length ? e.map(item => {
      const isSaved = saved.has(item.expression);
      const cardJson = JSON.stringify({
        type: "expression",
        front: item.expression,
        reading: item.reading,
        en: item.en,
        zh: item.zh,
        example: item.context,
        tags: "japanese expression"
      }).replace(/'/g, "&#39;");
      return `<div class="card"><div class="card-front">${esc(item.expression)}<span class="card-reading">【${esc(item.reading)}】</span></div><div class="card-body"><div class="card-en">${esc(item.en)}</div><div class="card-zh">${esc(item.zh)}</div><div class="flex items-center justify-between mt-1">${jishoLinkHTML(item.expression)}${isSaved ? savedBtnHTML(true) : savedBtnHTML(false, cardJson)}</div></div></div>`;
    }).join("") : `<p class="panel-empty">No expressions</p>`;
  }

  /**
   * Renders the context-specific vocabulary and grammar cards.
   * @param {Array<Object>} c Context array
   * @param {Set<string>} saved Already-saved word set
   */
  function renderContext(c, saved) {
    saved = saved || savedWords;
    panelContext.innerHTML = c.length ? c.map(item => {
      const word = item.word || item.pattern;
      const isSaved = saved.has(word);
      const cardJson = JSON.stringify({
        type: "context-specific",
        front: word,
        reading: item.reading,
        en: item.en || item.meaning_en,
        zh: item.zh || item.meaning_zh,
        example: item.example,
        level: "context-specific",
        tags: "japanese context-specific"
      }).replace(/'/g, "&#39;");
      return `<div class="card" style="border-color:rgba(167,139,250,0.2);"><div class="card-front">${esc(word)}${item.reading?`<span class="card-reading">【${esc(item.reading)}】</span>`:""}<span class="card-level card-level-context-specific ml-auto">ctx</span></div><div class="card-body"><div class="card-en">${esc(item.en||item.meaning_en)}</div><div class="card-zh">${esc(item.zh||item.meaning_zh)}</div><div class="flex items-center justify-between mt-1">${jishoLinkHTML(word)}${isSaved ? savedBtnHTML(true) : savedBtnHTML(false, cardJson)}</div></div></div>`;
    }).join("") : `<p class="panel-empty">No ctx</p>`;
  }
});