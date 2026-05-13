import { Filesystem, Directory } from '@capacitor/filesystem';

const API_BASE = "https://mimichan.up.railway.app";
const AUTH_HEADER = "";
const API_BASE_MEDIA = "https://mimichan.up.railway.app";

document.addEventListener("DOMContentLoaded", async () => {
  const urlParams = new URLSearchParams(window.location.search);
  const dateStr = urlParams.get('id');
  if (!dateStr) {
    const headerTitle = document.getElementById("header-title");
    if (headerTitle) headerTitle.innerText = "Error: No episode ID";
    return;
  }

  async function fetchOfflineOrNetwork(endpoint, filename) {
    try {
      const local = await Filesystem.readFile({
        path: `japanese_pipeline/${dateStr}/${filename}`,
        directory: Directory.Data,
        encoding: "utf8"
      });
      return JSON.parse(local.data);
    } catch (e) {
      const r = await fetch(`${API_BASE}${endpoint}`, {
        headers: { "Authorization": AUTH_HEADER }
      });
      if (!r.ok) throw r;
      return await r.json();
    }
  }

  // Fetch meta first
  let metaObj = {};
  try {
    metaObj = await fetchOfflineOrNetwork(`/api/episode/${dateStr}/meta`, 'meta.json');
  } catch (err) {
    console.error("Meta fetch error", err);
  }

  const videoId = metaObj.video_id || "";
  const isYoutube = !!videoId && ((metaObj.url || "").includes("youtu"));
  // On iOS (Capacitor), default to audio player — video can be toggled on demand
  const isCapacitor = !!window.Capacitor?.isNativePlatform?.();
  let useYoutube = isYoutube && !isCapacitor;

  // Update UI with Meta
  document.title = `${metaObj.title || dateStr} — 日本語 Pipeline`;
  const navTitle = document.getElementById("nav-title");
  if (navTitle) navTitle.innerText = metaObj.title || dateStr;
  const headerTitle = document.getElementById("header-title");
  if (headerTitle) headerTitle.innerText = metaObj.title || dateStr;

  let metaHtml = "";
  if (metaObj.channel) metaHtml += `<span>${metaObj.channel} · </span>`;
  metaHtml += `<span>${dateStr}</span>`;
  if (metaObj.duration) {
    const mins = Math.floor(metaObj.duration / 60);
    const secs = String(metaObj.duration % 60).padStart(2, "0");
    metaHtml += `<span> · ${mins}:${secs}</span>`;
  }
  if (metaObj.level) {
    const levelLabels = {
      'beginner': 'Beginner · N5',
      'beginner-intermediate': 'Beginner–Intermediate · N4–N3',
      'intermediate': 'Intermediate · N3',
      'intermediate-advanced': 'Intermediate–Advanced · N2',
      'advanced': 'Advanced · N2–N1'
    };
    const levelText = levelLabels[metaObj.level] || metaObj.level;
    metaHtml += `<span class="text-xs bg-gray-800 border border-gray-700 rounded px-2 py-0.5 text-gray-400 ml-2">${levelText}</span>`;
  }
  const headerMeta = document.getElementById("header-meta");
  if (headerMeta) headerMeta.innerHTML = metaHtml;

  if (metaObj.url) {
    const srcLink = document.getElementById("source-link");
    if (srcLink) {
      srcLink.href = metaObj.url;
      srcLink.classList.remove("hidden");
    }
  }

  const csvLink = document.getElementById("download-csv-link");
  if (csvLink) csvLink.href = `${API_BASE}/episode/${dateStr}/cards.csv`;

  // Update Audio Src
  const audioSource = document.getElementById("audio-source");
  const audioTrack = document.getElementById("audio-track");

  try {
    // readFile throws if the file doesn't exist — use it to verify before using local URI
    await Filesystem.readFile({
      path: `japanese_pipeline/${dateStr}/audio.mp3`,
      directory: Directory.Data,
      encoding: null,
    });
    const audioUri = await Filesystem.getUri({
      path: `japanese_pipeline/${dateStr}/audio.mp3`,
      directory: Directory.Data
    });
    if (audioSource) audioSource.src = window.Capacitor.convertFileSrc(audioUri.uri);
  } catch (e) {
    if (audioSource) audioSource.src = `${API_BASE_MEDIA}/episode/${dateStr}/audio`;
  }

  if (audioTrack) {
    try {
      await Filesystem.readFile({
        path: `japanese_pipeline/${dateStr}/subtitles.vtt`,
        directory: Directory.Data,
        encoding: "utf8"
      });
      const vttUri = await Filesystem.getUri({
        path: `japanese_pipeline/${dateStr}/subtitles.vtt`,
        directory: Directory.Data
      });
      audioTrack.src = window.Capacitor.convertFileSrc(vttUri.uri);
    } catch (e) {
      audioTrack.src = `${API_BASE_MEDIA}/episode/${dateStr}/subtitles.vtt`;
    }
  }

  const audio = document.getElementById("audio-player");
  if (audio) audio.load();

  // Download logic
  const btnDownload = document.getElementById("btn-download-offline");
  if (btnDownload) {
    btnDownload.addEventListener("click", async () => {
      try {
        btnDownload.classList.add("animate-pulse");
        btnDownload.style.pointerEvents = "none";
        
        const endpoints = [
          { ep: `/api/episode/${dateStr}/meta`, file: 'meta.json' },
          { ep: `/api/episode/${dateStr}/transcript`, file: 'transcript.json' },
          { ep: `/api/episode/${dateStr}/analysis`, file: 'analysis.json' },
          { ep: `/episode/${dateStr}/subtitles.vtt`, file: 'subtitles.vtt' }
        ];

        for (const item of endpoints) {
          const r = await fetch(`${API_BASE}${item.ep}`, {
            headers: { "Authorization": AUTH_HEADER }
          });
          if (r.ok) {
            const text = await r.text();
            await Filesystem.writeFile({
              path: `japanese_pipeline/${dateStr}/${item.file}`,
              data: text,
              directory: Directory.Data,
              encoding: "utf8",
              recursive: true
            });
          }
        }

        const aRes = await fetch(`${API_BASE}/episode/${dateStr}/audio`, {
          headers: { "Authorization": AUTH_HEADER }
        });
        const aBlob = await aRes.blob();

        const reader = new FileReader();
        reader.readAsDataURL(aBlob);
        reader.onloadend = async () => {
          const base64data = reader.result.split(',')[1];
          await Filesystem.writeFile({
            path: `japanese_pipeline/${dateStr}/audio.mp3`,
            data: base64data,
            directory: Directory.Data,
            recursive: true
          });
          
          btnDownload.classList.remove("animate-pulse");
          btnDownload.classList.add("text-green-400");
          btnDownload.style.pointerEvents = "auto";
          showToast("Episode downloaded for offline use!", "success");
        };
      } catch (err) {
        console.error(err);
        showToast("Download failed: " + err.message);
        btnDownload.classList.remove("animate-pulse");
        btnDownload.style.pointerEvents = "auto";
      }
    });
  }

  if (isYoutube) {
    if (useYoutube) {
      document.getElementById("yt-player-wrap")?.classList.remove("hidden");
      document.getElementById("audio-player-wrap")?.classList.add("hidden");
    }
    // Always show toggle button for YouTube episodes so user can switch to video
    document.getElementById("btn-toggle-player")?.classList.remove("hidden");
    document.getElementById("btn-toggle-player").textContent = useYoutube ? "Audio only" : "Watch video";
    document.querySelectorAll("#btn-full-transcript").forEach(btn => btn.classList.remove("hidden"));
  }
  const transcriptEl = document.getElementById("transcript");
  const loadingEl = document.getElementById("transcript-loading");
  const tooltip = document.getElementById("tooltip");

  const panelVocab = document.getElementById("panel-vocab");
  const panelGrammar = document.getElementById("panel-grammar");
  const panelExpr = document.getElementById("panel-expressions");
  const panelContext = document.getElementById("panel-context");
  const modalTranscriptEl = document.getElementById("modal-transcript");
  const jumpPill = document.getElementById("jump-now-pill");
  const modalJumpPill = document.getElementById("modal-jump-pill");
  const transcriptCard = document.getElementById("transcript-card");

  let segments = [];
  let highlights = [];
  let currentIdx = -1;
  let showEn = false;
  let showZh = false;
  let ytPlayer = null;

  const isTouch = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  const isDesktopLayout = window.innerWidth >= 1024 || (!isTouch && window.innerWidth >= 768);

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

  // ── AnkiConnect Integration ──────────────────────────────────────────────

  const ANKI_URL = "http://localhost:8765";

  async function invokeAnki(action, version, params = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.addEventListener("error", () => reject("Failed to issue request to AnkiConnect."));
      xhr.addEventListener("load", () => {
        try {
          const response = JSON.parse(xhr.responseText);
          if (response.error) throw response.error;
          resolve(response.result);
        } catch (e) {
          reject(e);
        }
      });
      xhr.open("POST", ANKI_URL);
      xhr.send(JSON.stringify({ action, version, params }));
    });
  }

  async function syncToAnki(card) {
    const deckName = "Japanese Pipeline";
    const modelName = "Japanese Pipeline Model";

    // 1. Ensure deck exists
    await invokeAnki("createDeck", 6, { deck: deckName });

    // 2. Ensure model exists
    const models = await invokeAnki("modelNames", 6);
    if (!models.includes(modelName)) {
      await invokeAnki("createModel", 6, {
        modelName,
        inOrderFields: ["Front", "Reading", "English", "Chinese", "Example", "Level", "Type"],
        css: ".card { font-family: 'Hiragino Sans', 'Meiryo', sans-serif; text-align: center; color: #d1d5db; background-color: #111827; padding: 20px; } .ja { font-size: 32px; color: #fff; margin-bottom: 10px; } .reading { font-size: 18px; color: #9ca3af; } .translation { margin-top: 15px; font-size: 16px; } .en { color: #93c5fd; } .zh { color: #6ee7b7; } .example { margin-top: 15px; font-style: italic; color: #6b7280; font-size: 14px; border-top: 1px solid #374151; padding-top: 10px; }",
        cardTemplates: [{
          Name: "Recognition",
          Front: "<div class='card'><div class='ja'>{{Front}}</div><div class='reading'>{{Reading}}</div></div>",
          Back: "<div class='card'><div class='ja'>{{Front}}</div><div class='reading'>{{Reading}}</div><hr><div class='translation'><div class='en'>{{English}}</div><div class='zh'>{{Chinese}}</div></div><div class='example'>{{Example}}</div></div>"
        }]
      });
    }

    // 3. Add note
    return await invokeAnki("addNote", 6, {
      note: {
        deckName,
        modelName,
        fields: {
          Front: card.front,
          Reading: card.reading || "",
          English: card.en || "",
          Chinese: card.zh || "",
          Example: card.example || "",
          Level: card.level || "",
          Type: card.type || ""
        },
        tags: (card.tags || "japanese").split(" "),
        options: { allowDuplicate: false }
      }
    });
  }

  async function handleAnkiSync(e) {
    const btn = e.target.closest(".btn-anki");
    if (!btn) return;
    e.stopPropagation();

    const cardData = JSON.parse(btn.dataset.card);
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin h-3 w-3" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>';

    try {
      await syncToAnki(cardData);
      btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      btn.title = "Synced!";
    } catch (err) {
      console.error(err);
      showToast("AnkiConnect error: " + err + "\n\nMake sure Anki is open and AnkiConnect is installed.");
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
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
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
    let pos = 0;
    for (const { start, end, hl } of kept) {
      html += esc(text.slice(pos, start));
      const typeCls = hl.type === "vocab" ? "hl-vocab" : "hl-grammar";
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
      const jaHtml = annotate(seg.ja, allHighlights);
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
      const jaHtml = annotate(seg.ja, allHighlights);
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
      el.classList.toggle("seg-nb-near", dist > 0 && dist <= near);
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
    if (useYoutube && !isDesktopLayout) return;

    const el = document.getElementById(`seg-${idx}`);
    if (!el || (!force && !autoFollow)) return;

    const overflowY = window.getComputedStyle(transcriptEl).overflowY;
    const isContainerScrollable = (overflowY === "auto" || overflowY === "scroll") &&
      transcriptEl.scrollHeight > transcriptEl.clientHeight + 1;

    setProgrammaticScroll(isContainerScrollable ? transcriptEl : window);

    if (isContainerScrollable) {
      const cRect = transcriptEl.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const isVisible = (elRect.top >= cRect.top) && (elRect.bottom <= cRect.bottom);
      if (force || !isVisible) {
        const target = transcriptEl.scrollTop + (elRect.top - cRect.top) - (transcriptEl.clientHeight / 2) + (elRect.height / 2);
        transcriptEl.scrollTo({ top: target, behavior: "smooth" });
      }
    } else {
      el.scrollIntoView({ behavior: "smooth", block: force ? "center" : "nearest" });
    }

    // Also sync the full-screen modal if it's open
    if (!modalTranscriptEl.parentElement.parentElement.classList.contains("hidden")) {
      const modalSeg = document.getElementById(`modal-seg-${idx}`);
      if (modalSeg) {
        setProgrammaticScroll(modalTranscriptEl);
        const mRect = modalTranscriptEl.getBoundingClientRect();
        const melRect = modalSeg.getBoundingClientRect();
        const isVisible = (melRect.top >= mRect.top) && (melRect.bottom <= mRect.bottom);
        if (force || !isVisible) {
          const target = modalTranscriptEl.scrollTop + (melRect.top - mRect.top) - (modalTranscriptEl.clientHeight / 2) + (melRect.height / 2);
          modalTranscriptEl.scrollTo({ top: target, behavior: "smooth" });
        }
      }
    }
  }

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

  function seekTo(t) {
    if (useYoutube) {
      ytPlayer?.seekTo(t, true);
      ytPlayer?.playVideo();
    } else {
      if (audio) { audio.currentTime = t; audio.play().catch(() => { }); }
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
          width: "100%",
          height: "100%",
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

  let transcriptData = { segments: [] };
  let analysisData = { highlights: [], vocab: [], grammar: [], expressions: [] };

  const [transcriptResult, analysisResult] = await Promise.allSettled([
    fetchOfflineOrNetwork(`/api/episode/${dateStr}/transcript`, 'transcript.json'),
    fetchOfflineOrNetwork(`/api/episode/${dateStr}/analysis`, 'analysis.json'),
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

  segments = transcriptData.segments || [];
  highlights = analysisData.highlights || [];

  const ctxVocab = (analysisData.vocab || []).filter(v => v.level === "context-specific");
  const ctxGrammar = (analysisData.grammar || []).filter(g => g.level === "context-specific");
  const ctxHighlights = highlights.filter(h => h.level === "context-specific");

  const LEVEL_TIERS = {
    "beginner": ["N5"],
    "beginner-intermediate": ["N4", "N3"],
    "intermediate": ["N3"],
    "intermediate-advanced": ["N2"],
    "advanced": ["N2", "N1"],
  };
  const episodeLevel = metaObj.level || "";
  const allowedTiers = LEVEL_TIERS[episodeLevel] || null;

  if (allowedTiers) {
    highlights = highlights.filter(h => allowedTiers.includes(h.level));
    analysisData.vocab = (analysisData.vocab || []).filter(v => allowedTiers.includes(v.level));
    analysisData.grammar = (analysisData.grammar || []).filter(g => allowedTiers.includes(g.level));
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

    const targetInSidebar = isDesktopLayout && useYoutube;
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
  renderVocab(analysisData.vocab || []);
  renderGrammar(analysisData.grammar || []);
  renderExpressions(analysisData.expressions || []);
  renderContext([...ctxVocab, ...ctxGrammar]);
  repositionTranscript();

  // Stats bar
  const vocabCount = (analysisData.vocab || []).length + ctxVocab.length;
  const grammarCount = (analysisData.grammar || []).length + ctxGrammar.length;
  const exprCount = (analysisData.expressions || []).length;
  const ctxCount = ctxVocab.length + ctxGrammar.length;
  if (vocabCount + grammarCount + exprCount > 0) {
    const statsEl = document.createElement("div");
    statsEl.className = "text-xs text-gray-500 px-3 py-1.5 border-b border-gray-800 flex items-center gap-3 flex-wrap";
    const parts = [];
    if (vocabCount) parts.push(`<span class="text-gray-400">${vocabCount}</span> vocab`);
    if (grammarCount) parts.push(`<span class="text-gray-400">${grammarCount}</span> grammar`);
    if (exprCount) parts.push(`<span class="text-gray-400">${exprCount}</span> expressions`);
    if (ctxCount) parts.push(`<span style="color:#a78bfa">${ctxCount}</span> context-specific`);
    statsEl.innerHTML = parts.join(' <span class="text-gray-700">·</span> ');
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
  }

  sizeMobileTranscript();
  window.addEventListener("resize", sizeMobileTranscript, { passive: true });

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
  document.getElementById("speed-up")?.addEventListener("click", () => { if (speedIdx < SPEEDS.length - 1) applySpeed(speedIdx + 1); });

  // ── Auto-follow Logic ─────────────────────────────────────────────────────

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
      if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true);
    }, AUTO_FOLLOW_INACTIVITY_MS);
  }

  transcriptEl.addEventListener("scroll", () => { if (!programmaticScroll) markUserNavigation(); }, { passive: true });
  window.addEventListener("touchmove", () => { if (isTouch && !useYoutube) markUserNavigation(); }, { passive: true });
  window.addEventListener("wheel", () => { if (!useYoutube) markUserNavigation(); }, { passive: true });

  jumpPill?.addEventListener("click", () => {
    autoFollow = true;
    jumpPill.classList.add("hidden");
    if (autoFollowTimer) clearTimeout(autoFollowTimer);
    if (currentIdx >= 0) scrollActiveIntoView(currentIdx, true);
  });

  function updatePillPosition() {
    // pill-fixed was needed when the transcript scrolled the whole page on mobile.
    // Now the transcript is a bounded container on all layouts, so the pill sits
    // position:absolute at the bottom of the card — never needs fixed positioning.
    jumpPill?.classList.remove("pill-fixed");
  }

  // Size the transcript to fill the space between the sticky audio player and the
  // bottom drawer bar, mirroring the max-h approach used by the full-screen modal.
  function sizeMobileTranscript() {
    if (isDesktopLayout || useYoutube) return;
    const cardBody = document.getElementById("transcript-card-body");
    const drawerBar = document.getElementById("drawer-trigger-bar");
    if (!cardBody) return;
    const top = cardBody.getBoundingClientRect().top + window.scrollY;
    const drawerH = drawerBar ? drawerBar.offsetHeight : 48;
    // Subtract top offset from document origin, page bottom padding, and drawer bar
    const available = document.documentElement.clientHeight - top - drawerH - 8;
    transcriptEl.style.height = Math.max(200, available) + "px";
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
      const resp = await fetch(`${API_BASE}/api/explain`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": AUTH_HEADER },
        body: JSON.stringify({ text: jaText })
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
    if (isTouch && e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (seg) seekTo(parseFloat(seg.dataset.start));
  });

  // ── Sync & Poll ───────────────────────────────────────────────────────────

  if (audio) {
    audio.addEventListener("timeupdate", () => {
      if (useYoutube) return;
      const t = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
    });
    audio.addEventListener("seeked", () => {
      if (useYoutube) return;
      const t = audio.currentTime;
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx >= 0) { setActive(idx); scrollActiveIntoView(idx, true); }
    });
  }

  if (isYoutube) {
    (async () => {
      try {
        await waitForYTAPI();
        ytPlayer = await createYTPlayer(videoId);
        if (speedIdx !== 2) ytPlayer.setPlaybackRate(SPEEDS[speedIdx]);
      } catch (err) {
        console.error("YouTube Player failed:", err);
        const p = document.getElementById("yt-player");
        if (p) p.innerHTML = `<div class="p-6 text-center text-gray-500">Video failed to load.</div>`;
      }
    })();

    setInterval(() => {
      if (!useYoutube || !ytPlayer || typeof ytPlayer.getCurrentTime !== "function") return;
      const t = ytPlayer.getCurrentTime();
      const idx = segments.findIndex(s => t >= s.start && t < s.end);
      if (idx !== currentIdx) setActive(idx);
    }, 250);
  }

  // ── Toggle & UI ───────────────────────────────────────────────────────────

  const btnTogglePlayer = document.getElementById("btn-toggle-player");
  const ytPlayerWrap = document.getElementById("yt-player-wrap");
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
        sizeMobileTranscript();
      }
      btnTogglePlayer.textContent = "Watch video";
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

  function syncTranslationUI() {
    document.getElementById("toggle-en")?.classList.toggle("active", showEn);
    document.getElementById("toggle-zh")?.classList.toggle("active", showZh);
    const fabEn = document.getElementById("fab-en-btn");
    const fabZh = document.getElementById("fab-zh-btn");
    const fabMain = document.getElementById("fab-main");
    if (fabEn) fabEn.classList.toggle("active", showEn);
    if (fabZh) fabZh.classList.toggle("active", showZh);
    if (fabMain) fabMain.classList.toggle("fab-on", showEn || showZh);
    [transcriptEl, modalTranscriptEl].forEach(container => {
      if (!container) return;
      container.querySelectorAll(".translation-en").forEach(el => el.classList.toggle("hidden", !showEn));
      container.querySelectorAll(".translation-zh").forEach(el => el.classList.toggle("hidden", !showZh));
    });
  }

  document.getElementById("toggle-en")?.addEventListener("click", () => { showEn = !showEn; syncTranslationUI(); });
  document.getElementById("toggle-zh")?.addEventListener("click", () => { showZh = !showZh; syncTranslationUI(); });

  const fabMain = document.getElementById("fab-main");
  const fabTray = document.getElementById("fab-tray");
  const fabEnBtn = document.getElementById("fab-en-btn");
  const fabZhBtn = document.getElementById("fab-zh-btn");
  let fabOpen = false;
  fabMain?.addEventListener("click", e => { e.stopPropagation(); fabOpen ? closeTray() : openTray(); });
  fabEnBtn?.addEventListener("click", e => { e.stopPropagation(); showEn = !showEn; syncTranslationUI(); });
  fabZhBtn?.addEventListener("click", e => { e.stopPropagation(); showZh = !showZh; syncTranslationUI(); });
  function openTray() { fabOpen = true; fabTray?.classList.replace("fab-tray-hidden", "fab-tray-visible"); }
  function closeTray() { fabOpen = false; fabTray?.classList.replace("fab-tray-visible", "fab-tray-hidden"); }
  document.addEventListener("click", closeTray);

  // ── Keyboard ──────────────────────────────────────────────────────────────

  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.isContentEditable) return;
    switch (e.key) {
      case " ": e.preventDefault();
        if (useYoutube) { if (ytPlayer?.getPlayerState() === 1) ytPlayer.pauseVideo(); else ytPlayer?.playVideo(); }
        else if (audio) { if (audio.paused) audio.play().catch(() => { }); else audio.pause(); }
        break;
      case "ArrowLeft": if (useYoutube) ytPlayer?.seekTo(Math.max(0, ytPlayer.getCurrentTime() - 5), true); else if (audio) audio.currentTime -= 5; break;
      case "ArrowRight": if (useYoutube) ytPlayer?.seekTo(ytPlayer.getCurrentTime() + 5, true); else if (audio) audio.currentTime += 5; break;
      case "ArrowUp": if (currentIdx > 0) seekTo(segments[currentIdx - 1].start); break;
      case "ArrowDown": if (currentIdx < segments.length - 1) seekTo(segments[currentIdx + 1].start); break;
      case "e": showEn = !showEn; syncTranslationUI(); break;
      case "c": showZh = !showZh; syncTranslationUI(); break;
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
    });
  });

  const sidePanel = document.getElementById("side-panel");
  sidePanel?.addEventListener("click", handleAnkiSync);
  const drawerOverlay = document.getElementById("drawer-overlay");
  const btnOpenDrawer = document.getElementById("btn-open-drawer");
  const drawerHandle = document.getElementById("drawer-handle");
  btnOpenDrawer?.addEventListener("click", () => { sidePanel.classList.add("drawer-open"); drawerOverlay.classList.remove("hidden"); });
  drawerOverlay?.addEventListener("click", () => { sidePanel.classList.remove("drawer-open"); drawerOverlay.classList.add("hidden"); });
  drawerHandle?.addEventListener("click", () => { sidePanel.classList.remove("drawer-open"); drawerOverlay.classList.add("hidden"); });

  // ── Tooltips & Modals ─────────────────────────────────────────────────────

  function showTooltip(hl, x, y) {
    const lvl = (hl.level || "").toLowerCase();
    const jishoUrl = `https://jisho.org/search/${encodeURIComponent(hl.word)}`;
    tooltip.innerHTML = `<div><span class="tt-word">${esc(hl.word)}</span><span class="tt-reading">【${esc(hl.reading)}】</span><span class="tt-badge tt-badge-${lvl}">${esc(hl.level)}</span></div><div class="tt-register">${esc(hl.register)}</div><div class="tt-en">${esc(hl.en)}</div><div class="tt-zh">${esc(hl.zh)}</div><div class="mt-2 pt-2 border-t border-gray-700 flex justify-end"><a href="${jishoUrl}" target="_blank" class="text-[10px] text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1">Search on Jisho ↗</a></div>`;
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
      const bc = db ? (window.innerHeight - db.getBoundingClientRect().top + 8) : 56;
      showTooltip(hl, Math.max(8, Math.min(rect.left, window.innerWidth - 268)), Math.min(rect.bottom + 8, window.innerHeight - 160 - bc));
      activeSpan = span;
    });
  }

  if (isTouch) { setupTouchTooltips(transcriptEl); document.addEventListener("scroll", () => tooltip.classList.add("hidden"), { passive: true }); }
  else {
    document.addEventListener("mousemove", e => { if (!tooltip.classList.contains("hidden")) { tooltip.style.left = (e.clientX + 14) + "px"; tooltip.style.top = (e.clientY + 14) + "px"; } });
    document.addEventListener("mouseover", e => { const span = e.target.closest("[data-hl]"); if (span) showTooltip(JSON.parse(span.dataset.hl), e.clientX + 14, e.clientY + 14); });
    document.addEventListener("mouseout", e => { if (!e.relatedTarget?.closest("[data-hl]")) tooltip.classList.add("hidden"); });
  }

  const transcriptModal = document.getElementById("transcript-modal");
  const modalClose = document.getElementById("modal-close");
  const btnFullTranscript = document.getElementById("btn-full-transcript");
  let modalProgrammaticScroll = false;
  btnFullTranscript?.addEventListener("click", () => { transcriptModal.classList.remove("hidden"); document.body.style.overflow = "hidden"; setTimeout(scrollModalToActive, 50); });
  modalClose?.addEventListener("click", () => { transcriptModal.classList.add("hidden"); document.body.style.overflow = ""; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { transcriptModal?.classList.add("hidden"); document.body.style.overflow = ""; } });
  modalTranscriptEl?.addEventListener("scroll", () => { if (!modalProgrammaticScroll) modalJumpPill?.classList.remove("hidden"); }, { passive: true });
  modalJumpPill?.addEventListener("click", () => { modalJumpPill.classList.add("hidden"); scrollModalToActive(); });
  modalTranscriptEl?.addEventListener("click", e => {
    if (e.target.closest(".btn-explain")) {
      handleExplain(e);
      return;
    }
    if (isTouch && e.target.closest("[data-hl]")) return;
    const seg = e.target.closest("[data-start]");
    if (seg) { seekTo(parseFloat(seg.dataset.start)); transcriptModal.classList.add("hidden"); document.body.style.overflow = ""; }
  });
  if (isTouch && modalTranscriptEl) setupTouchTooltips(modalTranscriptEl);

  // side-panel renderers
  /**
   * Renders the vocabulary cards.
   * @param {Array<{word: string, reading: string, en: string, zh: string, level: string}>} v Vocabulary array
   */
  function renderVocab(v) {
    panelVocab.innerHTML = v.length ? v.map(item => {
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
      return `<div class="card"><div class="card-front">${esc(item.word)}<span class="card-reading">【${esc(item.reading)}】</span><span class="card-level card-level-${(item.level || "").toLowerCase()} ml-auto">${esc(item.level)}</span></div><div class="card-body"><div class="card-en">${esc(item.en)}</div><div class="card-zh">${esc(item.zh)}</div><div class="flex justify-end mt-1"><button class="btn-anki text-gray-500 hover:text-blue-400 p-1" title="Sync to Anki" data-card='${cardJson}'><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button></div></div></div>`;
    }).join("") : `<p class="panel-empty">No vocab</p>`;
  }

  /**
   * Renders the grammar cards.
   * @param {Array<{pattern: string, reading: string, meaning_en: string, meaning_zh: string, level: string}>} g Grammar array
   */
  function renderGrammar(g) {
    panelGrammar.innerHTML = g.length ? g.map(item => {
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
      return `<div class="card"><div class="card-front">${esc(item.pattern)}<span class="card-level card-level-${(item.level || "").toLowerCase()} ml-auto">${esc(item.level)}</span></div><div class="card-body"><div class="card-en">${esc(item.meaning_en)}</div><div class="card-zh">${esc(item.meaning_zh)}</div><div class="flex justify-end mt-1"><button class="btn-anki text-gray-500 hover:text-blue-400 p-1" title="Sync to Anki" data-card='${cardJson}'><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button></div></div></div>`;
    }).join("") : `<p class="panel-empty">No grammar</p>`;
  }

  /**
   * Renders the expressions cards.
   * @param {Array<{expression: string, reading: string, en: string, zh: string}>} e Expressions array
   */
  function renderExpressions(e) {
    panelExpr.innerHTML = e.length ? e.map(item => {
      const cardJson = JSON.stringify({
        type: "expression",
        front: item.expression,
        reading: item.reading,
        en: item.en,
        zh: item.zh,
        example: item.context,
        tags: "japanese expression"
      }).replace(/'/g, "&#39;");
      return `<div class="card"><div class="card-front">${esc(item.expression)}<span class="card-reading">【${esc(item.reading)}】</span></div><div class="card-body"><div class="card-en">${esc(item.en)}</div><div class="card-zh">${esc(item.zh)}</div><div class="flex justify-end mt-1"><button class="btn-anki text-gray-500 hover:text-blue-400 p-1" title="Sync to Anki" data-card='${cardJson}'><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button></div></div></div>`;
    }).join("") : `<p class="panel-empty">No expressions</p>`;
  }

  /**
   * Renders the context-specific vocabulary and grammar cards.
   * @param {Array<Object>} c Context array
   */
  function renderContext(c) {
    panelContext.innerHTML = c.length ? c.map(item => {
      const cardJson = JSON.stringify({
        type: "context-specific",
        front: item.word || item.pattern,
        reading: item.reading,
        en: item.en || item.meaning_en,
        zh: item.zh || item.meaning_zh,
        example: item.example,
        level: "context-specific",
        tags: "japanese context-specific"
      }).replace(/'/g, "&#39;");
      return `<div class="card" style="border-color:rgba(167,139,250,0.2);"><div class="card-front">${esc(item.word || item.pattern)}${item.reading ? `<span class="card-reading">【${esc(item.reading)}】</span>` : ""}<span class="card-level card-level-context-specific ml-auto">ctx</span></div><div class="card-body"><div class="card-en">${esc(item.en || item.meaning_en)}</div><div class="card-zh">${esc(item.zh || item.meaning_zh)}</div><div class="flex justify-end mt-1"><button class="btn-anki text-gray-500 hover:text-blue-400 p-1" title="Sync to Anki" data-card='${cardJson}'><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button></div></div></div>`;
    }).join("") : `<p class="panel-empty">No ctx</p>`;
  }
});