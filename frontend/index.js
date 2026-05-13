const API_BASE = "https://mimichan.up.railway.app";
const AUTH_HEADER = "";
const OFFLINE_KEY = "mimichan_offline_episodes";

function getOfflineMap() {
  try {
    return JSON.parse(localStorage.getItem(OFFLINE_KEY) || "{}");
  } catch { return {}; }
}

function episodeCard(ep, isDownloaded, showServerTags) {
  const dateStr = ep.date;
  const yyyymm = dateStr.slice(0, 7);
  const dd = dateStr.slice(8);
  const title = ep.meta?.title || "Untitled Episode";
  const channel = ep.meta?.channel || "";
  const duration = ep.meta?.duration
    ? Math.floor(ep.meta.duration / 60) + ":" + String(ep.meta.duration % 60).padStart(2, "0")
    : "";

  let tags = "";
  if (showServerTags) {
    if (ep.has_audio !== false) tags += `<span class="text-emerald-400">● audio</span> `;
    if (ep.has_transcript !== false) tags += `<span class="text-blue-400">● transcript</span> `;
    if ((ep.meta?.url || "").includes("youtu")) tags += `<span class="text-red-400">● video</span>`;
  }
  if (isDownloaded) tags += `<span class="text-green-400">● offline</span>`;

  return `
    <div class="relative border border-gray-800 rounded-xl hover:border-gray-600 hover:bg-gray-900 transition-all group">
      <a href="episode.html?id=${dateStr}" class="block p-4">
        <div class="flex items-start gap-4">
          <div class="shrink-0 text-center bg-gray-800 rounded-lg px-3 py-2 group-hover:bg-gray-700 transition-colors">
            <div class="text-xs text-gray-400 font-mono">${yyyymm}</div>
            <div class="text-lg font-bold font-mono leading-none text-white">${dd}</div>
          </div>
          <div class="flex-1 min-w-0">
            <div class="font-medium text-white truncate">${title}</div>
            <div class="text-sm text-gray-400 mt-0.5">${channel}</div>
            <div class="flex items-center gap-3 mt-2 text-xs">${tags}</div>
          </div>
          ${duration ? `<div class="shrink-0 text-sm text-gray-500 font-mono pr-8">${duration}</div>` : ""}
        </div>
      </a>
    </div>`;
}

async function loadEpisodes() {
  const countEl = document.getElementById("ep-count");
  const list = document.getElementById("episodes-list");
  const offlineMap = getOfflineMap();

  let episodes = null;
  let isOffline = false;

  try {
    const res = await fetch(`${API_BASE}/api/episodes`, {
      headers: AUTH_HEADER ? { Authorization: AUTH_HEADER } : {},
    });
    if (!res.ok) throw new Error("Failed to fetch");
    episodes = await res.json();
  } catch {
    isOffline = true;
    episodes = Object.values(offlineMap).sort((a, b) => b.date.localeCompare(a.date));
  }

  if (isOffline) {
    if (episodes.length === 0) {
      countEl.innerText = "Offline";
      list.innerHTML = `
        <div class="text-center py-24 border border-dashed border-gray-800 rounded-2xl">
          <div class="text-5xl mb-4">📵</div>
          <p class="text-lg font-medium text-white mb-2">No connection</p>
          <p class="text-sm text-gray-500">Download episodes while online to access them here.</p>
        </div>`;
      return;
    }

    const banner = document.createElement("div");
    banner.className =
      "text-xs text-amber-500 bg-amber-950/40 border border-amber-900/50 rounded-lg px-3 py-2 mb-3 flex items-center gap-2";
    banner.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.56 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg> Offline — showing downloaded episodes only`;
    list.before(banner);
  }

  countEl.innerText = `${episodes.length} episode${episodes.length !== 1 ? "s" : ""}`;

  if (episodes.length === 0) {
    list.innerHTML = `
      <div class="text-center py-24 border border-dashed border-gray-800 rounded-2xl">
        <div class="text-5xl mb-4">🎧</div>
        <p class="text-lg font-medium text-white mb-2">No episodes yet</p>
      </div>`;
    return;
  }

  list.innerHTML = episodes
    .map((ep) => episodeCard(ep, !!offlineMap[ep.date], !isOffline))
    .join("");
}

loadEpisodes();
