const API_BASE = "https://mimichan.up.railway.app";

const vocabList  = document.getElementById("vocab-list");
const vocabCount = document.getElementById("vocab-count");
const searchInput = document.getElementById("search-input");
const levelFilter = document.getElementById("level-filter");
const typeFilter  = document.getElementById("type-filter");
const emptyState  = document.getElementById("empty-state");
const exportLink  = document.getElementById("export-link");

exportLink.href = `${API_BASE}/vocab/export.csv`;

let allItems = [];

function esc(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;");
}

function renderItems(items) {
  vocabCount.textContent = `${items.length} item${items.length !== 1 ? "s" : ""} saved`;

  if (items.length === 0) {
    vocabList.innerHTML = "";
    vocabList.classList.add("hidden");
    emptyState.classList.remove("hidden");
    return;
  }

  emptyState.classList.add("hidden");
  vocabList.classList.remove("hidden");

  vocabList.innerHTML = items.map(item => {
    const level     = (item.level || "").toLowerCase();
    const typeLabel = item.type === "expression" ? "phrase" : (item.type || "");
    return `
      <div class="card" data-id="${esc(item.id)}">
        <div class="card-front">
          <span class="text-base font-bold text-white">${esc(item.word)}</span>
          ${item.reading ? `<span class="card-reading">【${esc(item.reading)}】</span>` : ""}
          <span class="card-level card-level-${level} ml-auto">${esc(item.level || "ctx")}</span>
        </div>
        <div class="card-body">
          <div class="card-en text-blue-400 font-medium">${esc(item.en)}</div>
          <div class="card-zh text-emerald-400 opacity-80">${esc(item.zh)}</div>
          ${item.example ? `<div class="text-xs text-gray-500 italic mt-2 border-t border-gray-800/50 pt-2">${esc(item.example)}</div>` : ""}
          <div class="flex items-center justify-between mt-3 pt-2 border-t border-gray-800/50">
            <span class="text-[10px] text-gray-600 uppercase tracking-wider font-bold">${esc(typeLabel)}</span>
            <button class="btn-delete p-1.5 rounded-md text-gray-600 hover:text-red-400 hover:bg-red-950/50 transition-all" data-id="${esc(item.id)}" title="Remove from bank">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
            </button>
          </div>
        </div>
      </div>`;
  }).join("");
}

function filterItems() {
  const q     = searchInput.value.toLowerCase();
  const level = levelFilter.value.toLowerCase();
  const type  = typeFilter.value.toLowerCase();

  const filtered = allItems.filter(item => {
    const matchesSearch = !q
      || (item.word    || "").toLowerCase().includes(q)
      || (item.reading || "").toLowerCase().includes(q)
      || (item.en      || "").toLowerCase().includes(q)
      || (item.zh      || "").toLowerCase().includes(q);
    const matchesLevel = level === "all" || (item.level || "").toLowerCase() === level;
    const matchesType  = type  === "all" || (item.type  || "").toLowerCase() === type;
    return matchesSearch && matchesLevel && matchesType;
  });

  renderItems(filtered);
}

async function deleteItem(id) {
  if (!confirm("Remove this item from your vocab bank?")) return;
  try {
    const resp = await fetch(`${API_BASE}/api/vocab/${id}`, { method: "DELETE" });
    if (resp.ok) {
      allItems = allItems.filter(i => i.id !== id);
      filterItems();
    } else {
      alert("Failed to delete item.");
    }
  } catch (err) {
    console.error(err);
    alert("Failed to delete item.");
  }
}

// Event delegation — avoids global onclick functions in module scope
vocabList.addEventListener("click", e => {
  const btn = e.target.closest(".btn-delete");
  if (btn) deleteItem(btn.dataset.id);
});

searchInput.addEventListener("input",  filterItems);
levelFilter.addEventListener("change", filterItems);
typeFilter.addEventListener("change",  filterItems);

async function fetchVocab() {
  try {
    const resp = await fetch(`${API_BASE}/api/vocab`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    allItems = await resp.json();
    renderItems(allItems);
  } catch (err) {
    console.error(err);
    vocabList.innerHTML = `<div class="py-10 text-center text-red-400 text-sm">Failed to load vocab. Check your connection.</div>`;
  }
}

fetchVocab();
