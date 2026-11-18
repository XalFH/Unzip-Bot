import logging
import os
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

MONGO_DB = os.getenv("MONGO_DB", "")
DEFAULT_DAILY_LIMIT_MB = 20

client = AsyncIOMotorClient(MONGO_DB, serverSelectionTimeoutMS=5000)
db = client["unzip_premium_bot"]
users_col = db["users"]
settings_col = db["settings"]


def today_str() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")


async def ping_db() -> bool:
    try:
        await client.admin.command("ping")
        logger.info("MongoDB connected successfully!")
        return True
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return False


# ================= GLOBAL BOT SETTINGS (ADMIN PANEL) =================
async def get_bot_settings() -> dict:
    doc = await settings_col.find_one({"_id": "global"})
    if not doc:
        doc = {
            "_id": "global",
            "fsub_enabled": False,
            "fsub_channel": None,       # int ID (-100...) or "@username"
            "fsub_invite_link": None,
            "log_channel": None,        # int ID (-100...)
            "daily_limit_mb": DEFAULT_DAILY_LIMIT_MB,
            "maintenance": False,
        }
        await settings_col.insert_one(doc)
    return doc


async def update_bot_settings(**kwargs):
    await settings_col.update_one(
        {"_id": "global"},
        {"$set": kwargs},
        upsert=True,
    )


# ================= USER MANAGEMENT =================
async def add_user(user_id: int, first_name: str = "", username: str = "") -> bool:
    """Returns True if this is a brand-new user."""
    user = await users_col.find_one({"_id": user_id})
    if not user:
        await users_col.insert_one({
            "_id": user_id,
            "first_name": first_name,
            "username": username,
            "is_premium": False,
            "premium_until": None,
            "is_banned": False,
            "daily_used": 0,
            "last_date": today_str(),
            "total_extractions": 0,
            "upload_as_doc": True,
            "custom_prefix": "",
            "joined_at": datetime.now(timezone.utc),
        })
        return True
    else:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"first_name": first_name, "username": username}},
        )
        return False


async def get_user(user_id: int) -> dict:
    user = await users_col.find_one({"_id": user_id})
    if not user:
        await add_user(user_id)
        user = await users_col.find_one({"_id": user_id})

    today = today_str()
    if user.get("last_date") != today:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"daily_used": 0, "last_date": today}},
        )
        user["daily_used"] = 0
        user["last_date"] = today

    if user.get("is_premium") and user.get("premium_until"):
        expiry = user["premium_until"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expiry:
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"is_premium": False, "premium_until": None}},
            )
            user["is_premium"] = False
            user["premium_until"] = None

    return user


async def update_user_pref(user_id: int, **kwargs):
    await users_col.update_one({"_id": user_id}, {"$set": kwargs}, upsert=True)


async def check_quota(user_id: int, file_size: int, admin_ids: list[int]) -> tuple[bool, int, int]:
    """Returns (allowed, remaining_bytes, limit_bytes)"""
    settings = await get_bot_settings()
    limit_bytes = int(settings.get("daily_limit_mb", DEFAULT_DAILY_LIMIT_MB)) * 1024 * 1024

    if user_id in admin_ids:
        return True, -1, limit_bytes

    user = await get_user(user_id)
    if user.get("is_premium"):
        return True, -1, limit_bytes

    used = user.get("daily_used", 0)
    remaining = max(0, limit_bytes - used)
    if used + file_size > limit_bytes:
        return False, remaining, limit_bytes
    return True, remaining - file_size, limit_bytes


async def add_usage(user_id: int, file_size: int):
    await users_col.update_one(
        {"_id": user_id},
        {
            "$inc": {"daily_used": file_size, "total_extractions": 1},
            "$set": {"last_date": today_str()},
        },
    )


async def set_premium(user_id: int, days: int):
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"is_premium": True, "premium_until": expiry}},
        upsert=True,
    )
    return expiry


async def remove_premium(user_id: int):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"is_premium": False, "premium_until": None}},
    )


async def set_ban_status(user_id: int, banned: bool):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"is_banned": banned}},
        upsert=True,
    )


async def get_stats() -> dict:
    total = await users_col.count_documents({})
    premium = await users_col.count_documents({"is_premium": True})
    banned = await users_col.count_documents({"is_banned": True})
    pipeline = [{"$group": {"_id": None, "extractions": {"$sum": "$total_extractions"}}}]
    agg = await users_col.aggregate(pipeline).to_list(1)
    extractions = agg[0]["extractions"] if agg else 0
    return {
        "total": total,
        "premium": premium,
        "banned": banned,
        "extractions": extractions,
    }


async def get_all_user_ids() -> list[int]:
    cursor = users_col.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]
