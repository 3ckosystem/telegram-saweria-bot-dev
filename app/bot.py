# --- app/bot.py (membership gate + filtered buttons + title resolver) ---

from dotenv import load_dotenv
load_dotenv()

import os, json, time, asyncio, re
from typing import Any, Optional, List, Tuple, Dict

from telegram import (
    Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler
)
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut, NetworkError

# ===================== ENV & CONFIG BASE =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum di-set. Isi di .env / Railway Variables.")

BASE_URL = os.getenv("BASE_URL") or "http://127.0.0.1:8000"
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()

# GROUPS untuk pemetaan id->nama (dipakai saat kirim undangan)
GROUPS = json.loads(os.getenv("GROUP_IDS_JSON") or "[]")
GROUP_NAME_BY_ID: Dict[str, str] = {}
try:
    for g in GROUPS:
        gid = str(g.get("id") or "").strip()
        nm  = str(g.get("name") or g.get("label") or gid).strip()
        if gid:
            GROUP_NAME_BY_ID[gid] = nm
except Exception:
    pass

ALLOWED_STATUSES = {"member", "administrator", "creator"}

def build_app() -> Application:
    return Application.builder().token(BOT_TOKEN).build()

# ===================== DEBUG HELPERS =====================

async def gate_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gate_debug -> tampilkan ENV gate yang dibaca runtime."""
    envs = [
        "REQUIRED_GROUP_IDS", "REQUIRED_CHANNEL_IDS",
        "REQUIRED_GROUP_INVITES", "REQUIRED_CHANNEL_INVITES",
        "REQUIRED_MODE", "REQUIRED_MIN_COUNT",
        "REQUIRED_GROUP_USERNAMES", "REQUIRED_CHANNEL_USERNAMES"
    ]
    out = []
    for e in envs:
        val = os.getenv(e, "")
        if len(val) > 200:
            val = val[:200] + "..."
        out.append(f"{e} = {val or '(kosong)'}")
    await update.message.reply_text("🔎 Gate ENV Debug:\n" + "\n".join(out))

async def reset_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset_keyboard -> paksa hapus tombol reply keyboard."""
    await update.message.reply_text("Keyboard dihapus.", reply_markup=ReplyKeyboardRemove())

# ===================== UTIL: WEBAPP BUTTON =====================

def _webapp_url_for(uid: int) -> str:
    if WEBAPP_URL:
        sep = "&" if ("?" in WEBAPP_URL) else "?"
        return f"{WEBAPP_URL}{sep}uid={uid}&t={int(time.time())}"
    return f"{BASE_URL}/webapp/index.html?v=neon4&uid={uid}"

async def _send_webapp_button(chat_id: int, uid: int, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton(text="🛍️ Katalog Grup VIP", web_app=WebAppInfo(url=_webapp_url_for(uid)))]]
    await context.bot.send_message(
        chat_id=chat_id,
        text="Silahkan lanjutkan pemesanan dan pembayaran dengan klik tombol 🛍️ Katalog Grup VIP di bawah.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

# ===================== GATE: RUNTIME ENV LOADER =====================

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")  # username publik valid (tanpa @)

def _split_env(name: str) -> List[str]:
    v = os.getenv(name, "") or ""
    return [x.strip() for x in v.split(",") if x.strip()]

def _valid_usernames(items: List[str]) -> List[str]:
    return [x for x in items if _USERNAME_RE.fullmatch(x)]

def _load_gate_env():
    """Baca semua variabel gate SETIAP KALI dipanggil (runtime reload)."""
    group_ids   = _split_env("REQUIRED_GROUP_IDS")
    channel_ids = _split_env("REQUIRED_CHANNEL_IDS")
    group_links = _split_env("REQUIRED_GROUP_INVITES")
    chan_links  = _split_env("REQUIRED_CHANNEL_INVITES")
    group_users = _valid_usernames(_split_env("REQUIRED_GROUP_USERNAMES"))
    chan_users  = _valid_usernames(_split_env("REQUIRED_CHANNEL_USERNAMES"))
    mode        = (os.getenv("REQUIRED_MODE", "ALL") or "ALL").upper()
    try:
        min_count = int(os.getenv("REQUIRED_MIN_COUNT", "1"))
    except ValueError:
        min_count = 1

    return {
        "group_ids": group_ids,
        "channel_ids": channel_ids,
        "group_links": group_links,
        "chan_links": chan_links,
        "group_users": group_users,
        "chan_users": chan_users,
        "mode": mode,
        "min_count": min_count,
    }

# ===================== GATE: HELPERS =====================

async def _is_member(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: str) -> Optional[bool]:
    if not chat_id:
        return True
    try:
        cm = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return getattr(cm, "status", "") in ALLOWED_STATUSES
    except (Forbidden, BadRequest):
        return None
    except Exception:
        return None

# ---- title resolver (cache) ----
_NAME_CACHE: Dict[str, str] = {}

async def _resolve_titles(context: ContextTypes.DEFAULT_TYPE, cfg) -> Tuple[List[str], List[str]]:
    """
    Kembalikan (group_titles[], channel_titles[]) urutannya sejajar dengan id.
    Sumber nama:
      1) GROUP_NAME_BY_ID (dari katalog)
      2) get_chat(title) (di-cache)
      3) fallback: id/username
    """
    group_titles: List[str] = []
    channel_titles: List[str] = []

    async def _title_for(chat_id: str) -> str:
        key = str(chat_id)
        if key in GROUP_NAME_BY_ID:
            title = GROUP_NAME_BY_ID[key]
        elif key in _NAME_CACHE:
            title = _NAME_CACHE[key]
        else:
            try:
                chat = await context.bot.get_chat(chat_id=chat_id)
                title = getattr(chat, "title", None) or f"@{getattr(chat, 'username', '')}".strip("@") or key
            except Exception:
                title = key
            if len(title) > 32:
                title = title[:29] + "..."
            _NAME_CACHE[key] = title
        return title

    for gid in cfg["group_ids"]:
        group_titles.append(await _title_for(gid))
    for cid in cfg["channel_ids"]:
        channel_titles.append(await _title_for(cid))

    return group_titles, channel_titles

def _join_button(label: str, invite: Optional[str], username: Optional[str]) -> InlineKeyboardButton:
    if invite:
        return InlineKeyboardButton(label, url=invite)
    if username:
        return InlineKeyboardButton(label, url=f"https://t.me/{username}")
    return InlineKeyboardButton(f"{label} (minta admin set link)", callback_data="noop")

def _need_access_tips(cfg, any_cannot_check: bool) -> str:
    if not any_cannot_check:
        return ""
    tips = []
    if cfg["group_ids"]:
        tips.append("• Tambahkan bot ke semua GRUP wajib (minimal member).")
    if cfg["channel_ids"]:
        tips.append("• Jadikan bot ADMIN di semua CHANNEL wajib.")
    return "\n\nBot belum bisa memeriksa salah satu/lebih chat:\n" + "\n".join(tips)

async def _count_memberships(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, cfg
) -> Tuple[int, int, int, bool, List[Optional[bool]], List[Optional[bool]]]:
    """
    Return:
      ok_count, total_checkable, total_required, any_cannot_check, mem_groups, mem_channels
    mem_* berisi:
      True  -> terdeteksi member
      False -> terdeteksi bukan member
      None  -> tidak bisa diperiksa (bot belum punya akses)
    """
    total_required = len(cfg["group_ids"]) + len(cfg["channel_ids"])
    ok_count = 0
    total_checkable = 0
    any_cannot_check = False

    mem_groups: List[Optional[bool]] = []
    mem_channels: List[Optional[bool]] = []

    for chat_id in cfg["group_ids"]:
        res = await _is_member(context, user_id, chat_id)
        mem_groups.append(res)
        if res is None:
            any_cannot_check = True
        else:
            total_checkable += 1
            if res: ok_count += 1

    for chat_id in cfg["channel_ids"]:
        res = await _is_member(context, user_id, chat_id)
        mem_channels.append(res)
        if res is None:
            any_cannot_check = True
        else:
            total_checkable += 1
            if res: ok_count += 1

    return ok_count, total_checkable, total_required, any_cannot_check, mem_groups, mem_channels

def _gate_keyboard_filtered(
    cfg,
    mem_groups: List[Optional[bool]],
    mem_channels: List[Optional[bool]],
    group_titles: List[str],
    channel_titles: List[str],
) -> InlineKeyboardMarkup:
    """Render tombol hanya untuk chat yang belum join/subscribe."""
    rows: List[List[InlineKeyboardButton]] = []

    for i, st in enumerate(mem_groups):
        if st is True:
            continue
        inv = cfg["group_links"][i] if i < len(cfg["group_links"]) else ""
        usr = cfg["group_users"][i] if i < len(cfg["group_users"]) else ""
        name = group_titles[i] if i < len(group_titles) else "Group"
        base = f"Join {name}"
        label = base if (st is False) else f"{base} (bot perlu akses)"
        rows.append([_join_button(label, inv, usr)])

    for i, st in enumerate(mem_channels):
        if st is True:
            continue
        inv = cfg["chan_links"][i] if i < len(cfg["chan_links"]) else ""
        usr = cfg["chan_users"][i] if i < len(cfg["chan_users"]) else ""
        name = channel_titles[i] if i < len(channel_titles) else "Channel"
        base = f"Subscribe {name}"
        label = base if (st is False) else f"{base} (bot admin)"
        rows.append([_join_button(label, inv, usr)])

    rows.append([InlineKeyboardButton("✅ Saya sudah join (Re-check)", callback_data="recheck_membership")])
    return InlineKeyboardMarkup(rows)

def _is_pass(ok_count: int, total_required: int, cfg) -> bool:
    if total_required == 0:
        return True
    if cfg["mode"] == "ALL":
        return ok_count >= total_required
    min_need = max(1, cfg["min_count"])
    if min_need > total_required:
        min_need = total_required
    return ok_count >= min_need

# ===================== HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    cfg = _load_gate_env()

    # Jika tak ada syarat → langsung buka Mini App
    if not cfg["group_ids"] and not cfg["channel_ids"]:
        await _send_webapp_button(chat_id, uid, context)
        return

    ok_count, _, total_required, any_cannot_check, mem_groups, mem_channels = await _count_memberships(context, uid, cfg)
    passed = _is_pass(ok_count, total_required, cfg)

    if passed and not any_cannot_check:
        await _send_webapp_button(chat_id, uid, context)
        return

    # Belum lolos gate → sembunyikan keyboard "Buka Katalog" lama
    await context.bot.send_message(chat_id=chat_id, text="EnSEXlopedia Mini Apps BOT", reply_markup=ReplyKeyboardRemove())

    # Kirim instruksi + tombol Join/Subscribe + Re-check (inline) — hanya yang belum join
    group_titles, channel_titles = await _resolve_titles(context, cfg)

    lines = []
    if cfg["mode"] == "ALL":
        lines.append(f"Hi Kak, sebelum join ke VIP Kk diwajibkan join/subscribe **semua** ({total_required}) grup/channel berikut.")
    else:
        min_need = max(1, cfg["min_count"])
        if total_required and min_need > total_required: min_need = total_required
        lines.append(f"Hi Kak, sebelum join ke VIP Kk diwajibkan join/subscribe **minimal {min_need}** dari {total_required} grup/channel berikut.")
    lines.append(f"\nStatus terdeteksi: {ok_count}/{total_required} sudah join.")
    tips = _need_access_tips(cfg, any_cannot_check)
    text = "\n".join(lines) + (tips or "") + "\n\nSetelah join/subscribe, klik tombol Re-check di bawah."

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_gate_keyboard_filtered(cfg, mem_groups, mem_channels, group_titles, channel_titles)
    )

async def on_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    chat_id = query.message.chat_id
    cfg = _load_gate_env()

    ok_count, _, total_required, any_cannot_check, mem_groups, mem_channels = await _count_memberships(context, uid, cfg)
    passed = _is_pass(ok_count, total_required, cfg)

    if passed and not any_cannot_check:
        await query.edit_message_text("✅ Terima kasih! Kamu sudah lolos verifikasi.")
        await _send_webapp_button(chat_id, uid, context)
    else:
        # Pastikan keyboard lama hilang
        await context.bot.send_message(chat_id=chat_id, text="EnSEXlopedia Mini Apps BOT", reply_markup=ReplyKeyboardRemove())

        min_need_info = ""
        if cfg["mode"] == "ANY":
            min_need_info = f"(minimal {max(1, min(cfg['min_count'], total_required))}) "
        tips = _need_access_tips(cfg, any_cannot_check)

        await query.edit_message_text(
            f"Belum memenuhi syarat {min_need_info}: {ok_count}/{total_required} terdeteksi join.{tips}\n\nSilakan lengkapi lalu Re-check lagi."
        )

        group_titles, channel_titles = await _resolve_titles(context, cfg)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Klik tombol di bawah untuk join/re-check:",
            reply_markup=_gate_keyboard_filtered(cfg, mem_groups, mem_channels, group_titles, channel_titles)
        )

# ===================== INVITE LINK (sesuai versi stabil) =====================

async def _to_int_or_str(v: Any):
    try:
        return int(str(v))
    except Exception:
        return str(v)

async def _create_link_with_retry(bot, chat_id, **kwargs):
    delays = [0, 0.7, 1.2]
    last_err: Optional[Exception] = None
    for d in delays:
        if d:
            await asyncio.sleep(d)
        try:
            return await bot.create_chat_invite_link(chat_id=chat_id, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", 1.5))
            last_err = e
        except (TimedOut, NetworkError) as e:
            last_err = e
        except (Forbidden, BadRequest) as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
    if last_err:
        print("[invite] create_chat_invite_link failed:", last_err)
    return None

async def send_invite_link(app: Application, user_id: int, target_group_id):
    """Kirim 1 undangan untuk 1 grup (dipanggil dari main.py)."""
    group_id_norm = await _to_int_or_str(target_group_id)
    group_id_str  = str(target_group_id)
    group_name    = GROUP_NAME_BY_ID.get(group_id_str, group_id_str)

    expire_ts = int(time.time()) + 15 * 60
    link_obj = await _create_link_with_retry(
        app.bot,
        chat_id=group_id_norm,
        member_limit=1,
        expire_date=expire_ts,
        creates_join_request=False,
        name="Paid join",
    )

    invite_link_url: Optional[str] = None
    if link_obj and getattr(link_obj, "invite_link", None):
        invite_link_url = link_obj.invite_link
    else:
        try:
            invite_link_url = await app.bot.export_chat_invite_link(chat_id=group_id_norm)
        except Exception as e:
            print(f"[invite] export_chat_invite_link failed for {group_id_str}:", e)

    if not invite_link_url:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Gagal membuat undangan untuk grup: {group_name}\n"
                     f"Pastikan bot adalah admin/diizinkan membuat link di grup tsb."
            )
        except Exception as e:
            print("[invite] notify user failed:", e)
        return

    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=f"✅ Pembayaran diterima.\nUndangan untuk {group_name}:\n{invite_link_url}"
        )
    except Exception as e:
        print("[invite] send DM failed:", e)

# ===================== REGISTER HANDLERS =====================

def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gate_debug", gate_debug))
    app.add_handler(CommandHandler("reset_keyboard", reset_keyboard))  # opsional
    app.add_handler(CallbackQueryHandler(on_recheck, pattern="^recheck_membership$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
