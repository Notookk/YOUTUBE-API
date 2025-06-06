import os
import logging
import requests
import asyncio
from fastapi import FastAPI, Query, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ytube_api import Ytube
from mongocache import get_cached_file, save_cached_file
from pyrogram import Client, filters
from pyrogram.types import Message
import httpx

API_KEY = "ishq_mein"
ADMIN_KEY = "XOTIK"
LOG_FILE = "api_requests.log"
API_ID = 25193832
API_HASH = "e154b1ccb0195edec0bc91ae7efebc2f"
BOT_TOKEN = "7918404318:AAGxfuRA6VVTPcAdxO0quOWzoVoGGLZ6An0"
CACHE_CHANNEL = -1002846625394  # <-- Set your private channel ID here
WEB_PORT = 8000

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI()
yt = Ytube()
templates = Jinja2Templates(directory="templates")

# For serving /static files
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

pyro_api = Client("api-helper", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
bot_app = Client("music-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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
            print(f"Failed to initialize channel {channel_id}: {e}")
            raise

async def search_cache(video_id, ext):
    entry = get_cached_file(video_id, ext)
    return entry

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
    save_cached_file(video_id, ext, sent.id, file_id)
    return sent

def check_api_key(request: Request):
    if request.url.path in ["/", "/status", "/", "/admin"]:
        return
    key = request.headers.get("x-api-key")
    if not key:
        key = request.query_params.get("api_key")
    if key != API_KEY:
        logging.warning(f"Unauthorized request from {request.client.host}")
        raise HTTPException(status_code=401, detail="Invalid API Key")

def check_admin_key(request: Request):
    key = request.query_params.get("admin_key")
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid admin key.")

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

@app.get("/search")
async def search(request: Request, q: str = Query(...)):
    check_api_key(request)
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
    audio_stream = f"{base}/download/audio?video_id={video_id}&api_key={API_KEY}"
    video_stream = f"{base}/download/video?video_id={video_id}&api_key={API_KEY}"
    return {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "audio_stream": audio_stream,
        "video_stream": video_stream
    }

@app.get("/download/audio")
async def download_audio(request: Request, video_id: str = Query(...)):
    check_api_key(request)
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        raise HTTPException(status_code=404, detail="No audio link found")
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title:
        title = video_id
    cached = await search_cache(video_id, "mp3")
    if cached and "message_id" in cached:
        file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
        file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp3")
        headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp3"'}
        def iterfile():
            with open(file_url, "rb") as f:
                while chunk := f.read(4096):
                    yield chunk
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)
    download_link = yt.get_download_link(item, format="mp3", quality="320")
    if not download_link or not getattr(download_link, 'url', None):
        raise HTTPException(status_code=404, detail="No audio link found")
    resp = requests.get(download_link.url, stream=True)
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch audio stream")
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{video_id}_new.mp3"
    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(4096):
            f.write(chunk)
    await cache_file_send(file_path, video_id, "mp3")
    headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp3"'}
    def iterfile():
        with open(file_path, "rb") as f:
            while chunk := f.read(4096):
                yield chunk
    return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)

@app.get("/download/video")
async def download_video(request: Request, video_id: str = Query(...)):
    check_api_key(request)
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        raise HTTPException(status_code=404, detail="No video link found")
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title:
        title = video_id
    cached = await search_cache(video_id, "mp4")
    if cached and "message_id" in cached:
        file = await pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"])
        file_url = await pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp4")
        headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
        def iterfile():
            with open(file_url, "rb") as f:
                while chunk := f.read(4096):
                    yield chunk
        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
    download_link = yt.get_download_link(item, format="mp4", quality="360")
    if not download_link or not getattr(download_link, 'url', None):
        raise HTTPException(status_code=404, detail="No video link found")
    resp = requests.get(download_link.url, stream=True)
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch video stream")
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{video_id}_new.mp4"
    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(4096):
            f.write(chunk)
    await cache_file_send(file_path, video_id, "mp4")
    headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
    def iterfile():
        with open(file_path, "rb") as f:
            while chunk := f.read(4096):
                yield chunk
    return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)

# ... (rest of your bot_app code remains unchanged)

async def run_bot():
    await bot_app.start()
    print("Bot started!")
    await bot_app.idle()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=WEB_PORT)
