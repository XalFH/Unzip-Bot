import logging
import os
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

MONGO_DB = os.getenv("MONGO_DB", "")
DAILY_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB Daily Free Limit

client = AsyncIOMotorClient(MONGO_DB, serverSelectionTimeoutMS=5000)
db = client["unzip_premium_bot"]
users_col = db["users"]


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


async def add_user(user_id: int, first_name: str = "", username: str = ""):
    user = await users_col.find_one({"_id": user_id})
    if not user:
        await users_col.insert_one({
            "_id": user_id,
            "first_name": first_name,
            "username": username,
            "is_premium": False,
            "premium_until": None,
            "daily_used": 0,
            "last_date": today_str(),
            "total_extractions": 0,
            "joined_at": datetime.now(timezone.utc),
        })
    else:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"first_name": first_name, "username": username}},
        )


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


async def check_quota(user_id: int, file_size: int, admin_ids: list[int]) -> tuple[bool, int]:
    if user_id in admin_ids:
        return True, -1

    user = await get_user(user_id)
    if user.get("is_premium"):
        return True, -1

    used = user.get("daily_used", 0)
    remaining = max(0, DAILY_LIMIT_BYTES - used)
    if used + file_size > DAILY_LIMIT_BYTES:
        return False, remaining
    return True, remaining - file_size


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


async def get_stats() -> dict:
    total = await users_col.count_documents({})
    premium = await users_col.count_documents({"is_premium": True})
    return {"total": total, "premium": premium}


async def get_all_user_ids() -> list[int]:
    cursor = users_col.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]
