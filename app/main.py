# app/main.py
import os, json, re, base64, hmac, hashlib, httpx
import asyncio
import random
from typing import Optional, List

from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Query
app = FastAPI()
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from telegram import Update, Bot
from telegram.ext import Application
from telegram.error import Forbidden, BadRequest

from .bot import build_app, register_handlers, send_invite_link
from . import payments, storage
from copy import deepcopy

# === penting: import fungsi scraper (signature baru: invoice_id & amount)
from .scraper import (
    debug_snapshot,
    debug_fill_snapshot,
    fetch_gopay_checkout_png,
    fetch_gopay_qr_hd_png,
)

# ------------- ENV -------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
bot_check = Bot(BOT_TOKEN)
BASE_URL = os.environ["BASE_URL"].strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ENV = os.getenv("ENV", "dev")  # "prod" di Railway untuk mematikan debug endpoints

# was:
# IMAGEKIT_PUBLIC_KEY = os.getenv("IMAGEKIT_PUBLIC_KEY", "").strip()
IMAGEKIT_PRIVATE_KEY = os.getenv("IMAGEKIT_PRIVATE_KEY", "").strip()
IMAGEKIT_BASE_URL   = (os.getenv("IMAGEKIT_BASE_URL", "").rstrip("/"))
IMAGEKIT_CACHE_TTL = int(os.getenv("IMAGEKIT_CACHE_TTL", "900"))
IMAGEKIT_IMG_WIDTH = int(os.getenv("IMAGEKIT_IMG_WIDTH", "600"))
IMAGEKIT_PER_REQUEST_TIMEOUT = float(os.getenv("IMAGEKIT_PER_REQUEST_TIMEOUT", "6"))
# cache sederhana di memori: { "/M": {"exp": ts, "items": [urls...] } }
_IMAGEKIT_CACHE: dict[str, dict] = {}

def _split_env(name: str) -> List[str]:
    v = os.getenv(name, "") or ""
    return [x.strip() for x in v.split(",") if x.strip()]

ALLOWED_STATUSES = {"member", "administrator", "creator"}

async def _is_member_server(user_id: int, chat_id: str) -> int:
    """return 1 joined, 0 not joined, -1 cannot check (no access)"""
    if not chat_id:
        return 1
    try:
        cm = await bot_check.get_chat_member(chat_id=chat_id, user_id=user_id)
        return 1 if getattr(cm, "status", "") in ALLOWED_STATUSES else 0
    except (Forbidden, BadRequest):
        return -1
    except Exception:
        return -1

@app.get("/api/gate/status")
async def gate_status(uid: int = Query(..., description="Telegram user_id")):
    group_ids   = _split_env("REQUIRED_GROUP_IDS")
    channel_ids = _split_env("REQUIRED_CHANNEL_IDS")
    mode        = (os.getenv("REQUIRED_MODE", "ALL") or "ALL").upper()
    try:
        min_count = int(os.getenv("REQUIRED_MIN_COUNT", "1"))
    except:
        min_count = 1

    total_required = len(group_ids) + len(channel_ids)
    if total_required == 0:
        return {"passed": True, "ok_count": 0, "total_required": 0}

    ok_count = 0
    any_cannot = False

    for cid in group_ids + channel_ids:
        res = await _is_member_server(uid, cid)
        if res == 1:
            ok_count += 1
        elif res == -1:
            any_cannot = True

    # evaluasi pass
    if mode == "ALL":
        passed = (ok_count >= total_required) and not any_cannot
    else:
        need = min(min_count, total_required) if total_required else 0
        passed = (ok_count >= need) and not any_cannot

    if not passed:
        # kirim juga link tombol agar WebApp bisa render halaman blokir
        detail = {
            "passed": False,
            "ok_count": ok_count,
            "total_required": total_required,
            "mode": mode,
            "min_count": min_count,
            "group_invites": _split_env("REQUIRED_GROUP_INVITES"),
            "channel_invites": _split_env("REQUIRED_CHANNEL_INVITES"),
        }
        raise HTTPException(status_code=403, detail=detail)

    return {"passed": True, "ok_count": ok_count, "total_required": total_required}


# Robust reader utk GROUP_IDS_JSON & PRICE_IDR
def _read_env_json(name: str, default_text: str = "[]"):
    raw = os.environ.get(name, default_text)
    if raw is None:
        return []
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        # fallback jika ada single quotes
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:
            return []

def _parse_groups_from_any(data):
    groups = []
    if isinstance(data, dict):
        for k, v in data.items():
            # dukung bentuk {"-100..": "Nama"} atau {"-100..": {"name":..., "desc":..., "image":...}}
            if isinstance(v, dict):
                gid  = str(k).strip()
                nm   = str(v.get("name") or v.get("label") or v.get("text") or gid).strip()
                init = str(v.get("initial") or "").strip()
                desc = str(v.get("desc") or v.get("description") or "").strip()
                image = str(v.get("image") or v.get("img") or "").strip()
                # NEW: folder per grup (boleh gunakan salah satu key di bawah)
                img_folder = str(
                    v.get("image_folder") or v.get("img_folder") or v.get("folder") or v.get("imgDir") or ""
                ).strip()
            else:
                gid, nm, init, desc, image = str(k).strip(), str(v).strip(), "", "", ""
                img_folder = ""
            if gid and nm:
                groups.append({
                    "id": gid, "name": nm, "initial": init, "desc": desc, "image": image,
                    "image_folder": img_folder
                })

    elif isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                gid  = str(it.get("id") or it.get("group_id") or it.get("value") or "").strip()
                nm   = str(it.get("name") or it.get("label")    or it.get("text")  or gid).strip()
                init = str(it.get("initial") or "").strip()
                desc = str(it.get("desc") or it.get("description") or "").strip()
                image = str(it.get("image") or it.get("img") or "").strip()
                # NEW: folder per grup (alias key)
                img_folder = str(
                    it.get("image_folder") or it.get("img_folder") or it.get("folder") or it.get("imgDir") or ""
                ).strip()
                if gid and nm:
                    groups.append({
                        "id": gid, "name": nm, "initial": init, "desc": desc, "image": image,
                        "image_folder": img_folder
                    })
            else:
                gid = str(it).strip()
                if gid:
                    groups.append({"id": gid, "name": gid, "initial": "", "desc": "", "image": "", "image_folder": ""})
    return groups


# BACA ENV SEKARANG (module scope)
GROUPS_DATA = _read_env_json("GROUP_IDS_JSON", "[]")
GROUPS = _parse_groups_from_any(GROUPS_DATA)

try:
    PRICE_IDR = int(os.environ.get("PRICE_IDR", "25000"))
except Exception:
    PRICE_IDR = 25000


# --- Helper ambil gambar random dari folder ImageKit ---

def _norm_folder_to_path(folder: str) -> str:
    """Terima path '/M' atau URL penuh '.../M/' → balikan path '/M'."""
    if not folder:
        return ""
    s = folder.strip()
    if s.startswith("http://") or s.startswith("https://"):
        if IMAGEKIT_BASE_URL and s.startswith(IMAGEKIT_BASE_URL):
            s = s[len(IMAGEKIT_BASE_URL):]
        # buang query/fragment
        s = s.split("?", 1)[0].split("#", 1)[0]
    if not s.startswith("/"):
        s = "/" + s
    # pastikan tanpa trailing slash agar konsisten di API path query
    # (ImageKit menerima '/M' atau '/M/', tapi kita konsistenkan)
    if len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s

async def _imagekit_list_files_by_path(path: str) -> List[str]:
    """Ambil daftar URL file gambar dari folder pakai API (dengan cache)."""
    if not IMAGEKIT_PRIVATE_KEY:
        return []

    # CACHE HIT
    now = asyncio.get_event_loop().time()
    ent = _IMAGEKIT_CACHE.get(path)
    if ent and ent.get("exp", 0) > now and ent.get("items"):
        return ent["items"]

    # FETCH (limit 100 sudah cukup untuk randomizer)
    url = "https://api.imagekit.io/v1/files"
    params = {"path": path, "limit": 100}
    try:
        async with httpx.AsyncClient(timeout=IMAGEKIT_PER_REQUEST_TIMEOUT) as client:
            r = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": "Basic " + base64.b64encode(f"{IMAGEKIT_PRIVATE_KEY}:".encode()).decode()
                },
            )
        r.raise_for_status()
        data = r.json()
        items = [f["url"] for f in data if f.get("fileType") == "image" and f.get("url")]
        # set cache
        _IMAGEKIT_CACHE[path] = {"exp": now + IMAGEKIT_CACHE_TTL, "items": items}
        return items
    except Exception as e:
        print("[ImageKit] list files error:", e)
        # fallback: gunakan cache lama jika masih ada meskipun expired
        if ent and ent.get("items"):
            return ent["items"]
        return []



async def _scrape_folder_for_images(url: str) -> List[str]:
    """
    Fallback: GET folder URL (HTML indexing) lalu regex semua *.jpg/png/webp.
    Return: list URL absolut.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        html = resp.text
        names = re.findall(r'([\w\-\./%]+?\.(?:jpg|jpeg|png|webp))', html, flags=re.I)
        out = []
        for n in names:
            if n.startswith("http"):
                out.append(n)
            else:
                base = url.rstrip("/") + "/"
                out.append(base + n.lstrip("/"))
        # deduplicate
        seen, uniq = set(), []
        for u in out:
            if u not in seen:
                uniq.append(u); seen.add(u)
        return uniq
    except Exception as e:
        print("[ImageScrape] error:", e)
        return []

async def _pick_random_image_from_folder(folder: str) -> Optional[str]:
    """Pilih 1 URL gambar secara acak dari folder ImageKit (gunakan transform)."""
    path = _norm_folder_to_path(folder)
    if not path:
        return None
    files = await _imagekit_list_files_by_path(path)
    if not files:
        return None
    import random
    url = random.choice(files)
    # tambahkan transform ringan agar cepat (ignorant query aman di ImageKit)
    # contoh: https://.../file.jpg?tr=w-600,fo-auto
    return f"{url}?tr=w-{IMAGEKIT_IMG_WIDTH},fo-auto"


# ------------- APP & BOT -------------
# app = FastAPI()
storage.init_db()

bot_app: Application = build_app()
register_handlers(bot_app)

# >>> helper kirim undangan (idempotent-ish)
async def _send_invites_for_invoice(inv: dict) -> None:
    try:
        groups = json.loads(inv.get("groups_json") or "[]")
    except Exception:
        groups = []
    if not groups:
        return

    logs = storage.list_invite_logs(inv["invoice_id"])
    already = { str(l.get("group_id")) for l in logs if l.get("group_id") }

    for gid in groups:
        gid_str = str(gid)
        if gid_str in already:
            continue

        # normalisasi id telegram
        try:
            gid_norm = int(gid_str)
        except Exception:
            gid_norm = gid_str

        # kirim link
        try:
            await send_invite_link(bot_app, inv["user_id"], gid_norm)
            try:
                storage.add_invite_log(inv["invoice_id"], gid_str, "(sent)", None)
            except Exception as e:
                print("[invite-log] failed to insert success log:", e)
        except Exception as e:
            # catat error tanpa menghentikan loop grup lainnya
            try:
                storage.add_invite_log(inv["invoice_id"], gid_str, None, str(e))
            except Exception as e2:
                print("[invite-log] failed to insert error log:", e2)

        await asyncio.sleep(0.7)  # throttle ringan



# Serve Mini App statics
app.mount("/webapp", StaticFiles(directory="app/webapp", html=True), name="webapp")
app.mount("/static", StaticFiles(directory="app/webapp"), name="static")

# ------------- TELEGRAM WEBHOOK -------------
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    # optional secret validation
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return JSONResponse({"ok": True})


# ------------- API: CREATE INVOICE -------------
class CreateInvoiceIn(BaseModel):
    user_id: int
    groups: List[str]
    amount: int

@app.post("/api/invoice")
async def create_invoice(payload: CreateInvoiceIn):
    # --- DEBUG LOG (bisa hapus setelah stabil)
    import logging
    logging.info(f"[create_invoice] uid={payload.user_id} groups={payload.groups} amount={payload.amount}")

    # --- VALIDASI amount (minimal>0; boleh set MIN_PRICE_IDR di env)
    try:
        MIN_PRICE_IDR = int(os.environ.get("MIN_PRICE_IDR", "1"))
    except Exception:
        MIN_PRICE_IDR = 1
    if not isinstance(payload.amount, int) or payload.amount < MIN_PRICE_IDR:
        raise HTTPException(400, f"Invalid amount. Min {MIN_PRICE_IDR}")

    # --- VALIDASI groups dari ENV (id harus match)
    try:
        allowed = {str(g["id"]) for g in GROUPS}
    except Exception:
        allowed = set()
    for gid in payload.groups:
        if str(gid) not in allowed:
            raise HTTPException(400, f"Invalid group {gid}.")

    # --- CALL payments.create_invoice
    try:
        inv = await payments.create_invoice(payload.user_id, payload.groups, payload.amount)
        return inv
    except Exception as e:
        import traceback, logging
        logging.error("create_invoice failed: %s", e)
        logging.error(traceback.format_exc())
        raise HTTPException(400, f"Create invoice error: {e}")


# ------------- API: CONFIG -------------
@app.get("/api/config")
async def get_config():
    try:
        result_groups = deepcopy(GROUPS)  # jangan ubah global
        # buat task paralel untuk set image dari folder
        async def enrich(g: dict):
            folder = str(g.get("image_folder") or "").strip()
            if folder:
                img = await _pick_random_image_from_folder(folder)
                if img:
                    g["image"] = img

        await asyncio.gather(*(enrich(g) for g in result_groups))
        return {"price_idr": PRICE_IDR, "groups": result_groups}
    except Exception as e:
        print("[config] random image error:", e)
        return {"price_idr": PRICE_IDR, "groups": deepcopy(GROUPS)}




# ------------- API: STATUS & QR IMAGE -------------
_DATA_URL_RE = re.compile(r"^data:(image/[^;]+);base64,(.+)$")

@app.get("/api/invoice/{invoice_id}/status")
async def invoice_status(invoice_id: str):
    st = payments.get_status(invoice_id)
    if not st:
        raise HTTPException(404, "Invoice not found")

    # Fallback auto-kirim undangan saat status sudah PAID
    try:
        if (st.get("status") or "").upper() == "PAID":
            logs = storage.list_invite_logs(invoice_id)
            if not logs:
                inv = payments.get_invoice(invoice_id)  # berisi user_id & groups_json
                if inv:
                    await _send_invites_for_invoice(inv)
    except Exception as e:
        print("[invoice_status] auto-send invites failed:", e)

    return st


# --- QR endpoint (disederhanakan; message dipaksa INV:<invoice_id> di scraper) ---
@app.get("/api/qr/{raw_id}")
async def qr_png(
    raw_id: str,
    amount: int | None = Query(None, description="Amount; jika None, ambil dari invoice"),
    wait: int = Query(0, description="Wait seconds for background cache (max 8)"),
    hd: bool = Query(True, description="(ignored; QR selalu HD bila tersedia)"),
):
    # 1) Normalisasi ID: izinkan .../{invoice_id}.png atau .jpg
    invoice_id = re.sub(r"\.(png|jpg|jpeg)$", "", raw_id, flags=re.I)

    # 2) Ambil invoice dari DB
    inv = payments.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")

    # 3) Amount
    amt = inv.get("amount") or amount
    if not isinstance(amt, int) or amt <= 0:
        raise HTTPException(400, "Invalid amount")

    # 4) Jika sudah ada payload di DB → langsung kirim
    payload = inv.get("qris_payload")
    if payload:
        m = _DATA_URL_RE.match(payload)
        if not m:
            raise HTTPException(400, "Bad image payload")
        mime, b64 = m.groups()
        return Response(
            content=base64.b64decode(b64),
            media_type=mime,
            headers={"Cache-Control": "public, max-age=300"},
        )

    # 5) Tunggu sebentar background (opsional)
    if wait and isinstance(wait, int) and wait > 0:
        for _ in range(min(wait, 8)):
            await asyncio.sleep(1)
            inv2 = payments.get_invoice(invoice_id)
            payload2 = inv2.get("qris_payload") if inv2 else None
            if payload2:
                m = _DATA_URL_RE.match(payload2)
                if not m:
                    break
                mime, b64 = m.groups()
                return Response(
                    content=base64.b64decode(b64),
                    media_type=mime,
                    headers={"Cache-Control": "public, max-age=300"},
                )

    # 6) Generate on-demand (HD) + cache ke DB
    try:
        png = await fetch_gopay_qr_hd_png(invoice_id=invoice_id, amount=amt)
        if not png:
            return Response(content=b"QR not found", status_code=502)

        try:
            b64 = base64.b64encode(png).decode()
            storage.update_qris_payload(invoice_id, f"data:image/png;base64,{b64}")
        except Exception:
            pass

        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )
    except Exception as e:
        print("[qr_png] error:", e)
        return Response(content=b"Error", status_code=500)


# ------------- SAWERIA WEBHOOK -------------
class SaweriaWebhookIn(BaseModel):
    status: str
    invoice_id: Optional[str] = None
    external_id: Optional[str] = None
    message: Optional[str] = None

SAWERIA_WEBHOOK_SECRET = os.getenv("SAWERIA_WEBHOOK_SECRET", "")

def _verify_saweria_signature(req: Request, raw_body: bytes) -> bool:
    if not SAWERIA_WEBHOOK_SECRET:
        return True
    sig_hdr = req.headers.get("X-Saweria-Signature")
    if not sig_hdr:
        return False
    calc = hmac.new(SAWERIA_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, sig_hdr)

INV_RE = re.compile(r"(?:^|\b)INV[:\s]*([0-9a-fA-F-]{36})\b")

@app.post("/api/saweria/webhook")
async def saweria_webhook(request: Request):
    raw = await request.body()

    # 1) Optional HMAC verify (X-Saweria-Signature)
    if not _verify_saweria_signature(request, raw):
        raise HTTPException(403, "Bad signature")

    # 2) Parse JSON
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # 3) Support 2 styles of payload:
    #    A) Minimal: {"status":"paid","invoice_id":"..."}
    #    B) Saweria donation payload: {"type":"donation", "message":"INV:<uuid>", ...}
    invoice_id = None
    is_paid = False

    # Style A
    status = str(data.get("status", "")).lower()
    if status == "paid":
        is_paid = True
        invoice_id = data.get("invoice_id") or data.get("external_id")

    # Style B (Saweria)
    if not is_paid and str(data.get("type", "")).lower() == "donation":
        is_paid = True
        msg = str(data.get("message", ""))
        m = INV_RE.search(msg)
        if m:
            invoice_id = m.group(1)

    if not is_paid:
        return {"ok": True, "ignored": True}

    if not invoice_id:
        raise HTTPException(400, "Cannot resolve invoice_id from payload")

    # 4) Tandai PAID dan kirim undangan
    inv = payments.mark_paid(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")

    try:
        groups = json.loads(inv.get("groups_json") or "[]")
    except Exception:
        groups = []

    for gid in groups:
        try:
            await send_invite_link(bot_app, inv["user_id"], gid)
            storage.add_invite_log(inv["invoice_id"], gid, "(sent-via-webhook)", None)
        except Exception as e:
            storage.add_invite_log(inv["invoice_id"], gid, None, str(e))

    return {"ok": True}


# >>> endpoint manual trigger kirim undangan (debug)
@app.post("/api/invoice/{invoice_id}/send-invites")
async def manual_send_invites(invoice_id: str, secret: Optional[str] = Query(None)):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    inv = payments.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    await _send_invites_for_invoice(inv)
    return {"ok": True, "invoice_id": invoice_id, "logs": storage.list_invite_logs(invoice_id)}


# ------------- HEALTH / DEBUG -------------
@app.get("/health")
def health():
    return {"ok": True}

if ENV != "prod":
    @app.get("/debug/invoices")
    def debug_invoices(limit: int = 20):
        return {"items": payments.list_invoices(limit)}

    @app.get("/debug/invite-logs/{invoice_id}")
    def debug_invite_logs(invoice_id: str):
        return {"invoice_id": invoice_id, "logs": storage.list_invite_logs(invoice_id)}

# ---- DEBUG: tes HTTP fetch langsung (tanpa Chromium) ----
@app.get("/debug/fetch-saweria")
async def debug_fetch_saweria():
    username = os.getenv("SAWERIA_USERNAME", "").strip()
    if not username:
        raise HTTPException(400, "SAWERIA_USERNAME belum di-set")
    url = f"https://saweria.co/{username}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        })
    return {"url": url, "status": r.status_code, "len": len(r.text), "snippet": r.text[:300]}

# ---- DEBUG: ambil PNG dari Chromium (Playwright) ----
@app.get("/debug/saweria-snap")
async def debug_saweria_snap():
    png = await debug_snapshot()
    if not png:
        raise HTTPException(500, "Gagal snapshot (lihat logs)")
    return Response(content=png, media_type="image/png")

@app.get("/debug/saweria-fill")
async def debug_saweria_fill(invoice_id: str, amount: int = 25000, method: str = "gopay"):
    png = await debug_fill_snapshot(invoice_id=invoice_id, amount=amount, method=method)
    if not png:
        raise HTTPException(500, "Gagal snapshot setelah pengisian form (lihat logs)")
    return Response(content=png, media_type="image/png")

@app.get("/debug/saweria-pay")
async def debug_saweria_pay(invoice_id: str, amount: int = 25000):
    png = await fetch_gopay_checkout_png(invoice_id=invoice_id, amount=amount)
    if not png:
        raise HTTPException(500, "Gagal menuju halaman pembayaran")
    return Response(content=png, media_type="image/png")

@app.get("/debug/saweria-qr-hd")
async def debug_saweria_qr_hd(invoice_id: str, amount: int = 25000):
    png = await fetch_gopay_qr_hd_png(invoice_id=invoice_id, amount=amount)
    if not png:
        raise HTTPException(500, "Gagal ambil QR HD")
    return Response(content=png, media_type="image/png")

# ------------- STARTUP / SHUTDOWN -------------
@app.on_event("startup")
async def on_start():
    await bot_app.initialize()
    if BASE_URL.startswith("https://"):
        await bot_app.bot.set_webhook(
            url=f"{BASE_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET or None,
        )
    else:
        print("Skipping set_webhook: BASE_URL must start with https://")

    # --- prewarm ImageKit folder cache (agar first load cepat) ---
    try:
        folders = []
        for g in GROUPS:
            fld = str(g.get("image_folder") or "").strip()
            if fld:
                p = _norm_folder_to_path(fld)
                if p not in folders:
                    folders.append(p)
        if folders and IMAGEKIT_PRIVATE_KEY:
            await asyncio.gather(*(_imagekit_list_files_by_path(p) for p in folders))
            print(f"[startup] Prefetched ImageKit folders: {len(folders)}")
    except Exception as e:
        print("[startup] prewarm image folders failed:", e)

    await bot_app.start()


@app.on_event("shutdown")
async def on_stop():
    await bot_app.stop()
    await bot_app.shutdown()
