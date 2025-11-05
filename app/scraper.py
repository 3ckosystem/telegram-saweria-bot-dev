# app/scraper.py
# ------------------------------------------------------------
# Scraper Saweria:
#  - Isi form (amount, name/email random, message=INV:<invoice_id>)
#  - Pilih GoPay (tanpa submit) untuk bikin UI siap
#  - Klik "Kirim Dukungan"
#  - Ambil QR HD dari halaman/iframe checkout:
#       * jika <img> → unduh bytes-nya via context.request (share cookie)
#       * jika <canvas> / tak ada src → screenshot elemen
#       * jika elemen QR tak ketemu → screenshot panel/halaman
#
# ENV:
#   SAWERIA_USERNAME  (contoh: "payments")
# ------------------------------------------------------------

from __future__ import annotations
import os, re, uuid, base64, asyncio
from typing import Optional
from urllib.parse import urljoin
from playwright.async_api import async_playwright, Page, Frame, Error as PWError

SAWERIA_USERNAME = os.getenv("SAWERIA_USERNAME", "").strip()
PROFILE_URL = f"https://saweria.co/{SAWERIA_USERNAME}" if SAWERIA_USERNAME else None
INV_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

# Paksa event input/change supaya binding reaktif di halaman terpicu
FORCE_DISPATCH = True

# --- Reuse browser instance untuk menekan latency ---
_PLAY = None
_BROWSER = None


async def _get_browser():
    """Start playwright+browser sekali, reuse di panggilan berikutnya."""
    global _PLAY, _BROWSER
    if _PLAY is None:
        _PLAY = await async_playwright().start()
    if _BROWSER is None:
        _BROWSER = await _PLAY.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
    return _BROWSER


async def _new_context():
    browser = await _get_browser()
    return await browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        viewport={"width": 1366, "height": 960},
        device_scale_factor=2,
        locale="id-ID",
        timezone_id="Asia/Jakarta",
    )


# ---------- util umum ----------
async def _find_payment_root(node: Page | Frame):
    candidates = [
        '[data-testid*="donate" i]',
        '[data-testid*="payment" i]',
        '[class*="donate" i]',
        '[class*="payment" i]',
        'form',
        'section:has(button)',
        'div:has(button)',
    ]
    for sel in candidates:
        try:
            el = await node.wait_for_selector(sel, timeout=1800)
            return el
        except Exception:
            pass
    return None


async def _scan_all_frames_for_visual(page: Page):
    el = await _find_payment_root(page)
    if el:
        return el
    for fr in page.frames:
        try:
            url = (fr.url or "").lower()
        except Exception:
            url = ""
        if any(k in url for k in ["gopay", "qris", "payment", "pay", "xendit", "midtrans", "snap", "checkout", "iframe"]):
            print("[scraper] scanning frame:", url[:140])
        el = await _find_payment_root(fr)
        if el:
            return el
    return None


async def _maybe_dispatch(page: Page, handle):
    """Opsional: paksa event input/change bila FORCE_DISPATCH=True."""
    if not FORCE_DISPATCH or handle is None:
        return
    try:
        await page.evaluate(
            "(e)=>{"
            " if(!e) return;"
            " e.dispatchEvent(new Event('input',{bubbles:true}));"
            " e.dispatchEvent(new Event('change',{bubbles:true}));"
            " e.blur && e.blur();"
            "}", handle
        )
    except Exception:
        pass


# ---------- helper: pilih GoPay & tunggu Total > 0 ----------
async def _select_gopay_and_wait_total(page: Page, amount: int):
    """Klik GoPay dan tunggu 'Total' berubah > 0 (metode ter-apply)."""
    gopay_selectors = [
        '[data-testid="gopay-button"]',
        'button[data-testid="gopay-button"]',
        'button:has-text("GoPay")',
        '[role="radio"]:has-text("GoPay")',
        '[data-testid*="gopay"]',
    ]
    clicked = False
    for sel in gopay_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=2500)
            await el.scroll_into_view_if_needed()
            await el.click(force=True)
            print("[scraper] clicked GoPay via", sel)
            clicked = True
            break
        except Exception:
            pass

    if not clicked:
        print("[scraper] WARN: GoPay button not found")

    # pancing re-render ringan
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass
    await page.wait_for_timeout(200)

    # cek "Jumlah Dukungan: Rp{amount}"
    try:
        rupiah = f"{amount:,}".replace(",", ".")
        await page.get_by_text(re.compile(rf"Jumlah Dukungan:\s*Rp{rupiah}\b")).wait_for(timeout=4000)
        print("[scraper] amount reflected in UI")
    except Exception:
        print("[scraper] WARN: amount not reflected in 'Jumlah Dukungan'")

    # tunggu Total > 0
    try:
        await page.wait_for_function(
            """
            () => {
              const el = [...document.querySelectorAll('*')]
                .find(n => /Total:\s*Rp/i.test(n.textContent||''));
              if (!el) return false;
              const m = (el.textContent||'').match(/Total:\s*Rp\s*([\d.]+)/i);
              if (!m) return false;
              const num = parseInt(m[1].replace(/\./g,''));
              return Number.isFinite(num) && num > 0;
            }
            """,
            timeout=6000,
        )
        print("[scraper] Total > 0 (OK)")
    except Exception:
        print("[scraper] WARN: Total still 0 after selecting GoPay")


# ---------- builder pesan INV ----------
def _build_inv_message(invoice_id: str) -> str:
    """
    Bangun pesan kanonik untuk Saweria: INV:<invoice_id>.
    Tetap paksa format INV:... meskipun bukan UUID valid (untuk amannya).
    """
    if not invoice_id:
        return "INV:UNKNOWN"
    return f"INV:{invoice_id}"


# ---------- isi form TANPA submit ----------
async def _fill_without_submit(page: Page, amount: int, invoice_id: str, method: str):
    # ===== amount =====
    amount_ok = False
    amount_handle = None
    for sel in [
        'input[placeholder*="Ketik jumlah" i]',
        'input[aria-label*="Nominal" i]',
        'input[name="amount"]',
        'input[type="number"]',
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=3000)
            await el.scroll_into_view_if_needed()
            await el.click()
            # clear
            try:
                await page.keyboard.press("Control+A")
            except Exception:
                await page.keyboard.press("Meta+A")
            await page.keyboard.press("Backspace")
            # ketik
            await el.type(str(amount))
            amount_handle = el
            amount_ok = True
            print("[scraper] filled amount via", sel)
            break
        except Exception:
            pass
    if not amount_ok:
        print("[scraper] WARN: amount field not found")
    await _maybe_dispatch(page, amount_handle)
    await page.wait_for_timeout(200)

    # ===== name (Dari) =====
    name_ok = False
    for sel in [
        'input[name="name"]',
        'input[placeholder*="Dari" i]',
        'input[aria-label*="Dari" i]',
        'label:has-text("Dari") ~ input',
        'input[required][type="text"]',
        'input[type="text"]',
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=2000)
            await el.scroll_into_view_if_needed()
            await el.fill("Budi")
            await _maybe_dispatch(page, el)
            name_ok = True
            print("[scraper] filled name via", sel)
            break
        except Exception:
            pass
    if not name_ok:
        print("[scraper] WARN: name field not found")
    await page.wait_for_timeout(150)

    # ===== email =====
    email_val = f"donor+{uuid.uuid4().hex[:8]}@example.com"
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]']:
        try:
            el = await page.wait_for_selector(sel, timeout=2000)
            await el.scroll_into_view_if_needed()
            await el.fill(email_val)
            await _maybe_dispatch(page, el)
            print("[scraper] filled email via", sel)
            break
        except Exception:
            pass
    await page.wait_for_timeout(150)

    # ===== message (Pesan) — selalu INV:<invoice_id> =====
    message = _build_inv_message(invoice_id)
    msg_ok = False
    for sel in [
        'input[name="message"]',
        'input[data-testid="message-input"]',
        '#message',
        'input[placeholder*="Selamat pagi" i]',
        'input[placeholder*="pesan" i]',
        'textarea[name="message"]',
        'textarea',
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=1800)
            await el.scroll_into_view_if_needed()
            await el.fill(message)
            await _maybe_dispatch(page, el)
            msg_ok = True
            print("[scraper] filled message via", sel, "→", message)
            break
        except Exception:
            pass
    if not msg_ok:
        print("[scraper] WARN: message field not found at all")
    await page.wait_for_timeout(200)

    # ===== centang checkbox wajib (kalau ada) =====
    for text in ["17 tahun", "menyetujui", "kebijakan privasi", "ketentuan"]:
        try:
            node = page.get_by_text(re.compile(text, re.I))
            await node.scroll_into_view_if_needed()
            await node.click()
            print("[scraper] checked:", text)
        except Exception:
            pass
    await page.wait_for_timeout(150)

    # ===== pilih metode (GoPay) =====
    if (method or "gopay").lower() == "gopay":
        # scroll ke area metode (biar visible)
        try:
            area = await page.get_by_text(
                re.compile("Moda pembayaran|Metode pembayaran|GoPay|QRIS", re.I)
            ).element_handle()
            if area:
                await area.scroll_into_view_if_needed()
        except Exception:
            await page.mouse.wheel(0, 600)

        await _select_gopay_and_wait_total(page, amount)

    # selesai; TIDAK submit
    await page.wait_for_timeout(350)


# ====== Klik DONATE + ambil target checkout ======
async def _click_donate_and_get_checkout_page(page: Page, context):
    """
    Klik "Kirim Dukungan" dan kembalikan object 'target' berisi:
    - page   : Page (jika membuka tab baru / same-page nav)
    - frame  : Frame (jika pembayaran di dalam iframe)
    """
    donate_selectors = [
        'button[data-testid="donate-button"]',
        'button:has-text("Kirim Dukungan")',
        'text=/\\bKirim\\s+Dukungan\\b/i',
    ]

    # siapkan listener tab baru (kalau ada)
    new_page_task = context.wait_for_event("page")

    clicked = False
    for sel in donate_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=3000)
            await el.scroll_into_view_if_needed()
            await el.click()
            print("[scraper] clicked DONATE via", sel)
            clicked = True
            break
        except Exception:
            pass
    if not clicked:
        raise RuntimeError("Tombol 'Kirim Dukungan' tidak ditemukan")

    # 1) tab baru?
    target_page = None
    try:
        target_page = await new_page_task
    except Exception:
        pass
    if target_page:
        await target_page.wait_for_load_state("domcontentloaded")
        await target_page.wait_for_load_state("networkidle")
        print("[scraper] checkout opened in NEW TAB:", target_page.url)
        return {"page": target_page, "frame": None}

    # 2) same-page navigation?
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
        print("[scraper] checkout likely SAME PAGE:", page.url)
        return {"page": page, "frame": None}
    except Exception:
        pass

    # 3) iframe?
    for fr in page.frames:
        u = (fr.url or "").lower()
        if any(k in u for k in ["gopay", "qris", "xendit", "midtrans", "snap", "checkout", "pay"]):
            print("[scraper] checkout appears in IFRAME:", u[:120])
            return {"page": None, "frame": fr}

    print("[scraper] WARN: fallback to current page for checkout")
    return {"page": page, "frame": None}


async def _find_qr_or_checkout_panel(node: Page | Frame):
    """Cari elemen QR / panel checkout untuk discreenshot."""
    selectors = [
        # gambar/canvas QR umum
        'img.qr-image',
        'img.qr-image--with-wrapper',
        'img[alt*="qr-code" i]',
        'img[src*="/qr-code"]',
        '[data-testid="qrcode"] img',
        '[class*="qrcode" i] img',
        'img[alt*="QRIS" i]',
        "canvas",
        # panel pembayaran
        '[data-testid*="checkout" i]',
        '[class*="checkout" i]',
        'div:has-text("Cek status")',
        'div:has-text("Download QRIS")',
    ]
    for sel in selectors:
        try:
            el = await node.wait_for_selector(sel, timeout=5000)
            return el
        except Exception:
            pass
    return None


# ---------- entrypoint: QR HD ----------
async def fetch_gopay_qr_hd_png(invoice_id: str, amount: int) -> Optional[bytes]:
    """
    Kembalikan bytes PNG QR asli. Jika tidak berhasil menemukan <img.qr-image .../qr-code>,
    return None (JANGAN fallback ke screenshot form).
    """
    if not PROFILE_URL:
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        try:
            await page.goto(PROFILE_URL, wait_until="networkidle", timeout=45000)

            # Isi form: name/email/message=INV:<uuid>
            await page.fill('input[name="name"]', "EnSEXlopedia User")
            await page.fill('input[name="email"]', f"no-reply+{invoice_id[:8]}@example.com")
            await page.fill('textarea[name="message"]', f"INV:{invoice_id}")

            # Set nominal (pakai tombol preset atau input angka)
            # Preferensi: jika ada input amount
            if await page.locator('input[name="amount"]').count():
                await page.fill('input[name="amount"]', str(amount))
            else:
                # fallback klik tombol preset terdekat
                # (opsional; boleh dihapus kalau tak diperlukan)
                pass

            # Pilih GoPay/QRIS
            # Gunakan beberapa selector agar tahan perubahan DOM
            selectors = [
                'button:has-text("GoPay")',
                'button[aria-label*="GoPay"]',
                '.payment-methods button:nth-child(1)'
            ]
            clicked = False
            for sel in selectors:
                if await page.locator(sel).first.is_visible():
                    await page.locator(sel).first.click()
                    clicked = True
                    break
            if not clicked:
                return None  # tak bisa memilih GoPay

            # Tunggu img QR muncul
            await page.wait_for_timeout(500)  # beri napas
            img = page.locator('img.qr-image, img[alt="qr-code"]')
            await img.wait_for({ "state": "visible" }, timeout=15000)

            # Ambil src
            src = await img.get_attribute("src")
            if not src:
                return None

            # Validasi: pastikan ini benar-benar endpoint QR (umumnya mengandung "/qr-code")
            if "/qr-code" not in src and "qr" not in src.lower():
                return None

            # Ambil bytes PNG langsung via request konteks
            resp = await ctx.request.get(src)
            if not resp.ok:
                return None
            content_type = resp.headers.get("content-type", "")
            if "image/png" not in content_type:
                return None

            data = await resp.body()
            # sanity check kecil
            if data and len(data) > 5000:
                return data
            return None
        finally:
            await ctx.close()
            await browser.close()



# ---------- entrypoints tambahan (opsional / debugging) ----------
async def fetch_qr_png(*, invoice_id: str, amount: int, method: Optional[str] = "gopay") -> Optional[bytes]:
    """
    TANPA submit: isi form (message=INV:<invoice_id>) + pilih GoPay → screenshot panel/halaman (untuk debugging).
    """
    if not PROFILE_URL:
        print("[scraper] ERROR: SAWERIA_USERNAME belum di-set")
        return None

    context = await _new_context()
    page = await context.new_page()
    try:
        await page.goto(PROFILE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        await page.mouse.wheel(0, 480)

        await _fill_without_submit(page, amount, invoice_id, method or "gopay")
        await page.wait_for_timeout(700)

        target = page
        el = await _scan_all_frames_for_visual(target)
        if el:
            try:
                await el.scroll_into_view_if_needed()
                png = await el.screenshot()
                print("[scraper] captured filled panel PNG:", len(png))
            except Exception:
                png = await target.screenshot(full_page=False)
                print("[scraper] fallback target screenshot:", len(png))
        else:
            png = await target.screenshot(full_page=False)
            print("[scraper] WARN: no panel; page screenshot:", len(png))

        await context.close()
        return png

    except Exception as e:
        print("[scraper] error(fetch_qr_png):", e)
        try:
            snap = await page.screenshot(full_page=True)
            print("[scraper] debug page screenshot bytes:", len(snap))
        except Exception:
            pass
        await context.close()
        return None


async def fetch_gopay_checkout_png(*, invoice_id: str, amount: int) -> Optional[bytes]:
    """
    Klik 'Kirim Dukungan' dan screenshot panel checkout (jika butuh tampilan penuh).
    Pesan di field selalu INV:<invoice_id>.
    """
    if not PROFILE_URL:
        print("[scraper] ERROR: SAWERIA_USERNAME belum di-set")
        return None

    context = await _new_context()
    page = await context.new_page()
    try:
        await page.goto(PROFILE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        await page.mouse.wheel(0, 480)

        await _fill_without_submit(page, amount, invoice_id, "gopay")
        target = await _click_donate_and_get_checkout_page(page, context)
        node = target["frame"] if target["frame"] else (target["page"] or page)

        el = await _find_qr_or_checkout_panel(node)
        if el:
            await el.scroll_into_view_if_needed()
            png = await el.screenshot()
            print("[scraper] captured CHECKOUT panel PNG:", len(png))
        else:
            # fallback screenshot halaman
            if target["page"]:
                png = await target["page"].screenshot(full_page=True)
            else:
                png = await page.screenshot(full_page=True)
            print("[scraper] WARN: no specific QR element; page screenshot:", len(png))
        await context.close()
        return png

    except Exception as e:
        print("[scraper] error(fetch_gopay_checkout_png):", e)
        try:
            snap = await page.screenshot(full_page=True)
            print("[scraper] debug page screenshot bytes:", len(snap))
        except Exception:
            pass
        await context.close()
        return None


# ---------- debug helpers ----------
async def debug_snapshot() -> Optional[bytes]:
    if not PROFILE_URL:
        print("[debug_snapshot] ERROR: SAWERIA_USERNAME belum di-set")
        return None
    context = await _new_context()
    page = await context.new_page()
    await page.goto(PROFILE_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    await page.mouse.wheel(0, 600)
    png = await page.screenshot(full_page=True)
    await context.close()
    return png


async def debug_fill_snapshot(*, invoice_id: str, amount: int, method: str = "gopay") -> Optional[bytes]:
    if not PROFILE_URL:
        print("[debug_fill_snapshot] ERROR: SAWERIA_USERNAME belum di-set")
        return None
    context = await _new_context()
    page = await context.new_page()
    try:
        await page.goto(PROFILE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        await page.mouse.wheel(0, 480)

        await _fill_without_submit(page, amount, invoice_id, method or "gopay")
        await page.wait_for_timeout(700)

        png = await page.screenshot(full_page=True)
        print(f"[debug_fill_snapshot] bytes={len(png)}")
        await context.close()
        return png
    except Exception as e:
        print("[debug_fill_snapshot] error:", e)
        try:
            snap = await page.screenshot(full_page=True)
            await context.close()
            return snap
        except Exception:
            await context.close()
            return None
