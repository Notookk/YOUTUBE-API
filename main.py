import os
import logging
import requests
import asyncio
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from ytube_api import Ytube
from mongocache import get_cached_file, save_cached_file
from pyrogram import Client

API_KEY = "ishq_mein"
ADMIN_KEY = "XOTIK"
LOG_FILE = "api_requests.log"
API_ID = 25193832
API_HASH = "e154b1ccb0195edec0bc91ae7efebc2f"
BOT_TOKEN = "7918404318:AAGxfuRA6VVTPcAdxO0quOWzoVoGGLZ6An0"
CACHE_CHANNEL = -1002846625394
WEB_PORT = 8000

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = Flask(__name__)
yt = Ytube()
pyro_api = Client("api-helper", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def make_caption(video_id, ext):
    return f"yt_{video_id}_{ext}"

def check_api_key():
    key = request.headers.get("x-api-key") or request.args.get("api_key")
    return key == API_KEY

def check_admin_key():
    key = request.args.get("admin_key")
    return key == ADMIN_KEY

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
    return get_cached_file(video_id, ext)

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

@app.route("/", methods=["GET"])
def index():
    # Always show the normal user page
    return render_template("index.html")

@app.route("/admin", methods=["GET"])
def admin_panel():
    # Only show the admin panel if admin_key is right, else redirect to /
    if check_admin_key():
        return render_template("admin.html")
    return redirect(url_for("index"))

@app.route("/search")
def search():
    if not check_api_key():
        return jsonify({"error": "Invalid API Key"}), 401
    q = request.args.get("q")
    if not q:
        return jsonify({"error": "Missing search query"}), 400
    results = yt.search_videos(q)
    if not results.items:
        return jsonify({"error": "No results found"}), 404
    entry = results.items[0]
    video_id = getattr(entry, 'id', None) or (entry['id'] if isinstance(entry, dict) and 'id' in entry else None)
    title = getattr(entry, 'title', None) or (entry['title'] if isinstance(entry, dict) and 'title' in entry else None)
    if not video_id:
        return jsonify({"error": "No video id found"}), 404
    if not title:
        title = video_id
    base = request.url_root.rstrip("/")
    audio_stream = f"{base}/download/audio?video_id={video_id}&api_key={API_KEY}"
    video_stream = f"{base}/download/video?video_id={video_id}&api_key={API_KEY}"
    return jsonify({
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "audio_stream": audio_stream,
        "video_stream": video_stream
    })

@app.route("/download/audio")
def download_audio():
    if not check_api_key():
        return jsonify({"error": "Invalid API Key"}), 401
    video_id = request.args.get("video_id")
    if not video_id:
        return jsonify({"error": "Missing video_id"}), 400
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        return jsonify({"error": "No audio link found"}), 404
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title: title = video_id
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cached = loop.run_until_complete(search_cache(video_id, "mp3"))
    if cached and "message_id" in cached:
        file = loop.run_until_complete(pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"]))
        file_url = loop.run_until_complete(pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp3"))
        return send_file(file_url, as_attachment=True, download_name=f"{video_id}.mp3")
    download_link = yt.get_download_link(item, format="mp3", quality="320")
    if not download_link or not getattr(download_link, 'url', None):
        return jsonify({"error": "No audio link found"}), 404
    resp = requests.get(download_link.url, stream=True)
    if resp.status_code != 200:
        return jsonify({"error": "Failed to fetch audio stream"}), 500
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{video_id}_new.mp3"
    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(4096):
            f.write(chunk)
    loop.run_until_complete(cache_file_send(file_path, video_id, "mp3"))
    return send_file(file_path, as_attachment=True, download_name=f"{video_id}.mp3")

@app.route("/download/video")
def download_video():
    if not check_api_key():
        return jsonify({"error": "Invalid API Key"}), 401
    video_id = request.args.get("video_id")
    if not video_id:
        return jsonify({"error": "Missing video_id"}), 400
    results = yt.search_videos(f"https://www.youtube.com/watch?v={video_id}")
    if not results.items:
        return jsonify({"error": "No video link found"}), 404
    item = results.items[0]
    title = getattr(item, "title", None) or (item["title"] if isinstance(item, dict) and "title" in item else None)
    if not title: title = video_id
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cached = loop.run_until_complete(search_cache(video_id, "mp4"))
    if cached and "message_id" in cached:
        file = loop.run_until_complete(pyro_api.get_messages(CACHE_CHANNEL, cached["message_id"]))
        file_url = loop.run_until_complete(pyro_api.download_media(file, file_name=f"downloads/{video_id}_cache.mp4"))
        return send_file(file_url, as_attachment=True, download_name=f"{video_id}.mp4")
    download_link = yt.get_download_link(item, format="mp4", quality="360")
    if not download_link or not getattr(download_link, 'url', None):
        return jsonify({"error": "No video link found"}), 404
    resp = requests.get(download_link.url, stream=True)
    if resp.status_code != 200:
        return jsonify({"error": "Failed to fetch video stream"}), 500
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{video_id}_new.mp4"
    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(4096):
            f.write(chunk)
    loop.run_until_complete(cache_file_send(file_path, video_id, "mp4"))
    return send_file(file_path, as_attachment=True, download_name=f"{video_id}.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=True)
