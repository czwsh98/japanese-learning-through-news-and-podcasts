"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const meta        = document.getElementById("episode-meta");
  const dateStr     = meta?.dataset.date;
  if (!dateStr) return;

  const audio       = document.getElementById("audio-player");
  const transcriptEl= document.getElementById("transcript");
  const loadingEl   = document.getElementById("transcript-loading");
  const tooltip     = document.getElementById("tooltip");

  const panelVocab  = document.getElementById("panel-vocab");
  const panelGrammar= document.getElementById("panel-grammar");
  const panelExpr   = document.getElementById("panel-expressions");

  let segments      = [];
  let highlights    = [];
  let currentIdx    = -1;
  let showEn        = false;
  let showZh        = false;

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

  // ── Filter to episode level ───────────────────────────────────────────────

  const LEVEL_TIERS = {
    "beginner":             ["N5"],
    "beginner-intermediate":["N4", "N3"],
    "intermediate":         ["N3"],
    "intermediate-advanced":["N2"],
    "advanced":             ["N2", "N1"],
  };
  const episodeLevel = meta?.dataset.level || "";
  const allowedTiers = LEVEL_TIERS[episodeLevel] || null;

  if (allowedTiers) {
    highlights          = highlights.filter(h => allowedTiers.includes(h.level));
    analysisData.vocab    = (analysisData.vocab    || []).filter(v => allowedTiers.includes(v.level));
    analysisData.grammar  = (analysisData.grammar  || []).filter(g => allowedTiers.includes(g.level));
  }

  // ── Render ────────────────────────────────────────────────────────────────

  renderTranscript();
  renderVocab(analysisData.vocab        || []);
  renderGrammar(analysisData.grammar    || []);
  renderExpressions(analysisData.expressions || []);

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

  // Click a segment → seek
  transcriptEl.addEventListener("click", e => {
    const seg = e.target.closest("[data-start]");
    if (!seg) return;
    audio.currentTime = parseFloat(seg.dataset.start);
    audio.play().catch(() => {});
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
      transcriptEl.querySelectorAll(".translation-en").forEach(el =>
        el.classList.toggle("hidden", !showEn)
      );
      transcriptEl.querySelectorAll(".translation-zh").forEach(el =>
        el.classList.toggle("hidden", !showZh)
      );

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

  document.getElementById("toggle-en").addEventListener("click", function () {
    showEn = !showEn;
    this.classList.toggle("active", showEn);
    transcriptEl.querySelectorAll(".translation-en").forEach(el =>
      el.classList.toggle("hidden", !showEn)
    );
  });

  document.getElementById("toggle-zh").addEventListener("click", function () {
    showZh = !showZh;
    this.classList.toggle("active", showZh);
    transcriptEl.querySelectorAll(".translation-zh").forEach(el =>
      el.classList.toggle("hidden", !showZh)
    );
  });

  // ── Side-panel tab switching ──────────────────────────────────────────────

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("tab-active"));
      btn.classList.add("tab-active");
      document.querySelectorAll("[data-panel]").forEach(p => p.classList.add("hidden"));
      document.querySelector(`[data-panel="${btn.dataset.tab}"]`).classList.remove("hidden");
    });
  });

  // ── Tooltip ───────────────────────────────────────────────────────────────

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
    tooltip.classList.remove("hidden");
  });

  document.addEventListener("mouseout", e => {
    if (!e.relatedTarget?.closest("[data-hl]")) {
      tooltip.classList.add("hidden");
    }
  });

  // ── Helpers ───────────────────────────────────────────────────────────────

  function renderTranscript() {
    transcriptEl.innerHTML = segments.map((seg, i) => {
      const jaHtml = annotate(seg.ja, highlights);
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
    // Scroll into view only if not already visible
    const rect = el.getBoundingClientRect();
    const container = transcriptEl.getBoundingClientRect();
    if (rect.top < container.top || rect.bottom > container.bottom) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
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
});
