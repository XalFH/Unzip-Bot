import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

# Fix event loop for Python 3.11+ BEFORE importing/initializing Pyrogram
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiohttp import web
from jinja2 import Environment, FileSystemLoader
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
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

PENDING_PASSWORDS: dict[int, dict] = {}
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
    for root, _, filenames in os.walk(out_dir):
        for fname in sorted(filenames):
            fpath = Path(root) / fname
            if fpath.is_file():
                rel = fpath.relative_to(out_dir).as_posix()
                files.append({
                    "name": fname,
                    "rel_path": rel,
                    "size": fpath.stat().st_size,
                    "size_str": human_size(fpath.stat().st_size),
                    "ext": fpath.suffix.lower().lstrip(".") or "file",
                })
    return files


def save_job_metadata(token: str, archive_name: str, files: list[dict]):
    meta_path = EXTRACT_DIR / token / ".meta.json"
    meta_path.write_text(
        json.dumps({
            "archive_name": archive_name,
            "created_at": int(time.time()),
            "total_files": len(files),
            "total_size": human_size(sum(f["size"] for f in files)),
            "files": files,
        }),
        encoding="utf-8",
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


async def finalize_and_send(message: Message, status_msg: Message, archive_path: Path, file_name: str, file_size: int, out_dir: Path, token: str):
    files = collect_extracted_files(out_dir)

    if archive_path.exists():
        archive_path.unlink(missing_ok=True)

    if not files:
        shutil.rmtree(out_dir, ignore_errors=True)
        await status_msg.edit_text("❌ **Archive khali tha ya koi file extract nahi hui.**")
        return

    save_job_metadata(token, file_name, files)
    try:
        await db.add_usage(message.from_user.id, file_size)
    except Exception as e:
        logger.error(f"DB add_usage error: {e}")

    cloud_url = f"{BASE_URL}/files/{token}"
    total_size = human_size(sum(f["size"] for f in files))

    await status_msg.edit_text(
        f"✅ **Extraction Complete!**\n\n"
        f"📦 **Archive:** `{file_name}`\n"
        f"📂 **Total Files:** `{len(files)}`\n"
        f"💾 **Total Size:** `{total_size}`\n\n"
        f"☁️ **Cloud Link ready hai!** Ab files Telegram par bhej raha hoon...",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("☁️ Open Cloud Download Page", url=cloud_url)]
        ]),
    )

    max_tg_send = 30
    sent_count = 0
    for f in files[:max_tg_send]:
        fpath = out_dir / f["rel_path"]
        try:
            await message.reply_document(
                document=str(fpath),
                caption=f"📄 `{f['name']}` ({f['size_str']})",
                quote=False,
            )
            sent_count += 1
            await asyncio.sleep(0.6)
        except Exception as e:
            logger.error(f"Send file error ({f['name']}): {e}")

    extra_note = ""
    if len(files) > max_tg_send:
        extra_note = f"\n⚠️ `{len(files) - max_tg_send}` aur files Cloud Link par available hain."

    await message.reply_text(
        f"🎉 **Sab Ready Hai!**\n\n"
        f"📤 **Telegram par bheji gayi files:** `{sent_count}/{len(files)}`\n"
        f"🔗 **Cloud Download Link:** {cloud_url}\n"
        f"⏳ *Note: Cloud link 2 ghante tak valid rahega.*{extra_note}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Direct Cloud Downloads", url=cloud_url)]
        ]),
        disable_web_page_preview=True,
    )


# ================= TELEGRAM HANDLERS =================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    u = message.from_user
    logger.info(f"/start received from {u.id}")

    try:
        await db.add_user(u.id, u.first_name or "", u.username or "")
        user = await db.get_user(u.id)
    except Exception as e:
        logger.error(f"MongoDB error in /start: {e}")
        user = {"is_premium": False, "daily_used": 0}

    is_prem = user.get("is_premium") or (u.id in ADMIN_IDS)
    plan = "💎 PREMIUM (Unlimited)" if is_prem else "🆓 FREE (20 MB / Day)"
    used = human_size(user.get("daily_used", 0))
    rem = "Unlimited" if is_prem else human_size(max(0, db.DAILY_LIMIT_BYTES - user.get("daily_used", 0)))

    text = (
        f"👋 **Welcome, {u.first_name}!**\n\n"
        f"⚡ **Ultra Unzip & Cloud Bot**\n"
        f"Mujhe koi bhi archive file bhejo — main use unlock & extract karke Telegram + **Instant Cloud Download Link** dono dunga!\n\n"
        f"📦 **Supported Formats:**\n"
        f"`.zip` `.rar` `.7z` `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` `.gz` `.iso`\n\n"
        f"🔐 **Password Lock Support:**\n"
        f"Agar file password-protected hai, toh main password mangunga aur unlock kar dunga.\n\n"
        f"📊 **Your Plan:** `{plan}`\n"
        f"📈 **Used Today:** `{used}` | **Remaining:** `{rem}`"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 My Quota", callback_data="my_quota"),
            InlineKeyboardButton("💎 Get Premium", callback_data="get_premium"),
        ],
        [InlineKeyboardButton("❓ Supported Formats", callback_data="formats")],
    ])
    await message.reply_text(text, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)


@app.on_callback_query()
async def cb_handler(client: Client, cb: CallbackQuery):
    u = cb.from_user
    if cb.data == "my_quota":
        try:
            user = await db.get_user(u.id)
        except Exception:
            user = {"is_premium": False, "daily_used": 0}
        is_prem = user.get("is_premium") or (u.id in ADMIN_IDS)
        used = human_size(user.get("daily_used", 0))
        rem = "Unlimited" if is_prem else human_size(max(0, db.DAILY_LIMIT_BYTES - user.get("daily_used", 0)))
        await cb.answer(f"Used: {used} | Left: {rem}", show_alert=True)
    elif cb.data == "get_premium":
        await cb.message.reply_text(
            "💎 **Upgrade to Premium**\n\n"
            "✅ Unlimited Daily Unzip Quota\n"
            "✅ Priority Fast Extraction\n"
            "✅ Instant Cloud Download Links\n\n"
            "📩 Premium lene ke liye Admin se contact karein!"
        )
        await cb.answer()
    elif cb.data == "formats":
        await cb.answer("ZIP, RAR, 7Z, TAR, GZ, TGZ, BZ2, XZ, CAB, ISO", show_alert=True)


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if uid in PENDING_PASSWORDS:
        info = PENDING_PASSWORDS.pop(uid)
        if info["archive_path"].exists():
            info["archive_path"].unlink(missing_ok=True)
        await message.reply_text("❌ **Password process cancel kar diya gaya.**")
    else:
        await message.reply_text("ℹ️ Koi active process nahi chal raha.")


@app.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    u = message.from_user
    doc = message.document
    file_name = doc.file_name or "archive.zip"
    file_size = doc.file_size or 0

    if not is_supported_archive(file_name):
        await message.reply_text(
            "❌ **Unsupported File Format!**\n\n"
            "Sirf archive files bhejo: `.zip`, `.rar`, `.7z`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.gz`, `.iso`"
        )
        return

    try:
        await db.add_user(u.id, u.first_name or "", u.username or "")
        allowed, remaining = await db.check_quota(u.id, file_size, ADMIN_IDS)
    except Exception as e:
        logger.error(f"DB quota error: {e}")
        allowed, remaining = True, -1

    if not allowed:
        await message.reply_text(
            f"🚫 **Daily 20 MB Limit Reached!**\n\n"
            f"📦 **File Size:** `{human_size(file_size)}`\n"
            f"📉 **Remaining Today:** `{human_size(remaining)}`\n\n"
            f"Kal quota reset ho jayega, ya Unlimited access ke liye **Premium** lein! 💎"
        )
        return

    status_msg = await message.reply_text(f"⬇️ **Downloading** `{file_name}` (`{human_size(file_size)}`)...")

    token = secrets.token_urlsafe(10)
    archive_path = DOWNLOAD_DIR / f"{token}_{file_name}"
    out_dir = EXTRACT_DIR / token

    try:
        await message.download(file_name=str(archive_path))
    except Exception as e:
        await status_msg.edit_text(f"❌ **Download Failed:** `{e}`")
        return

    await status_msg.edit_text("⚙️ **Extracting archive...**")

    status, err = await asyncio.to_thread(extract_archive_sync, archive_path, out_dir, "")

    if status == "ok":
        await finalize_and_send(message, status_msg, archive_path, file_name, file_size, out_dir, token)
    elif status == "need_password":
        PENDING_PASSWORDS[u.id] = {
            "archive_path": archive_path,
            "file_name": file_name,
            "file_size": file_size,
            "token": token,
            "out_dir": out_dir,
        }
        await status_msg.edit_text(
            f"🔐 **Password Protected Archive Detected!**\n\n"
            f"📦 **File:** `{file_name}`\n"
            f"👇 Ab is file ka **Password** send karo (ya `/cancel` bhejo)."
        )
    else:
        archive_path.unlink(missing_ok=True)
        await status_msg.edit_text(f"❌ **Extraction Error:**\n`{err}`")


@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel", "stats", "broadcast", "addpremium", "removepremium"]))
async def handle_password_input(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in PENDING_PASSWORDS:
        return

    password = message.text.strip()
    job = PENDING_PASSWORDS[uid]
    status_msg = await message.reply_text("🔓 **Unlocking & Extracting with your password...**")

    status, err = await asyncio.to_thread(
        extract_archive_sync, job["archive_path"], job["out_dir"], password
    )

    if status == "ok":
        PENDING_PASSWORDS.pop(uid, None)
        await finalize_and_send(
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
            "❌ **Galat Password!**\n\nDobara sahi password bhejo ya `/cancel` karo."
        )
    else:
        PENDING_PASSWORDS.pop(uid, None)
        if job["archive_path"].exists():
            job["archive_path"].unlink(missing_ok=True)
        await status_msg.edit_text(f"❌ **Extraction Failed:**\n`{err}`")


# ================= ADMIN COMMANDS =================
@app.on_message(filters.command("stats") & filters.private)
async def stats_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    s = await db.get_stats()
    await message.reply_text(
        f"📊 **Bot Statistics**\n\n"
        f"👥 **Total Users:** `{s['total']}`\n"
        f"💎 **Premium Users:** `{s['premium']}`"
    )


@app.on_message(filters.command("addpremium") & filters.private)
async def add_prem_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply_text("Usage: `/addpremium <user_id> <days>`")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.reply_text("❌ Valid numeric `user_id` aur `days` do.")
        return

    expiry = await db.set_premium(uid, days)
    await message.reply_text(f"✅ User `{uid}` ko **{days} days** ka Premium mil gaya!\nValid till: `{expiry.strftime('%Y-%m-%d')}`")
    try:
        await client.send_message(uid, f"🎉 **Congratulations!** Aapko **{days} days** ka 💎 **Premium Plan** mil gaya hai (Unlimited Unzip)!")
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
    await message.reply_text(f"✅ User `{uid}` ka Premium hata diya gaya.")


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd(client: Client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.reply_to_message:
        await message.reply_text("Kisi message ko reply karke `/broadcast` likho.")
        return
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    prog = await message.reply_text(f"📢 Broadcasting to `{len(user_ids)}` users...")
    for uid in user_ids:
        try:
            await message.reply_to_message.copy(uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await prog.edit_text(f"✅ **Broadcast Done!**\nSent: `{sent}` | Failed: `{failed}`")


# ================= WEB SERVER =================
async def health_check(request: web.Request):
    return web.Response(text="Unzip Cloud Bot is Running!", content_type="text/plain")


async def files_page(request: web.Request):
    token = request.match_info["token"]
    folder = EXTRACT_DIR / token
    meta_file = folder / ".meta.json"

    if not meta_file.exists():
        return web.Response(
            text="<h2 style='font-family:sans-serif;text-align:center;margin-top:80px;color:#fff;background:#0b0f19;'>404 — Link Expired or Not Found</h2>",
            status=404,
            content_type="text/html",
        )

    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    for f in meta["files"]:
        f["dl_url"] = f"/dl/{token}/{quote(f['rel_path'])}"

    template = jinja_env.get_template("index.html")
    html = template.render(
        archive_name=meta["archive_name"],
        total_files=meta["total_files"],
        total_size=meta["total_size"],
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

    headers = {
        "Content-Disposition": f'attachment; filename="{target.name}"'
    }
    return web.FileResponse(path=target, headers=headers)


async def start_services():
    await db.ping_db()

    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/files/{token}", files_page)
    web_app.router.add_get("/dl/{token}/{filepath:.+}", download_file)

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
