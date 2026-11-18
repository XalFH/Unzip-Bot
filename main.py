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
            await send_
