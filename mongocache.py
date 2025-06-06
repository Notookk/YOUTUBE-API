from pymongo import MongoClient, ASCENDING

# Set your MongoDB connection string here
MONGO_URL = "mongodb+srv://ytube:ytube@cluster0.pck7csy.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
MONGO_DB = "yt_cache"
MONGO_COLL = "files"

client = MongoClient(MONGO_URL)
collection = client[MONGO_DB][MONGO_COLL]
collection.create_index(
    [("video_id", ASCENDING), ("ext", ASCENDING)], unique=True
)

def get_cached_file(video_id, ext):
    doc = collection.find_one({"video_id": video_id, "ext": ext})
    return doc

def save_cached_file(video_id, ext, message_id, file_id):
    collection.update_one(
        {"video_id": video_id, "ext": ext},
        {"$set": {"message_id": message_id, "file_id": file_id}},
        upsert=True
    )
