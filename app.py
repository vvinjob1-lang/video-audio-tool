import asyncio
import base64
import html
import json
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import requests
import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Lovable previews may run on http://id-preview-*.lovable.app (browser shows "Not secure")
# as well as https://*.lovable.app.  The previous CORS list allowed only HTTPS
# Lovable origins, so browser fetches to /rewrite-options If AI fails:
# - Do not return first N characters as final script
# - Do not return prefix-only summary
# - Either return success:false with needs_retry:true
# - Or return distributed extractive fallback from beginning + middle + ending
# - Mark fallback as tts_safe:false
# worked from Railway. Keep this permissive for app/API endpoints only.
#
# Backend rewrite fallback rule:
# - Do not return first N characters as final script.
# - Do not return prefix-only summary.
# - If AI fails, return success:false with needs_retry:true, or return distributed fallback from beginning/middle/ending.
# - Mark fallback as tts_safe:false.
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    expose_headers=["Content-Type", "Content-Disposition"],
    supports_credentials=False,
    max_age=86400,
)


_ALLOWED_ORIGIN_RE = re.compile(
    r"^https?://(
    r"localhost(:\d+)?|127\.0\.0\.1(:\d+)?|
    r".*\.lovable\.app|.*\.lovableproject\.com|.*\.lovable\.dev"
    r")$",
    re.IGNORECASE,
)


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin and _ALLOWED_ORIGIN_RE.match(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    elif not origin:
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
    response.headers.setdefault("Access-Control-Expose-Headers", "Content-Type, Content-Disposition")
    response.headers.setdefault("Access-Control-Max-Age", "86400")
    return response


@app.before_request
def handle_preflight_requests():
    if request.method == "OPTIONS":
        return ("", 204)


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
UPLOAD_DIR = BASE_DIR / "uploads"
SRT_DIR = BASE_DIR / "srt"
AUDIO_DIR = BASE_DIR / "audio"
TTS_DIR = BASE_DIR / "tts"
SCRIPT_DIR = BASE_DIR / "scripts"
COOKIE_FILE = BASE_DIR / "cookies.txt"
GENERATED_COOKIE_FILE = Path(os.getenv("YOUTUBE_COOKIES_GENERATED_FILE", "/tmp/youtube_cookies.txt"))
for directory in (DOWNLOAD_DIR, UPLOAD_DIR, SRT_DIR, AUDIO_DIR, TTS_DIR, SCRIPT_DIR):
    directory.mkdir(exist_ok=True)


def get_public_base_url() -> str:
    """Return a stable public HTTPS base URL for download links.

    Railway/Flask may see the internal request as http and return http:// URLs.
    Browser downloads from Lovable are more reliable when we return the public
    HTTPS hostname explicitly. PUBLIC_BASE_URL can override this if the domain
    changes.
    """
    configured = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    host = request.headers.get("X-Forwarded-Host") or request.host
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
    if host.endswith(".up.railway.app"):
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def build_public_url(path: str) -> str:
    path = "/" + (path or "").lstrip("/")
    return f"{get_public_base_url()}{path}"


def safe_download_name(requested_name: str | None, fallback_name: str, expected_ext: str | None = None) -> str:
    """Return a browser-safe filename for Content-Disposition."""
    raw = (requested_name or fallback_name or "download").strip()
    raw = Path(raw).name
    raw = secure_filename(raw) or secure_filename(fallback_name or "download") or "download"
    if expected_ext:
        ext = expected_ext if expected_ext.startswith(".") else f".{expected_ext}"
        if not raw.lower().endswith(ext.lower()):
            raw = f"{Path(raw).stem}{ext}"
    return raw


def add_download_name(url: str, download_name: str | None) -> str:
    """Append ?download_name=... so cross-origin downloads keep friendly names."""
    if not url or not download_name:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["download_name"] = download_name
    return urlunparse(parsed._replace(query=urlencode(query)))


def requested_download_name(fallback_name: str, expected_ext: str | None = None) -> str:
    return safe_download_name(
        request.args.get("download_name") or request.args.get("name") or request.args.get("filename"),
        fallback_name,
        expected_ext,
    )


def tts_pause_friendly_text(text: str) -> str:
    """Add light Myanmar sentence endings to improve Edge-TTS pauses."""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    out: list[str] = []
    for raw_line in cleaned.split("\n"):
        line = raw_line.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        line = re.sub(r"\s+\u100B\s*", "\u100B ", line)
        line = re.sub(r"\s*([!?])\s*", r"\1 ", line).strip()
        if len(line) >= 18 and not END_PUNCT_RE.search(line):
            line = f"{line}။"
        out.append(line)
    return "\n".join(out).strip()


app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "250")) * 1024 * 1024

ALLOWED_UPLOAD_EXTENSIONS = {
    "mp4", "mov", "m4v", "mkv", "webm", "avi",
    "mp3", "m4a", "wav", "aac", "ogg", "flac",
}
ALLOWED_SRT_EXTENSIONS = {"srt", "vtt", "txt"}
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
SRT_TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}.*$"
)
END_PUNCT_RE = re.compile(r"[။.!?…]$")
YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,my;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    # Helps avoid the consent interstitial on fresh datacenter sessions.
    "Cookie": os.getenv("YOUTUBE_CAPTION_COOKIE_HEADER", "CONSENT=YES+cb"),
}

YOUTUBE_INNERTUBE_DEFAULT_API_KEY = os.getenv(
    "YOUTUBE_INNERTUBE_API_KEY",
    # Public web client key. Can be overridden from Railway Variables if YouTube changes it.
    "AIzaSyAO_FJ2SLqU8Q4STEHB_Wcilw_Y9_11qcW8",
)

YOUTUBE_INNERTUBE_CLIENTS = [
    {
        "label": "WEB",
        "clientName": "WEB",
        "clientVersion": os.getenv("YOUTUBE_WEB_CLIENT_VERSION", "2.20240726.00.00"),
        "hl": "en",
        "gl": "US",
    },
    {
        "label": "MWEB",
        "clientName": "MWEB",
        "clientVersion": os.getenv("YOUTUBE_MWEB_CLIENT_VERSION", "2.20240726.00.00"),
        "hl": "en",
        "gl": "US",
    },
    {
        "label": "WEB_EMBEDDED_PLAYER",
        "clientName": "WEB_EMBEDDED_PLAYER",
        "clientVersion": os.getenv("YOUTUBE_WEB_EMBEDDED_CLIENT_VERSION", "1.20240723.01.00"),
        "hl": "en",
        "gl": "US",
        "thirdParty": {"embedUrl": "https://www.youtube.com/"},
    },
]


class YTDLPLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        print(f"yt-dlp warning: {msg}", flush=True)

    def error(self, msg):
        print(f"yt-dlp error: {msg}", flush=True)


def json_error(message: str, status_code: int = 500, **extra):
    payload = {"ok": False, "success": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status_code


def allowed_upload_filename(filename: str) -> bool:
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS)


def allowed_srt_filename(filename: str) -> bool:
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_SRT_EXTENSIONS)


def save_uploaded_media(file_storage) -> Path:
    if not file_storage or not getattr(file_storage, "filename", ""):
        raise ValueError("Missing uploaded file")
    filename = secure_filename(file_storage.filename)
    if not allowed_upload_filename(filename):
        raise ValueError("Unsupported file type. Upload MP4, MOV, MKV, WEBM, MP3, M4A, WAV, AAC, OGG, or FLAC.")
    ext = filename.rsplit(".", 1)[1].lower()
    upload_path = UPLOAD_DIR / f"{Path(filename).stem}_{uuid.uuid4().hex[:8]}.{ext}"
    file_storage.save(upload_path)
    if not upload_path.exists() or upload_path.stat().st_size == 0:
        raise RuntimeError("Uploaded file was empty or could not be saved")
    return upload_path


def read_uploaded_srt(file_storage) -> tuple[str | None, str | None]:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None
    filename = secure_filename(file_storage.filename)
    if not allowed_srt_filename(filename):
        raise ValueError("Unsupported subtitle file. Upload .srt, .vtt, or .txt.")
    raw = file_storage.read()
    text = raw.decode("utf-8", errors="replace")
    lower_name = filename.lower()
    if lower_name.endswith(".vtt"):
        text = vtt_to_srt_text(text)
    if "-->" not in text and lower_name.endswith(".txt"):
        text = text_to_basic_srt(text)
    if not text.strip():
        raise ValueError("Uploaded subtitle file is empty")
    return text, filename


def text_to_basic_srt(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    blocks = []
    start = 0.0
    for idx, line in enumerate(lines, start=1):
        end = start + 3.0
        blocks.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{line}\n")
        start = end
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def normalize_youtube_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.strip("/")
    video_id = None
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"} and path.startswith("shorts/"):
        video_id = path.split("/")[1]
    elif host == "youtu.be" and path:
        video_id = path.split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"} and (path.startswith("embed/") or path.startswith("live/")):
        video_id = path.split("/")[1]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    if video_id:
        video_id = re.sub(r"[^0-9A-Za-z_-]", "", video_id)
        if not video_id:
            raise ValueError("Invalid YouTube video id")
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def get_youtube_video_id(url: str) -> str | None:
    try:
        normalized = normalize_youtube_url(url)
        parsed = urlparse(normalized)
        return parse_qs(parsed.query).get("v", [None])[0]
    except Exception:
        return None


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.netloc or "").lower().replace("www.", "")
        return host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"}
    except Exception:
        return False


def get_cookie_file() -> Path | None:
    cookie_b64 = os.getenv("YOUTUBE_COOKIES_B64") or os.getenv("YOUTUBE_COOKIES_BASE64")
    cookie_text = os.getenv("YOUTUBE_COOKIES_TXT")
    try:
        if cookie_b64:
            GENERATED_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            decoded = base64.b64decode(cookie_b64).decode("utf-8", errors="replace")
            GENERATED_COOKIE_FILE.write_text(decoded, encoding="utf-8")
            GENERATED_COOKIE_FILE.chmod(0o600)
            if GENERATED_COOKIE_FILE.stat().st_size > 0:
                return GENERATED_COOKIE_FILE
        if cookie_text:
            GENERATED_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            GENERATED_COOKIE_FILE.write_text(cookie_text, encoding="utf-8")
            GENERATED_COOKIE_FILE.chmod(0o600)
            if GENERATED_COOKIE_FILE.stat().st_size > 0:
                return GENERATED_COOKIE_FILE
    except Exception as exc:
        print(f"cookie setup warning: {exc}", flush=True)
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0:
        return COOKIE_FILE
    return None


_YOUTUBE_COOKIE_HEADER_CACHE: str | None = None


def _parse_netscape_cookie_file_to_header(cookie_path: Path | None) -> str:
    """Convert a Netscape cookies.txt file into a Cookie header for direct YouTube caption requests."""
    if not cookie_path or not cookie_path.exists() or cookie_path.stat().st_size <= 0:
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    try:
        for raw_line in cookie_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = (raw_line or "").strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _flag, _path, _secure, _expires, name, value = parts[:7]
                domain_low = (domain or "").lower()
                if "youtube.com" not in domain_low and "google.com" not in domain_low:
                    continue
                if not name or name in seen:
                    continue
                seen.add(name)
                pairs.append(f"{name}={value}")
            elif "=" in line and ";" in line:
                # Accept an already-built Cookie header if the env/file contains one.
                for chunk in line.split(";"):
                    chunk = chunk.strip()
                    if not chunk or "=" not in chunk:
                        continue
                    name, value = chunk.split("=", 1)
                    if name and name not in seen:
                        seen.add(name)
                        pairs.append(f"{name}={value}")
    except Exception as exc:
        print(f"cookie header parse warning: {exc}", flush=True)
    return "; ".join(pairs)


def get_youtube_cookie_header() -> str:
    """Prefer explicit Cookie header env, then parse cookies.txt, then use consent-only fallback."""
    global _YOUTUBE_COOKIE_HEADER_CACHE
    explicit = (os.getenv("YOUTUBE_CAPTION_COOKIE_HEADER") or "").strip()
    if explicit:
        return explicit
    if _YOUTUBE_COOKIE_HEADER_CACHE is not None:
        return _YOUTUBE_COOKIE_HEADER_CACHE
    parsed = _parse_netscape_cookie_file_to_header(get_cookie_file())
    _YOUTUBE_COOKIE_HEADER_CACHE = parsed
    return parsed


def build_youtube_request_headers(json_payload: bool = False) -> dict:
    headers = dict(YOUTUBE_HEADERS)
    cookie_header = get_youtube_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    elif not headers.get("Cookie"):
        headers["Cookie"] = "CONSENT=YES+cb"
    if json_payload:
        headers.update({
            "Content-Type": "application/json",
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        })
    return headers


def friendly_youtube_error(error: Exception) -> tuple[str, int, dict]:
    message = str(error)
    lowered = message.lower()
    extra = {
        "needs_upload": True,
        "fallback_message": "Upload an SRT/VTT file, or use the Upload tab with content you have rights to process.",
        "fallback_options": ["Upload .srt/.vtt", "Upload video/audio", "Try another video with public captions"],
    }
    if "drm" in lowered or "drm protected" in lowered:
        extra.update({"drm_protected": True, "bypass_supported": False, "audio_available": False, "subtitle_only_possible": True})
        return (
            "This YouTube video is DRM protected for media download. This app cannot bypass DRM. "
            "If public captions exist, the SRT-first flow can still use them; otherwise use manual SRT/upload fallback.",
            451,
            extra,
        )
    if "sign in to confirm" in lowered or "not a bot" in lowered or "use --cookies" in lowered or "cookies" in lowered:
        return ("YouTube is rejecting Railway/datacenter access for this request. Upload/manual SRT fallback may be required.", 403, extra)
    if "requested format is not available" in lowered or "only images are available" in lowered or "no video formats" in lowered:
        return ("YouTube did not expose a downloadable audio/video format from Railway. Captions may still work if public.", 502, extra)
    return (message or "Unknown YouTube error", 500, extra)


def build_ydl_opts(
    output_template: str = "%(title)s-%(id)s",
    format_spec: str = "bestaudio/best",
    extract_audio: bool = True,
) -> dict:
    return {
        "quiet": False,
        "no_warnings": True,
        "logger": YTDLPLogger(),
        "outtmpl": output_template,
        "format": format_spec,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "extract_audio": extract_audio,
        "audio_format": "mp3",
        "audio_quality": "192",
    }


def download_audio_as_mp3(url: str) -> tuple[Path, dict]:
    normalized = normalize_youtube_url(url)
    with yt_dlp.YoutubeDL(build_ydl_opts(output_template=str(AUDIO_DIR / "%(title)s-%(id)s"))) as ydl:
        info = ydl.extract_info(normalized, download=True)
    audio_file = AUDIO_DIR / f"{info['title']}-{info['id']}.mp3"
    if not audio_file.exists():
        raise FileNotFoundError("Downloaded audio file not found")
    return audio_file, info


def srt_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@app.get("/")
def index():
    return {
        "ok": True,
        "service": "video-audio-tool",
        "status": "running"
    }, 200


@app.get("/health")
def health():
    return {"ok": True, "status": "healthy"}, 200


@app.post("/download")
def download():
    return json_error("POST /download not yet implemented", 501)


@app.get("/debug-youtube-captions")
def debug_youtube_captions():
    return json_error("GET /debug-youtube-captions not yet implemented", 501)


@app.post("/extract-srt")
def extract_srt():
    return json_error("POST /extract-srt not yet implemented", 501)


@app.post("/translate-srt")
def translate_srt():
    return json_error("POST /translate-srt not yet implemented", 501)


@app.post("/upload-to-mp3")
def upload_to_mp3():
    return json_error("POST /upload-to-mp3 not yet implemented", 501)


@app.post("/extract-srt-upload")
def extract_srt_upload():
    return json_error("POST /extract-srt-upload not yet implemented", 501)


@app.post("/process-upload")
def process_upload():
    return json_error("POST /process-upload not yet implemented", 501)


@app.post("/rewrite")
def rewrite():
    return json_error("POST /rewrite not yet implemented", 501)


@app.get("/rewrite-options")
def rewrite_options():
    return json_error("GET /rewrite-options not yet implemented", 501)


@app.post("/tts")
def tts():
    return json_error("POST /tts not yet implemented", 501)


@app.post("/final-srt")
def final_srt():
    return json_error("POST /final-srt not yet implemented", 501)


@app.get("/audio/<filename>")
def serve_audio(filename):
    try:
        return send_from_directory(AUDIO_DIR, secure_filename(filename))
    except Exception as e:
        return json_error(f"Audio file not found: {e}", 404)


@app.get("/srt/<filename>")
def serve_srt(filename):
    try:
        return send_from_directory(SRT_DIR, secure_filename(filename))
    except Exception as e:
        return json_error(f"SRT file not found: {e}", 404)


@app.get("/tts/<filename>")
def serve_tts(filename):
    try:
        return send_from_directory(TTS_DIR, secure_filename(filename))
    except Exception as e:
        return json_error(f"TTS file not found: {e}", 404)


@app.get("/script/<filename>")
def serve_script(filename):
    try:
        return send_from_directory(SCRIPT_DIR, secure_filename(filename))
    except Exception as e:
        return json_error(f"Script file not found: {e}", 404)


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(exc):
    max_mb = int(os.getenv("MAX_UPLOAD_MB", "250"))
    return json_error(f"File too large. Max {max_mb}MB.", 413)


@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    return json_error(exc.description or str(exc), exc.code or 500)


@app.errorhandler(Exception)
def handle_unexpected_exception(exc):
    print(f"Unexpected error: {exc}", flush=True)
    return json_error(str(exc) or "Internal server error", 500)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
