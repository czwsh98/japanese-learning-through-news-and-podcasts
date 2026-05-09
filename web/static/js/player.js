"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const meta        = document.getElementById("episode-meta");
  const dateStr     = meta?.dataset.date;
  if (!dateStr) return;

  const audio       = document.getElementById("audio-player");
  const transcriptEl= document.getElementById("transcript");
  const loadingEl   = document.getElementById("transcript-loading");
  const tooltip     = document.getElementById("tooltip");

  const panelVocab   = document.getElementById("panel-vocab");
  const panelGrammar = document.getElementById("panel-grammar");
  const panelExpr    = document.getElementById("panel-expressions");
  const panelContext = document.getElementById("panel-context");

  let segments      = [];
  let highlights    = [];
  let currentIdx    = -1;
  let showEn        = false;
  let showZh        = false;

  const isTouch = window.matchMedia("(hover: none) and (pointer: coarse)").matches;

  // ── Mobile auto-follow (pause on user scroll, resume after inactivity) ────

  const AUTO_FOLLOW_INACTIVITY_MS = 20_000;
  let autoFollow = true;
  let autoFollowTimer = null;
  let programmaticScroll = false;
  let programmaticScrollTimer = null;

  function markUserNavigation() {
    if (programmaticScroll) return;
    // User is intentionally browsing: stop auto scrolling for a while
    autoFollow = false;
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
    autoFollowTimer = setTimeout(() => {
      autoFollow = true;
      // Re-focus the currently speaking line (if any)
      if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true);
    }, AUTO_FOLLOW_INACTIVITY_MS);
  }

  function scrollActiveIntoView(idx, force) {
    const el = document.getElementById(`seg-${idx}`);
    if (!el) return;
    if (!force && !autoFollow) return;

    // If it's already reasonably visible, don't fight the user's scroll position.
    const rect = el.getBoundingClientRect();
    const topPad = 120;    // clear sticky nav + player
    const bottomPad = 180; // clear bottom drawer trigger bar
    const inView = rect.top >= topPad && rect.bottom <= (window.innerHeight - bottomPad);
    if (!force && inView) return;

    // Prevent our own scroll from being interpreted as user navigation.
    // Use 1200ms — smooth scroll on mobile can take up to ~1s on long distances.
    programmaticScroll = true;
    if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer);
    programmaticScrollTimer = setTimeout(() => { programmaticScroll = false; }, 1200);

    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ── Fetch data ────────────────────────────────────────────────────────────

  let transcriptData = { segments: [] };
  let analysisData   = { highlights: [], vocab: [], grammar: [], expressions: [] };

  try {
    [transcriptData, analysisData] = await Promise.all([
      fetch(`/api/episode/${dateStr}/transcript`).then(r => { if (!r.ok) throw r; return r.json(); }),
      fetch(`/api/episode/${dateStr}/analysis`).then(r => { if (!r.ok) throw r; return r.json(); }),
    ]);
  } catch (e) {
    loadingEl.textContent = "Could not load transcript (pipeline may still be running).";
    return;
  }

  segments   = transcriptData.segments  || [];
  highlights = analysisData.highlights  || [];

  // ── Separate context-specific items (always shown regardless of level) ────

  const ctxVocab    = (analysisData.vocab    || []).filter(v => v.level === "context-specific");
  const ctxGrammar  = (analysisData.grammar  || []).filter(g => g.level === "context-specific");
  const ctxHighlights = highlights.filter(h => h.level === "context-specific");

  // ── Filter JLPT highlights to episode level ───────────────────────────────

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

  // Merge context-specific back into highlights for transcript annotation
  const allHighlights = [...highlights, ...ctxHighlights];

  // ── Render ────────────────────────────────────────────────────────────────

  renderTranscript();
  renderVocab(analysisData.vocab        || []);
  renderGrammar(analysisData.grammar    || []);
  renderExpressions(analysisData.expressions || []);
  renderContext([...ctxVocab, ...ctxGrammar]);

  loadingEl.classList.add("hidden");
  transcriptEl.classList.remove("hidden");

  // ── Audio sync ────────────────────────────────────────────────────────────

  audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    const idx = segments.findIndex(s => t >= s.start && t < s.end);
    if (idx !== currentIdx) {
      setActive(idx);
      currentIdx = idx;
    }
  });

  // When the user drags the scrubber / seeks, immediately jump transcript focus.
  audio.addEventListener("seeked", () => {
    const t = audio.currentTime;
    const idx = segments.findIndex(s => t >= s.start && t < s.end);
    if (idx >= 0) {
      currentIdx = idx;
      setActive(idx);
      scrollActiveIntoView(idx, true);
    }
  });

  // Pause auto-follow when the user intentionally scrolls; resume after 20s inactivity.
  // Use touchmove (not touchstart) on mobile — touchstart fires on any tap, including
  // word tooltips and UI controls, which would incorrectly pause auto-follow.
  // touchmove only fires when the finger actually moves (i.e. a real scroll gesture).
  window.addEventListener("touchmove", () => { if (isTouch) markUserNavigation(); }, { passive: true });
  window.addEventListener("wheel",     () => { markUserNavigation(); },              { passive: true });

  // Click/tap a segment → seek (skip if tapping a highlight on touch — tooltip handles it)
  transcriptEl.addEventListener("click", e => {
    if (isTouch && e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (!seg) return;
    audio.currentTime = parseFloat(seg.dataset.start);
    audio.play().catch(() => {});

    // If they explicitly choose a line, resume following from there.
    autoFollow = true;
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
  });

  // ── Translation toggles ───────────────────────────────────────────────────

  // ── Re-translate ─────────────────────────────────────────────────────────

  const btnRetranslate   = document.getElementById("btn-retranslate");
  const retranslateStatus = document.getElementById("retranslate-status");

  btnRetranslate.addEventListener("click", async () => {
    if (!confirm("Re-run translation for this episode? This may take a minute.")) return;

    btnRetranslate.disabled = true;
    btnRetranslate.textContent = "Translating…";
    retranslateStatus.textContent = "";
    retranslateStatus.classList.add("hidden");

    try {
      const res = await fetch(`/episode/${dateStr}/retranslate`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      segments = data.segments || [];
      renderTranscript();

      // Restore visible translation rows based on current toggle state
      syncTranslationUI();

      retranslateStatus.textContent = `✓ ${segments.length} segments re-translated`;
      retranslateStatus.classList.remove("hidden");
    } catch (err) {
      retranslateStatus.textContent = `Error: ${err.message}`;
      retranslateStatus.classList.remove("hidden");
    } finally {
      btnRetranslate.disabled = false;
      btnRetranslate.textContent = "Re-translate";
    }
  });

  // ── Translation toggle helpers (shared by header buttons + FAB) ──────────

  function syncTranslationUI() {
    // Header buttons (desktop)
    document.getElementById("toggle-en").classList.toggle("active", showEn);
    document.getElementById("toggle-zh").classList.toggle("active", showZh);
    // FAB pills
    const fabEn = document.getElementById("fab-en-btn");
    const fabZh = document.getElementById("fab-zh-btn");
    if (fabEn) fabEn.classList.toggle("active", showEn);
    if (fabZh) fabZh.classList.toggle("active", showZh);
    // FAB main button: lit when either translation is on
    const fabMain = document.getElementById("fab-main");
    if (fabMain) fabMain.classList.toggle("fab-on", showEn || showZh);
    // Rows
    transcriptEl.querySelectorAll(".translation-en").forEach(el =>
      el.classList.toggle("hidden", !showEn)
    );
    transcriptEl.querySelectorAll(".translation-zh").forEach(el =>
      el.classList.toggle("hidden", !showZh)
    );
  }

  document.getElementById("toggle-en").addEventListener("click", () => {
    showEn = !showEn; syncTranslationUI();
  });
  document.getElementById("toggle-zh").addEventListener("click", () => {
    showZh = !showZh; syncTranslationUI();
  });

  // ── Translation FAB ───────────────────────────────────────────────────────

  const fabMain  = document.getElementById("fab-main");
  const fabTray  = document.getElementById("fab-tray");
  const fabEnBtn = document.getElementById("fab-en-btn");
  const fabZhBtn = document.getElementById("fab-zh-btn");
  let fabOpen = false;

  function openFab()  { fabOpen = true;  fabTray.classList.replace("fab-tray-hidden", "fab-tray-visible"); }
  function closeFab() { fabOpen = false; fabTray.classList.replace("fab-tray-visible", "fab-tray-hidden"); }

  fabMain?.addEventListener("click", e => {
    e.stopPropagation();
    fabOpen ? closeFab() : openFab();
  });
  fabEnBtn?.addEventListener("click", e => {
    e.stopPropagation();
    showEn = !showEn; syncTranslationUI();
  });
  fabZhBtn?.addEventListener("click", e => {
    e.stopPropagation();
    showZh = !showZh; syncTranslationUI();
  });
  document.addEventListener("click", () => { if (fabOpen) closeFab(); });

  // ── Playback speed ────────────────────────────────────────────────────────

  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
  let speedIdx = 2; // 1× default
  const speedDisplay = document.getElementById("speed-display");

  function applySpeed(idx) {
    speedIdx = idx;
    const s = SPEEDS[speedIdx];
    audio.playbackRate = s;
    if (speedDisplay) speedDisplay.textContent = s + "×";
    document.querySelectorAll(".speed-btn[data-speed]").forEach(b =>
      b.classList.toggle("speed-active", parseFloat(b.dataset.speed) === s)
    );
  }

  document.getElementById("speed-btns").addEventListener("click", e => {
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

  if (isTouch) {
    // Tap to reveal, tap same again or elsewhere to dismiss
    let activeSpan = null;

    transcriptEl.addEventListener("click", e => {
      const span = e.target.closest("[data-hl]");
      if (!span) { tooltip.classList.add("hidden"); activeSpan = null; return; }
      if (span === activeSpan) { tooltip.classList.add("hidden"); activeSpan = null; return; }

      let hl;
      try { hl = JSON.parse(span.dataset.hl); } catch { return; }

      const rect = span.getBoundingClientRect();
      const ttWidth = 260;
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - ttWidth - 8));
      const top  = Math.min(rect.bottom + 8, window.innerHeight - 160);
      showTooltip(hl, left, top);
      activeSpan = span;
    });

    document.addEventListener("scroll", () => {
      tooltip.classList.add("hidden");
      activeSpan = null;
    }, { passive: true });

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

  // ── Mobile bottom drawer ──────────────────────────────────────────────────

  const sidePanel      = document.getElementById("side-panel");
  const drawerOverlay  = document.getElementById("drawer-overlay");
  const btnOpenDrawer  = document.getElementById("btn-open-drawer");
  const drawerHandle   = document.getElementById("drawer-handle");

  function openDrawer() {
    sidePanel.classList.add("drawer-open");
    drawerOverlay.classList.remove("hidden");
  }
  function closeDrawer() {
    sidePanel.classList.remove("drawer-open");
    drawerOverlay.classList.add("hidden");
  }

  btnOpenDrawer?.addEventListener("click", openDrawer);
  drawerOverlay?.addEventListener("click", closeDrawer);
  drawerHandle?.addEventListener("click", closeDrawer);

  // ── Helpers ───────────────────────────────────────────────────────────────

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

  function setActive(idx) {
    document.querySelectorAll(".segment.segment-active")
      .forEach(el => el.classList.remove("segment-active"));
    if (idx < 0) return;
    const el = document.getElementById(`seg-${idx}`);
    if (!el) return;
    el.classList.add("segment-active");
    scrollActiveIntoView(idx, false);
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

    // Sort: earlier start first; on tie, longer match first
    intervals.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));

    // Keep non-overlapping
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
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Side panel renderers ──────────────────────────────────────────────────

  function renderVocab(vocab) {
    // Fall back to highlights when the dedicated vocab array is empty
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
            <span class="card-level card-level-${(v.level||'').toLowerCase()}">${esc(v.level)}</span>
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
    // Fall back to highlights when the dedicated grammar array is empty
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
            <span class="card-level card-level-${(g.level||'').toLowerCase()}">${esc(g.level)}</span>
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
      const word      = isGrammar ? item.pattern  : item.word;
      const reading   = isGrammar ? item.reading  : item.reading;
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
