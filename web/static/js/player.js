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
  let ytPlayer   = null;

  const isTouch = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  const inYtDesktopMode = () => useYoutube && !isTouch && window.innerWidth >= 768;

  // ── Playback speed ────────────────────────────────────────────────────────

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
    if (!btn) return;
    applySpeed(SPEEDS.indexOf(parseFloat(btn.dataset.speed)));
  });
  document.getElementById("speed-down")?.addEventListener("click", () => {
    if (speedIdx > 0) applySpeed(speedIdx - 1);
  });
  document.getElementById("speed-up")?.addEventListener("click", () => {
    if (speedIdx < SPEEDS.length - 1) applySpeed(speedIdx + 1);
  });

  // ── Auto-follow ───────────────────────────────────────────────────────────

  const AUTO_FOLLOW_INACTIVITY_MS = 8_000;
  let autoFollow = true;
  let autoFollowTimer = null;
  let programmaticScroll = false;
  let programmaticScrollTimer = null;

  function setProgrammaticScroll(scrollTarget) {
    programmaticScroll = true;
    if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer);
    if ("onscrollend" in scrollTarget) {
      scrollTarget.addEventListener("scrollend", () => { programmaticScroll = false; }, { once: true });
    } else {
      programmaticScrollTimer = setTimeout(() => { programmaticScroll = false; }, 1200);
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
      if (useYoutube) { if (currentIdx >= 0) updateNearbySegments(currentIdx); }
      else            { if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true); }
    }, AUTO_FOLLOW_INACTIVITY_MS);
  }

  // Scrolling within the transcript panel (desktop + YouTube mode)
  transcriptEl.addEventListener("scroll", () => {
    if (!programmaticScroll) markUserNavigation();
  }, { passive: true });

  // Mobile audio: transcript overflows the page — catch window-level events
  window.addEventListener("touchmove", () => { if (isTouch && !useYoutube) markUserNavigation(); }, { passive: true });
  window.addEventListener("wheel",     () => { if (!useYoutube) markUserNavigation(); },            { passive: true });

  function scrollActiveIntoView(idx, force) {
    if (useYoutube) return; // segments managed by updateNearbySegments
    const el = document.getElementById(`seg-${idx}`);
    if (!el || (!force && !autoFollow)) return;

    const containerRect = transcriptEl.getBoundingClientRect();
    const elRect        = el.getBoundingClientRect();

    if (transcriptEl.scrollHeight > transcriptEl.clientHeight) {
      // Desktop audio: scroll within the transcript panel
      const inView = elRect.top >= containerRect.top && elRect.bottom <= containerRect.bottom;
      if (!force && inView) return;

      setProgrammaticScroll(transcriptEl);
      el.scrollIntoView({ behavior: "smooth", block: force ? "center" : "nearest" });
    } else {
      // Mobile audio: transcript flows with the page — measure actual sticky element bounds
      const audioWrap = document.getElementById("audio-player-wrap");
      const drawerBar = document.getElementById("drawer-trigger-bar");
      const topClear  = audioWrap ? audioWrap.getBoundingClientRect().bottom : 120;
      const botClear  = drawerBar ? window.innerHeight - drawerBar.getBoundingClientRect().top : 120;
      const inView    = elRect.top >= topClear && elRect.bottom <= (window.innerHeight - botClear);
      if (!force && inView) return;

      setProgrammaticScroll(window);
      el.scrollIntoView({ behavior: "smooth", block: force ? "center" : "nearest" });
    }
  }

  // ── YouTube IFrame API ────────────────────────────────────────────────────

  async function waitForYTAPI() {
    if (window.YT?.Player || window._ytAPILoaded) return;
    await new Promise(resolve => { window._ytAPIReady = resolve; });
  }

  async function createYTPlayer(vid) {
    return new Promise(resolve => {
      new YT.Player("yt-player", {
        videoId: vid,
        width:   "100%",
        height:  "100%",
        playerVars: { rel: 0, modestbranding: 1, origin: window.location.origin },
        events: { onReady: e => resolve(e.target) },
      });
    });
  }

  // ── Fetch data ────────────────────────────────────────────────────────────

  let transcriptData = { segments: [] };
  let analysisData   = { highlights: [], vocab: [], grammar: [], expressions: [] };

  // Fetch transcript and analysis independently — show what we can even if one fails
  const [transcriptResult, analysisResult] = await Promise.allSettled([
    fetch(`/api/episode/${dateStr}/transcript`).then(r => { if (!r.ok) throw r; return r.json(); }),
    fetch(`/api/episode/${dateStr}/analysis`).then(r => { if (!r.ok) throw r; return r.json(); }),
  ]);

  if (transcriptResult.status === "fulfilled") {
    transcriptData = transcriptResult.value;
  } else {
    loadingEl.textContent = "Could not load transcript (pipeline may still be running).";
    return;
  }

  if (analysisResult.status === "fulfilled") {
    analysisData = analysisResult.value;
  } else {
    console.warn("Analysis data not available — showing transcript without highlights");
  }

  segments   = transcriptData.segments || [];
  highlights = analysisData.highlights || [];

  // ── Filter highlights to episode level ────────────────────────────────────

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

  // ── Render ────────────────────────────────────────────────────────────────

  renderTranscript();
  renderModalTranscript();
  renderVocab(analysisData.vocab        || []);
  renderGrammar(analysisData.grammar    || []);
  renderExpressions(analysisData.expressions || []);
  renderContext([...ctxVocab, ...ctxGrammar]);

  // ── Episode stats bar ──────────────────────────────────────────────────────
  const vocabCount  = (analysisData.vocab || []).length + ctxVocab.length;
  const grammarCount = (analysisData.grammar || []).length + ctxGrammar.length;
  const exprCount   = (analysisData.expressions || []).length;
  const ctxCount    = ctxVocab.length + ctxGrammar.length;
  if (vocabCount + grammarCount + exprCount > 0) {
    const statsEl = document.createElement("div");
    statsEl.className = "text-xs text-gray-500 px-3 py-1.5 border-b border-gray-800 flex items-center gap-3 flex-wrap";
    const parts = [];
    if (vocabCount)  parts.push(`<span class="text-gray-400">${vocabCount}</span> vocab`);
    if (grammarCount) parts.push(`<span class="text-gray-400">${grammarCount}</span> grammar`);
    if (exprCount)   parts.push(`<span class="text-gray-400">${exprCount}</span> expressions`);
    if (ctxCount)    parts.push(`<span style="color:#a78bfa">${ctxCount}</span> context-specific`);
    statsEl.innerHTML = parts.join(' <span class="text-gray-700">·</span> ');
    // Insert after the panel header in transcript card
    const panelHeader = transcriptCard?.querySelector(".border-b.border-gray-800");
    if (panelHeader) panelHeader.after(statsEl);
  }

  loadingEl.classList.add("hidden");
  transcriptEl.classList.remove("hidden");
  if (isYoutube) {
    transcriptEl.classList.add("compact-mode");
    updateNearbySegments(-1);
  }

  if (!isTouch && window.innerWidth >= 768) {
    const nav = document.querySelector("nav");
    if (nav) document.documentElement.style.setProperty("--nav-h", nav.getBoundingClientRect().height + "px");
    document.body.classList.add("ep-desktop");
    if (isYoutube) document.body.classList.add("yt-mode");
  }

  updatePillPosition();

  // ── Seek helper ───────────────────────────────────────────────────────────

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

  jumpPill?.addEventListener("click", () => {
    autoFollow = true;
    jumpPill.classList.add("hidden");
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
    if (useYoutube) { if (currentIdx >= 0) updateNearbySegments(currentIdx); }
    else            { if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true); }
  });

  function updatePillPosition() {
    jumpPill?.classList.toggle("pill-fixed", isTouch && !useYoutube);
  }

  // Transcript click-to-seek
  transcriptEl.addEventListener("click", e => {
    if (isTouch && e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (!seg) return;
    seekTo(parseFloat(seg.dataset.start));
  });

  // ── Playback sync ─────────────────────────────────────────────────────────

  // Audio listeners run always; guard with !useYoutube so they go silent in video mode
  if (audio) {
    audio.addEventListener("timeupdate", () => {
      if (useYoutube) return;
      const t   = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
    });
    audio.addEventListener("seeked", () => {
      if (useYoutube) return;
      const t   = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx >= 0) { setActive(idx); scrollActiveIntoView(idx, true); }
    });
  }

  if (isYoutube) {
    await waitForYTAPI();
    ytPlayer = await createYTPlayer(videoId);

    if (speedIdx !== 2) ytPlayer.setPlaybackRate(SPEEDS[speedIdx]);

    // Poll current time; guard with useYoutube so it goes silent in audio mode
    setInterval(() => {
      if (!useYoutube) return;
      const t   = ytPlayer.getCurrentTime();
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
    }, 250);
  }

  // ── Video / audio toggle ──────────────────────────────────────────────────

  const btnTogglePlayer = document.getElementById("btn-toggle-player");
  const ytPlayerWrap    = document.getElementById("yt-player-wrap");
  const audioPlayerWrap = document.getElementById("audio-player-wrap");

  btnTogglePlayer?.addEventListener("click", () => {
    const currentTime = useYoutube
      ? (ytPlayer?.getCurrentTime() || 0)
      : (audio?.currentTime || 0);

    useYoutube = !useYoutube;

    if (useYoutube) {
      ytPlayerWrap?.classList.remove("hidden");
      audioPlayerWrap?.classList.add("hidden");
      audio?.pause();
      ytPlayer?.seekTo(currentTime, true);
      transcriptEl.classList.add("compact-mode");
      transcriptEl.classList.remove("flex-1");
      transcriptEl.style.height = "35vh";
      transcriptCard?.classList.remove("flex-1");
      btnTogglePlayer.textContent = "Audio only";
      btnFullTranscript?.classList.remove("hidden");
      updateNearbySegments(currentIdx);
      if (!isTouch && window.innerWidth >= 768) document.body.classList.add("yt-mode");
    } else {
      ytPlayerWrap?.classList.add("hidden");
      audioPlayerWrap?.classList.remove("hidden");
      ytPlayer?.pauseVideo();
      if (audio) audio.currentTime = currentTime;
      if (transcriptModal && !transcriptModal.classList.contains("hidden")) closeModal();
      transcriptEl.classList.remove("compact-mode");
      transcriptEl.classList.add("flex-1");
      transcriptEl.style.height = "";
      transcriptCard?.classList.add("flex-1");
      btnTogglePlayer.textContent = "Video";
      btnFullTranscript?.classList.add("hidden");
      document.body.classList.remove("yt-mode");
      segments.forEach((_, i) => {
        const el = document.getElementById(`seg-${i}`);
        if (el) el.classList.remove("seg-nb-hidden", "seg-nb-near");
      });
    }

    updatePillPosition();
    if (!useYoutube && currentIdx >= 0) setTimeout(() => scrollActiveIntoView(currentIdx, true), 50);
  });

  // ── Translation toggles ───────────────────────────────────────────────────

  function syncTranslationUI() {
    document.getElementById("toggle-en").classList.toggle("active", showEn);
    document.getElementById("toggle-zh").classList.toggle("active", showZh);
    const fabEn   = document.getElementById("fab-en-btn");
    const fabZh   = document.getElementById("fab-zh-btn");
    const fabMain = document.getElementById("fab-main");
    if (fabEn)   fabEn.classList.toggle("active", showEn);
    if (fabZh)   fabZh.classList.toggle("active", showZh);
    if (fabMain) fabMain.classList.toggle("fab-on", showEn || showZh);
    transcriptEl.querySelectorAll(".translation-en").forEach(el => el.classList.toggle("hidden", !showEn));
    transcriptEl.querySelectorAll(".translation-zh").forEach(el => el.classList.toggle("hidden", !showZh));
  }

  document.getElementById("toggle-en")?.addEventListener("click", () => { showEn = !showEn; syncTranslationUI(); });
  document.getElementById("toggle-zh")?.addEventListener("click", () => { showZh = !showZh; syncTranslationUI(); });

  // ── Translation FAB ───────────────────────────────────────────────────────

  const fabMain  = document.getElementById("fab-main");
  const fabTray  = document.getElementById("fab-tray");
  const fabEnBtn = document.getElementById("fab-en-btn");
  const fabZhBtn = document.getElementById("fab-zh-btn");
  let fabOpen = false;

  function openFab()  { fabOpen = true;  fabTray?.classList.replace("fab-tray-hidden", "fab-tray-visible"); }
  function closeFab() { fabOpen = false; fabTray?.classList.replace("fab-tray-visible", "fab-tray-hidden"); }

  fabMain?.addEventListener("click",  e => { e.stopPropagation(); fabOpen ? closeFab() : openFab(); });
  fabEnBtn?.addEventListener("click", e => { e.stopPropagation(); showEn = !showEn; syncTranslationUI(); });
  fabZhBtn?.addEventListener("click", e => { e.stopPropagation(); showZh = !showZh; syncTranslationUI(); });
  document.addEventListener("click",  () => { if (fabOpen) closeFab(); });

  // One-time discovery hint for the translation FAB
  const fabHint = document.getElementById("fab-hint");
  if (fabHint && !localStorage.getItem("jp-fab-seen")) {
    localStorage.setItem("jp-fab-seen", "1");
    fabHint.classList.remove("hidden");
    setTimeout(() => {
      fabHint.classList.add("fab-hint-fade");

      setTimeout(() => fabHint.classList.add("hidden"), 500);
    }, 3500);
  }

  // ── Keyboard shortcuts ──────────────────────────────────────────────────────

  document.addEventListener("keydown", e => {
    // Skip when focus is in an input, textarea, or contenteditable
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable) return;

    switch (e.key) {
      case " ": { // Space — play/pause
        e.preventDefault();
        if (useYoutube) {
          if (!ytPlayer) break;
          const state = ytPlayer.getPlayerState();
          state === 1 ? ytPlayer.pauseVideo() : ytPlayer.playVideo();
        } else if (audio) {
          audio.paused ? audio.play().catch(() => {}) : audio.pause();
        }
        break;
      }
      case "ArrowLeft": { // ← seek −5s
        e.preventDefault();
        if (useYoutube && ytPlayer) ytPlayer.seekTo(Math.max(0, ytPlayer.getCurrentTime() - 5), true);
        else if (audio) audio.currentTime = Math.max(0, audio.currentTime - 5);
        break;
      }
      case "ArrowRight": { // → seek +5s
        e.preventDefault();
        if (useYoutube && ytPlayer) ytPlayer.seekTo(ytPlayer.getCurrentTime() + 5, true);
        else if (audio) audio.currentTime += 5;
        break;
      }
      case "ArrowUp": { // ↑ previous segment
        e.preventDefault();
        if (currentIdx > 0) seekTo(segments[currentIdx - 1].start);
        break;
      }
      case "ArrowDown": { // ↓ next segment
        e.preventDefault();
        if (currentIdx < segments.length - 1) seekTo(segments[currentIdx + 1].start);
        break;
      }
      case "e": { // Toggle English
        showEn = !showEn;
        syncTranslationUI();
        break;
      }
      case "c": { // Toggle Chinese
        showZh = !showZh;
        syncTranslationUI();
        break;
      }
      case "[": { // Speed down
        if (speedIdx > 0) applySpeed(speedIdx - 1);
        break;
      }
      case "]": { // Speed up
        if (speedIdx < SPEEDS.length - 1) applySpeed(speedIdx + 1);
        break;
      }
    }
  });

  // ── Side-panel tab switching ──────────────────────────────────────────────

  const drawerLabel = document.getElementById("drawer-label");

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("tab-active"));
      btn.classList.add("tab-active");
      document.querySelectorAll("[data-panel]").forEach(p => p.classList.add("hidden"));
      document.querySelector(`[data-panel="${btn.dataset.tab}"]`).classList.remove("hidden");
      if (drawerLabel) drawerLabel.textContent = btn.textContent.trim();
    });
  });

  // ── Mobile bottom drawer ──────────────────────────────────────────────────

  const sidePanel     = document.getElementById("side-panel");
  const drawerOverlay = document.getElementById("drawer-overlay");
  const btnOpenDrawer = document.getElementById("btn-open-drawer");
  const drawerHandle  = document.getElementById("drawer-handle");

  function openDrawer()  { sidePanel.classList.add("drawer-open");    drawerOverlay.classList.remove("hidden"); }
  function closeDrawer() { sidePanel.classList.remove("drawer-open"); drawerOverlay.classList.add("hidden"); }

  btnOpenDrawer?.addEventListener("click", openDrawer);
  drawerOverlay?.addEventListener("click", closeDrawer);
  drawerHandle?.addEventListener("click",  closeDrawer);

  // ── Tooltip ───────────────────────────────────────────────────────────────

  function showTooltip(hl, x, y) {
    const lvlCls = "tt-badge-" + (hl.level || "").toLowerCase();
    tooltip.innerHTML = `
      <div>
        <span class="tt-word">${esc(hl.word)}</span>
        <span class="tt-reading">【${esc(hl.reading)}】</span>
        <span class="tt-badge ${lvlCls}">${esc(hl.level)}</span>
      </div>
      <div class="tt-register">${esc(hl.register)}</div>
      <div class="tt-en">${esc(hl.en)}</div>
      <div class="tt-zh">${esc(hl.zh)}</div>
    `;
    tooltip.style.left = x + "px";
    tooltip.style.top  = y + "px";
    tooltip.classList.remove("hidden");
  }

  function setupTouchTooltips(container) {
    let activeSpan = null;
    container.addEventListener("click", e => {
      const span = e.target.closest("[data-hl]");
      if (!span) { tooltip.classList.add("hidden"); activeSpan = null; return; }
      if (span === activeSpan) { tooltip.classList.add("hidden"); activeSpan = null; return; }
      let hl;
      try { hl = JSON.parse(span.dataset.hl); } catch { return; }
      const rect    = span.getBoundingClientRect();
      const ttWidth = 260;
      const left    = Math.max(8, Math.min(rect.left, window.innerWidth - ttWidth - 8));
      const top     = Math.min(rect.bottom + 8, window.innerHeight - 160);
      showTooltip(hl, left, top);
      activeSpan = span;
    });
  }

  if (isTouch) {
    setupTouchTooltips(transcriptEl);
    document.addEventListener("scroll", () => { tooltip.classList.add("hidden"); }, { passive: true });
  } else {
    document.addEventListener("mousemove", e => {
      if (!tooltip.classList.contains("hidden")) {
        tooltip.style.left = (e.clientX + 14) + "px";
        tooltip.style.top  = (e.clientY + 14) + "px";
      }
    });
    document.addEventListener("mouseover", e => {
      const span = e.target.closest("[data-hl]");
      if (!span) { tooltip.classList.add("hidden"); return; }
      let hl;
      try { hl = JSON.parse(span.dataset.hl); } catch { return; }
      showTooltip(hl, e.clientX + 14, e.clientY + 14);
    });
    document.addEventListener("mouseout", e => {
      if (!e.relatedTarget?.closest("[data-hl]")) tooltip.classList.add("hidden");
    });
  }

  // ── Full transcript modal ─────────────────────────────────────────────────

  const transcriptModal   = document.getElementById("transcript-modal");
  const modalBackdrop     = document.getElementById("modal-backdrop");
  const modalClose        = document.getElementById("modal-close");
  const btnFullTranscript = document.getElementById("btn-full-transcript");

  let modalProgrammaticScroll = false;

  function scrollModalToActive() {
    if (!modalTranscriptEl || currentIdx < 0) return;
    const el = document.getElementById(`modal-seg-${currentIdx}`);
    if (!el) return;
    modalProgrammaticScroll = true;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    if ("onscrollend" in modalTranscriptEl) {
      modalTranscriptEl.addEventListener("scrollend", () => { modalProgrammaticScroll = false; }, { once: true });
    } else {
      setTimeout(() => { modalProgrammaticScroll = false; }, 1200);
    }
  }

  function openModal() {
    transcriptModal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    modalJumpPill?.classList.add("hidden");
    setTimeout(scrollModalToActive, 50);
  }
  function closeModal() {
    transcriptModal.classList.add("hidden");
    document.body.style.overflow = "";
  }

  btnFullTranscript?.addEventListener("click", openModal);
  modalClose?.addEventListener("click", closeModal);
  modalBackdrop?.addEventListener("click", closeModal);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

  modalTranscriptEl?.addEventListener("scroll", () => {
    if (!modalProgrammaticScroll) modalJumpPill?.classList.remove("hidden");
  }, { passive: true });

  modalJumpPill?.addEventListener("click", () => {
    modalJumpPill.classList.add("hidden");
    scrollModalToActive();
  });

  modalTranscriptEl?.addEventListener("click", e => {
    if (isTouch && e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (!seg) return;
    seekTo(parseFloat(seg.dataset.start));
    closeModal();
  });

  if (isTouch && modalTranscriptEl) setupTouchTooltips(modalTranscriptEl);

  // ── Helpers ───────────────────────────────────────────────────────────────

  // Show only nearby segments in video mode (±1 around current, both mobile and desktop)
  function updateNearbySegments(idx) {
    const WINDOW = 1;
    segments.forEach((_, i) => {
      const el = document.getElementById(`seg-${i}`);
      if (!el) return;
      const dist = idx < 0 ? (i < 3 ? 0 : Infinity) : Math.abs(i - idx);
      el.classList.toggle("seg-nb-hidden", dist > WINDOW);
      el.classList.toggle("seg-nb-near",   dist === 1);
    });
  }

  function setActive(idx) {
    document.querySelectorAll(".segment.segment-active")
      .forEach(el => el.classList.remove("segment-active"));
    if (idx >= 0) {
      const el = document.getElementById(`seg-${idx}`);
      if (el) { el.classList.add("segment-active"); scrollActiveIntoView(idx, false); }
    }

    if (useYoutube) updateNearbySegments(idx);

    // Keep modal in sync when it's open
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

  function renderTranscript() {
    transcriptEl.innerHTML = segments.map((seg, i) => {
      const jaHtml = annotate(seg.ja, allHighlights);
      return `<div class="segment" id="seg-${i}" data-start="${seg.start}" data-end="${seg.end}">
        <span class="segment-time">${esc(seg.time || "")}</span>
        <div class="segment-body">
          <div class="segment-ja">${jaHtml}</div>
          <div class="translation-en hidden"><span class="trans-tag">EN</span>${esc(seg.en || "")}</div>
          <div class="translation-zh hidden"><span class="trans-tag">ZH</span>${esc(seg.zh || "")}</div>
        </div>
      </div>`;
    }).join("");
  }

  function renderModalTranscript() {
    if (!modalTranscriptEl) return;
    modalTranscriptEl.innerHTML = segments.map((seg, i) => {
      const jaHtml = annotate(seg.ja, allHighlights);
      return `<div class="modal-seg" id="modal-seg-${i}" data-start="${seg.start}" data-end="${seg.end}">
        <span class="modal-seg-time">${esc(seg.time || "")}</span>
        <div class="modal-seg-body">
          <div class="modal-seg-ja">${jaHtml}</div>
          ${seg.en ? `<div class="translation-en" style="display:flex;"><span class="trans-tag">EN</span>${esc(seg.en)}</div>` : ""}
          ${seg.zh ? `<div class="translation-zh" style="display:flex;"><span class="trans-tag">ZH</span>${esc(seg.zh)}</div>` : ""}
        </div>
      </div>`;
    }).join("");
  }

  /**
   * Annotate Japanese text with highlight spans.
   * Builds non-overlapping intervals (longest match wins on tie, first occurrence wins on overlap).
   */
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

  function esc(str) {
    return String(str || "")
      .replace(/&/g,  "&amp;")
      .replace(/</g,  "&lt;")
      .replace(/>/g,  "&gt;")
      .replace(/"/g,  "&quot;");
  }

  // ── Side panel renderers ──────────────────────────────────────────────────

  function renderVocab(vocab) {
    const items = vocab.length
      ? vocab
      : highlights.filter(h => h.type === "vocab").map(h => ({
          word: h.word, reading: h.reading, en: h.en, zh: h.zh,
          level: h.level, register: h.register, example: "",
        }));
    panelVocab.innerHTML = items.length
      ? items.map(v => `
        <div class="card">
          <div class="card-front">
            ${esc(v.word)}
            <span class="card-reading">【${esc(v.reading)}】</span>
            <span class="card-level card-level-${(v.level || "").toLowerCase()}">${esc(v.level)}</span>
          </div>
          <div class="card-body">
            <div class="card-en">${esc(v.en)}</div>
            <div class="card-zh">${esc(v.zh)}</div>
            ${v.register ? `<div class="card-register">${esc(v.register)}</div>` : ""}
            ${v.example  ? `<div class="card-example">${esc(v.example)}</div>`  : ""}
          </div>
        </div>`).join("")
      : `<p class="panel-empty">No N1/N2 vocabulary identified</p>`;
  }

  function renderGrammar(grammar) {
    const items = grammar.length
      ? grammar
      : highlights.filter(h => h.type === "grammar").map(h => ({
          pattern: h.word, reading: h.reading,
          meaning_en: h.en, meaning_zh: h.zh,
          level: h.level, construction: "", example: "",
        }));
    panelGrammar.innerHTML = items.length
      ? items.map(g => `
        <div class="card">
          <div class="card-front">
            ${esc(g.pattern)}
            <span class="card-level card-level-${(g.level || "").toLowerCase()}">${esc(g.level)}</span>
          </div>
          <div class="card-body">
            <div class="card-en">${esc(g.meaning_en)}</div>
            <div class="card-zh">${esc(g.meaning_zh)}</div>
            ${g.construction ? `<div class="card-construction">${esc(g.construction)}</div>` : ""}
            ${g.example      ? `<div class="card-example">${esc(g.example)}</div>`          : ""}
          </div>
        </div>`).join("")
      : `<p class="panel-empty">No N1/N2 grammar patterns identified</p>`;
  }

  function renderExpressions(exprs) {
    panelExpr.innerHTML = exprs.length
      ? exprs.map(e => `
        <div class="card">
          <div class="card-front">
            ${esc(e.expression)}
            <span class="card-reading">【${esc(e.reading)}】</span>
          </div>
          <div class="card-body">
            <div class="card-en">${esc(e.en)}</div>
            <div class="card-zh">${esc(e.zh)}</div>
            ${e.context ? `<div class="card-register">${esc(e.context)}</div>` : ""}
          </div>
        </div>`).join("")
      : `<p class="panel-empty">No set phrases identified</p>`;
  }

  function renderContext(items) {
    if (!items.length) {
      panelContext.innerHTML = `<p class="panel-empty">No context-specific terms identified</p>`;
      return;
    }
    panelContext.innerHTML = items.map(item => {
      const isGrammar = !!item.pattern;
      const word      = isGrammar ? item.pattern    : item.word;
      const reading   = isGrammar ? item.reading    : item.reading;
      const en        = isGrammar ? item.meaning_en : item.en;
      const zh        = isGrammar ? item.meaning_zh : item.zh;
      const example   = item.example || "";
      const extra     = isGrammar && item.construction
        ? `<div class="card-construction">${esc(item.construction)}</div>` : "";
      const tag       = isGrammar ? "grammar" : "vocab";
      return `
        <div class="card" style="border-color: rgba(167,139,250,0.2);">
          <div class="card-front">
            ${esc(word)}
            ${reading ? `<span class="card-reading">【${esc(reading)}】</span>` : ""}
            <span class="card-level card-level-context-specific">ctx · ${esc(tag)}</span>
          </div>
          <div class="card-body">
            <div class="card-en">${esc(en)}</div>
            <div class="card-zh">${esc(zh)}</div>
            ${extra}
            ${item.register ? `<div class="card-register">${esc(item.register)}</div>` : ""}
            ${example ? `<div class="card-example">${esc(example)}</div>` : ""}
          </div>
        </div>`;
    }).join("");
  }
});
