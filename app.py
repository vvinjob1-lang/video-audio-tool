import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
UPLOAD_DIR = BASE_DIR / "uploads"
SRT_DIR = BASE_DIR / "srt"
COOKIE_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
SRT_DIR.mkdir(exist_ok=True)

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


def srt_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format: HH:MM:SS,mmm."""
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
    """Return faster-whisper language code or None for auto-detect."""
    if not language:
        return None

    language = language.strip().lower()
    if language in {"auto", "detect", "auto-detect", "autodetect"}:
        return None

    language_map = {
        "myanmar": "my",
        "burmese": "my",
        "မြန်မာ": "my",
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
    }
    return language_map.get(language, language)


def transcribe_mp3_to_srt(mp3_path: Path, language: str | None = None) -> tuple[str, dict]:
    """Transcribe audio with faster-whisper and return SRT text + metadata."""
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Check requirements.txt and Railway deployment logs."
        ) from exc

    model_name = os.getenv("WHISPER_MODEL", "tiny")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    language_code = normalize_whisper_language(language)

    print(
        f"Loading Whisper model={model_name}, device={device}, compute_type={compute_type}, language={language_code or 'auto'}",
        flush=True,
    )

    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    segments, info = model.transcribe(
        str(mp3_path),
        language=language_code,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )

    srt_blocks = []
    segment_number = 0

    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue

        segment_number += 1
        srt_blocks.append(
            f"{segment_number}\n"
            f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}\n"
            f"{text}\n"
        )

    srt_text = "\n".join(srt_blocks).strip() + "\n" if srt_blocks else ""

    metadata = {
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "detected_language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": len(srt_blocks),
    }

    if not srt_text.strip():
        raise RuntimeError("Whisper finished but did not produce any subtitle text.")

    return srt_text, metadata



def normalize_translate_language(language: str | None, default: str = "my") -> str:
    """Return a Google Translate/deep-translator language code."""
    if not language:
        return default

    language = language.strip().lower()
    if language in {"auto", "detect", "auto-detect", "autodetect"}:
        return "auto"

    language_map = {
        "myanmar": "my",
        "burmese": "my",
        "မြန်မာ": "my",
        "my": "my",
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
        "en": "en",
        "thai": "th",
        "th": "th",
        "chinese": "zh-CN",
        "simplified chinese": "zh-CN",
        "traditional chinese": "zh-TW",
        "japanese": "ja",
        "korean": "ko",
        "spanish": "es",
        "french": "fr",
        "german": "de",
    }
    return language_map.get(language, language)


def parse_srt_blocks(srt_text: str) -> list[dict]:
    """Parse SRT into blocks while preserving index and timestamp lines."""
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
        text_lines = []

        if lines and re.fullmatch(r"\d+", lines[0].strip()):
            idx = lines.pop(0).strip()

        if lines and "-->" in lines[0]:
            time_line = lines.pop(0).strip()

        text_lines = lines

        if time_line and text_lines:
            parsed_blocks.append(
                {
                    "index": idx,
                    "time": time_line,
                    "text": " ".join(text_lines).strip(),
                }
            )

    return parsed_blocks


def build_srt_from_blocks(blocks: list[dict]) -> str:
    """Build valid SRT text from parsed/translated blocks."""
    output_blocks = []
    for position, block in enumerate(blocks, start=1):
        output_blocks.append(
            f"{position}\n"
            f"{block['time']}\n"
            f"{(block.get('translated_text') or block.get('text') or '').strip()}\n"
        )
    return "\n".join(output_blocks).strip() + "\n" if output_blocks else ""


def translate_texts_with_google(texts: list[str], source_language: str = "auto", target_language: str = "my") -> list[str]:
    """Translate a list of subtitle texts using Google Translate via deep-translator."""
    try:
        from deep_translator import GoogleTranslator
    except Exception as exc:
        raise RuntimeError(
            "deep-translator is not installed. Check requirements.txt and Railway deployment logs."
        ) from exc

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
    translated_texts = translate_texts_with_google(
        original_texts,
        source_language=source_language,
        target_language=target_language,
    )

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


@app.get("/")
def index():
    return jsonify(
        {
            "ok": True,
            "service": "video-audio-tool",
            "endpoints": [
                "POST /download",
                "POST /extract-srt",
                "POST /translate-srt",
                "GET /srt/<filename>",
            ],
        }
    )


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


@app.post("/extract-srt")
def extract_srt():
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url") or request.values.get("url")
        language = payload.get("language") or request.form.get("language") or request.values.get("language") or "auto"

        if not url:
            return jsonify({"success": False, "error": "Missing 'url'"}), 400

        mp3_path, audio_meta = download_audio_as_mp3(url)
        srt_text, whisper_meta = transcribe_mp3_to_srt(mp3_path, language=language)

        srt_filename = f"{audio_meta.get('video_id') or mp3_path.stem}_{uuid.uuid4().hex[:8]}.srt"
        srt_path = SRT_DIR / srt_filename
        srt_path.write_text(srt_text, encoding="utf-8")

        base_url = request.host_url.rstrip("/")
        srt_url = f"{base_url}/srt/{srt_filename}"

        return jsonify(
            {
                "success": True,
                "srt_text": srt_text,
                "srt_url": srt_url,
                "filename": srt_filename,
                "audio": audio_meta,
                "whisper": whisper_meta,
            }
        )

    except Exception as exc:
        print(f"extract-srt error: {exc}", flush=True)
        return jsonify({"success": False, "error": str(exc)}), 500



@app.post("/translate-srt")
def translate_srt():
    try:
        payload = request.get_json(silent=True) or {}
        srt_text = payload.get("srt_text") or payload.get("srt") or ""
        filename = payload.get("filename") or ""
        source_language = payload.get("source_language") or payload.get("source") or "auto"
        target_language = payload.get("target_language") or payload.get("target") or payload.get("language") or "my"

        # Frontend should normally send srt_text directly. Filename fallback is useful for testing.
        if not srt_text and filename:
            safe_filename = Path(filename).name
            srt_path = SRT_DIR / safe_filename
            if not srt_path.exists():
                return jsonify({"success": False, "error": "SRT filename not found"}), 404
            srt_text = srt_path.read_text(encoding="utf-8")

        if not srt_text:
            return jsonify({"success": False, "error": "Missing 'srt_text'"}), 400

        translated_srt_text, translation_meta = translate_srt_text(
            srt_text,
            source_language=source_language,
            target_language=target_language,
        )

        target_code = translation_meta["target_language"].replace("-", "_")
        translated_filename = f"translated_{target_code}_{uuid.uuid4().hex[:8]}.srt"
        translated_path = SRT_DIR / translated_filename
        translated_path.write_text(translated_srt_text, encoding="utf-8")

        base_url = request.host_url.rstrip("/")
        translated_srt_url = f"{base_url}/srt/{translated_filename}"

        return jsonify(
            {
                "success": True,
                "translated_srt_text": translated_srt_text,
                "translated_srt_url": translated_srt_url,
                "filename": translated_filename,
                "translation": translation_meta,
            }
        )

    except Exception as exc:
        print(f"translate-srt error: {exc}", flush=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.get("/srt/<path:filename>")
def serve_srt(filename):
    safe_filename = Path(filename).name
    return send_from_directory(
        SRT_DIR,
        safe_filename,
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=safe_filename,
        max_age=0,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
