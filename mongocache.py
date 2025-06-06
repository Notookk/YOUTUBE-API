from pymongo import MongoClient, ASCENDING, errors
from datetime import datetime, timedelta
from uuid import uuid4

# --- MongoDB Setup ---
MONGO_URL = "mongodb+srv://ytube:ytube@cluster0.pck7csy.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
MONGO_DB = "yt_cache"
MONGO_COLL = "files"
MONGO_KEY_COLL = "api_keys"
MONGO_LOG_COLL = "api_logs"

client = MongoClient(MONGO_URL)
collection = client[MONGO_DB][MONGO_COLL]
key_collection = client[MONGO_DB][MONGO_KEY_COLL]
log_collection = client[MONGO_DB][MONGO_LOG_COLL]

collection.create_index(
    [("video_id", ASCENDING), ("ext", ASCENDING)], unique=True
)
key_collection.create_index("key", unique=True)
key_collection.create_index("id", unique=True)

# ----------------- CACHE FILE OPERATIONS -----------------

def get_cached_file(video_id, ext):
    """
    Fetch a cached file document by video_id and ext.
    """
    return collection.find_one({"video_id": video_id, "ext": ext})

def save_cached_file(video_id, ext, message_id, file_id):
    """
    Upsert (insert or update) a cached file document.
    """
    collection.update_one(
        {"video_id": video_id, "ext": ext},
        {"$set": {"message_id": message_id, "file_id": file_id}},
        upsert=True
    )

def add_cached_file(video_id, ext, message_id, file_id):
    """
    Insert a new cached file document. Raises DuplicateKeyError if already exists.
    """
    doc = {
        "video_id": video_id,
        "ext": ext,
        "message_id": message_id,
        "file_id": file_id
    }
    try:
        collection.insert_one(doc)
    except errors.DuplicateKeyError:
        raise Exception("Cache entry already exists for this video_id and ext.")

def revoke_cached_file(video_id, ext):
    """
    Remove a cached file document.
    """
    collection.delete_one({"video_id": video_id, "ext": ext})

def list_cached_files(limit=50):
    """
    List cached files (for admin/debugging), sorted by most recent.
    """
    return list(collection.find().sort([("_id", -1)]).limit(limit))

# ----------------- API KEY OPERATIONS -----------------

def create_api_key(name, days_valid=30, daily_limit=100, is_admin=False):
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
        key_collection.insert_one(api_key)
    except errors.DuplicateKeyError:
        raise Exception("API key with this value or id already exists.")
    return api_key

def revoke_api_key(key_id):
    key_collection.delete_one({"id": key_id})

def get_api_key(key):
    return key_collection.find_one({"key": key})

def get_api_key_by_id(key_id):
    return key_collection.find_one({"id": key_id})

def list_api_keys():
    return list(key_collection.find().sort([("created_at", -1)]))

# ----------------- LOGGING & LIMITS -----------------

def log_api_request(api_key, endpoint, status):
    now = datetime.utcnow()
    log_collection.insert_one({
        "timestamp": now.isoformat(),
        "api_key": api_key,
        "endpoint": endpoint,
        "status": status
    })

def get_today_count(api_key):
    now = datetime.utcnow()
    today = now.date()
    today_start = datetime.combine(today, datetime.min.time()).isoformat()
    tomorrow_start = datetime.combine(today + timedelta(days=1), datetime.min.time()).isoformat()
    return log_collection.count_documents({
        "api_key": api_key,
        "timestamp": {"$gte": today_start, "$lt": tomorrow_start},
        "status": 200
    })

def check_api_key(key, endpoint):
    key_obj = get_api_key(key)
    now = datetime.utcnow()
    # Log the API request before raising exception
    if not key_obj:
        log_api_request(key, endpoint, 401)
        raise Exception("Invalid API Key")
    if datetime.fromisoformat(key_obj["valid_until"]) < now:
        log_api_request(key, endpoint, 401)
        raise Exception("API Key Expired")
    if not key_obj.get("is_admin", False):
        today_count = get_today_count(key)
        if today_count >= key_obj.get("daily_limit", 100):
            log_api_request(key, endpoint, 429)
            raise Exception("API Key daily limit exceeded")
    log_api_request(key, endpoint, 200)
    return key_obj
