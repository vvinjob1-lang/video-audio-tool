import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
UPLOAD_DIR = BASE_DIR / "uploads"
COOKIE_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_youtube_url(url: str) -> str:
    """Convert Shorts/live/embed/youtu.be links to a normal watch URL where possible."""
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")

    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.strip("/")

    video_id = None

    # https://youtube.com/shorts/<id>
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and path.startswith("shorts/"):
        video_id = path.split("/")[1]

    # https://youtu.be/<id>
    elif host == "youtu.be" and path:
        video_id = path.split("/")[0]

    # https://youtube.com/embed/<id> or /live/<id>
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and (
        path.startswith("embed/") or path.startswith("live/")
    ):
        video_id = path.split("/")[1]

    # https://youtube.com/watch?v=<id>
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]

    if video_id:
        video_id = re.sub(r"[^0-9A-Za-z_-]", "", video_id)
        if not video_id:
            raise ValueError("Invalid YouTube video id")
        return f"https://www.youtube.com/watch?v={video_id}"

    return url


class YTDLPLogger:
    def debug(self, msg):
        # Railway logs get too noisy if every debug line is printed.
        pass

    def warning(self, msg):
        print(f"yt-dlp warning: {msg}", flush=True)

    def error(self, msg):
        print(f"yt-dlp error: {msg}", flush=True)


def build_ydl_opts(output_base: Path, fallback: bool = False) -> dict:
    """yt-dlp config tuned for YouTube Shorts + normal videos."""
    player_clients = ["default", "mweb", "ios", "tv"] if fallback else ["default", "mweb", "ios"]

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(output_base) + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "http_headers": YOUTUBE_HEADERS,
        "logger": YTDLPLogger(),
        "extractor_args": {
            "youtube": {
                # Shorts often fail on one YouTube client but work on another.
                # default keeps yt-dlp's current best clients; mweb/ios/tv are fallbacks.
                "player_client": player_clients,
            }
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "prefer_ffmpeg": True,
    }

    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0:
        opts["cookiefile"] = str(COOKIE_FILE)

    # Some Railway images set ffmpeg in a custom path.
    ffmpeg_location = os.getenv("FFMPEG_LOCATION")
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    return opts


def download_audio_as_mp3(url: str) -> tuple[Path, dict]:
    normalized_url = normalize_youtube_url(url)
    output_base = DOWNLOAD_DIR / uuid.uuid4().hex
    final_mp3 = Path(str(output_base) + ".mp3")

    last_error = None
    for fallback in (False, True):
        try:
            ydl_opts = build_ydl_opts(output_base, fallback=fallback)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(normalized_url, download=True)

            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp did not return a valid video info object")

            if final_mp3.exists() and final_mp3.stat().st_size > 0:
                return final_mp3, {
                    "title": info.get("title") or "audio",
                    "video_id": info.get("id"),
                    "source_url": normalized_url,
                }

            # Very defensive fallback in case the postprocessor created a slightly different name.
            matches = list(DOWNLOAD_DIR.glob(f"{output_base.name}*.mp3"))
            if matches:
                return matches[0], {
                    "title": info.get("title") or "audio",
                    "video_id": info.get("id"),
                    "source_url": normalized_url,
                }

            raise RuntimeError("MP3 file was not created. Check that FFmpeg is installed on Railway.")

        except Exception as exc:
            last_error = exc
            print(f"download attempt failed fallback={fallback}: {exc}", flush=True)

    raise RuntimeError(str(last_error) if last_error else "Download failed")


@app.get("/")
def index():
    return jsonify({"ok": True, "service": "video-audio-tool", "endpoint": "POST /download"})


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/download")
def download():
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url") or request.values.get("url")

        if not url:
            return jsonify({"success": False, "error": "Missing 'url'"}), 400

        mp3_path, meta = download_audio_as_mp3(url)
        download_name = f"{meta.get('video_id') or mp3_path.stem}.mp3"

        return send_file(
            mp3_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
