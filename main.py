import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

# Create event loop before importing Pyrogram for Python 3.11+ compatibility
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiohttp import web
from jinja2 import Environment, FileSystemLoader
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION =================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_RAW = os.getenv("ADMIN", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_RAW.split(",") if x.strip().isdigit()]

PORT = int(os.getenv("PORT", "8080"))
BASE_URL = (
    os.getenv("BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or f"http://localhost:{PORT}"
).rstrip("/")

WORK_DIR = Path("/tmp/unzip_bot_work")
DOWNLOAD_DIR = WORK_DIR / "downloads"
EXTRACT_DIR = WORK_DIR / "extracted"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

SUPPORTED_EXTS = (
    ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz",
    ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".gz", ".bz2", ".xz", ".cab", ".iso"
)

# State Dictionaries
PENDING_PASSWORDS: dict[int, dict] = {}
ADMIN_INPUT_STATE: dict[int, str] = {}   # {admin_id: "set_fsub" | "set_log" | "set_limit" | "ban_user" | "unban_user"}
USER_INPUT_STATE: dict[int, str] = {}    # {user_id: "set_prefix"}
ACTIVE_DL_ALL: set[int] = set()          # Track users currently receiving "Download All"

jinja_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent)), autoescape=True)

app = Client(
    "unzip_cloud_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


# ================= HELPER FUNCTIONS =================
def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def is_supported_archive(filename: str) -> bool:
    name = filename.lower()
    return any(name.endswith(ext) for ext in SUPPORTED_EXTS)


async def send_log(client: Client, text: str):
    try:
        settings = await db.get_bot_settings()
        log_ch = settings.get("log_channel")
        if log_ch:
            await client.send_message(log_ch, text, disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"Failed to send log message: {e}")


async def check_fsub(client: Client, user_id: int) -> tuple[bool, str | None]:
    """Returns (is_joined: bool, invite_link: str | None)"""
    if user_id in ADMIN_IDS:
        return True, None

    settings = await db.get_bot_settings()
    if not settings.get("fsub_enabled") or not settings.get("fsub_channel"):
        return True, None

    ch = settings["fsub_channel"]
    invite = settings.get("fsub_invite_link")

    try:
        member = await client.get_chat_member(ch, user_id)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return True, None
    except UserNotParticipant:
        pass
    except Exception as e:
        logger.warning(f"FSub check error on {ch}: {e}")
        return True, None

    if not invite:
        try:
            chat = await client.get_chat(ch)
            invite = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
            if not invite:
                invite = await client.export_chat_invite_link(ch)
            await db.update_bot_settings(fsub_invite_link=invite)
        except Exception:
            if isinstance(ch, str) and ch.startswith("@"):
                invite = f"https://t.me/{ch.lstrip('@')}"
            else:
                invite = "https://t.me/"

    return False, invite


async def enforce_access(client: Client, message_or_cb) -> bool:
    """Checks ban status, maintenance mode, and Force Subscribe."""
    u = message_or_cb.from_user
    is_cb = isinstance(message_or_cb, CallbackQuery)
    msg = message_or_cb.message if is_cb else message_or_cb

    try:
        is_new = await db.add_user(u.id, u.first_name or "", u.username or "")
        if is_new:
            uname = f"@{u.username}" if u.username else "N/A"
            await send_log(
                client,
                f"🆕 **New User Joined**\n\n"
                f"👤 **Name:** {u.first_name}\n"
                f"🆔 **ID:** `{u.id}`\n"
                f"🔗 **Username:** {uname}",
            )
        user = await db.get_user(u.id)
        settings = await db.get_bot_settings()
    except Exception as e:
        logger.error(f"DB access error: {e}")
        return True

    if user.get("is_banned") and u.id not in ADMIN_IDS:
        text = "🚫 **Access Denied!**\nYou have been banned from using this bot."
        if is_cb:
            await message_or_cb.answer("You are banned!", show_alert=True)
        else:
            await msg.reply_text(text)
        return False

    if settings.get("maintenance") and u.id not in ADMIN_IDS and not user.get("is_premium"):
        text = "🛠️ **Maintenance Mode Active**\nThe bot is currently undergoing maintenance. Only Premium users and Admins can use it right now."
        if is_cb:
            await message_or_cb.answer("Bot is under maintenance!", show_alert=True)
        else:
            await msg.reply_text(text)
        return False

    joined, invite_url = await check_fsub(client, u.id)
    if not joined:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=invite_url)],
            [InlineKeyboardButton("🔄 Verify Membership", callback_data="verify_fsub")],
        ])
        fsub_text = (
            "🔒 **Membership Required!**\n\n"
            "You must join our official updates channel to use this bot.\n"
            "Click the button below to join, then tap **Verify Membership**."
        )
        if is_cb:
            await message_or_cb.answer("Please join our channel first!", show_alert=True)
            await msg.reply_text(fsub_text, reply_markup=kb)
        else:
            await msg.reply_text(fsub_text, reply_markup=kb)
        return False

    return True


def extract_archive_sync(archive_path: Path, out_dir: Path, password: str = "") -> tuple[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["7z", "x", "-y", f"-p{password}", f"-o{str(out_dir)}", str(archive_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout + "\n" + proc.stderr).lower()

    if proc.returncode == 0:
        return "ok", ""

    if any(k in output for k in ("wrong password", "can not open encrypted", "encrypted", "enter password")):
        shutil.rmtree(out_dir, ignore_errors=True)
        return ("wrong_password" if password else "need_password"), "Invalid or missing password."

    if archive_path.suffix.lower() == ".rar":
        out_dir.mkdir(parents=True, exist_ok=True)
        unrar_cmd = ["unrar-free", "-x"]
        if password:
            unrar_cmd.append(f"-p{password}")
        unrar_cmd.extend([str(archive_path), str(out_dir)])
        p2 = subprocess.run(unrar_cmd, capture_output=True, text=True)
        if p2.returncode == 0:
            return "ok", ""

    shutil.rmtree(out_dir, ignore_errors=True)
    return "error", (proc.stderr or proc.stdout or "Extraction failed.")[:300]


def collect_extracted_files(out_dir: Path) -> list[dict]:
    files = []
    idx = 0
    for root, _, filenames in os.walk(out_dir):
        for fname in sorted(filenames):
            if fname == ".meta.json" or fname.startswith(".bundle_"):
                continue
            fpath = Path(root) / fname
            if fpath.is_file():
                rel = fpath.relative_to(out_dir).as_posix()
                files.append({
                    "id": idx,
                    "name": fname,
                    "rel_path": rel,
                    "size": fpath.stat().st_size,
                    "size_str": human_size(fpath.stat().st_size),
                    "ext": fpath.suffix.lower().lstrip(".") or "file",
                })
                idx += 1
    return files


def save_job_metadata(token: str, owner_id: int, archive_name: str, files: list[dict]):
    meta_path = EXTRACT_DIR / token / ".meta.json"
    meta_path.write_text(
        json.dumps({
            "owner_id": owner_id,
            "archive_name": archive_name,
            "created_at": int(time.time()),
            "total_files": len(files),
            "total_size": human_size(sum(f["size"] for f in files)),
            "files": files,
        }),
        encoding="utf-8",
    )


def load_job_metadata(token: str) -> dict | None:
    meta_path = EXTRACT_DIR / token / ".meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main_extraction_keyboard(token: str, total_files: int) -> InlineKeyboardMarkup:
    cloud_url = f"{BASE_URL}/files/{token}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📥 Download All ({total_files})", callback_data=f"dlall:{token}"),
            InlineKeyboardButton("☁️ Cloud Download", url=cloud_url),
        ],
        [
            InlineKeyboardButton("📋 View & Pick Individual Files", callback_data=f"flist:{token}:0"),
        ],
    ])


def file_list_keyboard(token: str, files: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    per_page = 8
    total_pages = max(1, (len(files) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = files[start : start + per_page]

    rows = []
    for f in chunk:
        label = f"📄 {f['name'][:28]} ({f['size_str']})"
        rows.append([InlineKeyboardButton(label, callback_data=f"sfile:{token}:{f['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"flist:{token}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📑 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"flist:{token}:{page + 1}"))
    rows.append(nav)

    rows.append([
        InlineKeyboardButton("📥 Download All", callback_data=f"dlall:{token}"),
        InlineKeyboardButton("☁️ Cloud Page", url=f"{BASE_URL}/files/{token}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back to Summary", callback_data=f"backsum:{token}")])
    return InlineKeyboardMarkup(rows)


async def send_single_extracted_file(client: Client, chat_id: int, user_doc: dict, fpath: Path, fmeta: dict):
    prefix = (user_doc.get("custom_prefix") or "").strip()
    upload_as_doc = user_doc.get("upload_as_doc", True)

    target_path = fpath
    display_name = f"{prefix}_{fmeta['name']}" if prefix else fmeta["name"]
    caption = f"📄 **File:** `{display_name}`\n💾 **Size:** `{fmeta['size_str']}`"
    ext = fmeta["ext"].lower()

    while True:
        try:
            if not upload_as_doc and ext in ("mp4", "mkv", "mov", "webm"):
                await client.send_video(chat_id, video=str(target_path), caption=caption, file_name=display_name)
            elif not upload_as_doc and ext in ("mp3", "m4a", "flac", "wav", "ogg"):
                await client.send_audio(chat_id, audio=str(target_path), caption=caption, file_name=display_name)
            elif not upload_as_doc and ext in ("jpg", "jpeg", "png", "webp"):
                await client.send_photo(chat_id, photo=str(target_path), caption=caption)
            else:
                await client.send_document(chat_id, document=str(target_path), caption=caption, file_name=display_name)
            break
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)


async def finalize_extraction(
    client: Client,
    message: Message,
    status_msg: Message,
    archive_path: Path,
    file_name: str,
    file_size: int,
    out_dir: Path,
    token: str,
):
    files = collect_extracted_files(out_dir)

    if archive_path.exists():
        archive_path.unlink(missing_ok=True)

    if not files:
        shutil.rmtree(out_dir, ignore_errors=True)
        await status_msg.edit_text("❌ **Extraction Failed:** The archive appears to be empty.")
        return

    save_job_metadata(token, message.from_user.id, file_name, files)
    try:
        await db.add_usage(message.from_user.id, file_size)
    except Exception as e:
        logger.error(f"DB add_usage error: {e}")

    total_size = human_size(sum(f["size"] for f in files))
    cloud_url = f"{BASE_URL}/files/{token}"

    preview_lines = "\n".join(
        [f"• `{f['name'][:38]}` ({f['size_str']})" for f in files[:6]]
    )
    if len(files) > 6:
        preview_lines += f"\n*...and {len(files) - 6} more files*"

    await status_msg.edit_text(
        f"✅ **Archive Extracted Successfully!**\n\n"
        f"📦 **Archive:** `{file_name}`\n"
        f"📂 **Total Files:** `{len(files)}`\n"
        f"💾 **Total Extracted Size:** `{total_size}`\n\n"
        f"📋 **Files Preview:**\n{preview_lines}\n\n"
        f"👇 **Choose how you want to download your files:**\n"
        f"• Tap **📥 Download All** to get all files right here in Telegram.\n"
        f"• Tap **☁️ Cloud Download** for high-speed browser downloads.\n"
        f"• Tap **📋 View & Pick** to download specific files.",
        reply_markup=main_extraction_keyboard(token, len(files)),
        disable_web_page_preview=True,
    )

    await send_log(
        client,
        f"📦 **Archive Extracted**\n\n"
        f"👤 **User:** {message.from_user.first_name} (`{message.from_user.id}`)\n"
        f"📄 **Archive:** `{file_name}` (`{human_size(file_size)}`)\n"
        f"📂 **Extracted Files:** `{len(files)}` (`{total_size}`)\n"
        f"☁️ **Cloud Link:** [Open Page]({cloud_url})",
    )


async def cleanup_old_jobs():
    while True:
        await asyncio.sleep(900)
        now = time.time()
        for folder in EXTRACT_DIR.iterdir():
            if folder.is_dir():
                try:
                    if now - folder.stat().st_mtime > 7200:
                        shutil.rmtree(folder, ignore_errors=True)
                except Exception:
                    pass


# ================= ADMIN PANEL UI BUILDER =================
async def build_admin_panel() -> tuple[str, InlineKeyboardMarkup]:
    s = await db.get_stats()
    cfg = await db.get_bot_settings()

    fsub_status = "✅ Enabled" if cfg.get("fsub_enabled") else "❌ Disabled"
    fsub_ch = cfg.get("fsub_channel") or "Not Set"
    log_ch = cfg.get("log_channel") or "Not Set"
    limit_mb = cfg.get("daily_limit_mb", 20)
    maint = "🛠️ ON" if cfg.get("maintenance") else "🟢 OFF"

    text = (
        "⚙️ **Admin Control Panel**\n\n"
        f"👥 **Total Users:** `{s['total']}` | 💎 **Premium:** `{s['premium']}` | 🚫 **Banned:** `{s['banned']}`\n"
        f"📦 **Total Extractions:** `{s['extractions']}`\n\n"
        f"📢 **Force Subscribe:** `{fsub_status}`\n"
        f"🔗 **FSub Channel:** `{fsub_ch}`\n"
        f"📜 **Log Channel:** `{log_ch}`\n"
        f"📊 **Daily Free Limit:** `{limit_mb} MB / Day`\n"
        f"🚦 **Maintenance Mode:** `{maint}`"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"📢 FSub: {'ON' if cfg.get('fsub_enabled') else 'OFF'}",
                callback_data="adm:toggle_fsub",
            ),
            InlineKeyboardButton("✏️ Set FSub Channel", callback_data="adm:set_fsub"),
        ],
        [
            InlineKeyboardButton("🗑️ Clear FSub", callback_data="adm:clear_fsub"),
            InlineKeyboardButton("📜 Set Log Channel", callback_data="adm:set_log"),
        ],
        [
            InlineKeyboardButton(f"📦 Daily Limit ({limit_mb} MB)", callback_data="adm:set_limit"),
            InlineKeyboardButton(f"🛠️ Maint: {'ON' if cfg.get('maintenance') else 'OFF'}", callback_data="adm:toggle_maint"),
        ],
        [
            InlineKeyboardButton("🚫 Ban User", callback_data="adm:ban_user"),
            InlineKeyboardButton("✅ Unban User", callback_data="adm:unban_user"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Panel", callback_data="adm:refresh"),
        ],
    ])
    return text, kb


# ================= USER SETTINGS UI BUILDER =================
async def build_user_settings(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    mode = "📄 Document" if user.get("upload_as_doc", True) else "🎬 Streamable Media"
    prefix = user.get("custom_prefix") or "None"

    text = (
        "⚙️ **Your Personal Settings**\n\n"
        f"📤 **Telegram Upload Mode:** `{mode}`\n"
        f"🏷️ **Custom Filename Prefix:** `{prefix}`\n\n"
        "Customize how extracted files are sent to you on Telegram:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📤 Upload Mode: {mode}", callback_data="usr:toggle_mode")],
        [
            InlineKeyboardButton("🏷️ Set Prefix", callback_data="usr:set_prefix"),
            InlineKeyboardButton("🗑️ Clear Prefix", callback_data="usr:clear_prefix"),
        ],
    ])
    return text, kb


# ================= TELEGRAM COMMAND HANDLERS =================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    if not await enforce_access(client, message):
        return

    u = message.from_user
    user = await db.get_user(u.id)
    cfg = await db.get_bot_settings()
    limit_bytes = int(cfg.get("daily_limit_mb", 20)) * 1024 * 1024

    is_prem = user.get("is_premium") or (u.id in ADMIN_IDS)
    plan = "💎 PREMIUM (Unlimited)" if is_prem else f"🆓 FREE ({cfg.get('daily_limit_mb', 20)} MB / Day)"
    used = human_size(user.get("daily_used", 0))
    rem = "Unlimited" if is_prem else human_size(max(0, limit_bytes - user.get("daily_used", 0)))

    text = (
        f"👋 **Hello, {u.first_name}!**\n\n"
        f"⚡ **Welcome to Ultra Unzip & Cloud Downloader Bot**\n"
        f"Send me any archive file (even password-protected ones) and I will extract it instantly with **Direct Telegram Download All** and a **High-Speed Cloud Download Page**!\n\n"
        f"📦 **Supported Formats:**\n"
        f"`.zip` `.rar` `.7z` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` `.gz` `.iso`\n\n"
        f"📊 **Account Plan:** `{plan}`\n"
        f"📈 **Used Today:** `{used}` | **Remaining:** `{rem}`"
    )

    rows = [
        [
            InlineKeyboardButton("📊 My Plan & Quota", callback_data="my_quota"),
            InlineKeyboardButton("⚙️ My Settings", callback_data="open_usettings"),
        ],
        [
            InlineKeyboardButton("💎 Upgrade to Premium", callback_data="get_premium"),
            InlineKeyboardButton("❓ Help & Formats", callback_data="formats"),
        ],
    ]
    if u.id in ADMIN_IDS:
        rows.append([InlineKeyboardButton("🛠️ Admin Control Panel", callback_data="adm:refresh")])

    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command(["admin", "settings"]) & filters.private)
async def admin_panel_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply_text("❌ **Access Denied:** Admin only command.")
        return
    text, kb = await build_admin_panel()
    await message.reply_text(text, reply_markup=kb)


@app.on_message(filters.command("usettings") & filters.private)
async def user_settings_cmd(client: Client, message: Message):
    if not await enforce_access(client, message):
        return
    text, kb = await build_user_settings(message.from_user.id)
    await message.reply_text(text, reply_markup=kb)


@app.on_message(filters.command("myplan") & filters.private)
async def myplan_cmd(client: Client, message: Message):
    if not await enforce_access(client, message):
        return
    u = message.from_user
    user = await db.get_user(u.id)
    cfg = await db.get_bot_settings()
    limit_bytes = int(cfg.get("daily_limit_mb", 20)) * 1024 * 1024
    is_prem = user.get("is_premium") or (u.id in ADMIN_IDS)
    exp = user["premium_until"].strftime("%Y-%m-%d %H:%M UTC") if user.get("premium_until") else "Lifetime / N/A"
    used = human_size(user.get("daily_used", 0))
    rem = "Unlimited" if is_prem else human_size(max(0, limit_bytes - user.get("daily_used", 0)))

    await message.reply_text(
        f"📊 **Your Subscription & Quota**\n\n"
        f"👤 **User ID:** `{u.id}`\n"
        f"🏷️ **Plan:** `{'💎 PREMIUM' if is_prem else '🆓 FREE'}`\n"
        f"⏳ **Premium Expiry:** `{exp}`\n"
        f"📈 **Used Today:** `{used}`\n"
        f"🔋 **Remaining Today:** `{rem}`\n"
        f"📦 **Total Archives Extracted:** `{user.get('total_extractions', 0)}`"
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client: Client, message: Message):
    uid = message.from_user.id
    cancelled = False

    if uid in PENDING_PASSWORDS:
        info = PENDING_PASSWORDS.pop(uid)
        if info["archive_path"].exists():
            info["archive_path"].unlink(missing_ok=True)
        cancelled = True
    if uid in ADMIN_INPUT_STATE:
        ADMIN_INPUT_STATE.pop(uid, None)
        cancelled = True
    if uid in USER_INPUT_STATE:
        USER_INPUT_STATE.pop(uid, None)
        cancelled = True
    if uid in ACTIVE_DL_ALL:
        ACTIVE_DL_ALL.discard(uid)
        cancelled = True

    if cancelled:
        await message.reply_text("✅ **Active operation has been cancelled.**")
    else:
        await message.reply_text("ℹ️ You don't have any active operation running.")


# ================= DOCUMENT HANDLER =================
@app.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    if not await enforce_access(client, message):
        return

    u = message.from_user
    doc = message.document
    file_name = doc.file_name or "archive.zip"
    file_size = doc.file_size or 0

    if not is_supported_archive(file_name):
        await message.reply_text(
            "❌ **Unsupported File Format!**\n\n"
            "Please send a valid archive file:\n"
            "`.zip`, `.rar`, `.7z`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.gz`, `.iso`"
        )
        return

    allowed, remaining, limit_bytes = await db.check_quota(u.id, file_size, ADMIN_IDS)
    if not allowed:
        await message.reply_text(
            f"🚫 **Daily Limit Reached ({human_size(limit_bytes)} / Day)!**\n\n"
            f"📦 **Archive Size:** `{human_size(file_size)}`\n"
            f"🔋 **Remaining Quota Today:** `{human_size(remaining)}`\n\n"
            f"Your quota resets automatically at midnight (IST), or upgrade to **💎 Premium** for unlimited extractions!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Upgrade to Premium", callback_data="get_premium")]
            ]),
        )
        return

    status_msg = await message.reply_text(
        f"⬇️ **Downloading Archive...**\n\n"
        f"📦 **File:** `{file_name}`\n"
        f"💾 **Size:** `{human_size(file_size)}`"
    )

    token = secrets.token_urlsafe(10)
    archive_path = DOWNLOAD_DIR / f"{token}_{file_name}"
    out_dir = EXTRACT_DIR / token

    try:
        await message.download(file_name=str(archive_path))
    except Exception as e:
        await status_msg.edit_text(f"❌ **Download Failed:** `{e}`")
        return

    await status_msg.edit_text("⚙️ **Extracting archive, please wait...**")

    status, err = await asyncio.to_thread(extract_archive_sync, archive_path, out_dir, "")

    if status == "ok":
        await finalize_extraction(client, message, status_msg, archive_path, file_name, file_size, out_dir, token)
    elif status == "need_password":
        PENDING_PASSWORDS[u.id] = {
            "archive_path": archive_path,
            "file_name": file_name,
            "file_size": file_size,
            "token": token,
            "out_dir": out_dir,
        }
        await status_msg.edit_text(
            f"🔐 **Password-Protected Archive Detected!**\n\n"
            f"📦 **Archive:** `{file_name}`\n"
            f"🔑 Please reply with the **password** to unlock and extract this file (or send `/cancel` to abort)."
        )
    else:
        archive_path.unlink(missing_ok=True)
        await status_msg.edit_text(f"❌ **Extraction Error:**\n`{err}`")


# ================= CALLBACK QUERY HANDLER =================
@app.on_callback_query()
async def cb_handler(client: Client, cb: CallbackQuery):
    u = cb.from_user
    data = cb.data or ""

    if data == "noop":
        await cb.answer()
        return

    if data == "verify_fsub":
        joined, _ = await check_fsub(client, u.id)
        if joined:
            await cb.answer("✅ Membership verified! You can now use the bot.", show_alert=True)
            await cb.message.delete()
        else:
            await cb.answer("❌ You haven't joined the channel yet!", show_alert=True)
        return

    # Admin Panel Callbacks
    if data.startswith("adm:"):
        if u.id not in ADMIN_IDS:
            await cb.answer("Admin only!", show_alert=True)
            return

        action = data.split(":", 1)[1]
        cfg = await db.get_bot_settings()

        if action == "toggle_fsub":
            if not cfg.get("fsub_channel") and not cfg.get("fsub_enabled"):
                await cb.answer("Please set an FSub channel first!", show_alert=True)
                return
            new_val = not cfg.get("fsub_enabled", False)
            await db.update_bot_settings(fsub_enabled=new_val)
            await cb.answer(f"Force Subscribe {'Enabled' if new_val else 'Disabled'}!")
        elif action == "toggle_maint":
            new_val = not cfg.get("maintenance", False)
            await db.update_bot_settings(maintenance=new_val)
            await cb.answer(f"Maintenance Mode {'Enabled' if new_val else 'Disabled'}!")
        elif action == "clear_fsub":
            await db.update_bot_settings(fsub_enabled=False, fsub_channel=None, fsub_invite_link=None)
            await cb.answer("FSub channel cleared!")
        elif action == "set_fsub":
            ADMIN_INPUT_STATE[u.id] = "set_fsub"
            await cb.message.reply_text(
                "📢 **Set Force Subscribe Channel**\n\n"
                "Send the channel `@username` (e.g. `@mychannel`) or numeric ID (e.g. `-1001234567890`).\n"
                "⚠️ *Make sure this bot is an Admin in that channel!*\n\n"
                "Send `/cancel` to abort."
            )
            await cb.answer()
            return
        elif action == "set_log":
            ADMIN_INPUT_STATE[u.id] = "set_log"
            await cb.message.reply_text(
                "📜 **Set Log Channel**\n\n"
                "Send the Log Channel numeric ID (e.g. `-1001234567890`) or `0` to disable logging.\n"
                "⚠️ *Make sure the bot is an Admin in the log channel!*\n\n"
                "Send `/cancel` to abort."
            )
            await cb.answer()
            return
        elif action == "set_limit":
            ADMIN_INPUT_STATE[u.id] = "set_limit"
            await cb.message.reply_text(
                "📦 **Set Daily Free Quota (in MB)**\n\n"
                "Send the new daily limit in MB (for example: `20`, `50`, `100`).\n"
                "Send `/cancel` to abort."
            )
            await cb.answer()
            return
        elif action == "ban_user":
            ADMIN_INPUT_STATE[u.id] = "ban_user"
            await cb.message.reply_text("🚫 Send the numeric **User ID** to ban (or `/cancel`):")
            await cb.answer()
            return
        elif action == "unban_user":
            ADMIN_INPUT_STATE[u.id] = "unban_user"
            await cb.message.reply_text("✅ Send the numeric **User ID** to unban (or `/cancel`):")
            await cb.answer()
            return

        text, kb = await build_admin_panel()
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        await cb.answer()
        return

    # Check access for regular user callbacks
    if not await enforce_access(client, cb):
        return

    # User Settings Callbacks
    if data == "open_usettings":
        text, kb = await build_user_settings(u.id)
        await cb.message.reply_text(text, reply_markup=kb)
        await cb.answer()
        return

    if data.startswith("usr:"):
        act = data.split(":", 1)[1]
        user = await db.get_user(u.id)
        if act == "toggle_mode":
            new_mode = not user.get("upload_as_doc", True)
            await db.update_user_pref(u.id, upload_as_doc=new_mode)
            await cb.answer("Upload mode updated!")
        elif act == "clear_prefix":
            await db.update_user_pref(u.id, custom_prefix="")
            await cb.answer("Custom prefix cleared!")
        elif act == "set_prefix":
            USER_INPUT_STATE[u.id] = "set_prefix"
            await cb.message.reply_text(
                "🏷️ **Set Custom Filename Prefix**\n\n"
                "Send the text prefix you want added before every file name (e.g. `@MyChannel`), or `/cancel`."
            )
            await cb.answer()
            return

        text, kb = await build_user_settings(u.id)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        return

    # General Menu Callbacks
    if data == "my_quota":
        user = await db.get_user(u.id)
        cfg = await db.get_bot_settings()
        limit_bytes = int(cfg.get("daily_limit_mb", 20)) * 1024 * 1024
        is_prem = user.get("is_premium") or (u.id in ADMIN_IDS)
        used = human_size(user.get("daily_used", 0))
        rem = "Unlimited" if is_prem else human_size(max(0, limit_bytes - user.get("daily_used", 0)))
        await cb.answer(f"Used Today: {used} | Remaining: {rem}", show_alert=True)
        return

    if data == "get_premium":
        await cb.message.reply_text(
            "💎 **Upgrade to Premium Membership**\n\n"
            "✅ **Unlimited Daily Unzip Size**\n"
            "✅ **Priority High-Speed Extraction**\n"
            "✅ **One-Click Download All & Cloud ZIP Bundles**\n"
            "✅ **Access During Maintenance Mode**\n\n"
            "📩 Contact the administrator to activate Premium on your account!"
        )
        await cb.answer()
        return

    if data == "formats":
        await cb.answer(
            "Supported Formats: ZIP, RAR, 7Z, TAR, TAR.GZ, TGZ, TAR.BZ2, TAR.XZ, GZ, BZ2, XZ, CAB, ISO",
            show_alert=True,
        )
        return

    # Extraction Action Callbacks: Download All / File List / Single File
    if data.startswith(("dlall:", "flist:", "sfile:", "backsum:")):
        parts = data.split(":")
        action = parts[0]
        token = parts[1]

        meta = load_job_metadata(token)
        if not meta:
            await cb.answer("❌ This extraction session has expired (2h limit).", show_alert=True)
            return

        out_dir = EXTRACT_DIR / token
        files = meta["files"]

        if action == "backsum":
            preview_lines = "\n".join(
                [f"• `{f['name'][:38]}` ({f['size_str']})" for f in files[:6]]
            )
            if len(files) > 6:
                preview_lines += f"\n*...and {len(files) - 6} more files*"

            await cb.message.edit_text(
                f"✅ **Archive Extracted Successfully!**\n\n"
                f"📦 **Archive:** `{meta['archive_name']}`\n"
                f"📂 **Total Files:** `{meta['total_files']}`\n"
                f"💾 **Total Extracted Size:** `{meta['total_size']}`\n\n"
                f"📋 **Files Preview:**\n{preview_lines}\n\n"
                f"👇 **Choose how you want to download your files:**",
                reply_markup=main_extraction_keyboard(token, len(files)),
            )
            await cb.answer()
            return

        if action == "flist":
            page = int(parts[2]) if len(parts) > 2 else 0
            await cb.message.edit_text(
                f"📋 **Extracted Files — `{meta['archive_name']}`**\n"
                f"Tap any file below to receive it directly on Telegram:",
                reply_markup=file_list_keyboard(token, files, page),
            )
            await cb.answer()
            return

        if action == "sfile":
            file_id = int(parts[2])
            target_meta = next((x for x in files if x["id"] == file_id), None)
            if not target_meta:
                await cb.answer("File not found!", show_alert=True)
                return
            fpath = out_dir / target_meta["rel_path"]
            if not fpath.is_file():
                await cb.answer("File expired!", show_alert=True)
                return
            await cb.answer(f"📤 Sending {target_meta['name']}...")
            user_doc = await db.get_user(u.id)
            await send_single_extracted_file(client, u.id, user_doc, fpath, target_meta)
            return

        if action == "dlall":
            if u.id in ACTIVE_DL_ALL:
                await cb.answer("⏳ A 'Download All' job is already running for you!", show_alert=True)
                return

            await cb.answer("📥 Starting batch delivery of all files...")
            ACTIVE_DL_ALL.add(u.id)
            user_doc = await db.get_user(u.id)
            prog = await cb.message.reply_text(
                f"📥 **Sending All Extracted Files (0/{len(files)})...**\n"
                f"Send `/cancel` anytime to stop."
            )

            sent = 0
            try:
                for idx, f in enumerate(files, start=1):
                    if u.id not in ACTIVE_DL_ALL:
                        await prog.edit_text(f"🛑 **Download All stopped by user ({sent}/{len(files)} sent).**")
                        return
                    fpath = out_dir / f["rel_path"]
                    if not fpath.is_file():
                        continue
                    try:
                        await send_single_extracted_file(client, u.id, user_doc, fpath, f)
                        sent += 1
                    except Exception as e:
                        logger.error(f"Error sending file {f['name']}: {e}")

                    if idx % 4 == 0 or idx == len(files):
                        try:
                            await prog.edit_text(
                                f"📥 **Sending Extracted Files ({sent}/{len(files)})...**\n"
                                f"Send `/cancel` anytime to stop."
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(0.7)

                await prog.edit_text(
                    f"✅ **All Done! Sent `{sent}/{len(files)}` files to your chat.**\n"
                    f"☁️ You can also access them via the Cloud Download button anytime within 2 hours.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("☁️ Open Cloud Download Page", url=f"{BASE_URL}/files/{token}")]
                    ]),
                )
            finally:
                ACTIVE_DL_ALL.discard(u.id)


# ================= TEXT INPUT HANDLER (PASSWORDS + ADMIN/USER PROMPTS) =================
@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(["start", "admin", "settings", "usettings", "myplan", "cancel", "stats", "broadcast", "addpremium", "removepremium"])
)
async def handle_text_inputs(client: Client, message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    # 1. Admin interactive prompts
    if uid in ADMIN_INPUT_STATE and uid in ADMIN_IDS:
        state = ADMIN_INPUT_STATE.pop(uid)

        if state == "set_fsub":
            ch_val: int | str = int(text) if text.lstrip("-").isdigit() else text
            try:
                chat = await client.get_chat(ch_val)
                invite = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
                if not invite:
                    invite = await client.export_chat_invite_link(ch_val)
                await db.update_bot_settings(
                    fsub_channel=chat.id,
                    fsub_invite_link=invite,
                    fsub_enabled=True,
                )
                await message.reply_text(
                    f"✅ **Force Subscribe Configured & Enabled!**\n\n"
                    f"📢 **Channel:** {chat.title} (`{chat.id}`)\n"
                    f"🔗 **Invite Link:** {invite}"
                )
            except Exception as e:
                await message.reply_text(
                    f"❌ **Failed to verify channel `{text}`:** `{e}`\n\n"
                    f"Make sure the bot is added as an **Admin** in the channel with 'Invite Users via Link' permission."
                )
            return

        if state == "set_log":
            if text == "0":
                await db.update_bot_settings(log_channel=None)
                await message.reply_text("✅ **Log channel disabled.**")
                return
            if not text.lstrip("-").isdigit():
                await message.reply_text("❌ Please provide a valid numeric channel ID (e.g. `-1001234567890`).")
                return
            ch_id = int(text)
            try:
                msg = await client.send_message(ch_id, "✅ **Log Channel Connected Successfully!**")
                await db.update_bot_settings(log_channel=ch_id)
                await message.reply_text(f"✅ **Log Channel Set to `{ch_id}`!**")
            except Exception as e:
                await message.reply_text(f"❌ **Could not send message to `{ch_id}`:** `{e}`")
            return

        if state == "set_limit":
            if not text.isdigit() or int(text) < 1:
                await message.reply_text("❌ Please enter a valid positive number in MB.")
                return
            await db.update_bot_settings(daily_limit_mb=int(text))
            await message.reply_text(f"✅ **Daily Free Limit updated to `{text} MB`!**")
            return

        if state in ("ban_user", "unban_user"):
            if not text.isdigit():
                await message.reply_text("❌ Please enter a valid numeric User ID.")
                return
            target_id = int(text)
            is_ban = state == "ban_user"
            await db.set_ban_status(target_id, is_ban)
            await message.reply_text(
                f"✅ User `{target_id}` has been **{'Banned' if is_ban else 'Unbanned'}**."
            )
            return

    # 2. User custom prefix prompt
    if uid in USER_INPUT_STATE:
        state = USER_INPUT_STATE.pop(uid)
        if state == "set_prefix":
            clean_prefix = text[:30]
            await db.update_user_pref(uid, custom_prefix=clean_prefix)
            await message.reply_text(f"✅ **Custom filename prefix set to:** `{clean_prefix}`")
            return

    # 3. Password input for locked archive
    if uid in PENDING_PASSWORDS:
        job = PENDING_PASSWORDS[uid]
        status_msg = await message.reply_text("🔓 **Unlocking & extracting archive with your password...**")

        status, err = await asyncio.to_thread(
            extract_archive_sync, job["archive_path"], job["out_dir"], text
        )

        if status == "ok":
            PENDING_PASSWORDS.pop(uid, None)
            await finalize_extraction(
                client,
                message,
                status_msg,
                job["archive_path"],
                job["file_name"],
                job["file_size"],
                job["out_dir"],
                job["token"],
            )
        elif status == "wrong_password":
            await status_msg.edit_text(
                "❌ **Incorrect Password!**\n\nPlease reply with the correct password, or send `/cancel` to abort."
            )
        else:
            PENDING_PASSWORDS.pop(uid, None)
            if job["archive_path"].exists():
                job["archive_path"].unlink(missing_ok=True)
            await status_msg.edit_text(f"❌ **Extraction Failed:**\n`{err}`")


# ================= DIRECT ADMIN SLASH COMMANDS =================
@app.on_message(filters.command("stats") & filters.private)
async def stats_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    s = await db.get_stats()
    await message.reply_text(
        f"📊 **Bot Live Statistics**\n\n"
        f"👥 **Total Users:** `{s['total']}`\n"
        f"💎 **Premium Users:** `{s['premium']}`\n"
        f"🚫 **Banned Users:** `{s['banned']}`\n"
        f"📦 **Total Archives Extracted:** `{s['extractions']}`"
    )


@app.on_message(filters.command("addpremium") & filters.private)
async def add_prem_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.reply_text("Usage: `/addpremium <user_id> <days>`")
        return
    uid, days = int(parts[1]), int(parts[2])
    expiry = await db.set_premium(uid, days)
    await message.reply_text(
        f"✅ User `{uid}` has been granted **💎 Premium for {days} days** (until `{expiry.strftime('%Y-%m-%d')}`)."
    )
    try:
        await client.send_message(
            uid,
            f"🎉 **Congratulations!**\nYour account has been upgraded to **💎 Premium** for **{days} days**! Enjoy unlimited archive extractions.",
        )
    except Exception:
        pass


@app.on_message(filters.command("removepremium") & filters.private)
async def rem_prem_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/removepremium <user_id>`")
        return
    uid = int(parts[1])
    await db.remove_premium(uid)
    await message.reply_text(f"✅ Premium removed from user `{uid}`.")


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.reply_to_message:
        await message.reply_text("Reply to any message with `/broadcast` to send it to all users.")
        return
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    prog = await message.reply_text(f"📢 **Broadcasting to `{len(user_ids)}` users...**")
    for uid in user_ids:
        try:
            await message.reply_to_message.copy(uid)
            sent += 1
            await asyncio.sleep(0.05)
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except Exception:
            failed += 1
    await prog.edit_text(f"✅ **Broadcast Completed!**\n\n📤 Sent: `{sent}`\n❌ Failed: `{failed}`")


# ================= WEB SERVER (CLOUD DOWNLOADS + ZIP ALL) =================
async def health_check(request: web.Request):
    return web.Response(text="Unzip Cloud Bot is Running!", content_type="text/plain")


async def files_page(request: web.Request):
    token = request.match_info["token"]
    meta = load_job_metadata(token)

    if not meta:
        return web.Response(
            text="<h2 style='font-family:sans-serif;text-align:center;margin-top:80px;color:#f8fafc;background:#070b14;'>404 — Cloud Link Expired or Not Found</h2>",
            status=404,
            content_type="text/html",
        )

    for f in meta["files"]:
        f["dl_url"] = f"/dl/{token}/{quote(f['rel_path'])}"

    template = jinja_env.get_template("index.html")
    html = template.render(
        archive_name=meta["archive_name"],
        total_files=meta["total_files"],
        total_size=meta["total_size"],
        zip_all_url=f"/zipall/{token}",
        files=meta["files"],
    )
    return web.Response(text=html, content_type="text/html")


async def download_file(request: web.Request):
    token = request.match_info["token"]
    rel_path = request.match_info["filepath"]

    base_dir = (EXTRACT_DIR / token).resolve()
    target = (base_dir / rel_path).resolve()

    if not str(target).startswith(str(base_dir)) or not target.is_file():
        return web.Response(text="File not found or expired.", status=404)

    headers = {"Content-Disposition": f'attachment; filename="{target.name}"'}
    return web.FileResponse(path=target, headers=headers)


def create_bundle_zip_sync(base_dir: Path, zip_path: Path, files: list[dict]):
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                fp = base_dir / f["rel_path"]
                if fp.is_file():
                    zf.write(fp, arcname=f["rel_path"])


async def download_all_zip(request: web.Request):
    token = request.match_info["token"]
    meta = load_job_metadata(token)
    if not meta:
        return web.Response(text="Link expired or not found.", status=404)

    base_dir = (EXTRACT_DIR / token).resolve()
    bundle_path = base_dir / f".bundle_{token}.zip"

    await asyncio.to_thread(create_bundle_zip_sync, base_dir, bundle_path, meta["files"])
    clean_name = Path(meta["archive_name"]).stem + "_unlocked.zip"
    headers = {"Content-Disposition": f'attachment; filename="{clean_name}"'}
    return web.FileResponse(path=bundle_path, headers=headers)


async def start_services():
    await db.ping_db()

    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/files/{token}", files_page)
    web_app.router.add_get("/dl/{token}/{filepath:.+}", download_file)
    web_app.router.add_get("/zipall/{token}", download_all_zip)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT} | Base URL: {BASE_URL}")

    asyncio.create_task(cleanup_old_jobs())

    await app.start()
    me = await app.get_me()
    logger.info(f"Bot @{me.username} (ID: {me.id}) started successfully!")
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop.run_until_complete(start_services())
