import motor.motor_asyncio
from datetime import datetime, timedelta
from uuid import uuid4
from bson import ObjectId

MONGO_URL = "mongodb+srv://ytube:ytube@cluster0.pck7csy.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
MONGO_DB = "yt_cache"
MONGO_COLL = "files"
MONGO_KEY_COLL = "api_keys"
MONGO_LOG_COLL = "api_logs"

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
collection = client[MONGO_DB][MONGO_COLL]
key_collection = client[MONGO_DB][MONGO_KEY_COLL]
log_collection = client[MONGO_DB][MONGO_LOG_COLL]

# --- Serialization Helper ---
def fix_mongo_document(doc):
    """Recursively convert ObjectId/datetime in dict/list to str/ISO for FastAPI JSON responses."""
    if isinstance(doc, list):
        return [fix_mongo_document(x) for x in doc]
    if isinstance(doc, dict):
        newd = {}
        for k, v in doc.items():
            if isinstance(v, ObjectId):
                newd[k] = str(v)
            elif isinstance(v, datetime):
                newd[k] = v.isoformat()
            else:
                newd[k] = fix_mongo_document(v)
        return newd
    return doc

# ----------------- CACHE FILE OPERATIONS -----------------

async def get_cached_file(video_id, ext):
    doc = await collection.find_one({"video_id": video_id, "ext": ext})
    return fix_mongo_document(doc) if doc else None

async def save_cached_file(video_id, ext, message_id=None, file_id=None, status="ready"):
    await collection.update_one(
        {"video_id": video_id, "ext": ext},
        {"$set": {"message_id": message_id, "file_id": file_id, "status": status}},
        upsert=True
    )

async def set_pending_file(video_id, ext):
    existing = await collection.find_one({"video_id": video_id, "ext": ext})
    if existing:
        if existing.get("status") == "pending":
            return False
        if existing.get("status") == "ready":
            return False
    await collection.update_one(
        {"video_id": video_id, "ext": ext},
        {"$set": {"status": "pending", "message_id": None, "file_id": None}},
        upsert=True
    )
    return True

async def add_cached_file(video_id, ext, message_id, file_id):
    doc = {
        "video_id": video_id,
        "ext": ext,
        "message_id": message_id,
        "file_id": file_id,
        "status": "ready"
    }
    try:
        await collection.insert_one(doc)
    except Exception as e:
        raise Exception("Cache entry already exists for this video_id and ext.")

async def revoke_cached_file(video_id, ext):
    await collection.delete_one({"video_id": video_id, "ext": ext})

async def list_cached_files(limit=50):
    cursor = collection.find().sort([("_id", -1)]).limit(limit)
    docs = await cursor.to_list(length=limit)
    return fix_mongo_document(docs)

# ----------------- API KEY OPERATIONS -----------------

async def create_api_key(name, days_valid=30, daily_limit=100, is_admin=False):
    key = str(uuid4()).replace('-', '')[:32]
    now = datetime.utcnow()
    api_key = {
        "id": str(uuid4()),
        "key": key,
        "name": name,
        "created_at": now.isoformat(),
        "valid_until": (now + timedelta(days=days_valid)).isoformat(),
        "daily_limit": daily_limit,
        "is_admin": is_admin,
        "count": 0
    }
    try:
        await key_collection.insert_one(api_key)
    except Exception as e:
        raise Exception("API key with this value or id already exists.")
    return fix_mongo_document(api_key)

async def revoke_api_key(key_id):
    await key_collection.delete_one({"id": key_id})

async def get_api_key(key):
    doc = await key_collection.find_one({"key": key})
    return fix_mongo_document(doc) if doc else None

async def get_api_key_by_id(key_id):
    doc = await key_collection.find_one({"id": key_id})
    return fix_mongo_document(doc) if doc else None

async def list_api_keys():
    cursor = key_collection.find().sort([("created_at", -1)])
    docs = await cursor.to_list(length=1000)
    return fix_mongo_document(docs)

# ----------------- LOGGING & LIMITS -----------------

async def log_api_request(api_key, endpoint, status, op=None):
    now = datetime.utcnow()
    if op == "count_total":
        return await log_collection.count_documents({})
    if op == "count_today":
        today = now.date()
        today_start = datetime.combine(today, datetime.min.time()).isoformat()
        tomorrow_start = datetime.combine(today + timedelta(days=1), datetime.min.time()).isoformat()
        return await log_collection.count_documents({"timestamp": {"$gte": today_start, "$lt": tomorrow_start}})
    if op == "count_errors":
        return await log_collection.count_documents({"status": {"$gte": 400}})
    if op == "daily_requests":
        result = {}
        for i in range(7):
            d = (now - timedelta(days=6 - i)).date()
            day_start = datetime.combine(d, datetime.min.time()).isoformat()
            next_day = datetime.combine(d + timedelta(days=1), datetime.min.time()).isoformat()
            count = await log_collection.count_documents({
                "timestamp": {"$gte": day_start, "$lt": next_day}
            })
            result[d.strftime("%a")] = count
        return result
    if op == "recent":
        cursor = log_collection.find().sort([("_id", -1)]).limit(50)
        docs = await cursor.to_list(length=50)
        return fix_mongo_document(docs)
    await log_collection.insert_one({
        "timestamp": now.isoformat(),
        "api_key": api_key,
        "endpoint": endpoint,
        "status": status
    })

async def get_today_count(api_key):
    now = datetime.utcnow()
    today = now.date()
    today_start = datetime.combine(today, datetime.min.time()).isoformat()
    tomorrow_start = datetime.combine(today + timedelta(days=1), datetime.min.time()).isoformat()
    return await log_collection.count_documents({
        "api_key": api_key,
        "timestamp": {"$gte": today_start, "$lt": tomorrow_start},
        "status": 200
    })

async def check_api_key(key, endpoint):
    key_obj = await get_api_key(key)
    now = datetime.utcnow()
    if not key_obj:
        await log_api_request(key, endpoint, 401)
        raise Exception("Invalid API Key")
    if datetime.fromisoformat(key_obj["valid_until"]) < now:
        await log_api_request(key, endpoint, 401)
        raise Exception("API Key Expired")
    if not key_obj.get("is_admin", False):
        today_count = await get_today_count(key)
        if today_count >= key_obj.get("daily_limit", 100):
            await log_api_request(key, endpoint, 429)
            raise Exception("API Key daily limit exceeded")
    await log_api_request(key, endpoint, 200)
    return key_obj
