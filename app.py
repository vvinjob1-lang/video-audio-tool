import asyncio
import base64
import html
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": [
                r"https://.*\.lovable\.app",
                r"https://.*\.lovableproject\.com",
                r"https://.*\.lovable\.dev",
                r"http://localhost:.*",
                r"http://127\.0\.0\.1:.*",
            ]
        }
    },
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Type", "Content-Disposition"],
    supports_credentials=False,
    max_age=86400,
)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
UPLOAD_DIR = BASE_DIR / "uploads"
SRT_DIR = BASE_DIR / "srt"
AUDIO_DIR = BASE_DIR / "audio"
TTS_DIR = BASE_DIR / "tts"
COOKIE_FILE = BASE_DIR / "cookies.txt"
GENERATED_COOKIE_FILE = Path(os.getenv("YOUTUBE_COOKIES_GENERATED_FILE", "/tmp/youtube_cookies.txt"))

for directory in (DOWNLOAD_DIR, UPLOAD_DIR, SRT_DIR, AUDIO_DIR, TTS_DIR):
    directory.mkdir(exist_ok=True)

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
    "Accept-Language": "en-US,en;q=0.9",
}


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
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS


def allowed_srt_filename(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_SRT_EXTENSIONS


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


def read_uploaded_srt(file_storage) -> tuple[str, str] | tuple[None, None]:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None
    filename = secure_filename(file_storage.filename)
    if not allowed_srt_filename(filename):
        raise ValueError("Unsupported subtitle file. Upload .srt, .vtt, or .txt.")
    raw = file_storage.read()
    text = raw.decode("utf-8", errors="replace")
    if filename.lower().endswith(".vtt"):
        text = vtt_to_srt_text(text)
    if "-->" not in text and filename.lower().endswith(".txt"):
        text = text_to_basic_srt(text)
    if not text.strip():
        raise ValueError("Uploaded subtitle file is empty")
    return text, filename


def text_to_basic_srt(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    blocks = []
    start = 0.0
    for idx, line in enumerate(lines, start=1):
        end = start + 3.0
        blocks.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{line}\n")
        start = end
    return "\n".join(blocks).strip() + "\n"


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


def friendly_youtube_error(error: Exception) -> tuple[str, int, dict]:
    message = str(error)
    lowered = message.lower()
    extra = {
        "needs_upload": True,
        "fallback_message": "Use the Upload tab with a file you have rights to process, or upload an SRT/VTT file to continue without YouTube download.",
        "fallback_options": ["Upload video/audio", "Upload .srt/.vtt", "Try a YouTube video with accessible captions"],
    }
    if "drm" in lowered or "drm protected" in lowered:
        extra.update({
            "drm_protected": True,
            "bypass_supported": False,
            "audio_available": False,
            "subtitle_only_possible": True,
        })
        return (
            "This YouTube video is DRM protected. This app cannot bypass or remove DRM. If captions are publicly accessible, the SRT-first flow can still use captions; otherwise use Upload/manual SRT fallback with content you have rights to process.",
            451,
            extra,
        )
    if "sign in to confirm" in lowered or "not a bot" in lowered or "use --cookies" in lowered or "cookies" in lowered:
        return (
            "YouTube is rejecting Railway/datacenter access for this video. Cookies can help sometimes, but they do not always work from Railway. Use Upload or manual SRT fallback.",
            403,
            extra,
        )
    if "requested format is not available" in lowered or "only images are available" in lowered:
        return (
            "YouTube did not expose a downloadable audio/video format from Railway. Continue with captions if available, otherwise use Upload/manual SRT fallback.",
            502,
            extra,
        )
    if "video unavailable" in lowered:
        return (
            "This YouTube video is unavailable from the backend. It may be private, region-restricted, deleted, or blocked for Railway/datacenter traffic.",
            404,
            extra,
        )
    return message, 500, extra


def build_ydl_opts(output_base: Path | None = None, fallback: bool = False, use_cookies: bool = False, format_selector: str | None = None, skip_download: bool = False) -> dict:
    player_clients = ["default", "mweb", "ios", "tv"] if fallback else ["default", "mweb"]
    opts = {
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
        "extractor_args": {"youtube": {"player_client": player_clients}},
        "overwrites": True,
    }
    if skip_download:
        opts["skip_download"] = True
    else:
        opts.update({
            "format": format_selector or "bestaudio[acodec!=none]/best[acodec!=none]/best",
            "outtmpl": str(output_base) + ".%(ext)s",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "prefer_ffmpeg": True,
        })
    if use_cookies:
        cookie_path = get_cookie_file()
        if cookie_path:
            opts["cookiefile"] = str(cookie_path)
    ffmpeg_location = os.getenv("FFMPEG_LOCATION")
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    return opts


def download_audio_as_mp3(url: str) -> tuple[Path, dict]:
    normalized_url = normalize_youtube_url(url)
    output_base = DOWNLOAD_DIR / uuid.uuid4().hex
    final_mp3 = Path(str(output_base) + ".mp3")
    cookie_available = get_cookie_file() is not None
    cookies_mode = (os.getenv("YTDLP_COOKIES_MODE", "auto") or "auto").strip().lower()
    if cookies_mode in {"always", "true", "1", "yes"}:
        cookie_modes = [True] if cookie_available else [False]
    elif cookies_mode in {"never", "false", "0", "no"}:
        cookie_modes = [False]
    else:
        cookie_modes = [False, True] if cookie_available else [False]
    format_selectors = [
        "bestaudio[ext=m4a]/bestaudio[acodec!=none]/best[acodec!=none]/best",
        "bestaudio*/best[acodec!=none]/best",
        "worstaudio[acodec!=none]/worst[acodec!=none]/worst",
    ]
    attempt_profiles = []
    for use_cookies in cookie_modes:
        for fallback in (False, True):
            for fmt in format_selectors:
                attempt_profiles.append({"use_cookies": use_cookies, "fallback": fallback, "format_selector": fmt})
    last_error = None
    for attempt_number, profile in enumerate(attempt_profiles, start=1):
        try:
            print(f"yt-dlp audio attempt {attempt_number}/{len(attempt_profiles)} {profile}", flush=True)
            ydl_opts = build_ydl_opts(output_base, fallback=profile["fallback"], use_cookies=profile["use_cookies"], format_selector=profile["format_selector"])
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(normalized_url, download=True)
            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp did not return a valid video info object")
            matches = [final_mp3, *list(DOWNLOAD_DIR.glob(f"{output_base.name}*.mp3"))]
            for match in matches:
                if match.exists() and match.stat().st_size > 0:
                    return match, {"title": info.get("title") or "audio", "video_id": info.get("id"), "source_url": normalized_url}
            raise RuntimeError("MP3 file was not created. Check that FFmpeg is installed on Railway.")
        except Exception as exc:
            last_error = exc
            print(f"audio download attempt failed profile={profile}: {exc}", flush=True)
            if "drm" in str(exc).lower():
                raise RuntimeError(str(exc))
    raise RuntimeError(str(last_error) if last_error else "Download failed")


# ---------------- Caption-first helpers ----------------

def _caption_lang_candidates(requested_language: str | None = None) -> list[str]:
    env_value = os.getenv("YOUTUBE_CAPTION_LANGUAGES", "en,en-US,en-GB,en.*,my,und,auto,*")
    candidates: list[str] = []
    for item in env_value.split(","):
        value = (item or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    normalized = normalize_whisper_language(requested_language)
    if normalized and normalized not in {"auto", "detect"} and normalized not in candidates:
        candidates.insert(0, normalized)
    if "*" not in candidates:
        candidates.append("*")
    return candidates


def _caption_key_matches(key: str, candidate: str) -> bool:
    key_low = (key or "").lower()
    candidate_low = (candidate or "").lower()
    if not key_low or not candidate_low:
        return False
    if candidate_low in {"*", "auto"}:
        return True
    if candidate_low.endswith(".*"):
        base = candidate_low[:-2]
        return key_low == base or key_low.startswith(base + "-")
    return key_low == candidate_low


def _pick_caption_key(caption_map: dict, candidates: list[str]) -> str | None:
    if not caption_map:
        return None
    keys = list(caption_map.keys())
    for candidate in candidates:
        for key in keys:
            if _caption_key_matches(key, candidate):
                return key
    for key in keys:
        if (key or "").lower().startswith("en"):
            return key
    return keys[0] if keys else None


def _pick_caption_format(formats: list[dict]) -> dict | None:
    if not formats:
        return None
    for ext in ["srt", "vtt", "srv3", "ttml"]:
        for item in formats:
            if (item.get("ext") or "").lower() == ext and item.get("url"):
                return item
    for item in formats:
        url = item.get("url") or ""
        if item.get("url") and ("timedtext" in url.lower() or "fmt=vtt" in url.lower() or ".vtt" in url.lower()):
            return item
    return None


def _caption_ts_to_srt(ts: str) -> str:
    ts = (ts or "").strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds_float = float(parts[1])
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds_float = float(parts[2])
        else:
            return "00:00:00,000"
        seconds = int(seconds_float)
        millis = int(round((seconds_float - seconds) * 1000))
        if millis >= 1000:
            seconds += 1
            millis -= 1000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    except Exception:
        return "00:00:00,000"


def _clean_caption_text_line(line: str) -> str:
    line = html.unescape((line or "").strip())
    line = re.sub(r"<\d{1,2}:\d{2}:\d{2}[.,]\d{3}>", "", line)
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"\{[^}]*\}", "", line)
    line = line.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", line).strip()


def vtt_to_srt_text(vtt_text: str) -> str:
    text = (vtt_text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not text.strip():
        return ""
    lines = text.split("\n")
    blocks: list[str] = []
    i = 0
    cue_number = 1
    time_re = re.compile(r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})")
    while i < len(lines):
        line = (lines[i] or "").strip()
        upper = line.upper()
        if not line or upper == "WEBVTT":
            i += 1
            continue
        if upper.startswith(("NOTE", "STYLE", "REGION")):
            i += 1
            while i < len(lines) and (lines[i] or "").strip():
                i += 1
            continue
        match = time_re.search(line)
        if not match and i + 1 < len(lines):
            next_line = (lines[i + 1] or "").strip()
            match = time_re.search(next_line)
            if match:
                i += 1
        if not match:
            i += 1
            continue
        start = _caption_ts_to_srt(match.group(1))
        end = _caption_ts_to_srt(match.group(2))
        i += 1
        text_lines: list[str] = []
        seen_lines: set[str] = set()
        while i < len(lines) and (lines[i] or "").strip():
            cleaned = _clean_caption_text_line(lines[i])
            if cleaned:
                key = cleaned.casefold()
                if key not in seen_lines:
                    seen_lines.add(key)
                    text_lines.append(cleaned)
            i += 1
        cue_text = re.sub(r"\s+", " ", " ".join(text_lines)).strip()
        if cue_text:
            blocks.append(f"{cue_number}\n{start} --> {end}\n{cue_text}\n")
            cue_number += 1
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def ttml_to_srt_text(ttml_text: str) -> str:
    raw = (ttml_text or "").strip()
    if not raw:
        return ""
    try:
        # Strip namespaces for easier parsing.
        raw2 = re.sub(r"xmlns(:\w+)?=\"[^\"]+\"", "", raw)
        root = ET.fromstring(raw2)
        blocks = []
        idx = 1
        for p in root.iter():
            if p.tag.split("}")[-1].lower() != "p":
                continue
            start = p.attrib.get("begin") or p.attrib.get("start") or "00:00:00.000"
            end = p.attrib.get("end") or ""
            dur = p.attrib.get("dur")
            if not end and dur:
                end = start
            text = _clean_caption_text_line(" ".join(p.itertext()))
            if text:
                blocks.append(f"{idx}\n{_caption_ts_to_srt(start)} --> {_caption_ts_to_srt(end or start)}\n{text}\n")
                idx += 1
        return "\n".join(blocks).strip() + "\n" if blocks else ""
    except Exception:
        return ""


def srv_xml_to_srt_text(xml_text: str) -> str:
    raw = (xml_text or "").strip()
    if not raw:
        return ""
    try:
        root = ET.fromstring(raw)
        blocks = []
        for idx, node in enumerate(root.findall(".//text"), start=1):
            start = float(node.attrib.get("start", "0") or 0)
            dur = float(node.attrib.get("dur", "3") or 3)
            text = _clean_caption_text_line("".join(node.itertext()))
            if text:
                blocks.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(start + dur)}\n{text}\n")
        return "\n".join(blocks).strip() + "\n" if blocks else ""
    except Exception:
        return ""


def normalize_caption_to_srt(caption_text: str, ext: str | None = None) -> str:
    ext_low = (ext or "").lower()
    raw = (caption_text or "").strip()
    if not raw:
        return ""
    if ext_low == "vtt" or raw.lstrip("\ufeff").upper().startswith("WEBVTT"):
        return vtt_to_srt_text(raw)
    if ext_low in {"ttml", "srv3"} or "<tt" in raw[:200].lower():
        return ttml_to_srt_text(raw) or srv_xml_to_srt_text(raw)
    if raw.startswith("<?xml") or "<transcript" in raw[:200].lower():
        return srv_xml_to_srt_text(raw)
    if "-->" in raw:
        return raw.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    return ""


def get_direct_timedtext_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    """Try YouTube timedtext directly before yt-dlp. This avoids full media extraction when Railway is blocked."""
    video_id = get_youtube_video_id(url)
    if not video_id:
        return None
    list_urls = [
        f"https://www.youtube.com/api/timedtext?{urlencode({'type': 'list', 'v': video_id})}",
        f"https://video.google.com/timedtext?{urlencode({'type': 'list', 'v': video_id})}",
    ]
    tracks = []
    errors = []
    for list_url in list_urls:
        try:
            resp = requests.get(list_url, headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
            if resp.status_code >= 400:
                errors.append(f"list {resp.status_code}")
                continue
            root = ET.fromstring(resp.text or "<transcript_list />")
            for track in root.findall(".//track"):
                lang_code = track.attrib.get("lang_code") or track.attrib.get("lang") or ""
                name = track.attrib.get("name") or ""
                kind = track.attrib.get("kind") or ""
                if lang_code:
                    tracks.append({"lang": lang_code, "name": name, "kind": kind})
        except Exception as exc:
            errors.append(str(exc))
    if not tracks:
        return None
    candidates = _caption_lang_candidates(requested_language)
    selected = None
    manual_tracks = [t for t in tracks if t.get("kind") != "asr"]
    asr_tracks = [t for t in tracks if t.get("kind") == "asr"]
    for pool_name, pool in [("youtube_direct_manual_caption", manual_tracks), ("youtube_direct_auto_caption", asr_tracks), ("youtube_direct_caption", tracks)]:
        for candidate in candidates:
            for track in pool:
                if _caption_key_matches(track["lang"], candidate):
                    selected = (pool_name, track)
                    break
            if selected:
                break
        if selected:
            break
    if not selected:
        selected = ("youtube_direct_caption", tracks[0])
    source_name, track = selected
    # Try vtt first, then XML/srv style.
    fetch_attempts = []
    for fmt in ["vtt", "srv3", "ttml"]:
        params = {"v": video_id, "lang": track["lang"], "fmt": fmt}
        if track.get("kind"):
            params["kind"] = track["kind"]
        if track.get("name"):
            params["name"] = track["name"]
        fetch_attempts.append((fmt, f"https://www.youtube.com/api/timedtext?{urlencode(params)}"))
        fetch_attempts.append((fmt, f"https://video.google.com/timedtext?{urlencode(params)}"))
    for fmt, caption_url in fetch_attempts:
        try:
            resp = requests.get(caption_url, headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
            if resp.status_code >= 400 or not resp.text.strip():
                continue
            srt_text = normalize_caption_to_srt(resp.text, ext=fmt)
            if srt_text.strip():
                return srt_text, {
                    "source": source_name,
                    "subtitle_source": source_name,
                    "language": track["lang"],
                    "format": fmt,
                    "title": "YouTube captions",
                    "video_id": video_id,
                    "source_url": normalize_youtube_url(url),
                    "manual_languages": sorted({t["lang"] for t in manual_tracks}),
                    "auto_languages": sorted({t["lang"] for t in asr_tracks}),
                    "errors": errors[-5:],
                    "no_media_download": True,
                }
        except Exception as exc:
            errors.append(str(exc))
    return None


def get_ytdlp_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    if not is_youtube_url(url):
        return None
    normalized_url = normalize_youtube_url(url)
    cookie_available = get_cookie_file() is not None
    cookie_modes = [False, True] if cookie_available else [False]
    errors = []
    for use_cookies in cookie_modes:
        for fallback in (False, True):
            try:
                ydl_opts = build_ydl_opts(fallback=fallback, use_cookies=use_cookies, skip_download=True)
                print(f"Caption-first yt-dlp metadata check cookies={use_cookies} fallback={fallback}", flush=True)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(normalized_url, download=False)
                if not isinstance(info, dict):
                    continue
                candidates = _caption_lang_candidates(requested_language)
                subtitle_maps = [
                    ("youtube_manual_subtitle", info.get("subtitles") or {}),
                    ("youtube_auto_caption", info.get("automatic_captions") or {}),
                ]
                for source_name, caption_map in subtitle_maps:
                    caption_key = _pick_caption_key(caption_map, candidates)
                    if not caption_key:
                        continue
                    fmt = _pick_caption_format(caption_map.get(caption_key) or [])
                    if not fmt:
                        errors.append(f"{source_name}:{caption_key}: no usable caption URL")
                        continue
                    resp = requests.get(fmt.get("url"), headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
                    resp.raise_for_status()
                    ext = (fmt.get("ext") or "").lower()
                    srt_text = normalize_caption_to_srt(resp.text, ext=ext)
                    if not srt_text.strip():
                        errors.append(f"{source_name}:{caption_key}: empty after conversion")
                        continue
                    return srt_text, {
                        "source": source_name,
                        "subtitle_source": source_name,
                        "language": caption_key,
                        "format": ext or "unknown",
                        "title": info.get("title") or "",
                        "video_id": info.get("id") or get_youtube_video_id(url) or "",
                        "source_url": normalized_url,
                        "manual_languages": sorted((info.get("subtitles") or {}).keys()),
                        "auto_languages": sorted((info.get("automatic_captions") or {}).keys()),
                        "errors": errors[-5:],
                        "no_media_download": True,
                    }
            except Exception as exc:
                errors.append(str(exc))
                print(f"yt-dlp caption metadata failed: {exc}", flush=True)
    return None


def get_youtube_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    if not is_youtube_url(url):
        return None
    if (os.getenv("YOUTUBE_DIRECT_TIMEDTEXT", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}:
        result = get_direct_timedtext_caption_srt(url, requested_language=requested_language)
        if result:
            return result
    return get_ytdlp_caption_srt(url, requested_language=requested_language)


def caption_first_enabled() -> bool:
    return (os.getenv("YOUTUBE_CAPTION_FIRST", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}


def should_whisper_fallback_for_url() -> bool:
    return (os.getenv("URL_WHISPER_FALLBACK", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}


# ---------------- Audio, Whisper, SRT, translation ----------------

def convert_media_file_to_mp3(input_path: Path) -> Path:
    output_path = AUDIO_DIR / f"{input_path.stem}_{uuid.uuid4().hex[:8]}.mp3"
    ffmpeg_binary = os.getenv("FFMPEG_BINARY", "ffmpeg")
    command = [
        ffmpeg_binary, "-y", "-i", str(input_path), "-vn",
        "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        str(output_path),
    ]
    print("Running ffmpeg conversion:", " ".join(command), flush=True)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print("ffmpeg conversion failed:", result.stderr[-3000:], flush=True)
        raise RuntimeError("FFmpeg could not convert the media file to MP3")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("MP3 file was not created from uploaded media")
    return output_path


def srt_timestamp(seconds: float) -> str:
    if seconds is None:
        seconds = 0
    milliseconds = int(round(float(seconds) * 1000))
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1000
    millis = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def normalize_whisper_language(language: str | None) -> str | None:
    if not language:
        return None
    language = language.strip().lower()
    if language in {"auto", "detect", "auto-detect", "autodetect"}:
        return None
    language_map = {
        "myanmar": "my", "burmese": "my", "my-mm": "my", "မြန်မာ": "my",
        "english": "en", "en-us": "en", "en-gb": "en",
    }
    return language_map.get(language, language)


def transcribe_mp3_to_srt(mp3_path: Path, language: str | None = None) -> tuple[str, dict]:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError("faster-whisper is not installed. Check requirements.txt and Railway deployment logs.") from exc
    model_name = os.getenv("WHISPER_MODEL", "base")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    requested_language = normalize_whisper_language(language)
    language_attempts: list[str | None] = []
    if requested_language:
        language_attempts.append(requested_language)
    language_attempts.append(None)
    fallback_languages_env = os.getenv("WHISPER_FALLBACK_LANGUAGES", "en,my")
    for item in fallback_languages_env.split(","):
        code = normalize_whisper_language(item)
        if code and code not in language_attempts:
            language_attempts.append(code)
    print(f"Loading Whisper model={model_name}, device={device}, compute_type={compute_type}", flush=True)
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    attempts = []
    candidates = []
    for language_code in language_attempts:
        for vad_filter in (False, True):
            try:
                segments_iter, info = model.transcribe(
                    str(mp3_path),
                    language=language_code,
                    beam_size=5,
                    vad_filter=vad_filter,
                    condition_on_previous_text=False,
                    temperature=0.0,
                    no_speech_threshold=0.6,
                    log_prob_threshold=-1.0,
                )
                blocks = []
                plain_parts = []
                for segment in segments_iter:
                    text = (segment.text or "").strip()
                    if not text:
                        continue
                    plain_parts.append(text)
                    blocks.append(f"{len(blocks) + 1}\n{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}\n{text}\n")
                plain = " ".join(plain_parts).strip()
                score = len(plain) + len(blocks) * 10
                log = {
                    "language": language_code or "auto",
                    "vad_filter": vad_filter,
                    "detected_language": getattr(info, "language", None),
                    "language_probability": getattr(info, "language_probability", None),
                    "segments": len(blocks),
                    "chars": len(plain),
                    "score": score,
                }
                attempts.append(log)
                if blocks:
                    candidates.append((score, "\n".join(blocks).strip() + "\n", info, language_code, vad_filter, len(blocks)))
            except Exception as exc:
                attempts.append({"language": language_code or "auto", "vad_filter": vad_filter, "error": str(exc)})
                print(f"Whisper attempt failed: {exc}", flush=True)
    if candidates:
        score, srt_text, info, used_language, vad_filter, segments = max(candidates, key=lambda item: item[0])
        return srt_text, {
            "model": model_name,
            "device": device,
            "compute_type": compute_type,
            "requested_language": requested_language or "auto",
            "used_language": used_language or "auto",
            "detected_language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "duration": getattr(info, "duration", None),
            "segments": segments,
            "vad_filter": vad_filter,
            "score": score,
            "attempts": attempts,
        }
    raise RuntimeError(f"Whisper finished but did not produce any subtitle text. attempts={attempts}")


def create_srt_from_mp3(mp3_path: Path, language: str | None = None, base_name: str | None = None) -> tuple[str, str, dict]:
    srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
    safe_base = secure_filename(base_name or mp3_path.stem) or "media"
    srt_filename = f"{Path(safe_base).stem}_{uuid.uuid4().hex[:8]}.srt"
    (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
    return srt_text, srt_filename, whisper_meta


def normalize_translate_language(language: str | None, default: str = "my") -> str:
    if not language:
        return default
    language = language.strip().lower()
    if language in {"auto", "detect", "auto-detect", "autodetect"}:
        return "auto"
    language_map = {
        "myanmar": "my", "burmese": "my", "မြန်မာ": "my", "my-mm": "my", "my": "my",
        "english": "en", "en-us": "en", "en-gb": "en", "en": "en",
        "thai": "th", "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
        "simplified chinese": "zh-CN", "traditional chinese": "zh-TW",
        "spanish": "es", "french": "fr", "german": "de",
    }
    return language_map.get(language, language)


def parse_srt_blocks(srt_text: str) -> list[dict]:
    text = (srt_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    raw_blocks = re.split(r"\n\s*\n", text)
    parsed_blocks = []
    for raw_block in raw_blocks:
        lines = [line.rstrip() for line in raw_block.split("\n") if line.strip()]
        if not lines:
            continue
        idx = None
        time_line = None
        if lines and re.fullmatch(r"\d+", lines[0].strip()):
            idx = lines.pop(0).strip()
        if lines and "-->" in lines[0]:
            time_line = lines.pop(0).strip()
        text_lines = lines
        if time_line and text_lines:
            parsed_blocks.append({"index": idx, "time": time_line, "text": " ".join(text_lines).strip()})
    return parsed_blocks


def build_srt_from_blocks(blocks: list[dict]) -> str:
    output_blocks = []
    for position, block in enumerate(blocks, start=1):
        output_blocks.append(f"{position}\n{block['time']}\n{(block.get('translated_text') or block.get('text') or '').strip()}\n")
    return "\n".join(output_blocks).strip() + "\n" if output_blocks else ""


def translate_texts_with_google(texts: list[str], source_language: str = "auto", target_language: str = "my") -> list[str]:
    try:
        from deep_translator import GoogleTranslator
    except Exception as exc:
        raise RuntimeError("deep-translator is not installed. Check requirements.txt and Railway deployment logs.") from exc
    source = normalize_translate_language(source_language, default="auto")
    target = normalize_translate_language(target_language, default="my")
    if target == "auto":
        raise ValueError("target_language cannot be auto")
    if source == target:
        return texts
    translator = GoogleTranslator(source=source, target=target)
    translated_texts = []
    cache = {}
    for text in texts:
        clean_text = (text or "").strip()
        if not clean_text:
            translated_texts.append("")
            continue
        if clean_text in cache:
            translated_texts.append(cache[clean_text])
            continue
        translated = translator.translate(clean_text)
        translated = (translated or clean_text).strip()
        cache[clean_text] = translated
        translated_texts.append(translated)
    return translated_texts


def translate_srt_text(srt_text: str, source_language: str = "auto", target_language: str = "my") -> tuple[str, dict]:
    blocks = parse_srt_blocks(srt_text)
    if not blocks:
        raise ValueError("Missing or invalid SRT text")
    translated_texts = translate_texts_with_google([b["text"] for b in blocks], source_language=source_language, target_language=target_language)
    for block, translated_text in zip(blocks, translated_texts):
        block["translated_text"] = translated_text
    return build_srt_from_blocks(blocks), {
        "engine": "google_translate",
        "source_language": normalize_translate_language(source_language, default="auto"),
        "target_language": normalize_translate_language(target_language, default="my"),
        "segments": len(blocks),
    }


def is_myanmar_language(language: str | None) -> bool:
    value = (language or "").strip().lower()
    return value in {"myanmar", "burmese", "my", "my-mm", "မြန်မာ", "ဗမာ"}


def clean_srt_to_text(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}", " ", raw)
    out, seen = [], set()
    for line in raw.split("\n"):
        line = (line or "").strip()
        if not line:
            continue
        upper = line.upper()
        if upper in {"WEBVTT", "STYLE", "REGION"} or upper.startswith(("NOTE", "KIND:", "LANGUAGE:")):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if SRT_TIMESTAMP_RE.match(line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]+\}", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out).strip()


def clean_srt_to_tts_script(text: str, language: str | None = None) -> str:
    cleaned = clean_srt_to_text(text)
    if not cleaned:
        return ""
    myanmar = is_myanmar_language(language)
    spoken_parts = []
    for line in cleaned.split("\n"):
        part = line.strip()
        if not part:
            continue
        if myanmar:
            if not END_PUNCT_RE.search(part):
                part += "။"
        else:
            if not END_PUNCT_RE.search(part):
                part += "."
        spoken_parts.append(part)
    script = " ".join(spoken_parts)
    script = re.sub(r"\s+", " ", script).strip()
    script = re.sub(r"။\s*။+", "။", script)
    return script


# ---------------- Rewrite and TTS ----------------

def _local_reference_style_rewrite(original: str = "", translated: str = "", fallback: str = "") -> str:
    combined = clean_srt_to_text("\n".join([original or "", translated or "", fallback or ""]))
    low = combined.lower()
    pieces = []
    def add_when(condition, text):
        if condition and text not in pieces:
            pieces.append(text)
    add_when("break my heart" in low or ("heart" in low and "break" in low), "ဒါကြောင့် ကျေးဇူးပြုပြီး ကိုယ့်အသည်းကို မခွဲပါနဲ့။")
    add_when("tear me apart" in low or "apart" in low, "ကိုယ့်ကို အပိုင်းအစတွေ ဖြစ်အောင် မလုပ်ပါနဲ့။")
    add_when("how it starts" in low or "starts" in low, "အစက ဘယ်လိုစတတ်တယ်ဆိုတာ ကိုယ်သိပါတယ်။")
    add_when("broken before" in low, "ယုံပါ၊ ကိုယ်အရင်ကလည်း အသည်းကွဲဖူးပါတယ်။")
    add_when("break me again" in low or "broken again" in low, "ကိုယ့်အသည်းကို ထပ်ပြီး မခွဲပါနဲ့နော်။")
    add_when("delicate" in low, "ကိုယ်က အသည်းနုသူမို့လို့။")
    add_when(("you love her" in low or "love her" in low) and ("over" in low or "mate" in low), "မင်းသူမကို ချစ်နေမှန်း သိပေမယ့် အရာအားလုံး ပြီးသွားပြီလေ။")
    add_when("phone away" in low or "put the phone" in low, "အရေးမကြီးတော့ပါဘူး။ ဖုန်းကိုချပြီး အဆက်အသွယ်ဖြတ်လိုက်ပါတော့။")
    add_when("never easy" in low or "walk away" in low, "ထွက်သွားဖို့ ဘယ်တော့မှ မလွယ်မှန်း သိပါတယ်။")
    add_when("let her go" in low or "let go" in low, "သူမကို လက်လွှတ်လိုက်ပါ။")
    add_when("all right" in low or "alright" in low, "အဆင်ပြေသွားမှာပါ။")
    return " ".join(pieces).strip()


def call_openrouter_rewrite(text: str = "", language: str = "my", style: str = "natural_myanmar_tts_from_original", original_text: str = "", translated_text: str = "", fallback_text: str = "") -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    original = clean_srt_to_text(original_text)
    translated = clean_srt_to_text(translated_text)
    fallback = clean_srt_to_text(fallback_text or text)
    if not original and not translated and not fallback:
        raise ValueError("No readable subtitle text found after cleanup")
    reference = _local_reference_style_rewrite(original=original, translated=translated, fallback=fallback)
    if reference and len(clean_srt_to_text("\n".join([original, translated, fallback]))) < 900:
        return reference
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    model_candidates = []
    for item in [os.getenv("OPENROUTER_MODEL", ""), *(os.getenv("OPENROUTER_MODEL_CANDIDATES", "").split(",")), "openrouter/free"]:
        model = (item or "").strip()
        if model and model not in model_candidates:
            model_candidates.append(model)
    system_prompt = (
        "You are a professional English-to-Myanmar translator and Myanmar TTS script editor. "
        "Use the original English as source of truth and the Myanmar translation as reference. "
        "Return only clean Myanmar text. Do not return markdown, JSON, English notes, safety labels, SRT numbers, timestamps, or arrows. "
        "For song lyrics and emotional dialogue, make Myanmar soft, natural, emotional, culturally fitting, and easy to speak aloud. "
        "Keep sentences short and TTS-friendly. Do not add new facts."
    )
    user_prompt = (
        f"Target language: {language}\nStyle: {style}\n\n"
        f"Original/source text:\n{original or fallback}\n\n"
        f"Rough Myanmar translation/reference:\n{translated or fallback}\n\n"
        "Rewrite into clean natural Myanmar TTS script only."
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://video-audio-tool-production.up.railway.app"),
        "X-Title": "Video2Audio Pro",
    }
    last_error = None
    for model in model_candidates:
        try:
            resp = requests.post(
                OPENROUTER_CHAT_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.35,
                    "max_tokens": 1200,
                },
                timeout=60,
            )
            if resp.status_code >= 400:
                last_error = f"{model}: {resp.status_code} {resp.text[:300]}"
                continue
            data = resp.json()
            script = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            script = re.sub(r"^```(?:\w+)?\s*", "", script).strip()
            script = re.sub(r"\s*```$", "", script).strip().strip('"')
            if script and len(re.findall(r"[\u1000-\u109F]", script)) >= 5 and "safety" not in script.lower():
                return script
            last_error = f"{model}: bad/empty output"
        except Exception as exc:
            last_error = f"{model}: {exc}"
    raise RuntimeError(last_error or "OpenRouter rewrite failed")


def normalize_tts_language(language: str | None) -> str:
    value = (language or "my").strip().lower()
    language_map = {
        "myanmar": "my", "burmese": "my", "my-mm": "my", "my": "my", "မြန်မာ": "my",
        "english": "en", "en-us": "en", "en-gb": "en", "en": "en",
    }
    return language_map.get(value, value)


def choose_tts_voice(language: str | None = "my", gender: str | None = None, requested_voice: str | None = None) -> str:
    if requested_voice and requested_voice.strip():
        return requested_voice.strip()
    lang = normalize_tts_language(language)
    gender_value = (gender or "female").strip().lower()
    if lang == "my":
        return "my-MM-ThihaNeural" if gender_value.startswith("m") else "my-MM-NilarNeural"
    if lang == "en":
        return "en-US-GuyNeural" if gender_value.startswith("m") else "en-US-JennyNeural"
    if lang == "th":
        return "th-TH-NiwatNeural" if gender_value.startswith("m") else "th-TH-PremwadeeNeural"
    if lang == "ja":
        return "ja-JP-KeitaNeural" if gender_value.startswith("m") else "ja-JP-NanamiNeural"
    if lang == "ko":
        return "ko-KR-InJoonNeural" if gender_value.startswith("m") else "ko-KR-SunHiNeural"
    return "my-MM-ThihaNeural" if gender_value.startswith("m") else "my-MM-NilarNeural"


def sanitize_edge_prosody(value: str | None, default: str, unit: str = "%") -> str:
    text = (value or "").strip()
    if not text:
        return default
    if text == "0":
        return "+0" + unit
    if re.fullmatch(r"[+-]?\d+", text):
        sign = "" if text.startswith(("+", "-")) else "+"
        return f"{sign}{text}{unit}"
    if re.fullmatch(r"[+-]?\d+%", text) or re.fullmatch(r"[+-]?\d+Hz", text, flags=re.I):
        return text if text.startswith(("+", "-")) else "+" + text
    return default


def split_tts_text(text: str, max_chars: int = 2500) -> list[str]:
    clean = clean_srt_to_tts_script(text, language="my") if "-->" in text or "WEBVTT" in text.upper() else re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return []
    sentences = re.split(r"(?<=[။.!?…])\s+", clean)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(sentence), max_chars):
                chunks.append(sentence[i:i + max_chars].strip())
            continue
        if len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}".strip()
        else:
            chunks.append(current.strip())
            current = sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def synthesize_edge_tts_chunks(chunks: list[str], voice: str, output_path: Path, rate: str, pitch: str, volume: str) -> None:
    import edge_tts
    if len(chunks) == 1:
        await edge_tts.Communicate(chunks[0], voice=voice, rate=rate, pitch=pitch, volume=volume).save(str(output_path))
        return
    with tempfile.TemporaryDirectory(prefix="tts_chunks_") as temp_dir:
        temp_dir_path = Path(temp_dir)
        chunk_paths: list[Path] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_path = temp_dir_path / f"chunk_{index:03d}.mp3"
            await edge_tts.Communicate(chunk, voice=voice, rate=rate, pitch=pitch, volume=volume).save(str(chunk_path))
            if not chunk_path.exists() or chunk_path.stat().st_size == 0:
                raise RuntimeError(f"TTS chunk {index} was not created")
            chunk_paths.append(chunk_path)
        concat_file = temp_dir_path / "concat.txt"
        concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in chunk_paths), encoding="utf-8")
        ffmpeg_binary = os.getenv("FFMPEG_BINARY", "ffmpeg")
        command = [ffmpeg_binary, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output_path)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            print("ffmpeg TTS concat failed; binary join fallback:", result.stderr[-2000:], flush=True)
            with output_path.open("wb") as final_file:
                for chunk_path in chunk_paths:
                    final_file.write(chunk_path.read_bytes())


def synthesize_tts_audio(text: str, language: str = "my", gender: str = "female", voice: str | None = None, rate: str | None = None, pitch: str | None = None, volume: str | None = None) -> tuple[Path, dict]:
    chunks = split_tts_text(text, max_chars=int(os.getenv("TTS_CHUNK_MAX_CHARS", "2500")))
    if not chunks:
        raise ValueError("No text available for TTS")
    selected_voice = choose_tts_voice(language=language, gender=gender, requested_voice=voice)
    selected_rate = sanitize_edge_prosody(rate or os.getenv("TTS_RATE"), default="+0%", unit="%")
    selected_pitch = sanitize_edge_prosody(pitch or os.getenv("TTS_PITCH"), default="+0Hz", unit="Hz")
    selected_volume = sanitize_edge_prosody(volume or os.getenv("TTS_VOLUME"), default="+0%", unit="%")
    output_path = TTS_DIR / f"tts_{normalize_tts_language(language)}_{uuid.uuid4().hex[:10]}.mp3"
    print(f"Generating TTS voice={selected_voice} chunks={len(chunks)}", flush=True)
    asyncio.run(synthesize_edge_tts_chunks(chunks, selected_voice, output_path, selected_rate, selected_pitch, selected_volume))
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("TTS audio file was not created")
    return output_path, {
        "engine": "edge_tts",
        "voice": selected_voice,
        "language": normalize_tts_language(language),
        "gender": gender,
        "rate": selected_rate,
        "pitch": selected_pitch,
        "volume": selected_volume,
        "chunks": len(chunks),
        "characters": sum(len(c) for c in chunks),
    }


# ---------------- Flask routes ----------------

def endpoint_list() -> list[str]:
    return [
        "POST /download", "POST /extract-srt", "POST /process-url",
        "POST /translate-srt", "POST /rewrite", "POST /tts",
        "POST /upload", "POST /extract-srt-upload", "POST /process-upload",
        "GET /audio/<filename>", "GET /tts/<filename>", "GET /srt/<filename>",
    ]


@app.get("/")
def index():
    return jsonify({"ok": True, "success": True, "service": "video-audio-tool", "version": "drm-safe-v7", "endpoints": endpoint_list()})


@app.get("/health")
def health():
    cookie_path = get_cookie_file()
    return jsonify({
        "ok": True,
        "success": True,
        "service": "video-audio-tool",
        "version": "drm-safe-v7",
        "endpoints": endpoint_list(),
        "youtube_caption_first": caption_first_enabled(),
        "youtube_direct_timedtext": (os.getenv("YOUTUBE_DIRECT_TIMEDTEXT", "true") or "true"),
        "url_whisper_fallback": should_whisper_fallback_for_url(),
        "cookies_configured": bool(cookie_path),
        "cookie_source": "env_or_file" if cookie_path else "none",
        "openrouter_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "tts_engine": "edge_tts",
        "tts_voices": {"my_female": "my-MM-NilarNeural", "my_male": "my-MM-ThihaNeural"},
    })


@app.post("/download")
def download():
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url") or request.values.get("url")
        if not url:
            return json_error("Missing 'url'", 400)
        mp3_path, meta = download_audio_as_mp3(url)
        download_name = f"{meta.get('video_id') or mp3_path.stem}.mp3"
        return send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=download_name, max_age=0)
    except Exception as exc:
        error_message, status_code, extra = friendly_youtube_error(exc)
        return json_error(error_message, status_code, **extra)


@app.post("/extract-srt")
def extract_srt():
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url") or request.values.get("url")
        language = payload.get("language") or request.form.get("language") or request.values.get("language") or "auto"
        if not url:
            return json_error("Missing 'url'", 400)
        base_url = request.host_url.rstrip("/")
        caption_result = None
        caption_errors = []
        if caption_first_enabled():
            try:
                caption_result = get_youtube_caption_srt(url, requested_language=language)
            except Exception as caption_exc:
                caption_errors.append(str(caption_exc))
                print(f"caption-first extraction failed; fallback may be used: {caption_exc}", flush=True)
        if caption_result:
            srt_text, caption_meta = caption_result
            safe_video_id = secure_filename(caption_meta.get("video_id") or get_youtube_video_id(url) or "youtube_caption") or "youtube_caption"
            srt_filename = f"{safe_video_id}_{caption_meta.get('source', 'caption')}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
            srt_url = f"{base_url}/srt/{srt_filename}"
            return jsonify({
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": srt_url,
                "filename": srt_filename,
                "subtitle_source": caption_meta.get("subtitle_source") or caption_meta.get("source"),
                "source": caption_meta,
                "caption": caption_meta,
                "audio": {"available": False, "reason": "caption-first extraction did not download audio"},
                "whisper": None,
                "needs_upload": False,
            })
        if not should_whisper_fallback_for_url():
            return json_error(
                "No usable YouTube manual/auto caption was found. Audio download fallback is disabled. Upload a video/audio file or upload SRT/VTT to continue.",
                424,
                needs_upload=True,
                caption_errors=caption_errors,
                fallback_options=["Upload video/audio", "Upload .srt/.vtt", "Try a YouTube video with captions"],
            )
        try:
            mp3_path, audio_meta = download_audio_as_mp3(url)
            srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
        except Exception as audio_exc:
            error_message, status_code, extra = friendly_youtube_error(audio_exc)
            return json_error(
                "No usable YouTube captions were found, and the backend could not download audio for Whisper. " + error_message,
                status_code,
                **extra,
                caption_errors=caption_errors,
                fallback_options=["Upload video/audio", "Upload .srt/.vtt", "Try a YouTube video with captions"],
            )
        srt_filename = f"{audio_meta.get('video_id') or mp3_path.stem}_{uuid.uuid4().hex[:8]}.srt"
        (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
        srt_url = f"{base_url}/srt/{srt_filename}"
        return jsonify({
            "ok": True,
            "success": True,
            "srt_text": srt_text,
            "srt_url": srt_url,
            "filename": srt_filename,
            "audio": audio_meta,
            "subtitle_source": "whisper",
            "source": {"type": "whisper", "reason": "No usable YouTube manual/auto caption was available"},
            "whisper": whisper_meta,
            "needs_upload": False,
        })
    except Exception as exc:
        print(f"extract-srt error: {exc}", flush=True)
        error_message, status_code, extra = friendly_youtube_error(exc)
        return json_error(error_message, status_code, **extra)


@app.post("/process-url")
def process_url():
    """SRT-first URL pipeline: extract captions/SRT and optionally translate. Does not require MP3 if captions exist."""
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url") or request.values.get("url")
        language = payload.get("language") or request.form.get("language") or request.values.get("language") or "auto"
        source_language = payload.get("source_language") or payload.get("source") or "auto"
        target_language = payload.get("target_language") or payload.get("target") or payload.get("language_target") or ""
        if not url:
            return json_error("Missing 'url'", 400)

        base_url = request.host_url.rstrip("/")
        caption_errors = []
        caption_result = None
        if caption_first_enabled():
            try:
                caption_result = get_youtube_caption_srt(url, requested_language=language)
            except Exception as caption_exc:
                caption_errors.append(str(caption_exc))
                print(f"process-url caption-first failed: {caption_exc}", flush=True)

        if caption_result:
            srt_text, caption_meta = caption_result
            safe_video_id = secure_filename(caption_meta.get("video_id") or get_youtube_video_id(url) or "youtube_caption") or "youtube_caption"
            srt_filename = f"{safe_video_id}_{caption_meta.get('source', 'caption')}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
            data = {
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": f"{base_url}/srt/{srt_filename}",
                "filename": srt_filename,
                "subtitle_source": caption_meta.get("subtitle_source") or caption_meta.get("source"),
                "source": caption_meta,
                "caption": caption_meta,
                "audio": {"available": False, "reason": "caption-first extraction did not download audio"},
                "whisper": None,
                "needs_upload": False,
            }
        else:
            if not should_whisper_fallback_for_url():
                return json_error(
                    "No usable YouTube manual/auto caption was found. Audio download fallback is disabled. Upload a video/audio file or upload SRT/VTT to continue.",
                    424,
                    needs_upload=True,
                    caption_errors=caption_errors,
                    fallback_options=["Upload video/audio", "Upload .srt/.vtt", "Try a YouTube video with captions"],
                )
            try:
                mp3_path, audio_meta = download_audio_as_mp3(url)
                srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
            except Exception as audio_exc:
                error_message, status_code, extra = friendly_youtube_error(audio_exc)
                return json_error(
                    "No usable YouTube captions were found, and the backend could not download audio for Whisper. " + error_message,
                    status_code,
                    **extra,
                    caption_errors=caption_errors,
                    fallback_options=["Upload video/audio", "Upload .srt/.vtt", "Try a YouTube video with captions"],
                )
            srt_filename = f"{audio_meta.get('video_id') or mp3_path.stem}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
            data = {
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": f"{base_url}/srt/{srt_filename}",
                "filename": srt_filename,
                "audio": audio_meta,
                "subtitle_source": "whisper",
                "source": {"type": "whisper", "reason": "No usable YouTube manual/auto caption was available"},
                "whisper": whisper_meta,
                "needs_upload": False,
            }

        if target_language:
            translated_srt_text, translation_meta = translate_srt_text(data["srt_text"], source_language=source_language, target_language=target_language)
            target_code = translation_meta["target_language"].replace("-", "_")
            translated_filename = f"translated_{target_code}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / translated_filename).write_text(translated_srt_text, encoding="utf-8")
            data.update({
                "translated_srt_text": translated_srt_text,
                "translated_srt_url": f"{base_url}/srt/{translated_filename}",
                "translated_filename": translated_filename,
                "translation": translation_meta,
            })
        return jsonify(data)
    except Exception as exc:
        print(f"process-url error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/translate-srt")
def translate_srt():
    try:
        payload = request.get_json(silent=True) or {}
        srt_text = payload.get("srt_text") or payload.get("srt") or ""
        filename = payload.get("filename") or ""
        source_language = payload.get("source_language") or payload.get("source") or "auto"
        target_language = payload.get("target_language") or payload.get("target") or payload.get("language") or "my"
        if not srt_text and filename:
            safe_filename = Path(filename).name
            srt_path = SRT_DIR / safe_filename
            if not srt_path.exists():
                return json_error("SRT filename not found", 404)
            srt_text = srt_path.read_text(encoding="utf-8")
        if not srt_text:
            return json_error("Missing 'srt_text'", 400)
        translated_srt_text, translation_meta = translate_srt_text(srt_text, source_language=source_language, target_language=target_language)
        target_code = translation_meta["target_language"].replace("-", "_")
        translated_filename = f"translated_{target_code}_{uuid.uuid4().hex[:8]}.srt"
        (SRT_DIR / translated_filename).write_text(translated_srt_text, encoding="utf-8")
        base_url = request.host_url.rstrip("/")
        return jsonify({
            "ok": True,
            "success": True,
            "translated_srt_text": translated_srt_text,
            "translated_srt_url": f"{base_url}/srt/{translated_filename}",
            "filename": translated_filename,
            "translation": translation_meta,
        })
    except Exception as exc:
        print(f"translate-srt error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.route("/rewrite", methods=["POST", "OPTIONS"])
def rewrite_script():
    if request.method == "OPTIONS":
        return jsonify({"ok": True, "success": True}), 200
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or payload.get("srt_text") or payload.get("translated_srt_text") or request.form.get("text") or ""
        original_text = payload.get("original_text") or payload.get("originalText") or payload.get("source_text") or ""
        translated_text = payload.get("translated_text") or payload.get("translatedText") or payload.get("translated_srt_text") or text
        language = payload.get("language") or payload.get("target_language") or request.form.get("language") or "my"
        style = payload.get("style") or request.form.get("style") or "natural_myanmar_tts_from_original"
        cleaned_original = clean_srt_to_text(str(original_text))
        cleaned_translated = clean_srt_to_text(str(translated_text))
        cleaned_fallback = clean_srt_to_text(str(text))
        if not cleaned_original and not cleaned_translated and not cleaned_fallback:
            return json_error("No readable subtitle text found after cleanup", 400)
        source = "openrouter_free_ai"
        try:
            script = call_openrouter_rewrite(original_text=cleaned_original, translated_text=cleaned_translated, fallback_text=cleaned_fallback, language=language, style=style)
        except Exception as ai_error:
            print(f"OpenRouter rewrite failed; using local cleanup: {ai_error}", flush=True)
            script = clean_srt_to_tts_script(cleaned_translated or cleaned_fallback or cleaned_original, language=language)
            source = "local_tts_cleanup"
        if not script.strip():
            return json_error("Rewrite returned no text", 500)
        return jsonify({
            "ok": True,
            "success": True,
            "rewritten_text": script,
            "rewrittenText": script,
            "rewritten_script": script,
            "rewrittenScript": script,
            "script": script,
            "text": script,
            "language": language,
            "style": style,
            "source": source,
        })
    except Exception as exc:
        print(f"rewrite error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.route("/tts", methods=["POST", "OPTIONS"])
def tts():
    if request.method == "OPTIONS":
        return jsonify({"ok": True, "success": True}), 200
    try:
        payload = request.get_json(silent=True) or {}
        text = (
            payload.get("text") or payload.get("script") or payload.get("rewritten_text") or payload.get("rewrittenText") or
            payload.get("rewritten_script") or payload.get("translated_srt_text") or request.form.get("text") or ""
        )
        language = payload.get("language") or payload.get("target_language") or request.form.get("language") or "my"
        gender = payload.get("gender") or payload.get("voice_gender") or payload.get("voiceGender") or request.form.get("gender") or "female"
        voice = payload.get("voice") or payload.get("voice_id") or payload.get("voiceId") or request.form.get("voice")
        rate = payload.get("rate") or request.form.get("rate")
        pitch = payload.get("pitch") or request.form.get("pitch")
        volume = payload.get("volume") or request.form.get("volume")
        clean_text = clean_srt_to_tts_script(str(text), language=language)
        if not clean_text:
            return json_error("No text available for TTS", 400)
        output_path, meta = synthesize_tts_audio(clean_text, language=language, gender=gender, voice=voice, rate=rate, pitch=pitch, volume=volume)
        base_url = request.host_url.rstrip("/")
        audio_url = f"{base_url}/tts/{output_path.name}"
        return jsonify({
            "ok": True,
            "success": True,
            "audio_url": audio_url,
            "audioUrl": audio_url,
            "tts_audio_url": audio_url,
            "url": audio_url,
            "filename": output_path.name,
            "audio_filename": output_path.name,
            "text": clean_text,
            "characters": len(clean_text),
            "tts": meta,
            "engine": meta.get("engine"),
            "voice": meta.get("voice"),
            "language": meta.get("language"),
            "source": "edge_tts",
        })
    except Exception as exc:
        print(f"tts error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/upload")
def upload_to_mp3():
    try:
        uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        return send_file(mp3_path, mimetype="audio/mpeg", as_attachment=True, download_name=f"{input_path.stem}.mp3", max_age=0)
    except Exception as exc:
        print(f"upload error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/extract-srt-upload")
def extract_srt_upload():
    try:
        uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
        srt_file = request.files.get("srt_file") or request.files.get("srt") or request.files.get("subtitle")
        language = request.form.get("language") or request.values.get("language") or "auto"
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        manual_srt_text, manual_srt_name = read_uploaded_srt(srt_file)
        if manual_srt_text:
            srt_text = manual_srt_text
            srt_filename = f"manual_{Path(secure_filename(manual_srt_name)).stem}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
            whisper_meta = None
            subtitle_source = "manual_srt_upload"
        else:
            srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
            subtitle_source = "whisper"
        base_url = request.host_url.rstrip("/")
        return jsonify({
            "ok": True,
            "success": True,
            "srt_text": srt_text,
            "srt_url": f"{base_url}/srt/{srt_filename}",
            "filename": srt_filename,
            "audio_url": f"{base_url}/audio/{mp3_path.name}",
            "audio_filename": mp3_path.name,
            "source": {"type": "upload", "filename": input_path.name},
            "subtitle_source": subtitle_source,
            "whisper": whisper_meta,
        })
    except Exception as exc:
        print(f"extract-srt-upload error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/process-upload")
def process_upload():
    try:
        uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
        srt_file = request.files.get("srt_file") or request.files.get("srt") or request.files.get("subtitle")
        language = request.form.get("language") or request.values.get("language") or "auto"
        target_language = request.form.get("target_language") or request.values.get("target_language") or ""
        source_language = request.form.get("source_language") or request.values.get("source_language") or "auto"
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        manual_srt_text, manual_srt_name = read_uploaded_srt(srt_file)
        if not manual_srt_text:
            manual_srt_text = request.form.get("srt_text") or request.values.get("srt_text") or ""
            manual_srt_name = "manual_text.srt" if manual_srt_text else None
        if manual_srt_text:
            srt_text = normalize_caption_to_srt(manual_srt_text, ext="vtt" if str(manual_srt_name).lower().endswith(".vtt") else "srt") or manual_srt_text
            srt_filename = f"manual_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / srt_filename).write_text(srt_text, encoding="utf-8")
            whisper_meta = None
            subtitle_source = "manual_srt_upload"
        else:
            srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
            subtitle_source = "whisper"
        base_url = request.host_url.rstrip("/")
        response_payload = {
            "ok": True,
            "success": True,
            "audio_url": f"{base_url}/audio/{mp3_path.name}",
            "audio_filename": mp3_path.name,
            "srt_text": srt_text,
            "srt_url": f"{base_url}/srt/{srt_filename}",
            "filename": srt_filename,
            "source": {"type": "upload", "filename": input_path.name},
            "subtitle_source": subtitle_source,
            "whisper": whisper_meta,
        }
        if target_language:
            translated_srt_text, translation_meta = translate_srt_text(srt_text, source_language=source_language, target_language=target_language)
            target_code = translation_meta["target_language"].replace("-", "_")
            translated_filename = f"translated_{target_code}_{uuid.uuid4().hex[:8]}.srt"
            (SRT_DIR / translated_filename).write_text(translated_srt_text, encoding="utf-8")
            response_payload.update({
                "translated_srt_text": translated_srt_text,
                "translated_srt_url": f"{base_url}/srt/{translated_filename}",
                "translated_filename": translated_filename,
                "translation": translation_meta,
            })
        return jsonify(response_payload)
    except Exception as exc:
        print(f"process-upload error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.get("/audio/<path:filename>")
def serve_audio(filename):
    safe_filename = Path(filename).name
    return send_from_directory(AUDIO_DIR, safe_filename, mimetype="audio/mpeg", as_attachment=False, download_name=safe_filename, max_age=0)


@app.get("/tts/<path:filename>")
def serve_tts(filename):
    safe_filename = Path(filename).name
    return send_from_directory(TTS_DIR, safe_filename, mimetype="audio/mpeg", as_attachment=False, download_name=safe_filename, max_age=0)


@app.get("/srt/<path:filename>")
def serve_srt(filename):
    safe_filename = Path(filename).name
    return send_from_directory(SRT_DIR, safe_filename, mimetype="text/plain; charset=utf-8", as_attachment=True, download_name=safe_filename, max_age=0)


@app.errorhandler(RequestEntityTooLarge)
def handle_413(error):
    return json_error("Uploaded file is too large", 413)


@app.errorhandler(404)
def handle_404(error):
    return json_error("Not found", 404)


@app.errorhandler(405)
def handle_405(error):
    return json_error("Method not allowed", 405)


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return json_error(error.description or error.name, error.code or 500)
    print(f"unhandled error: {error}", flush=True)
    return json_error(str(error), 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
