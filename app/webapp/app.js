// app/webapp/app.js
const tg = window.Telegram?.WebApp;
try { tg?.ready?.(); } catch { }
try { tg?.expand?.(); } catch { }

let PRICE_PER_GROUP = 25000;
let LOADED_GROUPS = [];

// ====== Config truncate ======
const MAX_DESC_CHARS = 120;

const ADMIN_USERNAME = "ensexlopedia";         // Username admin
let __qrImageReady = false;                    // true kalau QR.png sudah load
let __statusPollTimer = null;                  // interval polling status

// ====== Helpers UI ======
function showEmpty(message) {
  const root = document.getElementById('list');
  root.innerHTML = `
    <div style="padding:20px;color:#cfc">
      <div style="font-weight:800;font-size:18px;margin-bottom:6px">Tidak ada data katalog</div>
      <div style="opacity:.85;margin-bottom:10px">${message}</div>
      <a href="/api/config" target="_blank" style="display:inline-block;padding:10px 12px;border:1px solid #ffffff22;border-radius:10px;color:#fff;text-decoration:none">Lihat /api/config</a>
    </div>
  `;
}

function openAdminChat(e) {
  try { e?.preventDefault?.(); } catch { }
  const url = `https://t.me/${ADMIN_USERNAME}`;

  // Prioritaskan API Telegram agar tidak menutup Mini App di Desktop
  if (window.Telegram?.WebApp?.openTelegramLink) {
    window.Telegram.WebApp.openTelegramLink(url);
  } else if (window.Telegram?.WebApp?.openLink) {
    // beberapa klien lama
    window.Telegram.WebApp.openLink(url, { try_instant_view: false });
  } else {
    // fallback browser
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

// Normalisasi transform ImageKit agar tidak dobel '?'
function withTransform(url, tr = 'w-600,fo-auto') {
  if (!url) return url;
  // kalau sudah ada ?tr=, biarkan saja
  if (/\btr=/.test(url)) return url;
  return url.includes('?') ? `${url}&tr=${tr}` : `${url}?tr=${tr}`;
}

// Truncate aman emoji + potong di batas kata
function truncateText(text, max = MAX_DESC_CHARS) {
  if (!text) return "";
  try {
    const seg = new Intl.Segmenter('id', { granularity: 'grapheme' });
    const graphemes = Array.from(seg.segment(text), s => s.segment);
    if (graphemes.length <= max) return text;
    const partial = graphemes.slice(0, max).join('');
    const lastSpace = partial.lastIndexOf(' ');
    const safe = lastSpace > 40 ? partial.slice(0, lastSpace) : partial;
    return safe.replace(/[.,;:!\s]*$/, '') + '…';
  } catch {
    if (text.length <= max) return text;
    let t = text.slice(0, max);
    const lastSpace = t.lastIndexOf(' ');
    if (lastSpace > 40) t = t.slice(0, lastSpace);
    return t.replace(/[.,;:!\s]*$/, '') + '…';
  }
}

/* ---------------- UID Utilities ---------------- */
function parseUidFromInitData(initDataStr) {
  if (!initDataStr) return null;
  try {
    const usp = new URLSearchParams(initDataStr);
    const userStr = usp.get('user');
    if (!userStr) return null;
    const obj = JSON.parse(userStr);
    const id = obj && obj.id ? Number(obj.id) : null;
    return Number.isFinite(id) ? id : null;
  } catch { return null; }
}

function getUserId() {
  const u1 = tg?.initDataUnsafe?.user?.id;
  if (u1) return Number(u1);
  const u2 = parseUidFromInitData(tg?.initData);
  if (u2) return u2;
  const qp = new URLSearchParams(window.location.search);
  const u3 = qp.get("uid");
  return u3 ? Number(u3) : null;
}

// Debug global
window.__UID__ = window.__UID__ ?? getUserId();

/* ---------------- Boot (idempotent) ---------------- */
async function initUI() {
  try {
    const r = await fetch('/api/config', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const cfg = await r.json();
    console.log('[config]', cfg);
    PRICE_PER_GROUP = parseInt(cfg?.price_idr ?? '25000', 10) || 25000;
    LOADED_GROUPS = Array.isArray(cfg?.groups) ? cfg.groups : [];
    if (!LOADED_GROUPS.length) {
      showEmpty('Server mengembalikan <code>groups: []</code>. Cek <code>GROUP_IDS_JSON</code> di Railway.');
      syncTotalText();
      return;
    }
  } catch (e) {
    console.error('Fetch /api/config error:', e);
    showEmpty('Gagal memuat konfigurasi dari server.');
    return;
  }

  renderNeonList(LOADED_GROUPS);
  syncTotalText();
  document.getElementById('pay')?.addEventListener('click', onPay);
}

// PENTING: jalan meski app.js diload setelah DOMContentLoaded
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initUI, { once: true });
} else {
  initUI();
}

/* ---------------- Render List ---------------- */
function renderNeonList(groups) {
  const root = document.getElementById('list');
  root.innerHTML = '';

  (groups || []).forEach(g => {
    const id = String(g.id);
    const name = String(g.name ?? id);
    const desc = String(g.desc ?? '').trim();
    const longDesc = String(g.long_desc ?? desc).trim();
    const img = withTransform(String(g.image ?? '').trim());

    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.id = id;

    // === CHECK BADGE (bisa diklik) ===
    const check = document.createElement('div');
    check.className = 'check';
    check.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16"><path fill="#fff" d="M9,16.2 4.8,12 3.4,13.4 9,19 21,7 19.6,5.6"/></svg>`;
    // interaksi: klik & keyboard
    check.style.cursor = 'pointer';
    check.tabIndex = 0;
    check.setAttribute('role', 'checkbox');
    check.setAttribute('aria-checked', 'false');
    check.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();      // jangan buka modal
      toggleSelect(card);
      // aria update
      check.setAttribute('aria-checked', String(card.classList.contains('selected')));
    });
    check.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        toggleSelect(card);
        check.setAttribute('aria-checked', String(card.classList.contains('selected')));
      }
    });

    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    if (img) thumb.style.backgroundImage = `url("${img}")`;

    const meta = document.createElement('div');
    meta.className = 'meta';

    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = name;

    const p = document.createElement('div');
    p.className = 'desc';
    p.textContent = truncateText(desc || 'Akses eksklusif grup pilihan.');

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-solid';
    btn.style.marginLeft = 'auto';
    btn.textContent = 'Pilih Grup';

    // tombol: toggle pilih
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleSelect(card);
      check.setAttribute('aria-checked', String(card.classList.contains('selected')));
    });

    // klik kartu (kecuali tombol/check) → buka detail
    card.addEventListener('click', (e) => {
      if (btn.contains(e.target) || check.contains(e.target)) return;
      openDetailModal({ id, name, desc: longDesc || desc, image: img });
    });

    meta.append(title, p, btn);
    card.append(check, thumb, meta);
    root.appendChild(card);

    // set state awal
    updateButtonState(card, btn);
    check.setAttribute('aria-checked', String(card.classList.contains('selected')));
  });

  updateBadge();
  ensureSelectAllUI();
  refreshSelectAllUI();

}


/* ---------------- Interaksi ---------------- */
function toggleSelect(card) {
  card.classList.toggle('selected');
  const btn = card.querySelector('button');
  if (btn) updateButtonState(card, btn);
  syncTotalText();
  updateBadge();
  refreshSelectAllUI();   // << tambah baris ini
}


function updateButtonState(card, btn) {
  const selected = card.classList.contains('selected');
  btn.textContent = selected ? 'Batal' : 'Pilih Grup';
  btn.classList.toggle('btn-solid', !selected);
  btn.classList.toggle('btn-ghost', selected);
  if (!btn.style.marginLeft) btn.style.marginLeft = 'auto';
}

function openDetailModal(item) {
  const m = document.getElementById('detail');
  const card = document.querySelector(`.card[data-id="${CSS.escape(item.id)}"]`);
  const selected = card?.classList.contains('selected');

  m.innerHTML = `
    <div class="sheet" id="sheet">
      <div class="hero" id="hero">
        ${item.image ? `<img id="detail-img" src="${withTransform(item.image)}" alt="${escapeHtml(item.name)}">` : ''}
      </div>
      <div class="title" id="ttl">${escapeHtml(item.name)}</div>
      <div class="desc" id="dsc">${escapeHtml(item.desc || '')}</div>
      <div class="row" id="btns">
        <button class="close">Tutup</button>
        <button class="add">${selected ? 'Batal' : 'Pilih Grup'}</button>
      </div>
    </div>
  `;
  m.hidden = false;

  const sheet = document.getElementById('sheet');
  const hero = document.getElementById('hero');
  const img = document.getElementById('detail-img');
  const ttl = document.getElementById('ttl');
  const dsc = document.getElementById('dsc');
  const btns = document.getElementById('btns');

  const fitHero = () => {
    const vh = window.innerHeight;
    const styles = getComputedStyle(sheet);
    const pad = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
    const gaps = 12 * 2;
    const nonImg = ttl.offsetHeight + dsc.offsetHeight + btns.offsetHeight + pad + gaps;

    const target = Math.max(200, Math.min(vh * 0.98 - nonImg, vh * 0.92));
    hero.style.maxHeight = `${Math.floor(target)}px`;

    if (img && img.naturalWidth && img.naturalHeight) {
      const portrait = img.naturalHeight > img.naturalWidth * 1.15;
      img.style.objectFit = portrait ? 'cover' : 'contain';
      if (portrait) { img.style.height = '100%'; hero.style.height = `${Math.floor(target)}px`; }
      else { img.style.height = 'auto'; hero.style.height = 'auto'; }
    }
  };

  if (img) {
    if (img.complete) fitHero();
    else img.addEventListener('load', fitHero, { once: true });
    window.addEventListener('resize', fitHero, { passive: true });
  }

  m.querySelector('.close')?.addEventListener('click', () => closeDetailModal());
  m.querySelector('.add')?.addEventListener('click', () => { if (card) toggleSelect(card); closeDetailModal(); });
  m.addEventListener('click', (e) => { if (e.target === m) closeDetailModal(); }, { once: true });
}

function closeDetailModal() {
  const m = document.getElementById('detail');
  m.hidden = true;
  m.innerHTML = '';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function getSelectedIds() {
  return [...document.querySelectorAll('.card.selected')].map(el => el.dataset.id);
}

function updateBadge() {
  const n = getSelectedIds().length;
  const b = document.getElementById('cartBadge');
  if (n > 0) { b.hidden = false; b.textContent = String(n); }
  else b.hidden = true;
}

function getCards() { return [...document.querySelectorAll('.card')]; }

function refreshSelectAllUI() {
  const bar = document.getElementById('selectAllBar');
  if (!bar) return;
  const cards = getCards();
  const total = cards.length;
  const selected = cards.filter(c => c.classList.contains('selected')).length;

  const btn = document.getElementById('selectAllBtn');
  const count = document.getElementById('selectAllCount');

  // state: none / mixed / all
  let state = 'false';
  if (selected === 0) state = 'false';
  else if (selected === total) state = 'true';
  else state = 'mixed';

  btn.setAttribute('aria-checked', state);
  count.textContent = `(${selected}/${total})`;
}

function setAllSelected(flag) {
  const cards = getCards();
  cards.forEach(card => {
    const already = card.classList.contains('selected');
    if (flag && !already) card.classList.add('selected');
    if (!flag && already) card.classList.remove('selected');
    // perbarui tombol di dalam kartu
    const btn = card.querySelector('button');
    if (btn) updateButtonState(card, btn);
  });
  updateBadge();
  syncTotalText();
  refreshSelectAllUI();
}

function ensureSelectAllUI() {
  if (document.getElementById('selectAllBar')) return;

  const headerEl = document.querySelector('.header');
  const bar = document.createElement('div');
  bar.className = 'select-all-bar';
  bar.id = 'selectAllBar';
  bar.innerHTML = `
    <div id="selectAllBtn" class="select-all-toggle" role="checkbox" aria-checked="false" aria-label="Pilih semua">
      <svg viewBox="0 0 24 24" width="16" height="16">
        <path fill="#fff" d="M9,16.2 4.8,12 3.4,13.4 9,19 21,7 19.6,5.6"/>
      </svg>
    </div>
    <div class="select-all-label">Pilih semua <span id="selectAllCount" class="select-all-count"></span></div>
  `;
  headerEl?.after(bar);

  const toggle = document.getElementById('selectAllBtn');
  const onToggle = (e) => {
    e.preventDefault();
    const state = toggle.getAttribute('aria-checked'); // 'false' | 'true' | 'mixed'
    // jika mixed → anggap klik = pilih semua
    const wantAll = state !== 'true';
    setAllSelected(wantAll);
  };
  toggle.addEventListener('click', onToggle);
  toggle.addEventListener('keydown', (e) => {
    if (e.key === ' ' || e.key === 'Enter') onToggle(e);
  });
}

function formatRupiah(n) {
  if (!Number.isFinite(n)) return "Rp 0";
  return "Rp " + n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function syncTotalText() {
  const t = getSelectedIds().length * PRICE_PER_GROUP;
  const payBtn = document.getElementById('pay');
  document.getElementById('total-text').textContent = formatRupiah(t);
  const disableBecauseNoUID = !window.__UID__;
  const disabled = t <= 0 || disableBecauseNoUID;
  payBtn?.toggleAttribute('disabled', disabled);
}

// ===== Countdown berbasis deadline (tetap jalan meski app di-background) =====
let __qrCountdownTimer = null;
let __qrDeadline = 0;

let __qrPayTimer = null;
let __payDeadline = 0;

function fmtMMSS(leftSec){
  const s  = Math.max(0, leftSec|0);
  const mm = String(Math.floor(s/60)).padStart(2,'0');
  const ss = String(s%60).padStart(2,'0');
  return `${mm}:${ss}`;
}

// --- FASE 1: menunggu QR muncul (maks 3 menit) ---
function startQrCountdown(maxSeconds = 180, onExpire){
  const msgEl  = document.getElementById('qrMsg');
  const progEl = document.getElementById('qrProg');
  __qrDeadline = Date.now() + maxSeconds*1000;

  const tick = () => {
    if (!msgEl) return stopQrCountdown();
    const left = Math.ceil((__qrDeadline - Date.now())/1000);
    if (left <= 0){
      stopQrCountdown();
      try{ onExpire?.(); }catch{}
      return;
    }
    msgEl.textContent = `Mohon tunggu sebentar (${fmtMMSS(left)})`;
    if (progEl){
      const spent = maxSeconds - left;
      progEl.style.width = `${Math.min(100, Math.max(0, (spent/maxSeconds)*100))}%`;
    }
  };

  stopQrCountdown();
  __qrCountdownTimer = setInterval(tick, 1000);
  tick();
}

function stopQrCountdown(){
  if (__qrCountdownTimer){ clearInterval(__qrCountdownTimer); __qrCountdownTimer = null; }
  __qrDeadline = 0;
}

// --- FASE 2: masa pembayaran (5 menit) ---
function startPayCountdown(maxSeconds = 300){
  const msgEl  = document.getElementById('qrMsg');
  const progEl = document.getElementById('qrProg');
  __payDeadline = Date.now() + maxSeconds*1000;

  const tick = () => {
    if (!msgEl) return stopPayCountdown();
    const left = Math.ceil((__payDeadline - Date.now())/1000);
    if (left <= 0){
      stopPayCountdown();
      showPaymentExpired();
      return;
    }
    msgEl.innerHTML = `Silahkan lakukan pembayaran dengan scan QRIS.<br>Waktu pelunasan pembayaran (${fmtMMSS(left)})`;
    if (progEl){
      const spent = maxSeconds - left;
      progEl.style.width = `${Math.min(100, Math.max(0, (spent/maxSeconds)*100))}%`;
    }
  };

  stopPayCountdown();
  __qrPayTimer = setInterval(tick, 1000);
  tick();
}

function stopPayCountdown(){
  if (__qrPayTimer){ clearInterval(__qrPayTimer); __qrPayTimer = null; }
  __payDeadline = 0;
}

// --- Saat kembali ke Mini App, kejar waktu & hidupkan ulang timer jika perlu ---
function handleVisibilityResume(){
  // fase 1
  if (__qrDeadline){
    const left = Math.ceil((__qrDeadline - Date.now())/1000);
    if (left <= 0){
      stopQrCountdown(); // onExpire sudah dipanggil dari startQrCountdown saat habis
    } else if (!__qrCountdownTimer){
      __qrCountdownTimer = setInterval(() => {
        const el = document.getElementById('qrMsg');
        if (!el) return stopQrCountdown();
        const l = Math.ceil((__qrDeadline - Date.now())/1000);
        if (l <= 0){ stopQrCountdown(); return; }
        el.textContent = `Mohon tunggu sebentar (${fmtMMSS(l)})`;
      }, 1000);
    }
  }

  // fase 2
  if (__payDeadline){
    const left = Math.ceil((__payDeadline - Date.now())/1000);
    if (left <= 0){
      stopPayCountdown();
      showPaymentExpired();
    } else if (!__qrPayTimer){
      __qrPayTimer = setInterval(() => {
        const el = document.getElementById('qrMsg');
        if (!el) return stopPayCountdown();
        const l = Math.ceil((__payDeadline - Date.now())/1000);
        if (l <= 0){ stopPayCountdown(); showPaymentExpired(); return; }
        el.innerHTML = `Silahkan lakukan pembayaran dengan scan QRIS.<br>Waktu pelunasan pembayaran (${fmtMMSS(l)})`;
      }, 1000);
    }
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') handleVisibilityResume();
});
window.addEventListener('focus', handleVisibilityResume);


function showPaymentExpired() {
  const box = document.querySelector('#qr > div');
  if (!box) return;
  box.innerHTML = `
    <div style="font-weight:900;font-size:20px;margin-bottom:6px">Batas waktu pembayaran sudah habis!</div>
    <div style="opacity:.85;margin:6px 0 12px">Silakan kembali ke halaman pemesanan untuk membuat tagihan baru.</div>
    <button class="close" id="btnBackOrder">Kembali ke Halaman Pemesanan</button>
  `;
  document.getElementById('btnBackOrder')?.addEventListener('click', hideQRModal);
}

async function onPay() {
  const selected = getSelectedIds();
  const selectedNames = getSelectedGroupNames(selected);
  const amount = selected.length * PRICE_PER_GROUP;
  if (!selected.length) return;

  const userId = window.__UID__ || getUserId();
  if (!userId) {
    showQRModal(`<div style="color:#f55">Gagal membaca user Telegram. Buka lewat tombol bot.</div>`);
    return;
  }

  let inv;
  try {
    const res = await fetch(`${window.location.origin}/api/invoice`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, groups: selected, amount })
    });
    if (!res.ok) throw new Error(await res.text());
    inv = await res.json();
  } catch (e) {
    showQRModal(`<div style="color:#f55">Create invoice gagal:<br><code>${escapeHtml(e.message || String(e))}</code></div>`);
    return;
  }

  const qrPngUrl = `${window.location.origin}/api/qr/${inv.invoice_id}.png?amount=${amount}&t=${Date.now()}`;

  // — Modal awal: judul + list pesanan + progress (TANPA img.src) —
  showQRModal(`
    <div style="text-align:center">
      <div style="font-weight:900;font-size:20px;margin-bottom:6px">Pembayaran Grup VIP</div>
      ${renderSelectedBadges(selectedNames)}
      <div id="qrMsg" style="margin:6px 0 12px; opacity:.85">Mohon tunggu sebentar (maks 3 menit)…</div>
      <div style="height:6px;background:#222;border-radius:6px;overflow:hidden;margin:8px 0 14px">
        <div id="qrProg" style="height:100%;width:0%;background:#fff3;border-radius:6px"></div>
      </div>
      <img id="qrImg" alt="QR" style="max-width:100%;display:none;margin:0 auto;border-radius:10px;border:1px solid #ffffff1a">
      <button class="close" id="closeModal">Tutup</button>
    </div>
  `);
  document.getElementById('closeModal')?.addEventListener('click', hideQRModal);

  // Fase 1: tunggu QR muncul (maks 3 menit). Jika habis → gagal.
  startQrCountdown(180, () => showPaymentLoadFailed(selectedNames));

  // Prefetch + verifikasi isi respons:
  const ac = new AbortController();
  const hardTimeout = setTimeout(() => ac.abort(), 180000); // 3 menit
  let ok = false;

  try {
    const res = await fetch(qrPngUrl, { cache: 'no-store', signal: ac.signal });
    const blob = await res.blob();
    const valid = res.ok && await isImageBlobValid(blob);

    if (valid) {
      const url = URL.createObjectURL(blob);
      const img = document.getElementById('qrImg');
      if (img) {
        img.onload = () => {
          // Safeguard: kalau dimensinya kelewat kecil, kemungkinan bukan QR yang benar
          const tooSmall = (img.naturalWidth < 200 || img.naturalHeight < 200);
          if (tooSmall) {
            URL.revokeObjectURL(url);
            stopQrCountdown();
            showPaymentLoadFailed(selectedNames);
            return;
          }
          // Sukses: tampilkan & mulai countdown pembayaran
          img.style.display = 'block';
          stopQrCountdown();
          document.getElementById('qrMsg').innerHTML =
            `Silahkan lakukan pembayaran dengan scan QRIS.<br>Waktu pelunasan pembayaran (05:00)`;
          startPayCountdown(300);

          // Mulai polling status (dan simpan id timer)
          if (__statusPollTimer) { clearInterval(__statusPollTimer); __statusPollTimer = null; }
          const statusUrl = `${window.location.origin}/api/invoice/${inv.invoice_id}/status`;
          __statusPollTimer = setInterval(async () => {
            try {
              const r = await fetch(statusUrl);
              if (!r.ok) return;
              const s = await r.json();
              if (s.status === "PAID") {
                clearInterval(__statusPollTimer);
                __statusPollTimer = null;
                hideQRModal();
                tg?.close?.();
              }
            } catch {}
          }, 2000);
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          stopQrCountdown();
          showPaymentLoadFailed(selectedNames);
        };
        img.src = url;
        ok = true;
      }
    }
  } catch (err) {
    // fetch error / aborted → biarkan handled di finally
  } finally {
    clearTimeout(hardTimeout);
  }

  if (!ok) {
    stopQrCountdown();
    showPaymentLoadFailed(selectedNames);
  }
}



function showQRModal(html) {
  const m = document.getElementById('qr');
  m.innerHTML = `<div>${html}</div>`;
  m.hidden = false;
}

function hideQRModal() {
  stopQrCountdown();
  stopPayCountdown();
  if (__statusPollTimer) { clearInterval(__statusPollTimer); __statusPollTimer = null; }
  const m = document.getElementById('qr');
  m.hidden = true;
  m.innerHTML = '';
}


function showPaymentLoadFailed(selectedNames) {
  const m = document.getElementById('qr');
  const list = (selectedNames || [])
    .map(n => `<span class="tag">${escapeHtml(n)}</span>`)
    .join("");

  m.innerHTML = `
    <div style="text-align:center">
      <div style="font-weight:900;font-size:22px;margin-bottom:8px">Halaman pembayaran gagal dimuat</div>
      ${list ? `<div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;justify-content:center">${list}</div>` : ""}
      <div style="opacity:.9;margin:0 0 14px">
        Silahkan chat admin dan kirimkan <b>screenshot</b> halaman ini untuk proses pembayaran manual.
      </div>
      <button id="btnChatAdmin" type="button" class="close" style="margin-bottom:10px">
        Chat Admin @${ADMIN_USERNAME}
      </button>
      <button id="btnBackOrder" type="button" class="close" style="background:#2b2b2b">
        Kembali ke Halaman Pemesanan
      </button>
    </div>
  `;
  m.hidden = false;

  // ✅ penting: pakai openTelegramLink, bukan <a href=...>
  document.getElementById('btnChatAdmin')?.addEventListener('click', openAdminChat);
  document.getElementById('btnBackOrder')?.addEventListener('click', hideQRModal);

}


// Ambil array nama grup dari daftar id terpilih
function getSelectedGroupNames(selectedIds) {
  const map = new Map(LOADED_GROUPS.map(g => [String(g.id), String(g.name || g.id)]));
  return (selectedIds || getSelectedIds()).map(id => map.get(String(id))).filter(Boolean);
}

// Render pill list kecil menyamping (chip mini) — tanpa kata VIP & EnSEXlopedia
function renderSelectedBadges(names) {
  if (!names?.length) return "";

  // Hapus kata "VIP" dan "EnSEXlopedia" (case-insensitive)
  const clean = names.map(n =>
    String(n)
      .replace(/\bVIP\b/gi, "")
      .replace(/\bEnSEXlopedia\b/gi, "")
      .trim()
  );

  return `
    <div style="
      display:flex; flex-wrap:wrap; gap:6px; justify-content:center;
      margin:6px 0 8px
    ">
      ${clean.map(n => `
        <span style="
          display:inline-flex; align-items:center;
          padding:6px 8px; border-radius:999px;
          background:#141414; border:1px solid #ffffff22; color:#fff;
          font-size:11px; line-height:1; font-weight:700; letter-spacing:.2px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
          max-width:calc(50% - 6px)
        ">
          ${escapeHtml(n)}
        </span>
      `).join("")}
    </div>
  `;
}

// === Helper: cek blob adalah PNG/JPEG (signature) ===
async function isImageBlobValid(blob) {
  try {
    if (!blob) return false;
    const type = blob.type || "";
    if (!type.startsWith("image/")) return false;

    // Baca 12 byte pertama untuk tanda tangan
    const ab = await blob.slice(0, 12).arrayBuffer();
    const b = new Uint8Array(ab);

    // PNG signature: 89 50 4E 47 0D 0A 1A 0A
    const isPng = b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4E && b[3] === 0x47 &&
      b[4] === 0x0D && b[5] === 0x0A && b[6] === 0x1A && b[7] === 0x0A;

    // JPEG signature: FF D8
    const isJpeg = b[0] === 0xFF && b[1] === 0xD8;

    return isPng || isJpeg;
  } catch { return false; }
}

// === Helper: bersihkan label grup (tanpa "VIP" & "EnSEXlopedia") ===
function shortName(name = "") {
  return String(name)
    .replace(/\bVIP\b/ig, "")
    .replace(/\bEnSEXlopedia\b/ig, "")
    .replace(/\s+/g, " ")
    .trim();
}
