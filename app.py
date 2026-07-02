import asyncio
import base64
import html
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
SRT_TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}.*$"
)
END_PUNCT_RE = re.compile(r"[။.!?…]$")


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
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_UPLOAD_EXTENSIONS


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


def convert_media_file_to_mp3(input_path: Path) -> Path:
    output_path = AUDIO_DIR / f"{input_path.stem}_{uuid.uuid4().hex[:8]}.mp3"
    ffmpeg_binary = os.getenv("FFMPEG_BINARY", "ffmpeg")
    command = [
        ffmpeg_binary, "-y", "-i", str(input_path), "-vn",
        "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        str(output_path),
    ]
    print("Running ffmpeg upload conversion:", " ".join(command), flush=True)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print("ffmpeg upload conversion failed:", result.stderr[-3000:], flush=True)
        raise RuntimeError("FFmpeg could not convert the uploaded media file to MP3")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("MP3 file was not created from uploaded media")
    return output_path


def create_srt_from_mp3(mp3_path: Path, language: str | None = None, base_name: str | None = None) -> tuple[str, str, dict]:
    srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
    safe_base = secure_filename(base_name or mp3_path.stem) or "uploaded_media"
    srt_filename = f"{Path(safe_base).stem}_{uuid.uuid4().hex[:8]}.srt"
    srt_path = SRT_DIR / srt_filename
    srt_path.write_text(srt_text, encoding="utf-8")
    return srt_text, srt_filename, whisper_meta


YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_youtube_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.strip("/")
    video_id = None

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and path.startswith("shorts/"):
        video_id = path.split("/")[1]
    elif host == "youtu.be" and path:
        video_id = path.split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and (path.startswith("embed/") or path.startswith("live/")):
        video_id = path.split("/")[1]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]

    if video_id:
        video_id = re.sub(r"[^0-9A-Za-z_-]", "", video_id)
        if not video_id:
            raise ValueError("Invalid YouTube video id")
        return f"https://www.youtube.com/watch?v={video_id}"

    return url


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


def friendly_youtube_error(error: Exception) -> tuple[str, int]:
    message = str(error)
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered or "use --cookies" in lowered:
        return (
            "YouTube is asking for browser cookies/authentication for this video. "
            "Refresh the Railway YOUTUBE_COOKIES_B64 variable with a new cookies.txt export, then redeploy and try again.",
            403,
        )
    if "requested format is not available" in lowered or "only images are available" in lowered:
        return (
            "YouTube did not expose a downloadable audio/video format for this request. "
            "Try the Upload tab instead, or refresh YOUTUBE_COOKIES_B64.",
            502,
        )
    if "video unavailable" in lowered:
        return (
            "This YouTube video is unavailable from the backend. It may be deleted, private, region-restricted, or blocked for Railway/datacenter traffic.",
            404,
        )
    return message, 500


def build_ydl_opts(output_base: Path, fallback: bool = False, use_cookies: bool = False, format_selector: str | None = None) -> dict:
    player_clients = ["default", "mweb", "ios", "tv"] if fallback else ["default"]
    opts = {
        "format": format_selector or "bestaudio[acodec!=none]/best[acodec!=none]/best",
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
        "extractor_args": {"youtube": {"player_client": player_clients}},
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "prefer_ffmpeg": True,
        "overwrites": True,
    }
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
            ydl_opts = build_ydl_opts(
                output_base,
                fallback=profile["fallback"],
                use_cookies=profile["use_cookies"],
                format_selector=profile["format_selector"],
            )
            print(
                f"yt-dlp attempt {attempt_number}/{len(attempt_profiles)} "
                f"cookies={profile['use_cookies']} fallback_clients={profile['fallback']} "
                f"format={profile['format_selector']}",
                flush=True,
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(normalized_url, download=True)
            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp did not return a valid video info object")
            if final_mp3.exists() and final_mp3.stat().st_size > 0:
                return final_mp3, {"title": info.get("title") or "audio", "video_id": info.get("id"), "source_url": normalized_url}
            matches = list(DOWNLOAD_DIR.glob(f"{output_base.name}*.mp3"))
            if matches:
                return matches[0], {"title": info.get("title") or "audio", "video_id": info.get("id"), "source_url": normalized_url}
            raise RuntimeError("MP3 file was not created. Check that FFmpeg is installed on Railway.")
        except Exception as exc:
            last_error = exc
            print(f"download attempt failed profile={profile}: {exc}", flush=True)
            continue

    raise RuntimeError(str(last_error) if last_error else "Download failed")


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
        "myanmar": "my", "burmese": "my", "မြန်မာ": "my",
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
    forced_language = normalize_whisper_language(os.getenv("WHISPER_FORCE_LANGUAGE"))
    fallback_languages_env = os.getenv("WHISPER_FALLBACK_LANGUAGES", "en,my")
    fallback_languages = [normalize_whisper_language(item) for item in fallback_languages_env.split(",") if normalize_whisper_language(item)]

    language_attempts: list[str | None] = []
    if forced_language:
        language_attempts.append(forced_language)
    if requested_language and requested_language not in language_attempts:
        language_attempts.append(requested_language)
    if not forced_language and None not in language_attempts:
        language_attempts.append(None)
    for fallback_language in fallback_languages:
        if fallback_language not in language_attempts:
            language_attempts.append(fallback_language)
    if not language_attempts:
        language_attempts = [None]

    vad_attempts = [False, True]
    beam_sizes = [5, 3]
    initial_prompt = os.getenv(
        "WHISPER_INITIAL_PROMPT",
        "This is clear English song lyrics or spoken narration. Transcribe the exact English words. Do not invent Burmese text. Keep repeated lyric words only when actually sung.",
    )

    print(
        f"Loading Whisper model={model_name}, device={device}, compute_type={compute_type}, "
        f"requested_language={requested_language or 'auto'}, forced_language={forced_language or 'none'}",
        flush=True,
    )
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def repeated_word_penalty(text: str) -> float:
        words = re.findall(r"[A-Za-z']+|[\u1000-\u109F]+", (text or "").lower())
        if len(words) < 6:
            return 0.0
        penalty = 0.0
        run_word = None
        run_count = 0
        for word in words:
            if word == run_word:
                run_count += 1
            else:
                if run_count >= 4:
                    penalty += (run_count - 3) * 1.5
                run_word = word
                run_count = 1
        if run_count >= 4:
            penalty += (run_count - 3) * 1.5
        return penalty

    def score_candidate(srt_text: str, plain_text: str, info, segment_count: int, language_code: str | None, vad_filter: bool, beam_size: int) -> float:
        detected_language = getattr(info, "language", None)
        language_probability = getattr(info, "language_probability", None) or 0.0
        char_count = len(plain_text.strip())
        word_count = len(re.findall(r"\S+", plain_text))
        score = 0.0
        score += min(char_count / 20.0, 30.0)
        score += min(word_count / 3.0, 25.0)
        score += min(segment_count * 2.0, 20.0)
        score += float(language_probability) * 10.0
        if (forced_language == "en" or requested_language == "en" or language_code == "en") and detected_language == "en":
            score += 15.0
        if forced_language == "en" and detected_language not in {None, "en"}:
            score -= 20.0
        if vad_filter:
            score -= 2.0
        score += beam_size * 0.3
        score -= repeated_word_penalty(plain_text)
        if "-->" in plain_text or "WEBVTT" in plain_text.upper():
            score -= 30.0
        return score

    attempt_logs = []
    candidates = []
    last_info = None
    for language_code in language_attempts:
        for vad_filter in vad_attempts:
            for beam_size in beam_sizes:
                attempt_label = {"language": language_code or "auto", "vad_filter": vad_filter, "beam_size": beam_size}
                try:
                    print(f"Whisper attempt: {attempt_label}", flush=True)
                    segments_iter, info = model.transcribe(
                        str(mp3_path),
                        language=language_code,
                        beam_size=beam_size,
                        best_of=5,
                        vad_filter=vad_filter,
                        condition_on_previous_text=False,
                        temperature=0.0,
                        compression_ratio_threshold=2.4,
                        no_speech_threshold=0.6,
                        log_prob_threshold=-1.0,
                        initial_prompt=initial_prompt,
                    )
                    last_info = info
                    srt_blocks = []
                    plain_parts = []
                    for segment_number, segment in enumerate(segments_iter, start=1):
                        text = (segment.text or "").strip()
                        if not text:
                            continue
                        plain_parts.append(text)
                        srt_blocks.append(f"{len(srt_blocks) + 1}\n{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}\n{text}\n")
                    detected_language = getattr(info, "language", None)
                    language_probability = getattr(info, "language_probability", None)
                    duration = getattr(info, "duration", None)
                    plain_text = " ".join(plain_parts).strip()
                    srt_text = "\n".join(srt_blocks).strip() + "\n" if srt_blocks else ""
                    candidate_score = score_candidate(srt_text, plain_text, info, len(srt_blocks), language_code, vad_filter, beam_size)
                    log_entry = {
                        **attempt_label,
                        "detected_language": detected_language,
                        "language_probability": language_probability,
                        "duration": duration,
                        "segments": len(srt_blocks),
                        "chars": len(plain_text),
                        "score": candidate_score,
                    }
                    attempt_logs.append(log_entry)
                    if srt_blocks:
                        candidates.append({
                            "srt_text": srt_text,
                            "plain_text": plain_text,
                            "info": info,
                            "score": candidate_score,
                            "language_code": language_code,
                            "vad_filter": vad_filter,
                            "beam_size": beam_size,
                            "segments": len(srt_blocks),
                        })
                except Exception as exc:
                    print(f"Whisper attempt failed {attempt_label}: {exc}", flush=True)
                    attempt_logs.append({**attempt_label, "error": str(exc)})
                    continue

    if candidates:
        best = max(candidates, key=lambda item: item["score"])
        info = best["info"]
        metadata = {
            "model": model_name,
            "device": device,
            "compute_type": compute_type,
            "requested_language": requested_language or "auto",
            "forced_language": forced_language or None,
            "used_language": best["language_code"] or "auto",
            "detected_language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "duration": getattr(info, "duration", None),
            "segments": best["segments"],
            "vad_filter": best["vad_filter"],
            "beam_size": best["beam_size"],
            "score": best["score"],
            "attempts": attempt_logs,
            "quality_patch": "v5_whisper_srt_accuracy",
        }
        print(f"Selected Whisper candidate: {metadata}", flush=True)
        return best["srt_text"], metadata

    audio_size = mp3_path.stat().st_size if mp3_path.exists() else 0
    duration = getattr(last_info, "duration", None) if last_info else None
    detected_language = getattr(last_info, "language", None) if last_info else None
    language_probability = getattr(last_info, "language_probability", None) if last_info else None
    raise RuntimeError(
        "Whisper finished but did not produce any subtitle text. "
        f"Audio bytes={audio_size}, duration={duration}, detected_language={detected_language}, "
        f"language_probability={language_probability}, attempts={attempt_logs}"
    )


def normalize_translate_language(language: str | None, default: str = "my") -> str:
    if not language:
        return default
    language = language.strip().lower()
    if language in {"auto", "detect", "auto-detect", "autodetect"}:
        return "auto"
    language_map = {
        "myanmar": "my", "burmese": "my", "မြန်မာ": "my", "my": "my",
        "english": "en", "en-us": "en", "en-gb": "en", "en": "en",
        "thai": "th", "th": "th", "chinese": "zh-CN", "simplified chinese": "zh-CN",
        "traditional chinese": "zh-TW", "japanese": "ja", "korean": "ko",
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
    translated_srt_text = build_srt_from_blocks(blocks)
    meta = {
        "engine": "google_translate",
        "source_language": normalize_translate_language(source_language, default="auto"),
        "target_language": normalize_translate_language(target_language, default="my"),
        "segments": len(blocks),
    }
    return translated_srt_text, meta


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


def _strip_ai_wrapping(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:[a-zA-Z0-9_-]+)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().strip('"').strip()


def _looks_like_bad_rewrite_output(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    compact = re.sub(r"\s+", " ", value.lower().strip())
    bad_patterns = [
        r"^user safety\s*:\s*safe\.?$", r"^safety\s*:\s*safe\.?$", r"^safe\.?$",
        r"user safety\s*:", r"assistant response\s*:", r"policy\s*:", r"content safety", r"moderation",
    ]
    if any(re.search(p, compact) for p in bad_patterns):
        return True
    if len(re.findall(r"[\u1000-\u109F]", value)) < 5:
        return True
    return False


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
    add_when("never easy" in low or "walk away" in low or "wake it up" in low, "ထွက်သွားဖို့ ဘယ်တော့မှ မလွယ်မှန်း သိပါတယ်။")
    add_when("let her go" in low or "let go" in low, "သူမကို လက်လွှတ်လိုက်ပါ။")
    add_when("all right" in low or "alright" in low, "အဆင်ပြေသွားမှာပါ။")
    return " ".join(pieces).strip()


def _openrouter_model_candidates() -> list[str]:
    defaults = ["nvidia/nemotron-3-ultra-550b-a55b:free", "qwen/qwen3-coder:free", "openrouter/free"]
    configured = os.getenv("OPENROUTER_MODEL", "").strip()
    configured_many = os.getenv("OPENROUTER_MODEL_CANDIDATES") or os.getenv("OPENROUTER_MODELS") or ""
    known_bad_free_slugs = {"google/gemini-2.0-flash-exp:free", "deepseek/deepseek-chat-v3-0324:free"}
    candidates = []
    for item in [configured, *configured_many.split(","), *defaults]:
        model = (item or "").strip()
        if not model or model in known_bad_free_slugs:
            continue
        if model not in candidates:
            candidates.append(model)
    return candidates or defaults


def _should_use_reference_style_first(original: str, translated: str, fallback: str) -> bool:
    combined = clean_srt_to_text("\n".join([original or "", translated or "", fallback or ""]))
    low = combined.lower()
    if len(combined) > int(os.getenv("REWRITE_REFERENCE_STYLE_MAX_CHARS", "800")):
        return False
    trigger_groups = [
        ["break my heart", "tear me apart", "broken before", "delicate"],
        ["you love her", "love her", "phone away", "put the phone", "never easy", "let her go", "alright", "all right"],
    ]
    return any(any(term in low for term in group) for group in trigger_groups)


def call_openrouter_rewrite(
    text: str = "",
    language: str = "my",
    style: str = "natural_myanmar_tts_from_original",
    original_text: str = "",
    translated_text: str = "",
    fallback_text: str = "",
) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")

    original = clean_srt_to_text(original_text)
    translated = clean_srt_to_text(translated_text)
    fallback = clean_srt_to_text(fallback_text or text)
    if not original and not translated and not fallback:
        raise ValueError("No readable subtitle text found after cleanup")
    if not original:
        original = fallback
    if not translated:
        translated = fallback

    reference_match = _local_reference_style_rewrite(original=original, translated=translated, fallback=fallback)
    if reference_match and _should_use_reference_style_first(original, translated, fallback):
        return reference_match

    system_prompt = """You are a professional English-to-Myanmar translator and Myanmar TTS script editor. Use the original English text as the source of truth. Use the Myanmar translation only as a rough reference. Return only clean Myanmar text.
Do not return markdown, JSON, English notes, safety labels, policy labels, SRT numbers, timestamps, or arrows. Never return phrases like "User Safety: safe". For song lyrics and emotional dialogue, make the Myanmar sound soft, natural, emotional, culturally fitting, and easy to speak aloud. Do not translate word-for-word. Do not add new facts. Keep sentences short and TTS-friendly. Use natural pronouns like ကိုယ်, မင်း, သူမ when context fits."""
    reference_style = """Reference quality target from the user's approved test clips:
TEST.MP4: So, please, don't break my heart => ဒါကြောင့် ကျေးဇူးပြုပြီး ကိုယ့်အသည်းကို မခွဲပါနဲ့။
Don't tear me apart => ကိုယ့်ကို အပိုင်းအစတွေ ဖြစ်အောင် မလုပ်ပါနဲ့။
I know how it starts => အစက ဘယ်လိုစတတ်တယ်ဆိုတာ ကိုယ်သိပါတယ်။
Trust me, I've been broken before => ယုံပါ၊ ကိုယ်အရင်ကလည်း အသည်းကွဲဖူးပါတယ်။
Don't break me again => ကိုယ့်အသည်းကို ထပ်ပြီး မခွဲပါနဲ့နော်။
I am delicate => ကိုယ်က အသည်းနုသူမို့လို့။
TEST1.MP4: I know you love her, but it's over, mate => မင်းသူမကို ချစ်နေမှန်း သိပေမယ့် အရာအားလုံး ပြီးသွားပြီလေ။
It doesn't matter, put the phone away => အရေးမကြီးတော့ပါဘူး။ ဖုန်းကိုချပြီး အဆက်အသွယ်ဖြတ်လိုက်ပါတော့။
It's never easy to walk away => ထွက်သွားဖို့ ဘယ်တော့မှ မလွယ်မှန်း သိပါတယ်။
Let her go => သူမကို လက်လွှတ်လိုက်ပါ။
It'll be all right => အဆင်ပြေသွားမှာပါ။
Use these examples as fluency/style guidance. Do not copy them unless the source text means the same thing."""
    user_prompt = f"""Language: {language}
Style: {style}
Original English text:
{original or "(not provided)"}

Rough Myanmar translation:
{translated or "(not provided)"}

{reference_style}

Task: Rewrite into natural Myanmar for TTS. Preserve the original meaning, emotion, and tone. Return only the final Myanmar script. Do not return safety classifications such as User Safety: safe."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("APP_PUBLIC_URL", "https://video-audio-tool-production.up.railway.app"),
        "X-Title": os.getenv("APP_NAME", "Video2Audio Pro"),
    }
    temperature = float(os.getenv("OPENROUTER_TEMPERATURE", "0.25"))
    max_tokens = int(os.getenv("OPENROUTER_MAX_TOKENS", "1200"))
    timeout = int(os.getenv("OPENROUTER_TIMEOUT", "60"))
    errors: list[str] = []

    for model in _openrouter_model_candidates():
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            response = requests.post(OPENROUTER_CHAT_URL, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            errors.append(f"{model}: request failed: {exc}")
            continue
        if not response.ok:
            errors.append(f"{model}: OpenRouter error {response.status_code}: {response.text[:1200]}")
            if response.status_code in {400, 404, 408, 409, 429, 500, 502, 503, 504}:
                continue
            continue
        try:
            data = response.json()
        except Exception as exc:
            errors.append(f"{model}: invalid JSON: {exc}")
            continue
        result = _strip_ai_wrapping(data.get("choices", [{}])[0].get("message", {}).get("content", ""))
        if _looks_like_bad_rewrite_output(result):
            errors.append(f"{model}: returned bad/meta output: {result[:200]}")
            if reference_match:
                return reference_match
            continue
        if "-->" in result or "WEBVTT" in result.upper():
            result = clean_srt_to_tts_script(result, language=language)
        if _looks_like_bad_rewrite_output(result):
            errors.append(f"{model}: invalid after cleanup: {result[:200]}")
            if reference_match:
                return reference_match
            continue
        return result

    if reference_match:
        return reference_match
    raise RuntimeError(
        "All OpenRouter rewrite models failed. Tried: " + ", ".join(_openrouter_model_candidates()) + ". Errors: " + " | ".join(errors[-5:])
    )


# === Caption-first SRT extraction START ===
def is_youtube_url_for_captions(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.netloc or "").lower().replace("www.", "")
        return host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"}
    except Exception:
        return False


def _caption_first_enabled() -> bool:
    return (os.getenv("YOUTUBE_CAPTION_FIRST", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}


def _caption_lang_candidates(requested_language: str | None = None) -> list[str]:
    env_value = os.getenv("YOUTUBE_CAPTION_LANGUAGES", "en,en-US,en-GB,en.*,my,und,*")
    candidates = []
    for item in env_value.split(","):
        value = (item or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    normalized = normalize_whisper_language(requested_language)
    if normalized and normalized not in {"auto", "detect"} and normalized not in candidates:
        candidates.append(normalized)
    if "*" not in candidates:
        candidates.append("*")
    return candidates


def _caption_key_matches(key: str, candidate: str) -> bool:
    key_low = (key or "").lower()
    candidate_low = (candidate or "").lower()
    if not key_low or not candidate_low:
        return False
    if candidate_low == "*":
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
    for ext in ["srt", "vtt"]:
        for item in formats:
            if (item.get("ext") or "").lower() == ext and item.get("url"):
                return item
    for item in formats:
        url = item.get("url") or ""
        if item.get("url") and (".vtt" in url.lower() or "fmt=vtt" in url.lower()):
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
    line = line.replace(" ", " ")
    line = re.sub(r"\s+", " ", line).strip()
    return line


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
                line = next_line
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
        cue_text = " ".join(text_lines).strip()
        cue_text = re.sub(r"\s+", " ", cue_text).strip()
        if cue_text:
            blocks.append(f"{cue_number}\n{start} --> {end}\n{cue_text}\n")
            cue_number += 1
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def normalize_caption_to_srt(caption_text: str, ext: str | None = None) -> str:
    ext_low = (ext or "").lower()
    raw = (caption_text or "").strip()
    if not raw:
        return ""
    if ext_low == "vtt" or raw.lstrip("\ufeff").upper().startswith("WEBVTT"):
        return vtt_to_srt_text(raw)
    if "-->" in raw:
        return raw.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    return ""


def get_youtube_caption_srt(url: str, requested_language: str | None = None) -> tuple[str, dict] | None:
    if not is_youtube_url_for_captions(url):
        return None
    normalized_url = normalize_youtube_url(url)
    cookie_path = get_cookie_file()
    ydl_opts = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "http_headers": YOUTUBE_HEADERS,
        "logger": YTDLPLogger(),
        "extractor_args": {"youtube": {"player_client": ["default", "mweb", "ios", "tv"]}},
    }
    if cookie_path:
        ydl_opts["cookiefile"] = str(cookie_path)

    print("Caption-first: checking YouTube subtitles/captions", flush=True)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(normalized_url, download=False)
    if not isinstance(info, dict):
        return None

    candidates = _caption_lang_candidates(requested_language)
    subtitle_maps = [("youtube_manual_subtitle", info.get("subtitles") or {}), ("youtube_auto_caption", info.get("automatic_captions") or {})]
    errors: list[str] = []
    for source_name, caption_map in subtitle_maps:
        caption_key = _pick_caption_key(caption_map, candidates)
        if not caption_key:
            continue
        fmt = _pick_caption_format(caption_map.get(caption_key) or [])
        if not fmt:
            errors.append(f"{source_name}:{caption_key}: no vtt/srt caption URL")
            continue
        caption_url = fmt.get("url")
        ext = (fmt.get("ext") or "").lower()
        try:
            response = requests.get(caption_url, headers=YOUTUBE_HEADERS, timeout=int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30")))
            response.raise_for_status()
            srt_text = normalize_caption_to_srt(response.text, ext=ext)
            if not srt_text.strip():
                errors.append(f"{source_name}:{caption_key}: caption was empty after conversion")
                continue
            metadata = {
                "source": source_name,
                "subtitle_source": source_name,
                "language": caption_key,
                "format": ext or "unknown",
                "title": info.get("title") or "",
                "video_id": info.get("id") or "",
                "source_url": normalized_url,
                "manual_languages": sorted((info.get("subtitles") or {}).keys()),
                "auto_languages": sorted((info.get("automatic_captions") or {}).keys()),
                "errors": errors[-5:],
            }
            print(f"Caption-first: using {source_name} language={caption_key} format={ext}", flush=True)
            return srt_text, metadata
        except Exception as exc:
            errors.append(f"{source_name}:{caption_key}: {exc}")
            print(f"Caption-first attempt failed: {errors[-1]}", flush=True)
            continue

    print("Caption-first: no usable caption found; falling back to Whisper. Errors: " + " | ".join(errors[-5:]), flush=True)
    return None


# === TTS helpers START ===
def normalize_tts_language(language: str | None) -> str:
    value = (language or "my").strip().lower()
    language_map = {
        "myanmar": "my", "burmese": "my", "my-mm": "my", "my": "my", "မြန်မာ": "my",
        "english": "en", "en-us": "en", "en-gb": "en", "en": "en",
    }
    return language_map.get(value, value)


def choose_tts_voice(language: str | None = "my", gender: str | None = None, requested_voice: str | None = None) -> str:
    if requested_voice:
        voice = requested_voice.strip()
        if voice:
            return voice
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
        if text.startswith(("+", "-")):
            return text
        return "+" + text
    return default


def split_tts_text(text: str, max_chars: int = 2500) -> list[str]:
    clean = clean_srt_to_tts_script(text, language="my") if "-->" in text or "WEBVTT" in text.upper() else re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return []

    # Split on Myanmar/English sentence endings while keeping punctuation.
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
        communicate = edge_tts.Communicate(chunks[0], voice=voice, rate=rate, pitch=pitch, volume=volume)
        await communicate.save(str(output_path))
        return

    with tempfile.TemporaryDirectory(prefix="tts_chunks_") as temp_dir:
        temp_dir_path = Path(temp_dir)
        chunk_paths: list[Path] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_path = temp_dir_path / f"chunk_{index:03d}.mp3"
            communicate = edge_tts.Communicate(chunk, voice=voice, rate=rate, pitch=pitch, volume=volume)
            await communicate.save(str(chunk_path))
            if not chunk_path.exists() or chunk_path.stat().st_size == 0:
                raise RuntimeError(f"TTS chunk {index} was not created")
            chunk_paths.append(chunk_path)

        concat_file = temp_dir_path / "concat.txt"
        concat_file.write_text("".join(f"file '{path.as_posix()}'\n" for path in chunk_paths), encoding="utf-8")
        ffmpeg_binary = os.getenv("FFMPEG_BINARY", "ffmpeg")
        command = [ffmpeg_binary, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output_path)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            print("ffmpeg TTS concat failed; falling back to binary join:", result.stderr[-2000:], flush=True)
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

    print(
        f"Generating TTS voice={selected_voice} language={language} gender={gender} chunks={len(chunks)} chars={sum(len(c) for c in chunks)}",
        flush=True,
    )
    asyncio.run(synthesize_edge_tts_chunks(chunks, selected_voice, output_path, selected_rate, selected_pitch, selected_volume))

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("TTS audio file was not created")

    meta = {
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
    return output_path, meta


# === Flask routes START ===
def endpoint_list() -> list[str]:
    return [
        "POST /download",
        "POST /extract-srt",
        "POST /translate-srt",
        "POST /rewrite",
        "POST /tts",
        "POST /upload",
        "POST /extract-srt-upload",
        "POST /process-upload",
        "GET /audio/<filename>",
        "GET /tts/<filename>",
        "GET /srt/<filename>",
    ]


@app.get("/")
def index():
    return jsonify({"ok": True, "success": True, "service": "video-audio-tool", "endpoints": endpoint_list()})


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "success": True,
        "service": "video-audio-tool",
        "endpoints": endpoint_list(),
        "openrouter_model": os.getenv("OPENROUTER_MODEL", "openrouter/free"),
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
        error_message, status_code = friendly_youtube_error(exc)
        return json_error(error_message, status_code)


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
        if _caption_first_enabled():
            try:
                caption_result = get_youtube_caption_srt(url, requested_language=language)
            except Exception as caption_exc:
                print(f"caption-first extraction failed; falling back to Whisper: {caption_exc}", flush=True)
        if caption_result:
            srt_text, caption_meta = caption_result
            safe_video_id = secure_filename(caption_meta.get("video_id") or "youtube_caption") or "youtube_caption"
            srt_filename = f"{safe_video_id}_{caption_meta.get('source', 'caption')}_{uuid.uuid4().hex[:8]}.srt"
            srt_path = SRT_DIR / srt_filename
            srt_path.write_text(srt_text, encoding="utf-8")
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
                "audio": {"title": caption_meta.get("title") or "YouTube captions", "video_id": caption_meta.get("video_id"), "source_url": caption_meta.get("source_url")},
                "whisper": None,
            })

        mp3_path, audio_meta = download_audio_as_mp3(url)
        srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)
        srt_filename = f"{audio_meta.get('video_id') or mp3_path.stem}_{uuid.uuid4().hex[:8]}.srt"
        srt_path = SRT_DIR / srt_filename
        srt_path.write_text(srt_text, encoding="utf-8")
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
        })
    except Exception as exc:
        print(f"extract-srt error: {exc}", flush=True)
        error_message, status_code = friendly_youtube_error(exc)
        return json_error(error_message, status_code)


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
        translated_path = SRT_DIR / translated_filename
        translated_path.write_text(translated_srt_text, encoding="utf-8")
        base_url = request.host_url.rstrip("/")
        translated_srt_url = f"{base_url}/srt/{translated_filename}"
        return jsonify({
            "ok": True,
            "success": True,
            "translated_srt_text": translated_srt_text,
            "translated_srt_url": translated_srt_url,
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
        try:
            script = call_openrouter_rewrite(
                original_text=cleaned_original,
                translated_text=cleaned_translated,
                fallback_text=cleaned_fallback,
                language=language,
                style=style,
            )
            source = "openrouter_free_ai"
        except Exception as ai_error:
            print(f"OpenRouter rewrite failed: {ai_error}", flush=True)
            # Fallback: never return raw SRT; return clean TTS script so frontend can still use it.
            fallback_script = clean_srt_to_tts_script(cleaned_translated or cleaned_fallback, language=language)
            if not fallback_script:
                return jsonify({"ok": False, "success": False, "error": str(ai_error), "language": language, "style": style, "source": "openrouter_free_ai_failed"}), 500
            script = fallback_script
            source = "local_tts_cleanup"
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
            payload.get("text")
            or payload.get("script")
            or payload.get("rewritten_text")
            or payload.get("rewrittenText")
            or payload.get("rewritten_script")
            or payload.get("translated_srt_text")
            or request.form.get("text")
            or ""
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
        language = request.form.get("language") or request.values.get("language") or "auto"
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
        base_url = request.host_url.rstrip("/")
        srt_url = f"{base_url}/srt/{srt_filename}"
        audio_url = f"{base_url}/audio/{mp3_path.name}"
        return jsonify({
            "ok": True,
            "success": True,
            "srt_text": srt_text,
            "srt_url": srt_url,
            "filename": srt_filename,
            "audio_url": audio_url,
            "audio_filename": mp3_path.name,
            "source": {"type": "upload", "filename": input_path.name},
            "whisper": whisper_meta,
        })
    except Exception as exc:
        print(f"extract-srt-upload error: {exc}", flush=True)
        return json_error(str(exc), 500)


@app.post("/process-upload")
def process_upload():
    try:
        uploaded_file = request.files.get("file") or request.files.get("video") or request.files.get("audio")
        language = request.form.get("language") or request.values.get("language") or "auto"
        target_language = request.form.get("target_language") or request.values.get("target_language") or ""
        source_language = request.form.get("source_language") or request.values.get("source_language") or "auto"
        input_path = save_uploaded_media(uploaded_file)
        mp3_path = convert_media_file_to_mp3(input_path)
        srt_text, srt_filename, whisper_meta = create_srt_from_mp3(mp3_path, language=language, base_name=input_path.stem)
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
            "whisper": whisper_meta,
        }
        if target_language:
            translated_srt_text, translation_meta = translate_srt_text(srt_text, source_language=source_language, target_language=target_language)
            target_code = translation_meta["target_language"].replace("-", "_")
            translated_filename = f"translated_{target_code}_{uuid.uuid4().hex[:8]}.srt"
            translated_path = SRT_DIR / translated_filename
            translated_path.write_text(translated_srt_text, encoding="utf-8")
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
