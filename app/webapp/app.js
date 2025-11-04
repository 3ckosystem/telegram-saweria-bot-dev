// app/webapp/app.js
const tg = window.Telegram?.WebApp;
try { tg?.ready?.(); } catch {}
try { tg?.expand?.(); } catch {}

let PRICE_PER_GROUP = 25000;
let LOADED_GROUPS = [];

// ====== Config truncate ======
const MAX_DESC_CHARS = 120;

// ====== Helpers UI ======
function showEmpty(message){
  const root = document.getElementById('list');
  root.innerHTML = `
    <div style="padding:20px;color:#cfc">
      <div style="font-weight:800;font-size:18px;margin-bottom:6px">Tidak ada data katalog</div>
      <div style="opacity:.85;margin-bottom:10px">${message}</div>
      <a href="/api/config" target="_blank" style="display:inline-block;padding:10px 12px;border:1px solid #ffffff22;border-radius:10px;color:#fff;text-decoration:none">Lihat /api/config</a>
    </div>
  `;
}

// Normalisasi transform ImageKit agar tidak dobel '?'
function withTransform(url, tr = 'w-600,fo-auto'){
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
    return safe.replace(/[.,;:!\s]*$/,'') + '…';
  } catch {
    if (text.length <= max) return text;
    let t = text.slice(0, max);
    const lastSpace = t.lastIndexOf(' ');
    if (lastSpace > 40) t = t.slice(0, lastSpace);
    return t.replace(/[.,;:!\s]*$/,'') + '…';
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
async function initUI(){
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
  document.addEventListener('DOMContentLoaded', initUI, { once:true });
} else {
  initUI();
}

/* ---------------- Render List ---------------- */
function renderNeonList(groups) {
  const root = document.getElementById('list');
  root.innerHTML = '';

  (groups || []).forEach(g => {
    const id   = String(g.id);
    const name = String(g.name ?? id);
    const desc = String(g.desc ?? '').trim();
    const longDesc = String(g.long_desc ?? desc).trim();
    const img  = withTransform(String(g.image ?? '').trim());

    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.id = id;

    const check = document.createElement('div');
    check.className = 'check';
    check.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16"><path fill="#fff" d="M9,16.2 4.8,12 3.4,13.4 9,19 21,7 19.6,5.6"/></svg>`;

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

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleSelect(card);
    });

    card.addEventListener('click', (e) => {
      if (btn.contains(e.target)) return;
      openDetailModal({ id, name, desc: longDesc || desc, image: img });
    });

    meta.append(title, p, btn);
    card.append(check, thumb, meta);
    root.appendChild(card);

    updateButtonState(card, btn);
  });

  updateBadge();
}

/* ---------------- Interaksi ---------------- */
function toggleSelect(card){
  card.classList.toggle('selected');
  const btn = card.querySelector('button');
  if (btn) updateButtonState(card, btn);
  syncTotalText();
  updateBadge();
}

function updateButtonState(card, btn){
  const selected = card.classList.contains('selected');
  btn.textContent = selected ? 'Batal' : 'Pilih Grup';
  btn.classList.toggle('btn-solid', !selected);
  btn.classList.toggle('btn-ghost', selected);
  if (!btn.style.marginLeft) btn.style.marginLeft = 'auto';
}

function openDetailModal(item){
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
  const hero  = document.getElementById('hero');
  const img   = document.getElementById('detail-img');
  const ttl   = document.getElementById('ttl');
  const dsc   = document.getElementById('dsc');
  const btns  = document.getElementById('btns');

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
    else img.addEventListener('load', fitHero, { once:true });
    window.addEventListener('resize', fitHero, { passive:true });
  }

  m.querySelector('.close')?.addEventListener('click', () => closeDetailModal());
  m.querySelector('.add')?.addEventListener('click', () => { if (card) toggleSelect(card); closeDetailModal(); });
  m.addEventListener('click', (e) => { if (e.target === m) closeDetailModal(); }, { once:true });
}

function closeDetailModal(){
  const m = document.getElementById('detail');
  m.hidden = true;
  m.innerHTML = '';
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

function getSelectedIds(){
  return [...document.querySelectorAll('.card.selected')].map(el => el.dataset.id);
}

function updateBadge(){
  const n = getSelectedIds().length;
  const b = document.getElementById('cartBadge');
  if (n > 0) { b.hidden = false; b.textContent = String(n); }
  else b.hidden = true;
}

function formatRupiah(n){
  if (!Number.isFinite(n)) return "Rp 0";
  return "Rp " + n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function syncTotalText(){
  const t = getSelectedIds().length * PRICE_PER_GROUP;
  const payBtn = document.getElementById('pay');
  document.getElementById('total-text').textContent = formatRupiah(t);
  const disableBecauseNoUID = !window.__UID__;
  const disabled = t <= 0 || disableBecauseNoUID;
  payBtn?.toggleAttribute('disabled', disabled);
}

// ===== Countdown untuk "menunggu QR muncul" (fase 1) =====
let __qrCountdownTimer = null;

function startQrCountdown(maxSeconds = 180) {
  const msgEl = document.getElementById('qrMsg');
  const progEl = document.getElementById('qrProg');
  let left = Math.max(0, maxSeconds);

  const tick = () => {
    if (!msgEl) return stopQrCountdown();
    const mm = String(Math.floor(left / 60)).padStart(2, '0');
    const ss = String(left % 60).padStart(2, '0');
    msgEl.textContent = `Mohon tunggu sebentar (${mm}:${ss})`;
    if (progEl) {
      const pct = ((maxSeconds - left) / maxSeconds) * 100;
      progEl.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    }
    if (left <= 0) return stopQrCountdown();
    left -= 1;
  };

  stopQrCountdown();
  __qrCountdownTimer = setInterval(tick, 1000);
  tick();
}
function stopQrCountdown() {
  if (__qrCountdownTimer) { clearInterval(__qrCountdownTimer); __qrCountdownTimer = null; }
}

// ===== Countdown untuk "masa bayar" (fase 2) =====
let __qrPayTimer = null;

function startPayCountdown(maxSeconds = 300) { // 5 menit
  const msgEl = document.getElementById('qrMsg');
  const progEl = document.getElementById('qrProg');
  let left = Math.max(0, maxSeconds);

  const tick = () => {
    if (!msgEl) return stopPayCountdown();
    const mm = String(Math.floor(left / 60)).padStart(2, '0');
    const ss = String(left % 60).padStart(2, '0');
    msgEl.innerHTML = `Silahkan lakukan pembayaran dengan scan QRIS.<br>Waktu pelunasan pembayaran (${mm}:${ss})`;
    if (progEl) {
      const pct = ((maxSeconds - left) / maxSeconds) * 100;
      progEl.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    }
    if (left <= 0) {
      stopPayCountdown();
      showPaymentExpired();
      return;
    }
    left -= 1;
  };

  stopPayCountdown();
  __qrPayTimer = setInterval(tick, 1000);
  tick();
}
function stopPayCountdown() {
  if (__qrPayTimer) { clearInterval(__qrPayTimer); __qrPayTimer = null; }
}

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

async function onPay(){
  const selected = getSelectedIds();
  const amount = selected.length * PRICE_PER_GROUP;
  if (!selected.length) return;

  const userId = window.__UID__ || getUserId();
  if (!userId) {
    showQRModal(`<div style="color:#f55">Gagal membaca user Telegram. Buka lewat tombol bot.</div>`);
    return;
  }

  let inv;
  try{
    const res = await fetch(`${window.location.origin}/api/invoice`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ user_id:userId, groups:selected, amount })
    });
    if (!res.ok) throw new Error(await res.text());
    inv = await res.json();
  }catch(e){
    return showQRModal(`<div style="color:#f55">Create invoice gagal:<br><code>${escapeHtml(e.message||String(e))}</code></div>`);
  }

  const qrPngUrl = `${window.location.origin}/api/qr/${inv.invoice_id}.png?amount=${amount}&t=${Date.now()}`;
  const selectedItems = LOADED_GROUPS.filter(g => selected.includes(String(g.id)));
  const chipsHtml = selectedItems.map(g => `
    <span style="
      display:inline-block;margin:2px 6px 8px 0;
      padding:6px 10px;border-radius:999px;
      background:#1b1b1b;border:1px solid #ffffff22;
      font-weight:700;font-size:12px;color:#fff;
    ">${escapeHtml(g.name || String(g.id))}</span>
  `).join("");

  showQRModal(`
    <div style="text-align:center">
      <div style="font-weight:900;font-size:20px;margin-bottom:6px">
        Pembayaran
      </div>

      <!-- Daftar grup yang dipesan -->
      <div style="margin:4px 0 6px;opacity:.9;font-size:13px">Pesanan kamu:</div>
      <div style="margin:0 0 6px">${chipsHtml || '<span style="opacity:.7">-</span>'}</div>

      <div id="qrMsg" style="margin:6px 0 12px; opacity:.85">
        Mohon tunggu sebentar (maks 3 menit) …
      </div>

      <div style="height:6px;background:#222;border-radius:6px;overflow:hidden;margin:8px 0 14px">
        <div id="qrProg" style="height:100%;width:0%;background:#fff3;border-radius:6px"></div>
      </div>

      <img id="qrImg" alt="QR" src="${qrPngUrl}"
          style="max-width:100%;display:block;margin:0 auto;border-radius:10px;border:1px solid #ffffff1a">

      <button class="close" id="closeModal">Tutup</button>
    </div>
  `);
  document.getElementById('closeModal')?.addEventListener('click', hideQRModal);

  // Fase 1: tunggu QR tampil (3 menit)
  startQrCountdown(180);

  // Saat QR selesai diload → masuk fase 2 (5 menit)
  const qrImg = document.getElementById('qrImg');
  if (qrImg) {
    const onReady = () => {
      stopQrCountdown();
      startPayCountdown(300); // 5 menit
    };
    if (qrImg.complete) onReady();
    else qrImg.addEventListener('load', onReady, { once: true });
  }

  // Poll status pembayaran
  const statusUrl = `${window.location.origin}/api/invoice/${inv.invoice_id}/status`;
  const timer = setInterval(async () => {
    try {
      const r = await fetch(statusUrl);
      if (!r.ok) return;
      const s = await r.json();
      if (s.status === "PAID") {
        clearInterval(timer);
        hideQRModal();        // ini juga mematikan countdown
        tg?.close?.();
      }
    } catch (err) {
      // abaikan error polling
    }
  }, 2000);
}

function showQRModal(html){
  const m = document.getElementById('qr');
  m.innerHTML = `<div>${html}</div>`;
  m.hidden = false;
}

function hideQRModal(){
  stopQrCountdown();
  stopPayCountdown();
  const m = document.getElementById('qr');
  m.hidden = true;
  m.innerHTML = '';
}
