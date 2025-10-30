#!/usr/bin/env python3
import os
import io
import uuid
import json
import shutil
import logging
import zipfile
import tempfile
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
import yt_dlp

# ==================== CONFIG ====================

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app, resources={r"/*": {"origins": "*"}})

LOG_FOLDER = "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(os.path.join(LOG_FOLDER, "igdl.log")), logging.StreamHandler()]
)
logger = logging.getLogger("IGDL")

MAX_FILE_SIZE_MB = 100
RATE_LIMIT_PER_HOUR = 50

# Cookie file lookup
COOKIES_FILE_PATHS = [
    "/etc/secrets/cookies.txt",
    "./cookies.txt",
    "/tmp/cookies.txt"
]

def get_cookies_file_path():
    writable_path = "/tmp/cookies.txt"
    for path in COOKIES_FILE_PATHS:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                if path.startswith("/etc/secrets/"):
                    shutil.copy2(path, writable_path)
                    logger.info(f"✓ Cookies copied from {path}")
                    return writable_path
                logger.info(f"✓ Using cookies from {path}")
                return path
            except Exception as e:
                logger.warning(f"Cookie copy failed: {e}")
                return path
    logger.warning("⚠️ No valid cookies found")
    return None

COOKIES_FILE_PATH = get_cookies_file_path()
download_tracker = {}

# ==================== HELPERS ====================

def check_rate_limit(ip):
    now = datetime.now()
    download_tracker.setdefault(ip, [])
    download_tracker[ip] = [t for t in download_tracker[ip] if now - t < timedelta(hours=1)]
    if len(download_tracker[ip]) >= RATE_LIMIT_PER_HOUR:
        return False
    download_tracker[ip].append(now)
    return True

def _mimetype(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in [".mp4", ".mkv", ".webm", ".mov"]:
        return "video/mp4"
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"

def get_ydl_opts(download=False, output_dir=None):
    opts = {
        "quiet": False,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.instagram.com/"
        }
    }
    if COOKIES_FILE_PATH:
        opts["cookiefile"] = COOKIES_FILE_PATH
    if download and output_dir:
        opts["outtmpl"] = os.path.join(output_dir, "%(playlist_index)s-%(id)s.%(ext)s")
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    return opts

def get_media_info(url):
    try:
        opts = get_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise Exception("No media found.")
            upload_date = info.get("upload_date")
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, "%Y%m%d").strftime("%Y-%m-%d")
                except:
                    upload_date = "Unknown"
            entries = info.get("entries") or []
            return {
                "title": info.get("title", "Instagram Media"),
                "thumbnail": info.get("thumbnail", ""),
                "uploader": info.get("uploader", "Unknown"),
                "upload_date": upload_date,
                "like_count": info.get("like_count", 0),
                "comment_count": info.get("comment_count", 0),
                "description": info.get("description", ""),
                "duration": info.get("duration", 0),
                "is_carousel": bool(entries),
                "media_count": len(entries) if entries else 1,
            }
    except Exception as e:
        logger.error(f"Media info error: {e}")
        raise

def download_to_buffer(url):
    tmp_dir = tempfile.mkdtemp(prefix="igdl_")
    try:
        opts = get_ydl_opts(download=True, output_dir=tmp_dir)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [
            os.path.join(r, f)
            for r, _, fs in os.walk(tmp_dir)
            for f in fs
            if f.lower().endswith((".mp4", ".jpg", ".jpeg", ".png", ".webp"))
        ]

        if not files:
            raise Exception("No media files downloaded")

        total_mb = sum(os.path.getsize(f) for f in files) / (1024 * 1024)
        if total_mb > MAX_FILE_SIZE_MB:
            raise Exception(f"Download too large ({total_mb:.1f} MB > {MAX_FILE_SIZE_MB} MB limit)")

        if len(files) == 1:
            fpath = files[0]
            with open(fpath, "rb") as f:
                data = io.BytesIO(f.read())
            data.seek(0)
            return {"file_data": data, "filename": os.path.basename(fpath), "mimetype": _mimetype(fpath)}

        zip_bio = io.BytesIO()
        with zipfile.ZipFile(zip_bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in files:
                zf.write(fpath, os.path.basename(fpath))
        zip_bio.seek(0)
        zip_name = f"instagram_post_{uuid.uuid4().hex[:8]}.zip"
        return {"file_data": zip_bio, "filename": zip_name, "mimetype": "application/zip"}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ==================== ROUTES ====================

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Instagram Downloader API running",
        "endpoints": [
            "/api/media/info",
            "/api/download",
            "/health"
        ]
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "cookies_configured": bool(COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH)),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/api/media/info", methods=["POST"])
def api_info():
    data = request.get_json(force=True)
    url = data.get("url")
    if not url:
        return jsonify({"status": "error", "message": "URL required"}), 400
    try:
        info = get_media_info(url)
        return jsonify({"status": "ok", "media_info": info})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    url = data.get("url")
    if not url:
        return jsonify({"status": "error", "message": "URL required"}), 400

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not check_rate_limit(client_ip):
        return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429

    try:
        result = download_to_buffer(url)
        file_data = result["file_data"]
        return send_file(
            file_data,
            as_attachment=True,
            download_name=result["filename"],
            mimetype=result["mimetype"]
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({"status": "error", "message": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal error: {e}")
    return jsonify({"status": "error", "message": "Internal server error"}), 500

# ==================== START ====================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🚀 Instagram Downloader API (No HTML) starting...")
    logger.info(f"📁 Cookies: {'Found' if COOKIES_FILE_PATH else 'Missing'}")
    logger.info(f"🌐 CORS: Enabled for all origins")
    logger.info("=" * 60)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
