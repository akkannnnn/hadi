/* ═══════════════════════════════════════════════════════
   Enderun AI v2.0 — Global JS Katmanı
   Tüm sayfalarda kullanılır.
═══════════════════════════════════════════════════════ */

const API_BASE = window.location.protocol === "file:" 
  ? "http://127.0.0.1:8742" 
  : "";

// ── Temel API çağrıcı ────────────────────────────────────────────
async function _fetch(yol, opts = {}) {
  const r = await fetch(API_BASE + yol, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(e.detail || r.statusText);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r;
}

const api = {
  get:    (yol) => _fetch(yol + (yol.includes('?') ? '&' : '?') + 't=' + Date.now()),
  post:   (yol, data) => _fetch(yol, { method: "POST", body: JSON.stringify(data) }),
  del:    (yol, data) => _fetch(yol, { method: "DELETE", body: JSON.stringify(data) }),

  upload: async (yol, file, field = "dosya") => {
    const fd = new FormData();
    fd.append(field, file);
    const r = await fetch(API_BASE + yol, { method: "POST", body: fd });
    if (!r.ok) {
      const e = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(e.detail || r.statusText);
    }
    return r.json();
  },

  download: async (yol, data, filename) => {
    const r = data
      ? await _fetch(yol, { method: "POST", body: JSON.stringify(data) })
      : await _fetch(yol);
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement("a"), { href: url, download: filename });
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  },
};

// ── Sayfa geçişi ─────────────────────────────────────────────────
function git(sayfa) { window.location.href = sayfa; }

// ── Bildirim ─────────────────────────────────────────────────────
function bildir(mesaj, tip = "bilgi", sure = 3800) {
  let wrap = document.getElementById("notif-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "notif-wrap";
    document.body.appendChild(wrap);
  }
  const ikonlar = { bilgi: "ℹ️", basari: "✅", uyari: "⚠️", hata: "❌" };
  const el = document.createElement("div");
  el.className = `notif notif-${tip}`;
  el.innerHTML = `<span>${ikonlar[tip]||"ℹ️"}</span><span>${mesaj}</span>`;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.cssText += "opacity:0;transition:opacity .35s";
    setTimeout(() => el.remove(), 380);
  }, sure);
}

// ── Modal ─────────────────────────────────────────────────────────
let _modalStack = [];

function modalAc(html, baslik = "") {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal-box">${baslik ? `<h3>${baslik}</h3>` : ""}${html}</div>`;
  bg.onclick = e => { if (e.target === bg) modalKapat(); };
  document.body.appendChild(bg);
  _modalStack.push(bg);
}

function modalKapat() {
  const bg = _modalStack.pop();
  if (bg) bg.remove();
}

// ── Onay kutusu ──────────────────────────────────────────────────
function onay(mesaj) {
  return new Promise(res => {
    window.__onayRes = res;
    modalAc(`
      <p style="color:var(--text);margin-bottom:20px">${mesaj}</p>
      <div class="flex" style="justify-content:flex-end;gap:10px">
        <button class="btn btn-ghost btn-sm" onclick="modalKapat();window.__onayRes(false)">İptal</button>
        <button class="btn btn-red btn-sm"   onclick="modalKapat();window.__onayRes(true)">Onayla</button>
      </div>`, "Onay Gerekiyor");
  });
}

// ── Global state yükle ────────────────────────────────────────────
async function durumYukle() {
  try { return await api.get("/api/durum"); }
  catch { return null; }
}

// ── Tablo sıralama ────────────────────────────────────────────────
// satirlar = array, kolon = key, yon ref object {v:1}
function satirSirala(satirlar, kolon, yon, sayisal = false) {
  return [...satirlar].sort((a, b) => {
    const av = a[kolon] ?? ""; const bv = b[kolon] ?? "";
    if (sayisal) return yon.v * (Number(av) - Number(bv));
    return yon.v * String(av).localeCompare(String(bv), "tr");
  });
}

// Tema — tüm sayfalarda otomatik uygula
(function(){
  const t=localStorage.getItem("akkan-tema")||"karanlik";
  if(t!=="karanlik")document.documentElement.setAttribute("data-tema",t);
})();

// Doğal sıralama — 9A < 10A < 11B
function _dogalSirala(s){
  return String(s).replace(/(\d+)/g, n=>n.padStart(6,'0'));
}
