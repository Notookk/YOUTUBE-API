import os
import logging
import asyncio
from fastapi import FastAPI, Query, Request, HTTPException, Body, BackgroundTasks
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from ytube_api import Ytube
from mongocache import (
    get_cached_file, save_cached_file, add_cached_file, revoke_cached_file, list_cached_files,
    create_api_key, revoke_api_key, get_api_key, list_api_keys, log_api_request, get_today_count, check_api_key as mongo_check_api_key, set_pending_file
)
from pyrogram import Client, filters
from pyrogram.types import Message
import httpx
from datetime import datetime, timedelta
from uuid import uuid4

ADMIN_KEY = os.environ.get("ADMIN_KEY", "XOTIK")
LOG_FILE = "api_requests.log"
API_ID = int(os.environ.get("API_ID", 25193832))
API_HASH = os.environ.get("API_HASH", "e154b1ccb0195edec0bc91ae7efebc2f")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7918404318:AAGxfuRA6VVTPcAdxO0quOWzoVoGGLZ6An0")
CACHE_CHANNEL = int(os.environ.get("CACHE_CHANNEL", -1002846625394))
WEB_PORT = int(os.environ.get("WEB_PORT", 8000))

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI()

# CORS for speed and safe public API use
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

yt = Ytube()
templates = Jinja2Templates(directory="templates")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

pyro_api = Client("api-helper", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
bot_app = Client("music-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Helpers ---
def make_caption(video_id, ext):
    return f"yt_{video_id}_{ext}"

async def ensure_pyrogram_running():
    if not pyro_api.is_connected:
        await pyro_api.start()
        await asyncio.sleep(2)

async def ensure_channel_known(client, channel_id):
    try:
        await client.get_chat(channel_id)
    except Exception:
        try:
            await client.send_message(channel_id, "Initializing channel for bot cache (safe to delete)")
        except Exception as e:
            logging.error(f"Failed to initialize channel {channel_id}: {e}")
            raise

async def search_cache(video_id, ext):
    return await get_cached_file(video_id, ext)

async def cache_file_send(file_path, video_id, ext):
    await ensure_pyrogram_running()
    await ensure_channel_known(pyro_api, CACHE_CHANNEL)
    caption = make_caption(video_id, ext)
    if ext == "mp3":
        sent = await pyro_api.send_audio(CACHE_CHANNEL, file_path, caption=caption)
        file_id = sent.audio.file_id
    else:
        sent = await pyro_api.send_video(CACHE_CHANNEL, file_path, caption=caption)
        file_id = sent.video.file_id
    await save_cached_file(video_id, ext, sent.id, file_id, status="ready")
    return sent

def check_admin_key(request: Request):
    key = request.query_params.get("admin_key")
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid admin key.")

async def check_api_key(request: Request):
    if request.url.path in ["/", "/status", "/", "/admin"]:
        return
    key = request.headers.get("x-api-key")
    if not key:
        key = request.query_params.get("api_key")
    try:
        await mongo_check_api_key(key, request.url.path)
    except Exception as e:
        logging.warning(f"Unauthorized/invalid request from {getattr(request.client, 'host', 'unknown')} : {str(e)}")
        raise HTTPException(status_code=401 if "limit" not in str(e) else 429, detail=str(e))

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logging.error(f"Unhandled Exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error."}
    )

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(pyro_api.start())
    await asyncio.sleep(2)
    asyncio.create_task(run_bot())

@app.on_event("shutdown")
async def shutdown_event():
    await pyro_api.stop()
    await bot_app.stop()

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    try:
        check_admin_key(request)
        return templates.TemplateResponse("admin.html", {"request": request})
    except HTTPException:
        return RedirectResponse(url="/")

# --- ADMIN ENDPOINTS FOR DASHBOARD ---
@app.get("/admin/metrics")
async def admin_metrics(request: Request):
    check_admin_key(request)
    now = datetime.utcnow()
    total_requests = await log_api_request(None, None, None, op="count_total")
    today_requests = await log_api_request(None, None, None, op="count_today")
    active_keys = len([k for k in await list_api_keys() if datetime.fromisoformat(k['valid_until']) > now])
    error_rate = 0
    if total_requests:
        error_rate = 100 * await log_api_request(None, None, None, op="count_errors") // total_requests
    daily_requests = await log_api_request(None, None, None, op="daily_requests")
    key_distribution = {k['name']: await get_today_count(k['key']) for k in await list_api_keys()}
    return {
        "total_requests": total_requests,
        "today_requests": today_requests,
        "active_keys": active_keys,
        "error_rate": error_rate,
        "daily_requests": daily_requests,
        "key_distribution": key_distribution,
    }

@app.get("/admin/list_api_keys")
async def admin_list_api_keys(request: Request):
    check_admin_key(request)
    now = datetime.utcnow()
    keys = await list_api_keys()
    for k in keys:
        k['count'] = await get_today_count(k['key'])
    return keys

@app.get("/admin/recent_logs")
async def admin_recent_logs(request: Request):
    check_admin_key(request)
    return await log_api_request(None, None, None, op="recent")

@app.post("/admin/create_api_key")
async def admin_create_api_key(request: Request, data: dict = Body(...)):
    check_admin_key(request)
    key_doc = await create_api_key(
        name=data.get("name", "unnamed"),
        days_valid=data.get("days_valid", 30),
        daily_limit=data.get("daily_limit", 100),
        is_admin=data.get("is_admin", False)
    )
    return {"api_key": key_doc["key"]}

@app.post("/admin/revoke_api_key")
async def admin_revoke_api_key(request: Request, data: dict = Body(...)):
    check_admin_key(request)
    await revoke_api_key(data["id"])
    return {"status": "ok"}

# --- API CORE ENDPOINTS ---

@app.get("/search")
async def search(request: Request, q: str = Query(...)):
    await check_api_key(request)
    results = yt.search_videos(q)
    if not results.items:
        return {"error": "No results found"}
    entry = results.items[0]
    video_id = getattr(entry, 'id', None) or (entry['id'] if isinstance(entry, dict) and 'id' in entry else None)
    title = getattr(entry, 'title', None) or (entry['title'] if isinstance(entry, dict) and 'title' in entry else None)
    if not video_id:
        return {"error": "No video id found"}
    if not title:
        title = video_id
    base = str(request.base_url).rstrip("/")
    audio_stream = f"{base}/download/audio?video_id={video_id}"
    video_stream = f"{base}/download/video?video_id={video_id}"
    return {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "audio_stream": audio_stream,
        "video_stream": video_stream
    }

@app.get("/download/audio")
async def download_audio(request: Request, video_id: str = Query(...), background_tasks: BackgroundTasks = None):
    await check_api_key(request)
    cached = await search_cache(video_id, "mp3")
    if cached:
        if cached.get("status") == "pending":
            # Wait up to 20s for the cache to be ready
            for _ in range(20):
                await asyncio.sleep(1)
                cached = await search_cache(video_id, "mp3")
                if cached and cached.get("status") == "ready" and "message_id" in cached:
                    break
            else:
                raise HTTPException(status_code=423, detail="File is being processed. Try again soon.")
        if cached.get("status") == "ready" and "message_id" in cached:
            file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
            if not file or not getattr(file, "audio", None):
                raise HTTPException(status_code=410, detail="Cached audio not found. Please try again.")
            file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp3")
            headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp3"'}
            def iterfile():
                with open(file_url, "rb") as f:
                    while chunk := f.read(4096):
                        yield chunk
            return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)
    # Try to set pending status atomically
    set_pending = await set_pending_file(video_id, "mp3")
    if not set_pending:
        # Wait for the other process to finish
        for _ in range(20):
            await asyncio.sleep(1)
            cached = await search_cache(video_id, "mp3")
            if cached and cached.get("status") == "ready" and "message_id" in cached:
                file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
                if not file or not getattr(file, "audio", None):
                    raise HTTPException(status_code=410, detail="Cached audio not found. Please try again.")
                file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp3")
                headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp3"'}
                def iterfile():
                    with open(file_url, "rb") as f:
                        while chunk := f.read(4096):
                            yield chunk
                return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)
        raise HTTPException(status_code=423, detail="File is being processed. Try again soon.")
    # Proceed with download
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        await save_cached_file(video_id, "mp3", message_id=None, file_id=None, status="error")
        raise HTTPException(status_code=404, detail="No audio link found")
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title:
        title = video_id
    download_link = yt.get_download_link(item, format="mp3", quality="320")
    if not download_link or not getattr(download_link, 'url', None):
        await save_cached_file(video_id, "mp3", message_id=None, file_id=None, status="error")
        raise HTTPException(status_code=404, detail="No audio link found")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(download_link.url)
        if resp.status_code != 200:
            await save_cached_file(video_id, "mp3", message_id=None, file_id=None, status="error")
            raise HTTPException(status_code=500, detail="Failed to fetch audio stream")
        os.makedirs("downloads", exist_ok=True)
        file_path = f"downloads/{video_id}_new.mp3"
        with open(file_path, "wb") as f:
            f.write(resp.content)
        if background_tasks:
            background_tasks.add_task(cache_file_send, file_path, video_id, "mp3")
        else:
            await cache_file_send(file_path, video_id, "mp3")
        headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp3"'}
        def iterfile():
            with open(file_path, "rb") as f:
                while chunk := f.read(4096):
                    yield chunk
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)

@app.get("/download/video")
async def download_video(request: Request, video_id: str = Query(...), background_tasks: BackgroundTasks = None):
    await check_api_key(request)
    cached = await search_cache(video_id, "mp4")
    if cached:
        if cached.get("status") == "pending":
            # Wait up to 20s for the cache to be ready
            for _ in range(20):
                await asyncio.sleep(1)
                cached = await search_cache(video_id, "mp4")
                if cached and cached.get("status") == "ready" and "message_id" in cached:
                    break
            else:
                raise HTTPException(status_code=423, detail="File is being processed. Try again soon.")
        if cached.get("status") == "ready" and "message_id" in cached:
            file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
            if not file or not getattr(file, "video", None):
                raise HTTPException(status_code=410, detail="Cached video not found. Please try again.")
            file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp4")
            headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
            def iterfile():
                with open(file_url, "rb") as f:
                    while chunk := f.read(4096):
                        yield chunk
            return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
    # Try to set pending status atomically
    set_pending = await set_pending_file(video_id, "mp4")
    if not set_pending:
        # Wait for the other process to finish
        for _ in range(20):
            await asyncio.sleep(1)
            cached = await search_cache(video_id, "mp4")
            if cached and cached.get("status") == "ready" and "message_id" in cached:
                file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
                if not file or not getattr(file, "video", None):
                    raise HTTPException(status_code=410, detail="Cached video not found. Please try again.")
                file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp4")
                headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
                def iterfile():
                    with open(file_url, "rb") as f:
                        while chunk := f.read(4096):
                            yield chunk
                return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
        raise HTTPException(status_code=423, detail="File is being processed. Try again soon.")
    # Proceed with download
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        await save_cached_file(video_id, "mp4", message_id=None, file_id=None, status="error")
        raise HTTPException(status_code=404, detail="No video link found")
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title:
        title = video_id
    download_link = yt.get_download_link(item, format="mp4", quality="360")
    if not download_link or not getattr(download_link, 'url', None):
        await save_cached_file(video_id, "mp4", message_id=None, file_id=None, status="error")
        raise HTTPException(status_code=404, detail="No video link found")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.get(download_link.url)
        if resp.status_code != 200:
            await save_cached_file(video_id, "mp4", message_id=None, file_id=None, status="error")
            raise HTTPException(status_code=500, detail="Failed to fetch video stream")
        os.makedirs("downloads", exist_ok=True)
        file_path = f"downloads/{video_id}_new.mp4"
        with open(file_path, "wb") as f:
            f.write(resp.content)
        if background_tasks:
            background_tasks.add_task(cache_file_send, file_path, video_id, "mp4")
        else:
            await cache_file_send(file_path, video_id, "mp4")
        headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
        def iterfile():
            with open(file_path, "rb") as f:
                while chunk := f.read(4096):
                    yield chunk
        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)

# Telegram Bot Section (unchanged but required for full function)
async def search_cache_bot(video_id, ext):
    return await get_cached_file(video_id, ext)

@bot_app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply(
        "👋 **Welcome!**\n\n"
        "Send /song <YouTube URL or ID> to get any song/audio.\n"
        "The bot caches each downloaded file in a channel for blazing fast future access!\n\n"
        "Example:\n/song https://youtu.be/dQw4w9WgXcQ"
    )

@bot_app.on_message(filters.command("song") & filters.private)
async def song_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("Usage: /song YOUTUBE_VIDEO_ID_or_URL")
        return
    query = message.command[1]
    if "youtube.com" in query or "youtu.be" in query:
        if "v=" in query:
            video_id = query.split("v=")[-1].split("&")[0]
        elif "youtu.be/" in query:
            video_id = query.split("youtu.be/")[-1].split("?")[0]
        else:
            await message.reply("Could not extract video ID.")
            return
    else:
        video_id = query

    title = video_id
    try:
        async with httpx.AsyncClient(timeout=10) as client2:
            r = await client2.get(f"http://127.0.0.1:{WEB_PORT}/search", params={"q": video_id})
            if r.status_code == 200 and "title" in r.json():
                title = r.json()["title"]
    except Exception as e:
        logging.error(f"[bot /song] search error: {e}")

    cached_msg = await search_cache_bot(video_id, "mp3")
    if cached_msg and "file_id" in cached_msg:
        await message.reply_audio(cached_msg["file_id"], caption=f"🎵 {title}\n(From cache)")
        return

    await message.reply("Downloading, please wait...")
    async with httpx.AsyncClient(timeout=180) as client3:
        try:
            r = await client3.get(
                f"http://127.0.0.1:{WEB_PORT}/download/audio",
                params={"video_id": video_id}
            )
            if r.status_code == 200:
                os.makedirs("downloads", exist_ok=True)
                file_path = f"downloads/{video_id}_bot.mp3"
                with open(file_path, "wb") as f:
                    f.write(r.content)
                sent = await client.send_audio(CACHE_CHANNEL, file_path, caption=make_caption(video_id, "mp3"))
                await save_cached_file(video_id, "mp3", sent.id, sent.audio.file_id, status="ready")
                await message.reply_audio(sent.audio.file_id, caption=f"🎵 {title}\n(Downloaded and cached)")
                os.remove(file_path)
                return
            else:
                await message.reply("API download failed.")
        except Exception as e:
            await message.reply("Failed to download and send audio.")
            logging.error(f"[bot /song] download error: {e}")

async def run_bot():
    await bot_app.start()
    logging.info("Bot started!")
    await bot_app.idle()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=WEB_PORT)
