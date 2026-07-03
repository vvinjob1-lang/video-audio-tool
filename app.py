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
    "Accept-Language": "en-US,en;q=0.9,my;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    if "video unavailable" in lowered:
        return ("This YouTube video is unavailable from the backend. It may be private, region-restricted, deleted, or blocked.", 404, extra)
    return message, 500, extra


def build_ydl_opts(
    output_base: Path | None = None,
    fallback: bool = False,
    use_cookies: bool = False,
    format_selector: str | None = None,
    skip_download: bool = False,
) -> dict:
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
        opts.update({
            "skip_download": True,
            "ignore_no_formats_error": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt/vtt/ttml/srv3/json3/best",
            "subtitleslangs": ["en", "en-US", "en-GB", "en.*", "my", "und", "all"],
        })
    else:
        if output_base is None:
            output_base = DOWNLOAD_DIR / uuid.uuid4().hex
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
            raise RuntimeError("MP3 file was not created. Check FFmpeg on Railway.")
        except Exception as exc:
            last_error = exc
            print(f"audio download attempt failed profile={profile}: {exc}", flush=True)
            if "drm" in str(exc).lower():
                raise RuntimeError(str(exc))
    raise RuntimeError(str(last_error) if last_error else "Download failed")


# ---------------- Downsub-style caption-first helpers ----------------

def _caption_lang_candidates(requested_language: str | None = None) -> list[str]:
    env_value = os.getenv("YOUTUBE_CAPTION_LANGUAGES", "en,en-US,en-GB,en.*,my,und,auto,*")
    candidates: list[str] = []
    normalized = normalize_whisper_language(requested_language)
    if normalized and normalized not in {"auto", "detect"}:
        candidates.append(normalized)
    for item in env_value.split(","):
        value = (item or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    if "*" not in candidates:
        candidates.append("*")
    return candidates


def _caption_key_matches(key: str, candidate: str) -> bool:
    key_low = (key or "").lower()
    candidate_low = (candidate or "").lower()
    if not key_low or not candidate_low:
        return False
    if candidate_low in {"*", "auto", "all"}:
        return True
    if candidate_low.endswith(".*"):
        base = candidate_low[:-2]
        return key_low == base or key_low.startswith(base + "-")
    return key_low == candidate_low


def _unique_ordered(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _pick_track(tracks: list[dict], requested_language: str | None = None) -> tuple[str, dict] | None:
    if not tracks:
        return None
    candidates = _caption_lang_candidates(requested_language)
    manual = [t for t in tracks if (t.get("kind") or "") != "asr"]
    auto = [t for t in tracks if (t.get("kind") or "") == "asr"]
    pools = [("youtube_caption_tracks", manual), ("youtube_auto_caption", auto), ("youtube_caption_tracks", tracks)]
    for source_name, pool in pools:
        for candidate in candidates:
            for track in pool:
                lang = track.get("languageCode") or track.get("lang") or track.get("language") or ""
                if _caption_key_matches(lang, candidate):
                    return source_name, track
    return ("youtube_caption_tracks", tracks[0])


def _set_query_param(url: str, **params) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _extract_json_after_marker(text: str, marker: str) -> dict | None:
    pos = text.find(marker)
    if pos < 0:
        return None
    start = text.find("{", pos)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = text[start:i + 1]
                try:
                    return json.loads(raw)
                except Exception:
                    return None
    return None


def get_watch_page_caption_tracks(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    """Downsub-style first method: read captionTracks/baseUrl from YouTube watch page."""
    if not is_youtube_url(url):
        return None
    normalized_url = normalize_youtube_url(url)
    video_id = get_youtube_video_id(normalized_url)
    watch_url = f"https://www.youtube.com/watch?v={video_id}&hl=en&persist_hl=1"
    errors = []
    try:
        resp = requests.get(watch_url, headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
        resp.raise_for_status()
        html_text = resp.text or ""
    except Exception as exc:
        print(f"watch-page fetch failed: {exc}", flush=True)
        return None

    player_response = _extract_json_after_marker(html_text, "ytInitialPlayerResponse")
    if not player_response:
        # Sometimes the JSON is assigned as window["ytInitialPlayerResponse"].
        m = re.search(r"ytInitialPlayerResponse\s*=\s*({.+?})\s*;", html_text)
        if m:
            try:
                player_response = json.loads(m.group(1))
            except Exception:
                player_response = None
    if not player_response:
        return None

    caption_renderer = (
        player_response.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
    )
    tracks = caption_renderer.get("captionTracks") or []
    if not tracks:
        return None

    manual_languages = sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") != "asr"})
    auto_languages = sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") == "asr"})
    selected = _pick_track(tracks, requested_language)
    if not selected:
        return None
    source_name, track = selected
    base_url = track.get("baseUrl") or ""
    if not base_url:
        return None

    fmt_attempts = ["vtt", "json3", "srv3", "ttml"]
    for fmt in fmt_attempts:
        try:
            caption_url = _set_query_param(base_url, fmt=fmt)
            cap = requests.get(caption_url, headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
            if cap.status_code >= 400 or not cap.text.strip():
                errors.append(f"{fmt}: http {cap.status_code}")
                continue
            srt_text = normalize_caption_to_srt(cap.text, ext=fmt)
            if srt_text.strip():
                return srt_text, {
                    "source": source_name,
                    "subtitle_source": source_name if track.get("kind") != "asr" else "youtube_auto_caption",
                    "language": track.get("languageCode") or "",
                    "format": fmt,
                    "title": (player_response.get("videoDetails") or {}).get("title") or "YouTube captions",
                    "video_id": video_id,
                    "source_url": normalized_url,
                    "manual_languages": manual_languages,
                    "auto_languages": auto_languages,
                    "caption_track_count": len(tracks),
                    "no_media_download": True,
                    "caption_first": True,
                    "errors": errors[-5:],
                }
            errors.append(f"{fmt}: empty after conversion")
        except Exception as exc:
            errors.append(f"{fmt}: {exc}")
            print(f"watch-page caption fetch failed fmt={fmt}: {exc}", flush=True)
    return None


def get_direct_timedtext_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
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
            root = ET.fromstring(resp.text or "")
            for track in root.findall(".//track"):
                lang_code = track.attrib.get("lang_code") or track.attrib.get("lang") or ""
                if not lang_code:
                    continue
                tracks.append({
                    "lang": lang_code,
                    "name": track.attrib.get("name") or "",
                    "kind": track.attrib.get("kind") or "",
                })
        except Exception as exc:
            errors.append(str(exc))
    if not tracks:
        return None

    manual_tracks = [t for t in tracks if t.get("kind") != "asr"]
    asr_tracks = [t for t in tracks if t.get("kind") == "asr"]
    selected = _pick_track(
        [{"languageCode": t["lang"], "kind": t.get("kind", ""), "name": t.get("name", "")} for t in tracks],
        requested_language,
    )
    if not selected:
        return None
    source_name, selected_track = selected
    track = {"lang": selected_track.get("languageCode"), "kind": selected_track.get("kind", ""), "name": selected_track.get("name", "")}
    if track.get("kind") == "asr":
        source_name = "youtube_direct_auto_caption"
    else:
        source_name = "youtube_direct_timedtext"

    for fmt in ["vtt", "json3", "srv3", "ttml"]:
        params = {"v": video_id, "lang": track["lang"], "fmt": fmt}
        if track.get("kind"):
            params["kind"] = track["kind"]
        if track.get("name"):
            params["name"] = track["name"]
        for host in ["https://www.youtube.com/api/timedtext", "https://video.google.com/timedtext"]:
            try:
                caption_url = f"{host}?{urlencode(params)}"
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
                        "no_media_download": True,
                        "caption_first": True,
                        "errors": errors[-5:],
                    }
            except Exception as exc:
                errors.append(str(exc))
    return None


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
    for ext in ["srt", "vtt", "json3", "srv3", "ttml"]:
        for item in formats:
            if (item.get("ext") or "").lower() == ext and item.get("url"):
                return item
    for item in formats:
        if item.get("url"):
            return item
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
                        "no_media_download": True,
                        "caption_first": True,
                        "errors": errors[-5:],
                    }
            except Exception as exc:
                errors.append(str(exc))
                print(f"yt-dlp caption metadata failed: {exc}", flush=True)
    return None


def get_youtube_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    if not is_youtube_url(url):
        return None
    methods = [
        ("watch_page_caption_tracks", get_watch_page_caption_tracks),
        ("direct_timedtext", get_direct_timedtext_caption_srt),
        ("ytdlp_metadata", get_ytdlp_caption_srt),
    ]
    for name, fn in methods:
        if name == "direct_timedtext" and (os.getenv("YOUTUBE_DIRECT_TIMEDTEXT", "true") or "true").strip().lower() in {"0", "false", "no", "off"}:
            continue
        try:
            result = fn(url, requested_language=requested_language)
            if result and result[0].strip():
                return result
        except Exception as exc:
            print(f"caption method failed {name}: {exc}", flush=True)
    return None


def _caption_ts_to_srt(ts: str) -> str:
    ts = (ts or "").strip().replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?", ts):
        return srt_timestamp(float(ts))
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
        if not line or upper == "WEBVTT" or upper.startswith("X-TIMESTAMP"):
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


def json3_to_srt_text(json_text: str) -> str:
    raw = (json_text or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    events = data.get("events") or []
    blocks = []
    for event in events:
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8") or "" for seg in segs)
        text = _clean_caption_text_line(text)
        if not text:
            continue
        start_ms = int(event.get("tStartMs") or 0)
        dur_ms = int(event.get("dDurationMs") or 3000)
        start = start_ms / 1000.0
        end = (start_ms + max(dur_ms, 500)) / 1000.0
        blocks.append(f"{len(blocks) + 1}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def ttml_to_srt_text(ttml_text: str) -> str:
    raw = (ttml_text or "").strip()
    if not raw:
        return ""
    try:
        raw2 = re.sub(r"xmlns(:\w+)?=\"[^\"]+\"", "", raw)
        root = ET.fromstring(raw2)
        blocks = []
        for p in root.iter():
            if p.tag.split("}")[-1].lower() != "p":
                continue
            start = p.attrib.get("begin") or p.attrib.get("start") or "00:00:00.000"
            end = p.attrib.get("end") or ""
            dur = p.attrib.get("dur")
            if not end and dur:
                try:
                    # Minimal duration support: convert begin + dur if dur is numeric seconds.
                    if dur.endswith("s"):
                        begin_seconds = _ts_to_seconds(start)
                        end = srt_timestamp(begin_seconds + float(dur[:-1])).replace(",", ".")
                    else:
                        end = start
                except Exception:
                    end = start
            text = _clean_caption_text_line(" ".join(p.itertext()))
            if text:
                blocks.append(f"{len(blocks) + 1}\n{_caption_ts_to_srt(start)} --> {_caption_ts_to_srt(end or start)}\n{text}\n")
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
        for node in root.findall(".//text"):
            start = float(node.attrib.get("start", "0") or 0)
            dur = float(node.attrib.get("dur", "3") or 3)
            text = _clean_caption_text_line("".join(node.itertext()))
            if text:
                blocks.append(f"{len(blocks) + 1}\n{srt_timestamp(start)} --> {srt_timestamp(start + dur)}\n{text}\n")
        return "\n".join(blocks).strip() + "\n" if blocks else ""
    except Exception:
        return ""


def normalize_caption_to_srt(caption_text: str, ext: str | None = None) -> str:
    ext_low = (ext or "").lower()
    raw = (caption_text or "").strip()
    if not raw:
        return ""
    if ext_low == "srt" or ("-->" in raw and not raw.lstrip("\ufeff").upper().startswith("WEBVTT")):
        return raw.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if ext_low == "vtt" or raw.lstrip("\ufeff").upper().startswith("WEBVTT"):
        return vtt_to_srt_text(raw)
    if ext_low == "json3" or raw.startswith("{"):
        return json3_to_srt_text(raw)
    if ext_low == "ttml" or raw.lstrip().startswith("<tt"):
        return ttml_to_srt_text(raw)
    if ext_low in {"srv1", "srv2", "srv3", "xml"} or raw.lstrip().startswith("<transcript"):
        return srv_xml_to_srt_text(raw)
    return ""


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
        "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(output_path),
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


def _ts_to_seconds(ts: str) -> float:
    ts = (ts or "").replace(",", ".").strip()
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts)


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
    for item in os.getenv("WHISPER_FALLBACK_LANGUAGES", "en,my").split(","):
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
    raise RuntimeError(f"Whisper finished but did not produce any subtitle text. Attempts={attempts}")


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
        "myanmar": "my", "burmese": "my", "မြန်မာ": "my", "my": "my",
        "english": "en", "en-us": "en", "en-gb": "en", "en": "en",
        "thai": "th", "chinese": "zh-CN", "simplified chinese": "zh-CN", "traditional chinese": "zh-TW",
        "japanese": "ja", "korean": "ko", "spanish": "es", "french": "fr", "german": "de",
    }
    return language_map.get(language, language)


def parse_srt_blocks(srt_text: str) -> list[dict]:
    text = (srt_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    # VTT uploads can reach here.
    if text.upper().startswith("WEBVTT"):
        text = vtt_to_srt_text(text)
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
        text_lines = [_clean_caption_text_line(x) for x in lines]
        text_lines = [x for x in text_lines if x]
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
        try:
            translated = translator.translate(clean_text)
        except Exception as exc:
            raise RuntimeError(f"Google Translate failed: {exc}") from exc
        translated = (translated or clean_text).strip()
        cache[clean_text] = translated
        translated_texts.append(translated)
    return translated_texts


def translate_srt_text(srt_text: str, source_language: str = "auto", target_language: str = "my") -> tuple[str, dict]:
    blocks = parse_srt_blocks(srt_text)
    if not blocks:
        raise ValueError("Missing or invalid SRT text")
    original_texts = [block["text"] for block in blocks]
    translated_texts = translate_texts_with_google(original_texts, source_language=source_language, target_language=target_language)
    for block, translated_text in zip(blocks, translated_texts):
        block["translated_text"] = translated_text
    meta = {
        "engine": "google_translate",
        "source_language": normalize_translate_language(source_language, default="auto"),
        "target_language": normalize_translate_language(target_language, default="my"),
        "segments": len(blocks),
    }
    return build_srt_from_blocks(blocks), meta


def clean_srt_to_text(srt_text: str) -> str:
    lines = []
    seen_recent = set()
    for raw_line in (srt_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT" or line.upper().startswith(("NOTE", "STYLE", "REGION")):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if SRT_TIMESTAMP_RE.match(line) or "-->" in line:
            continue
        line = _clean_caption_text_line(line)
        if not line:
            continue
        key = line.casefold()
        if key in seen_recent:
            continue
        seen_recent.add(key)
        if len(seen_recent) > 50:
            seen_recent = set(list(seen_recent)[-25:])
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def local_rewrite_for_tts(text: str, language: str = "my") -> str:
    cleaned = clean_srt_to_text(text)
    if not cleaned:
        cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    # Light cleanup only; no fake AI rewrite.
    cleaned = re.sub(r"\s+([၊။,.!?])", r"\1", cleaned)
    cleaned = re.sub(r"([။.!?])\s*", r"\1\n", cleaned)
    paragraphs = [p.strip() for p in cleaned.splitlines() if p.strip()]
    return "\n".join(paragraphs).strip()


def rewrite_with_openrouter(text: str, language: str = "my", style: str = "concise_natural_tts") -> tuple[str, str]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return local_rewrite_for_tts(text, language), "local_tts_cleanup"
    model = os.getenv("OPENROUTER_MODEL", "openrouter/cypher-alpha:free")
    prompt = (
        "Rewrite the following subtitle text into a clean, concise, natural script for TTS narration. "
        "Remove timestamps, subtitle numbers, duplicates, and overly literal phrasing. "
        "Keep the meaning. Return only the final script.\n\n"
        f"Language: {language}\nStyle: {style}\n\nText:\n{text}"
    )
    try:
        resp = requests.post(
            OPENROUTER_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://video-audio-tool-production.up.railway.app"),
                "X-Title": os.getenv("OPENROUTER_APP_TITLE", "Video2Audio Pro"),
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": int(os.getenv("OPENROUTER_MAX_TOKENS", "2000")),
            },
            timeout=int(os.getenv("OPENROUTER_TIMEOUT", "60")),
        )
        resp.raise_for_status()
        data = resp.json()
        rewritten = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if rewritten:
            return rewritten, "openrouter_free_ai"
    except Exception as exc:
        print(f"OpenRouter rewrite failed, falling back locally: {exc}", flush=True)
    return local_rewrite_for_tts(text, language), "local_tts_cleanup"


def save_srt_response(srt_text: str, base_name: str = "captions") -> tuple[str, str]:
    safe_base = secure_filename(base_name) or "captions"
    filename = f"{Path(safe_base).stem}_{uuid.uuid4().hex[:8]}.srt"
    path = SRT_DIR / filename
    path.write_text(srt_text, encoding="utf-8")
    base_url = request.host_url.rstrip("/")
    return filename, f"{base_url}/srt/{filename}"


def build_final_srt_from_script(script: str, seconds_per_line: float = 3.5) -> str:
    text = local_rewrite_for_tts(script)
    if not text:
        return ""
    # Split Myanmar and Western punctuation into readable subtitle cues.
    pieces = re.split(r"(?<=[။.!?])\s+|\n+", text)
    lines = [p.strip() for p in pieces if p.strip()]
    blocks = []
    start = 0.0
    for idx, line in enumerate(lines, start=1):
        # Approximate timing before/without forced alignment.
        duration = max(2.0, min(7.0, len(line) / 18.0)) if line else seconds_per_line
        end = start + duration
        blocks.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{line}\n")
        start = end
    return "\n".join(blocks).strip() + "\n" if blocks else ""


# ---------------- Routes ----------------

@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "service": "video-audio-tool",
        "caption_first": True,
        "endpoints": [
            "POST /download", "POST /extract-srt", "POST /translate-srt", "POST /process-upload",
            "POST /upload", "POST /extract-srt-upload", "POST /rewrite", "POST /tts", "POST /final-srt",
            "GET /audio/<filename>", "GET /srt/<filename>", "GET /tts/<filename>",
        ],
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "caption_first": True})


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

        caption_errors = []
        if is_youtube_url(url) and caption_first_enabled():
            try:
                caption_result = get_youtube_caption_srt(url, requested_language=language)
                if caption_result:
                    srt_text, meta = caption_result
                    srt_filename, srt_url = save_srt_response(srt_text, meta.get("video_id") or "youtube_captions")
                    return jsonify({
                        "ok": True,
                        "success": True,
                        "srt_text": srt_text,
                        "srt_url": srt_url,
                        "filename": srt_filename,
                        "subtitle_source": meta.get("subtitle_source") or meta.get("source") or "youtube_caption_tracks",
                        "source": meta.get("source") or meta.get("subtitle_source") or "youtube_caption_tracks",
                        "no_media_download": True,
                        "caption_first": True,
                        "manual_languages": meta.get("manual_languages") or [],
                        "auto_languages": meta.get("auto_languages") or [],
                        "caption": meta,
                    })
            except Exception as exc:
                caption_errors.append(str(exc))
                print(f"caption-first extraction error: {exc}", flush=True)

        # Whisper fallback only after all public caption methods fail.
        if not should_whisper_fallback_for_url():
            return json_error(
                "No public captions were found for this link. Upload an SRT/VTT file to continue.",
                404,
                needs_upload=True,
                caption_first=True,
                no_media_download=True,
                caption_errors=caption_errors[-5:],
            )
        try:
            mp3_path, audio_meta = download_audio_as_mp3(url)
            srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
            srt_filename, srt_url = save_srt_response(srt_text, audio_meta.get("video_id") or mp3_path.stem)
            return jsonify({
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": srt_url,
                "filename": srt_filename,
                "subtitle_source": "whisper_fallback",
                "caption_first": True,
                "no_media_download": False,
                "audio": audio_meta,
                "whisper": whisper_meta,
                "caption_errors": caption_errors[-5:],
            })
        except Exception as exc:
            print(f"Whisper fallback failed after caption methods: {exc}", flush=True)
            error_message, status_code, extra = friendly_youtube_error(exc)
            extra.update({
                "caption_first": True,
                "no_media_download": True,
                "needs_upload": True,
                "caption_errors": caption_errors[-5:],
                "media_download_error": error_message,
            })
            return json_error(
                "No public captions were found and Whisper fallback could not download audio.",
                status_code if status_code in {403, 404, 451, 502} else 502,
                **extra,
            )
    except Exception as exc:
        print(f"extract-srt error: {exc}", flush=True)
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
        uploaded_srt = request.files.get("srt_file") or request.files.get("srt") or request.files.get("subtitle")
        srt_text, srt_source_filename = read_uploaded_srt(uploaded_srt)
        base_url = request.host_url.rstrip("/")
        if srt_text:
            srt_filename, srt_url = save_srt_response(srt_text, Path(srt_source_filename or "manual_srt").stem)
            return jsonify({
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": srt_url,
                "filename": srt_filename,
                "subtitle_source": "manual_srt_upload",
                "source": {"type": "manual_srt_upload", "filename": srt_source_filename},
                "no_media_download": True,
            })
        uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
        language = request.form.get("language") or request.values.get("language") or "auto"
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
        return jsonify({
            "ok": True,
            "success": True,
            "srt_text": srt_text,
            "srt_url": f"{base_url}/srt/{srt_filename}",
            "filename": srt_filename,
            "audio_url": f"{base_url}/audio/{mp3_path.name}",
            "audio_filename": mp3_path.name,
            "subtitle_source": "whisper_upload",
            "source": {"type": "upload", "filename": input_path.name},
            "whisper": whisper_meta,
        })
    except Exception as exc:
        print(f"extract-srt-upload error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/process-upload")
def process_upload():
    try:
        uploaded_srt = request.files.get("srt_file") or request.files.get("srt") or request.files.get("subtitle")
        language = request.form.get("language") or request.values.get("language") or "auto"
        target_language = request.form.get("target_language") or request.values.get("target_language") or ""
        source_language = request.form.get("source_language") or request.values.get("source_language") or "auto"
        base_url = request.host_url.rstrip("/")
        srt_text, srt_source_filename = read_uploaded_srt(uploaded_srt)
        response_payload = {}
        if srt_text:
            srt_filename, srt_url = save_srt_response(srt_text, Path(srt_source_filename or "manual_srt").stem)
            response_payload.update({
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "srt_url": srt_url,
                "filename": srt_filename,
                "subtitle_source": "manual_srt_upload",
                "source": {"type": "manual_srt_upload", "filename": srt_source_filename},
                "no_media_download": True,
            })
        else:
            uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
            input_path = save_uploaded_media(uploaded_file)
            mp3_path = convert_media_file_to_mp3(input_path)
            srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
            response_payload.update({
                "ok": True,
                "success": True,
                "audio_url": f"{base_url}/audio/{mp3_path.name}",
                "audio_filename": mp3_path.name,
                "srt_text": srt_text,
                "srt_url": f"{base_url}/srt/{srt_filename}",
                "filename": srt_filename,
                "subtitle_source": "whisper_upload",
                "source": {"type": "upload", "filename": input_path.name},
                "whisper": whisper_meta,
            })
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


@app.post("/rewrite")
def rewrite():
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or payload.get("srt_text") or payload.get("translated_srt_text") or ""
        language = payload.get("language") or "my"
        style = payload.get("style") or "concise_natural_tts"
        if not text.strip():
            return json_error("Missing text to rewrite", 400)
        cleaned = clean_srt_to_text(text) or text
        rewritten, source = rewrite_with_openrouter(cleaned, language=language, style=style)
        if not rewritten.strip():
            return json_error("Rewrite returned no text", 502, source=source)
        return jsonify({
            "ok": True,
            "success": True,
            "script": rewritten,
            "rewrittenScript": rewritten,
            "rewritten_script": rewritten,
            "rewritten_text": rewritten,
            "source": source,
            "cleaned_text": cleaned,
        })
    except Exception as exc:
        print(f"rewrite error: {exc}", flush=True)
        return json_error(str(exc), 500)


async def _edge_tts_generate(text: str, voice: str, output_path: Path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def select_tts_voice(language: str | None = None, gender: str | None = None, requested_voice: str | None = None) -> str:
    if requested_voice:
        return requested_voice
    language_norm = normalize_translate_language(language or "my", default="my")
    gender_norm = (gender or "female").lower()
    if language_norm == "my":
        return os.getenv("TTS_VOICE_MY_MALE" if gender_norm.startswith("m") else "TTS_VOICE_MY_FEMALE", "my-MM-NilarNeural")
    if language_norm == "en":
        return os.getenv("TTS_VOICE_EN_MALE" if gender_norm.startswith("m") else "TTS_VOICE_EN_FEMALE", "en-US-AriaNeural")
    return os.getenv("TTS_VOICE_DEFAULT", "en-US-AriaNeural")


@app.post("/tts")
def tts():
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or payload.get("script") or payload.get("rewrittenScript") or ""
        language = payload.get("language") or "my"
        gender = payload.get("gender") or payload.get("voice_gender") or "female"
        requested_voice = payload.get("voice") or payload.get("voice_id")
        text = clean_srt_to_text(text) or (text or "").strip()
        if not text:
            return json_error("Missing text for TTS", 400)
        voice = select_tts_voice(language, gender, requested_voice)
        output_filename = f"tts_{uuid.uuid4().hex[:8]}.mp3"
        output_path = TTS_DIR / output_filename
        try:
            asyncio.run(_edge_tts_generate(text, voice, output_path))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_edge_tts_generate(text, voice, output_path))
            loop.close()
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("TTS audio file was not created")
        final_srt_text = build_final_srt_from_script(text)
        final_srt_filename = f"final_tts_{uuid.uuid4().hex[:8]}.srt"
        (SRT_DIR / final_srt_filename).write_text(final_srt_text, encoding="utf-8")
        base_url = request.host_url.rstrip("/")
        return jsonify({
            "ok": True,
            "success": True,
            "audio_url": f"{base_url}/tts/{output_filename}",
            "tts_audio_url": f"{base_url}/tts/{output_filename}",
            "audio_filename": output_filename,
            "voice": voice,
            "engine": "edge_tts",
            "final_srt_text": final_srt_text,
            "final_srt_url": f"{base_url}/srt/{final_srt_filename}",
            "final_srt_filename": final_srt_filename,
        })
    except Exception as exc:
        print(f"tts error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/final-srt")
def final_srt():
    try:
        payload = request.get_json(silent=True) or {}
        script = payload.get("script") or payload.get("text") or payload.get("rewrittenScript") or ""
        if not script.strip():
            return json_error("Missing script/text", 400)
        final_srt_text = build_final_srt_from_script(script)
        filename, url = save_srt_response(final_srt_text, "final_srt")
        return jsonify({"ok": True, "success": True, "final_srt_text": final_srt_text, "final_srt_url": url, "filename": filename})
    except Exception as exc:
        print(f"final-srt error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.get("/audio/<path:filename>")
def serve_audio(filename):
    safe_filename = Path(filename).name
    return send_from_directory(AUDIO_DIR, safe_filename, mimetype="audio/mpeg", as_attachment=True, download_name=safe_filename, max_age=0)


@app.get("/srt/<path:filename>")
def serve_srt(filename):
    safe_filename = Path(filename).name
    return send_from_directory(SRT_DIR, safe_filename, mimetype="text/plain; charset=utf-8", as_attachment=True, download_name=safe_filename, max_age=0)


@app.get("/tts/<path:filename>")
def serve_tts(filename):
    safe_filename = Path(filename).name
    return send_from_directory(TTS_DIR, safe_filename, mimetype="audio/mpeg", as_attachment=True, download_name=safe_filename, max_age=0)


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(exc):
    return json_error(f"Upload too large. Limit is {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB.", 413)


@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    return json_error(exc.description or exc.name, exc.code or 500)


@app.errorhandler(Exception)
def handle_unexpected_exception(exc):
    print(f"Unhandled error: {exc}", flush=True)
    return json_error(str(exc), 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
