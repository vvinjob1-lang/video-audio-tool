import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

APP_VERSION = "v17.3.1-download-route-fix"

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DOWNLOAD_DIR = BASE_DIR / "downloads"
SRT_DIR = BASE_DIR / "srt"
SCRIPT_DIR = BASE_DIR / "scripts"
for folder in (UPLOAD_DIR, DOWNLOAD_DIR, SRT_DIR, SCRIPT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://video-audio-tool-production.up.railway.app").rstrip("/")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024

CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)


# -----------------------------
# Generic helpers
# -----------------------------

def now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def uid(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_quality_mode(value: Optional[str] = None) -> str:
    """User-facing output quality tier.

    basic   = faster/cheaper, Google translation + Qwen/Flash rewrite + Edge TTS.
    premium = AI translation + Qwen/DeepSeek-Pro polish + stricter gates + optional stepaudio.
    """
    raw = (value or os.getenv("OUTPUT_QUALITY_DEFAULT", "basic") or "basic").strip().lower()
    if raw in {"premium", "pro", "quality", "high", "hq"}:
        return "premium"
    return "basic"


def public_url(prefix: str, filename: str, download_name: Optional[str] = None) -> str:
    url = f"{PUBLIC_BASE_URL}/{prefix.strip('/')}/{filename}"
    if download_name:
        url += f"?download_name={requests.utils.quote(download_name)}"
    return url


# -----------------------------
# YouTube / SRT reliability helpers
# -----------------------------

def youtube_cookiefile() -> Optional[str]:
    """Return a cookie file path for yt-dlp if available.

    Supported sources:
    1) YOUTUBE_COOKIES_B64 / YT_COOKIES_B64: base64 encoded Netscape cookies.txt
    2) YOUTUBE_COOKIES_TXT / YT_COOKIES_TXT: raw Netscape cookies.txt text
    3) cookies.txt committed next to app.py
    """
    env_b64 = os.getenv("YOUTUBE_COOKIES_B64") or os.getenv("YT_COOKIES_B64")
    env_txt = os.getenv("YOUTUBE_COOKIES_TXT") or os.getenv("YT_COOKIES_TXT")
    fp = BASE_DIR / "cookies.txt"
    try:
        if env_b64:
            raw = base64.b64decode(env_b64.strip()).decode("utf-8", errors="ignore")
            if raw.strip():
                fp.write_text(raw, encoding="utf-8")
        elif env_txt:
            raw = env_txt.replace("\\n", "\n")
            if raw.strip():
                fp.write_text(raw, encoding="utf-8")
    except Exception:
        pass
    if fp.exists() and fp.stat().st_size > 20:
        return str(fp)
    return None


def is_youtube_bot_check_error(text: object) -> bool:
    lower = str(text or "").lower()
    markers = [
        "sign in to confirm",
        "not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "use --cookies",
        "cookies-from-browser",
        "bot check",
        "verify that you are not a bot",
    ]
    return any(m in lower for m in markers)


def extract_youtube_video_id(url: str) -> Optional[str]:
    url = (url or "").strip()
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/watch\?[^#]*v=([A-Za-z0-9_-]{6,})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/live/([A-Za-z0-9_-]{6,})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # Last resort: plain 11-char video id.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def transcript_items_to_srt(items: List[Dict[str, object]]) -> str:
    blocks: List[Dict[str, str]] = []
    for i, it in enumerate(items, start=1):
        txt = remove_html_tags(str(it.get("text") or "")).replace("\n", " ")
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        start = float(it.get("start") or 0.0)
        duration = float(it.get("duration") or 2.5)
        end = max(start + 0.5, start + duration)
        blocks.append({"index": str(len(blocks) + 1), "start": seconds_to_srt_time(start), "end": seconds_to_srt_time(end), "text": txt})
    return compose_srt(blocks) if blocks else ""


def try_youtube_transcript_api_srt(url: str, language: str = "auto") -> Tuple[Optional[str], Dict[str, object]]:
    """Try YouTubeTranscriptApi before yt-dlp.

    This often works when yt-dlp metadata extraction is blocked by YouTube bot checks.
    It is optional; if the dependency is unavailable or YouTube blocks it, we fall back.
    """
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None, {"transcript_error": "not_youtube_or_video_id_not_found"}
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except Exception as exc:
        return None, {"transcript_error": f"youtube_transcript_api_unavailable: {exc}"}

    preferred = []
    if language and language not in {"auto", "best"}:
        preferred.append(language)
    preferred += ["en", "en-US", "en-GB", "my"]

    attempts: List[Dict[str, object]] = []
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        try:
            transcript = transcript_list.find_manually_created_transcript(preferred)
        except Exception as exc:
            attempts.append({"manual_error": str(exc)[:300]})
        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(preferred)
            except Exception as exc:
                attempts.append({"generated_error": str(exc)[:300]})
        if transcript is None:
            # Use the first available transcript if language preference did not match.
            for tr in transcript_list:
                transcript = tr
                break
        if transcript is None:
            return None, {"transcript_error": "no_transcript_found", "attempts": attempts, "video_id": video_id}
        fetched = transcript.fetch()
        # youtube-transcript-api may return dataclass-like objects in newer versions.
        items: List[Dict[str, object]] = []
        for item in fetched:
            if isinstance(item, dict):
                items.append(item)
            else:
                items.append({
                    "text": getattr(item, "text", ""),
                    "start": getattr(item, "start", 0.0),
                    "duration": getattr(item, "duration", 2.5),
                })
        srt = transcript_items_to_srt(items)
        if parse_srt_blocks(srt) or clean_srt_to_text(srt):
            return srt, {
                "caption_source": "youtube_transcript_api",
                "video_id": video_id,
                "language_code": getattr(transcript, "language_code", None),
                "is_generated": getattr(transcript, "is_generated", None),
                "segments": len(items),
                "attempts": attempts,
            }
        return None, {"transcript_error": "transcript_returned_empty", "video_id": video_id, "attempts": attempts}
    except Exception as exc:
        return None, {"transcript_error": str(exc)[:1000], "video_id": video_id, "is_bot_check": is_youtube_bot_check_error(exc)}


def manual_srt_fallback_response(url: str, caption_meta: Optional[Dict[str, object]] = None, whisper_error: str = "", reason: str = "auto_extract_failed"):
    encoded = requests.utils.quote(url, safe="")
    return jsonify({
        "success": False,
        "ok": False,
        "accepted_mode": True,
        "needs_manual_srt_upload": True,
        "needs_upload": True,
        "reason": reason,
        "message": "Automatic extraction did not work from our server. Open Downsub/SubDown and upload the SRT here to continue.",
        "user_message": "YouTube blocked server subtitle extraction. Open Downsub/SubDown, download the SRT/VTT file, then upload it here to continue.",
        "open_downsub_url": f"https://downsub.com/?url={encoded}",
        "open_subdown_url": f"https://subdown.org/youtube-subtitle-downloader?url={encoded}",
        "caption_attempt": caption_meta or {},
        "whisper_error": whisper_error,
        "bot_check": is_youtube_bot_check_error(caption_meta) or is_youtube_bot_check_error(whisper_error),
        "cookie_configured": bool(youtube_cookiefile()),
    }), 200


def json_error(message: str, status: int = 400, **extra):
    payload = {"success": False, "ok": False, "error": message, **extra}
    return jsonify(payload), status


def safe_name(name: str, fallback_ext: str = "") -> str:
    name = secure_filename(name or "")
    if not name:
        name = f"file_{uid()}{fallback_ext}"
    return name


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")


def resolve_existing_file(folder: Path, filename: str) -> Optional[Path]:
    """Resolve a generated file safely without breaking existing filenames.

    Generated filenames are already controlled by the backend, but download
    routes still receive URL path text from the browser.  Older routes used
    only safe_name(filename), which can change the name and make an existing
    file look missing.  Try the raw basename first, then Flask/Werkzeug-safe
    variants.
    """
    raw_name = os.path.basename(str(filename or "")).strip()
    if not raw_name:
        return None

    candidates = [
        folder / raw_name,
        folder / secure_filename(raw_name),
        folder / safe_name(raw_name),
    ]

    seen = set()
    for fp in candidates:
        key = str(fp)
        if key in seen:
            continue
        seen.add(key)
        try:
            if fp.exists() and fp.is_file():
                return fp
        except Exception:
            continue
    return None


def file_not_found_error(kind: str, folder: Path, filename: str):
    raw_name = os.path.basename(str(filename or "")).strip()
    candidates = [
        str(folder / raw_name) if raw_name else str(folder),
        str(folder / secure_filename(raw_name)) if raw_name else str(folder),
        str(folder / safe_name(raw_name)) if raw_name else str(folder),
    ]
    return json_error(
        f"{kind} file not found",
        404,
        filename=filename,
        raw_name=raw_name,
        checked_dir=str(folder),
        checked_paths=list(dict.fromkeys(candidates)),
    )


def run_cmd(cmd: List[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)


def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_exists() -> bool:
    return shutil.which("ffprobe") is not None


def audio_duration_seconds(path: Path) -> Optional[float]:
    if not ffprobe_exists() or not path.exists():
        return None
    try:
        cp = run_cmd([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ], timeout=30)
        if cp.returncode == 0:
            val = float(cp.stdout.strip())
            if val > 0:
                return val
    except Exception:
        return None
    return None


# -----------------------------
# Subtitle parsing / cleaning
# -----------------------------

TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}.*$"
)
INDEX_RE = re.compile(r"^\s*\d+\s*$")
BAD_TEXT_PATTERNS = [
    "Error 500", "Server Error", "That’s an error", "That's an error", "Please try again later",
    "We need to", "Let's tackle", "Return only", "Input subtitle text", "Preserve names",
    "The segment", "Possible translation", "In Myanmar", "Do not include", "reasoning_content",
]


def is_timestamp_line(line: str) -> bool:
    return bool(TIMESTAMP_RE.match(line or ""))


def normalize_timestamp(ts: str) -> str:
    ts = ts.strip().replace(".", ",")
    # Ensure milliseconds have three digits.
    if "," in ts:
        head, ms = ts.split(",", 1)
        ts = f"{head},{ms[:3].ljust(3, '0')}"
    return ts


def seconds_to_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_time_to_seconds(ts: str) -> float:
    ts = normalize_timestamp(ts)
    hms, ms = ts.split(",", 1)
    h, m, s = [int(x) for x in hms.split(":")]
    return h * 3600 + m * 60 + s + int(ms) / 1000.0


def parse_srt_blocks(text: str) -> List[Dict[str, str]]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^WEBVTT.*?(\n\n|$)", "", text, flags=re.I | re.S).strip()
    raw_blocks = re.split(r"\n\s*\n", text)
    blocks: List[Dict[str, str]] = []
    for raw in raw_blocks:
        lines = [ln.strip("\ufeff ") for ln in raw.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = ""
        ts_line = ""
        body_lines: List[str] = []
        for i, line in enumerate(lines):
            if is_timestamp_line(line):
                ts_line = line
                if i > 0 and INDEX_RE.match(lines[i - 1]):
                    idx = lines[i - 1].strip()
                body_lines = lines[i + 1:]
                break
        if not ts_line:
            continue
        try:
            start_raw, end_raw = re.split(r"\s*-->\s*", ts_line, maxsplit=1)
            end_raw = end_raw.split()[0]
            start = normalize_timestamp(start_raw)
            end = normalize_timestamp(end_raw)
        except Exception:
            continue
        body = "\n".join(body_lines).strip()
        if body:
            blocks.append({"index": idx or str(len(blocks) + 1), "start": start, "end": end, "text": body})
    return blocks


def compose_srt(blocks: List[Dict[str, str]], texts: Optional[List[str]] = None) -> str:
    out: List[str] = []
    for i, block in enumerate(blocks, start=1):
        txt = texts[i - 1] if texts is not None and i - 1 < len(texts) else block.get("text", "")
        txt = (txt or "").strip()
        if not txt:
            txt = block.get("text", "")
        out.append(f"{i}\n{block['start']} --> {block['end']}\n{txt}")
    return "\n\n".join(out).strip() + "\n"


def vtt_to_srt(vtt_text: str) -> str:
    lines = (vtt_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    clean = []
    for ln in lines:
        if ln.strip().upper().startswith("WEBVTT"):
            continue
        if ln.strip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        clean.append(ln.replace(".", ",") if "-->" in ln else ln)
    text = "\n".join(clean).strip()
    blocks = parse_srt_blocks(text)
    if blocks:
        return compose_srt(blocks)
    # Last-resort: keep text as one subtitle.
    plain = clean_srt_to_text(text)
    if not plain:
        return ""
    return f"1\n00:00:00,000 --> 00:00:05,000\n{plain}\n"


def remove_html_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text


def clean_srt_to_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    seen = set()
    for raw in text.split("\n"):
        line = raw.strip().strip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if INDEX_RE.match(line):
            continue
        if is_timestamp_line(line) or "-->" in line:
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        line = remove_html_tags(line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    # Keep paragraphs but avoid a single huge unpunctuated line.
    return "\n".join(lines).strip()


def sanitize_bad_service_text(text: str) -> str:
    text = text or ""
    lines = []
    for ln in text.splitlines():
        if any(pat.lower() in ln.lower() for pat in BAD_TEXT_PATTERNS[:5]):
            continue
        if "<html" in ln.lower() or "</html" in ln.lower():
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def latin_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z]", text or "")
    total = len(re.sub(r"\s+", "", text or "")) or 1
    return len(letters) / total


def contains_bad_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(pat.lower() in lower for pat in BAD_TEXT_PATTERNS) or bool(TIMESTAMP_RE.search(text or "")) or "-->" in (text or "")


def ensure_myanmar_punctuation(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    raw_parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    fixed: List[str] = []
    for part in raw_parts:
        # Split very long lines using common connectors and punctuation.
        subparts = re.split(r"(?<=[။.!?])\s+", part)
        expanded: List[str] = []
        for sp in subparts:
            sp = sp.strip()
            if len(sp) > 260:
                pieces = re.split(r"(ပြီး|သော်လည်း|ဒါကြောင့်|ထို့နောက်|အဲ့ဒီနောက်|နောက်ဆုံးမှာ|သို့သော်)", sp)
                buffer = ""
                for piece in pieces:
                    buffer += piece
                    if len(buffer) > 140:
                        expanded.append(buffer.strip())
                        buffer = ""
                if buffer.strip():
                    expanded.append(buffer.strip())
            elif sp:
                expanded.append(sp)
        for sp in expanded:
            sp = sp.strip(" ,")
            if sp and not re.search(r"[။.!?]$", sp):
                sp += "။"
            if sp:
                fixed.append(sp)
    # Paragraph breaks every 3 sentences.
    paragraphs = []
    for i in range(0, len(fixed), 3):
        paragraphs.append(" ".join(fixed[i:i + 3]))
    return "\n\n".join(paragraphs).strip()


def split_text_chunks(text: str, max_chars: int = 2200) -> List[str]:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    # Split on lines/sentence boundaries.
    units = []
    for para in re.split(r"\n+", text):
        para = para.strip()
        if not para:
            continue
        parts = re.split(r"(?<=[။.!?])\s+", para)
        units.extend([p.strip() for p in parts if p.strip()])
    chunks: List[str] = []
    cur = ""
    for unit in units:
        if len(cur) + len(unit) + 1 <= max_chars:
            cur = f"{cur}\n{unit}".strip()
        else:
            if cur:
                chunks.append(cur)
            if len(unit) > max_chars:
                for i in range(0, len(unit), max_chars):
                    chunks.append(unit[i:i + max_chars])
                cur = ""
            else:
                cur = unit
    if cur:
        chunks.append(cur)
    return chunks


def distributed_fallback_summary(text: str, target_ratio: float = 0.28) -> str:
    """Last-resort preview only. Never first-N only; sample beginning/middle/end."""
    cleaned = clean_srt_to_text(text)
    if not cleaned:
        return ""
    target = max(600, int(len(cleaned) * max(0.12, min(target_ratio, 0.35))))
    if len(cleaned) <= target:
        return ensure_myanmar_punctuation(cleaned)
    zones = [
        cleaned[: max(1, len(cleaned) // 3)],
        cleaned[len(cleaned) // 3: 2 * len(cleaned) // 3],
        cleaned[2 * len(cleaned) // 3:],
    ]
    zone_target = max(180, target // 3)
    pieces = []
    for zone in zones:
        sentences = re.split(r"(?<=[။.!?])\s+|\n+", zone)
        chosen = ""
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(chosen) + len(sent) + 1 > zone_target and len(chosen) > zone_target * 0.65:
                break
            chosen = f"{chosen} {sent}".strip()
        if not chosen:
            chosen = zone[:zone_target]
        pieces.append(chosen.strip())
    return ensure_myanmar_punctuation("\n".join(pieces))


def important_tokens(text: str, max_tokens: int = 28) -> List[str]:
    text = text or ""
    # English/Korean style names and longer Myanmar tokens.
    tokens = re.findall(r"[A-Z][A-Za-z-]{2,}|[က-အ][က-အါ-ှ]{3,}", text)
    freq: Dict[str, int] = {}
    for tok in tokens:
        if len(tok.strip()) < 4:
            continue
        freq[tok] = freq.get(tok, 0) + 1
    return [k for k, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:max_tokens]]


def coverage_score(source: str, script: str) -> Dict[str, object]:
    source = source or ""
    script_lower = (script or "").lower()
    n = len(source)
    if n < 800:
        return {"start": True, "middle": True, "end": True, "end_tokens": [], "missing_end": []}
    zones = {
        "start": source[: int(n * 0.2)],
        "middle": source[int(n * 0.4): int(n * 0.6)],
        "end": source[int(n * 0.8):],
    }
    result = {}
    for name, zone in zones.items():
        toks = important_tokens(zone, 15)
        if not toks:
            result[name] = True
            result[f"{name}_tokens"] = []
            result[f"missing_{name}"] = []
            continue
        hits = [t for t in toks if t.lower() in script_lower]
        result[name] = len(hits) >= max(1, math.ceil(len(toks) * 0.18))
        result[f"{name}_tokens"] = toks
        result[f"missing_{name}"] = [t for t in toks if t not in hits]
    return result


def script_quality(source_text: str, script: str, source: str = "") -> Dict[str, object]:
    script = script or ""
    source_text = source_text or ""
    ratio = len(script) / max(1, len(source_text))
    bad_quality = any(x in (source or "").lower() for x in ["fallback", "cleanup", "local"])
    cov = coverage_score(source_text, script)
    punctuation_count = script.count("။") + script.count(".") + script.count("!") + script.count("?")
    tts_safe = bool(script.strip()) and not contains_bad_text(script) and not bad_quality
    source_len = len(source_text)
    if source_len < 800:
        min_ratio = float(os.getenv("REWRITE_SHORT_MIN_RATIO", "0.18"))
        max_ratio = float(os.getenv("REWRITE_SHORT_MAX_RATIO", "1.45"))
    else:
        min_ratio = float(os.getenv("REWRITE_MIN_RATIO_CONCISE", "0.12"))
        max_ratio = float(os.getenv("REWRITE_MAX_RATIO_CONCISE", "0.55"))
    if ratio > max_ratio or ratio < min_ratio:
        tts_safe = False
    if source_len >= 800 and cov.get("end") is False:
        tts_safe = False
    if len(script) > 600 and punctuation_count < max(1, len(script) // 450):
        tts_safe = False
    if latin_ratio(script) > float(os.getenv("REWRITE_MAX_LATIN_RATIO", "0.25")):
        # Allow names, but block mostly English outputs.
        tts_safe = False
    return {
        "ratio": round(ratio, 4),
        "tts_safe": tts_safe,
        "needs_retry": not tts_safe,
        "bad_text": contains_bad_text(script),
        "latin_ratio": round(latin_ratio(script), 4),
        "coverage": cov,
        "punctuation_count": punctuation_count,
    }


# -----------------------------
# Translation
# -----------------------------

def translate_items_google(items: List[str], source_language: str, target_language: str) -> Tuple[List[str], Dict[str, int]]:
    from deep_translator import GoogleTranslator

    target = "my" if target_language in {"my", "mm", "burmese", "myanmar"} else target_language
    source = "auto" if not source_language or source_language == "auto" else source_language
    translator = GoogleTranslator(source=source, target=target)
    translated: List[str] = []
    stats = {"failed_segments": 0, "google_error_segments_removed": 0, "possibly_untranslated_segments": 0}
    batch_size = int(os.getenv("TRANSLATE_BATCH_ITEMS", "25"))
    max_chars = int(os.getenv("TRANSLATE_BATCH_CHARS", "2500"))

    i = 0
    while i < len(items):
        batch: List[str] = []
        chars = 0
        while i < len(items) and len(batch) < batch_size and chars + len(items[i]) <= max_chars:
            batch.append(items[i])
            chars += len(items[i])
            i += 1
        if not batch:
            batch = [items[i]]
            i += 1
        try:
            outs = translator.translate_batch(batch)
            if not isinstance(outs, list) or len(outs) != len(batch):
                raise ValueError("translate_batch returned unexpected result")
        except Exception:
            outs = []
            for item in batch:
                try:
                    outs.append(translator.translate(item))
                    time.sleep(0.05)
                except Exception:
                    stats["failed_segments"] += 1
                    outs.append(item)
        for original, out in zip(batch, outs):
            out = sanitize_bad_service_text(out or "")
            if not out:
                out = original
                stats["failed_segments"] += 1
            if any(p.lower() in out.lower() for p in ["error 500", "server error", "that's an error", "that’s an error"]):
                stats["google_error_segments_removed"] += 1
                out = original
            if target == "my" and latin_ratio(out) > 0.65 and len(out) > 20:
                stats["possibly_untranslated_segments"] += 1
            translated.append(out)
    return translated, stats


# -----------------------------
# IAMHC LLM integration
# -----------------------------

def iamhc_chat(model: str, system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None) -> Tuple[Optional[str], Dict[str, object]]:
    api_key = os.getenv("IAMHC_API_KEY", "").strip()
    base_url = os.getenv("IAMHC_BASE_URL", "https://api.iamhc.cn/v1").rstrip("/")
    if not api_key:
        return None, {"ok": False, "error": "IAMHC_API_KEY is not set"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(os.getenv("IAMHC_TEMPERATURE", "0.2")),
        "max_tokens": int(max_tokens or os.getenv("IAMHC_MAX_TOKENS", "2500")),
    }
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=int(os.getenv("IAMHC_TIMEOUT", "180")),
        )
        raw = resp.text
        if not resp.ok:
            return None, {"ok": False, "status": resp.status_code, "raw": raw[:1000]}
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if not content or not str(content).strip():
            return None, {
                "ok": False,
                "error": "empty_content",
                "model_returned": data.get("model"),
                "finish_reason": choice.get("finish_reason"),
                "has_reasoning_content": bool(message.get("reasoning_content")),
            }
        content = str(content).strip()
        meta = {
            "ok": True,
            "requested_model": model,
            "model_returned": data.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage", {}),
        }
        return content, meta
    except Exception as exc:
        return None, {"ok": False, "error": str(exc)}


def _extract_json_array_from_text(raw: str) -> Optional[List[str]]:
    """Extract a JSON array of strings from strict or slightly noisy LLM output."""
    raw = (raw or "").strip()
    if not raw:
        return None
    candidates = [raw]
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                return [str(x).strip() for x in obj]
            if isinstance(obj, dict):
                for key in ("translations", "items", "result", "output"):
                    if isinstance(obj.get(key), list):
                        return [str(x).strip() for x in obj[key]]
        except Exception:
            continue
    return None


def translate_items_iamhc(items: List[str], source_language: str, target_language: str, quality_mode: str = "premium") -> Tuple[Optional[List[str]], Dict[str, object]]:
    """AI translation router for Premium mode.

    Preserves SRT timing outside this function by translating only block text.
    Falls back per batch to Google Translate if all AI models fail.
    """
    if not env_bool("USE_AI_TRANSLATION", normalize_quality_mode(quality_mode) == "premium"):
        return None, {"engine": "ai_translation_disabled"}
    if not items:
        return [], {"engine": "iamhc_ai_translation", "segments": 0}

    if normalize_quality_mode(quality_mode) == "premium":
        models = [
            os.getenv("IAMHC_TRANSLATION_MODEL", os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B")),
            os.getenv("IAMHC_PRO_MODEL", os.getenv("IAMHC_FINAL_POLISH_MODEL", "DeepSeek-V4-Pro")),
            os.getenv("IAMHC_TRANSLATION_FAST_MODEL", os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash")),
        ]
    else:
        models = [os.getenv("IAMHC_TRANSLATION_FAST_MODEL", os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"))]
    # de-dupe while preserving order
    seen_models = []
    for m in models:
        if m and m not in seen_models:
            seen_models.append(m)
    models = seen_models

    batch_items = int(os.getenv("AI_TRANSLATE_BATCH_ITEMS", os.getenv("TRANSLATE_CHUNK_BLOCKS", "20")))
    batch_chars = int(os.getenv("AI_TRANSLATE_BATCH_CHARS", os.getenv("TRANSLATE_CHUNK_CHARS", "2200")))
    max_tokens = int(os.getenv("AI_TRANSLATE_MAX_TOKENS", os.getenv("TRANSLATE_MAX_TOKENS", "4000")))
    target_name = "Myanmar/Burmese" if target_language in {"my", "mm", "burmese", "myanmar"} else target_language

    system_prompt = (
        "You are a professional subtitle translator for Myanmar voice-over production. "
        "Translate meaning accurately and naturally. Do not summarize. Do not add notes. "
        "Return ONLY a valid JSON array of strings, same length and same order as input."
    )
    translated: List[str] = []
    stats: Dict[str, object] = {
        "engine": "iamhc_ai_translation",
        "output_quality": normalize_quality_mode(quality_mode),
        "models_tried": models,
        "ai_batches": 0,
        "google_fallback_batches": 0,
        "failed_batches": 0,
        "bad_segments_removed": 0,
        "possibly_untranslated_segments": 0,
    }

    i = 0
    while i < len(items):
        batch: List[str] = []
        chars = 0
        while i < len(items) and len(batch) < batch_items and chars + len(items[i]) <= batch_chars:
            batch.append(items[i])
            chars += len(items[i])
            i += 1
        if not batch:
            batch = [items[i]]
            i += 1

        batch_result: Optional[List[str]] = None
        input_json = json.dumps(batch, ensure_ascii=False)
        user_prompt = (
            f"Translate this JSON array from {source_language or 'auto'} to {target_name}.\n"
            "Rules:\n"
            "- Return ONLY a JSON array.\n"
            "- Same number of items.\n"
            "- Preserve names.\n"
            "- Natural Myanmar phrasing.\n"
            "- No English explanation.\n\n"
            f"Input JSON array:\n{input_json}"
        )
        for model in models:
            out, meta = iamhc_chat(model, system_prompt, user_prompt, max_tokens=max_tokens)
            if not out or contains_bad_text(out):
                continue
            arr = _extract_json_array_from_text(out)
            if arr and len(arr) == len(batch):
                cleaned = [sanitize_bad_service_text(x) or original for x, original in zip(arr, batch)]
                # Reject obviously wrong batch when most target Myanmar is still English.
                if target_language in {"my", "mm", "burmese", "myanmar"}:
                    bad_count = sum(1 for x in cleaned if latin_ratio(x) > 0.70 and len(x) > 20)
                    if bad_count > len(cleaned) * 0.65:
                        continue
                    stats["possibly_untranslated_segments"] = int(stats.get("possibly_untranslated_segments", 0)) + bad_count
                batch_result = cleaned
                stats["ai_batches"] = int(stats.get("ai_batches", 0)) + 1
                stats["last_model"] = model
                break

        if batch_result is None:
            # Per-batch fail-safe: keep pipeline alive using Google translation.
            google_out, google_stats = translate_items_google(batch, source_language, target_language)
            batch_result = google_out
            stats["google_fallback_batches"] = int(stats.get("google_fallback_batches", 0)) + 1
            stats["failed_batches"] = int(stats.get("failed_batches", 0)) + 1
            for k, v in google_stats.items():
                if isinstance(v, int):
                    stats[k] = int(stats.get(k, 0)) + v
        translated.extend(batch_result)

    stats["segments"] = len(items)
    stats["quality_warning"] = bool(stats.get("google_fallback_batches") or stats.get("possibly_untranslated_segments"))
    return translated, stats


def bad_llm_output(text: str) -> bool:
    if not text or len(text.strip()) < 20:
        return True
    if contains_bad_text(text):
        return True
    # Reject instruction/explanation leakage.
    leakage = [
        "Got it", "Let's tackle", "We need", "The sentence", "Possible translation",
        "Here is", "I will", "In Burmese", "In Myanmar", "note:", "bullet",
    ]
    low = text.lower()
    if any(x.lower() in low for x in leakage):
        return True
    return False


def rewrite_with_iamhc(text: str, option: str, target_ratio: float, quality_mode: str = "basic") -> Tuple[Optional[str], Dict[str, object]]:
    if not env_bool("USE_IAMHC_REWRITE", True):
        return None, {"ok": False, "error": "USE_IAMHC_REWRITE disabled"}
    # Ordered model router. Qwen is the main quality model.
    # DeepSeek-V4-Pro is included for stronger final-polish / retry.
    # DeepSeek-V4-Flash is the fast fallback.
    models = []
    raw_models = os.getenv("IAMHC_REWRITE_MODELS", "").strip()
    if raw_models:
        candidates = [m.strip() for m in raw_models.split(",") if m.strip()]
    else:
        if normalize_quality_mode(quality_mode) == "premium":
            # Premium intentionally includes DeepSeek-V4-Pro for stronger final repair/polish.
            candidates = [
                os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
                os.getenv("IAMHC_PRO_MODEL", os.getenv("IAMHC_FINAL_POLISH_MODEL", "DeepSeek-V4-Pro")),
                os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
                os.getenv("IAMHC_FALLBACK_MODEL", "Qwen3.5-397B-A17B"),
            ]
        else:
            # Basic is faster and cheaper: use Qwen first, then Flash.
            candidates = [
                os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
                os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
            ]
    for name in candidates:
        if name and name not in models:
            models.append(name)

    max_chunk_chars = int(os.getenv("OPENROUTER_REWRITE_CHUNK_CHARS", os.getenv("IAMHC_REWRITE_CHUNK_CHARS", "2200")))
    chunks = split_text_chunks(text, max_chunk_chars)
    if not chunks:
        return None, {"ok": False, "error": "empty_text"}

    if option == "emotional_tts":
        style_desc = (
            "စိတ်ဝင်စားစရာကောင်းတဲ့ storytelling voice-over ပုံစံ၊ စာကြောင်းတိုတို၊ pause ကောင်းကောင်း၊ "
            "နားထောင်သူကိုဆွဲဆောင်နိုင်တဲ့ emotional Myanmar narration"
        )
    else:
        style_desc = (
            "မူရင်းအဓိပ္ပါယ်မပျောက်စေဘဲ ရှင်းလင်းပြီး သဘာဝကျတဲ့ documentary/movie recap Myanmar narration"
        )

    system_prompt = (
        "You are a professional Myanmar voice-over script writer. "
        "Output only final Myanmar narration. No English explanation. No notes. No bullet points. "
        "Do not include reasoning. Do not include instructions. Use natural Burmese punctuation. "
        "Keep names if needed, but rewrite surrounding narration in Myanmar."
    )

    all_model_meta = []
    for model in models:
        rewritten_chunks: List[str] = []
        model_failed = False
        for idx, chunk in enumerate(chunks, start=1):
            user_prompt = (
                f"အောက်ကစာက video subtitle/translation ထဲက အပိုင်း {idx}/{len(chunks)} ဖြစ်ပါတယ်။\n"
                f"ဒီအပိုင်းကို {style_desc} အဖြစ် မြန်မာစကားပြေ voice-over narration ပြန်ရေးပါ။\n"
                f"အရေးကြီး: မူရင်းအပိုင်းထက် မရှည်စေရ။ ဒီအပိုင်းကို အကြမ်းဖျင်း {max(180, int(len(chunk) * max(0.18, min(target_ratio, 0.45))))} စာလုံးခန့်ဖြစ်အောင် ကျစ်လစ်ပါ။\n"
                "မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။ English explanation မထည့်ပါနဲ့။\n"
                "SRT timestamp, numbering, arrow မထည့်ပါနဲ့။\n"
                "ဇာတ်လမ်းအစဉ်အလိုက် အဓိကဖြစ်ရပ်တွေမပျောက်စေပါနဲ့။ စာကြောင်းအဆုံးတိုင်း ‘။’ သုံးပါ။\n\n"
                f"စာသား:\n{chunk}"
            )
            out, meta = iamhc_chat(model, system_prompt, user_prompt)
            all_model_meta.append(meta)
            if not out or bad_llm_output(out):
                model_failed = True
                break
            rewritten_chunks.append(out.strip())
        if model_failed or not rewritten_chunks:
            continue

        merged = "\n".join(rewritten_chunks).strip()

        # Final polish, especially for multi-chunk stories.
        # This is where DeepSeek-V4-Pro is intentionally used when configured.
        # If Pro fails or leaks instructions, keep the safer merged draft.
        polish_prompt = (
            "အောက်က အပိုင်းလိုက်ရေးထားတဲ့ မြန်မာ narration ကို ဇာတ်လမ်းအစ-အလယ်-အဆုံးမပျောက်စေဘဲ "
            f"တစ်ပုဒ်တည်းသော concise voice-over script အဖြစ်ပြန်စီပါ။ Target length က မူရင်းစာရဲ့ {int(target_ratio*100)}% ဝန်းကျင်။\n"
            "အဆုံးပိုင်း/ဖြေရှင်းချက် မဖြတ်ပါနဲ့။ အရှည်မတိုးပါနဲ့။ မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။ English မထည့်ပါနဲ့။\n\n"
            f"Draft:\n{merged}"
        )
        polish_models = []
        for pname in [
            os.getenv("IAMHC_FINAL_POLISH_MODEL", "DeepSeek-V4-Pro"),
            os.getenv("IAMHC_PRO_MODEL", "DeepSeek-V4-Pro"),
            os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
        ]:
            if pname and pname not in polish_models:
                polish_models.append(pname)

        polish_used = None
        do_polish = env_bool("USE_IAMHC_FINAL_POLISH", True) and (
            normalize_quality_mode(quality_mode) == "premium" or len(chunks) > 1 or env_bool("IAMHC_ALWAYS_FINAL_POLISH", False)
        )
        if do_polish:
            for polish_model in polish_models:
                polished, pmeta = iamhc_chat(
                    polish_model,
                    system_prompt,
                    polish_prompt,
                    max_tokens=int(os.getenv("IAMHC_POLISH_MAX_TOKENS", "4000")),
                )
                pmeta = dict(pmeta or {})
                pmeta["polish_attempt"] = True
                pmeta["polish_model"] = polish_model
                all_model_meta.append(pmeta)
                if polished and not bad_llm_output(polished):
                    candidate = ensure_myanmar_punctuation(sanitize_bad_service_text(polished.strip()))
                    cq = script_quality(text, candidate, source="iamhc_polish")
                    # Avoid accepting a polish that obviously drops the ending or becomes too tiny.
                    if not cq.get("needs_retry") or cq.get("coverage", {}).get("end"):
                        merged = candidate
                        polish_used = polish_model
                        break

        merged = ensure_myanmar_punctuation(sanitize_bad_service_text(merged))
        q = script_quality(text, merged, source="iamhc_ai")
        # Allow a little flexibility; mark unsafe if too long/coverage fail but still return.
        if bad_llm_output(merged):
            continue
        return merged, {
            "ok": True,
            "model": model,
            "generation_model": model,
            "polish_model": polish_used,
            "models_tried": models,
            "output_quality": normalize_quality_mode(quality_mode),
            "polish_models": polish_models,
            "meta": all_model_meta,
            "quality": q,
        }

    return None, {"ok": False, "error": "all_models_failed", "models_tried": models, "meta": all_model_meta[-4:]}


def repair_rewrite_with_iamhc(source_text: str, draft: str, option_id: str, target_ratio: float, quality_mode: str = "premium") -> Tuple[Optional[str], Dict[str, object]]:
    """Second-pass Premium repair when the first AI rewrite is too long/short/unsafe."""
    if normalize_quality_mode(quality_mode) != "premium" and not env_bool("IAMHC_REPAIR_IN_BASIC", False):
        return None, {"ok": False, "error": "repair_disabled_for_basic"}
    models = []
    for m in [
        os.getenv("IAMHC_FINAL_POLISH_MODEL", os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B")),
        os.getenv("IAMHC_PRO_MODEL", os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B")),
        os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
        os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
    ]:
        if m and m not in models:
            models.append(m)
    source_len = len(source_text or "")
    if source_len < 800:
        target_chars = max(180, min(int(os.getenv("REWRITE_SHORT_TARGET_CHARS", "320")), 520))
        min_chars = int(os.getenv("REWRITE_SHORT_MIN_CHARS", "120"))
        max_chars = int(os.getenv("REWRITE_SHORT_MAX_CHARS", "520"))
    else:
        target_chars = max(400, int(source_len * target_ratio))
        min_chars = max(300, int(source_len * float(os.getenv("REWRITE_MIN_RATIO_CONCISE", "0.12"))))
        max_chars = max(target_chars + 500, int(source_len * float(os.getenv("REWRITE_MAX_RATIO_CONCISE", "0.55"))))
    tone = "dramatic emotional storytelling" if option_id == "emotional_tts" else "natural accurate documentary recap"
    system_prompt = (
        "You are a senior Myanmar voice-over editor. Return ONLY final Myanmar narration. "
        "No English explanation, no notes, no bullets, no labels, no markdown. "
        "Use Burmese punctuation and short TTS-friendly sentences."
    )
    user_prompt = (
        f"အောက်က Draft ကို Premium final {tone} script အဖြစ် ပြန်ပြင်ပါ။\n"
        f"Target: {target_chars} စာလုံးခန့်။ အနည်းဆုံး {min_chars}၊ အများဆုံး {max_chars} စာလုံး။\n"
        "အဓိကအချက်: မူရင်းဇာတ်လမ်းအဓိပ္ပါယ်၊ အစ-အလယ်-အဆုံး မပျောက်စေပါနဲ့။\n"
        "Draft ကရှည်လွန်းရင် အဓိကဇာတ်လမ်းကိုပဲ ကျစ်လစ်အောင်ချုံ့ပါ။\n"
        "Draft ကတိုလွန်းရင် မူရင်းအဓိပ္ပါယ်အပြည့်ပြန်ဖြည့်ပါ။\n"
        "မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။ English word/explanation မပါရ။ Timestamp/SRT မပါရ။\n\n"
        f"မူရင်းအချက်အလက်:\n{source_text[:9000]}\n\n"
        f"Draft:\n{draft[:9000]}"
    )
    attempts = []
    for model in models:
        out, meta = iamhc_chat(model, system_prompt, user_prompt, max_tokens=int(os.getenv("IAMHC_REPAIR_MAX_TOKENS", os.getenv("IAMHC_POLISH_MAX_TOKENS", "4000"))))
        meta = dict(meta or {})
        meta["repair_model"] = model
        attempts.append(meta)
        if not out or bad_llm_output(out):
            continue
        candidate = ensure_myanmar_punctuation(sanitize_bad_service_text(out.strip()))
        q = script_quality(source_text, candidate, source="iamhc_repair")
        if not q.get("needs_retry"):
            return candidate, {"ok": True, "repair_model": model, "attempts": attempts, "quality": q}
        if source_len < 800 and len(candidate) <= max_chars and not contains_bad_text(candidate) and latin_ratio(candidate) <= float(os.getenv("REWRITE_MAX_LATIN_RATIO", "0.25")):
            return candidate, {"ok": True, "repair_model": model, "attempts": attempts, "quality": q, "accepted_short_repair": True}
    return None, {"ok": False, "error": "repair_failed", "attempts": attempts}


def make_rewrite_option(text: str, option_id: str, title: str, target_ratio: float, quality_mode: str = "basic") -> Dict[str, object]:
    script, meta = rewrite_with_iamhc(text, option_id, target_ratio, quality_mode=quality_mode)
    source = "iamhc_qwen_ai"
    quality = "ai_rewrite"
    if not script:
        script = distributed_fallback_summary(text, target_ratio)
        source = "LOCAL_SANITIZED_SUMMARY_FALLBACK"
        quality = "local_sanitized_summary_fallback"
    q = script_quality(text, script, source=source)
    repaired_meta = None
    if quality == "ai_rewrite" and q.get("needs_retry") and env_bool("USE_IAMHC_REWRITE_REPAIR", normalize_quality_mode(quality_mode) == "premium"):
        repaired, repaired_meta = repair_rewrite_with_iamhc(text, script, option_id, target_ratio, quality_mode=quality_mode)
        if repaired:
            repaired_q = script_quality(text, repaired, source="iamhc_repair")
            if not repaired_q.get("needs_retry") or (len(text) < 800 and len(repaired) < len(script)):
                script = repaired
                source = "iamhc_qwen_ai_repaired"
                q = repaired_q
                meta = {**(meta or {}), "repair": repaired_meta}
    filename = f"{option_id}_{now_stamp()}_{uid()}.txt"
    write_text_file(SCRIPT_DIR / filename, script)
    return {
        "id": option_id,
        "title": title,
        "script": script,
        "text": script,
        "quality": quality,
        "source": source,
        "tts_safe": bool(q["tts_safe"] and quality == "ai_rewrite"),
        "needs_retry": bool(q["needs_retry"] or quality != "ai_rewrite"),
        "script_url": public_url("script", filename, f"{option_id}.txt"),
        "download_url": public_url("script", filename, f"{option_id}.txt"),
        "rewrite": {
            "input_chars": len(text),
            "output_chars": len(script),
            "target_ratio": target_ratio,
            "output_quality": normalize_quality_mode(quality_mode),
            "actual_ratio": q["ratio"],
            "engine_meta": meta,
        },
        "quality_checks": q,
    }


# -----------------------------
# SRT extraction / media processing
# -----------------------------

def youtube_download_audio(url: str, out_dir: Path) -> Path:
    import yt_dlp

    outtmpl = str(out_dir / f"audio_{uid()}.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "cookiefile": youtube_cookiefile(),
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp after postprocessor may not update filepath reliably.
        candidates = sorted(out_dir.glob("audio_*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
        requested = info.get("requested_downloads") or []
        for item in requested:
            fp = item.get("filepath")
            if fp and Path(fp).exists():
                return Path(fp)
    raise RuntimeError("Audio download failed")


def try_caption_first_srt(url: str, language: str = "auto") -> Tuple[Optional[str], Dict[str, object]]:
    """Caption-first SRT extraction.

    Order:
    1) youtube-transcript-api, which can avoid yt-dlp bot-check metadata extraction.
    2) yt-dlp subtitle-only with cookiefile support.
    This function never downloads audio/video.
    """
    attempts: Dict[str, object] = {}

    transcript_srt, transcript_meta = try_youtube_transcript_api_srt(url, language=language)
    attempts["youtube_transcript_api"] = transcript_meta
    if transcript_srt:
        transcript_meta = dict(transcript_meta or {})
        transcript_meta["attempts"] = attempts
        return transcript_srt, transcript_meta

    import yt_dlp

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        outtmpl = str(tdir / "subtitle_%(id)s.%(ext)s")
        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt/vtt/best",
            "subtitleslangs": ["en", "en.*", "my", "my.*", "en-US", "en-GB"],
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "cookiefile": youtube_cookiefile(),
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            "extractor_args": {"youtube": {"player_client": ["ios", "android", "web_creator", "web"]}},
        }
        ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            err = str(exc)
            return None, {
                "caption_error": err,
                "is_bot_check": is_youtube_bot_check_error(err),
                "cookie_configured": bool(youtube_cookiefile()),
                "attempts": attempts,
            }
        files = list(tdir.glob("*.srt")) + list(tdir.glob("*.vtt"))
        files = sorted(files, key=lambda p: (0 if p.suffix.lower() == ".srt" else 1, p.name))
        for fp in files:
            try:
                raw = fp.read_text(encoding="utf-8", errors="ignore")
                srt = raw if fp.suffix.lower() == ".srt" else vtt_to_srt(raw)
                if parse_srt_blocks(srt) or clean_srt_to_text(srt):
                    return srt, {
                        "caption_source": "yt_dlp_subtitles",
                        "subtitle_file": fp.name,
                        "video_id": info.get("id"),
                        "title": info.get("title"),
                        "cookie_configured": bool(youtube_cookiefile()),
                        "attempts": attempts,
                    }
            except Exception:
                continue
    return None, {"caption_error": "No subtitle file returned", "cookie_configured": bool(youtube_cookiefile()), "attempts": attempts}

def transcribe_audio_to_srt(audio_path: Path, language: str = "auto") -> Tuple[str, Dict[str, object]]:
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "tiny")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    lang_arg = None if not language or language == "auto" else language
    segments, info = model.transcribe(str(audio_path), language=lang_arg, vad_filter=True)
    blocks = []
    count = 0
    for seg in segments:
        count += 1
        blocks.append({
            "index": str(count),
            "start": seconds_to_srt_time(seg.start),
            "end": seconds_to_srt_time(seg.end),
            "text": (seg.text or "").strip(),
        })
    srt = compose_srt(blocks)
    meta = {
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "detected_language": getattr(info, "language", None),
        "segments": count,
    }
    return srt, meta


def extract_audio_from_upload(src: Path, dest: Path) -> Path:
    if src.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}:
        if src.suffix.lower() == ".mp3":
            shutil.copyfile(src, dest)
            return dest
    if not ffmpeg_exists():
        raise RuntimeError("ffmpeg is not available")
    cp = run_cmd(["ffmpeg", "-y", "-i", str(src), "-vn", "-acodec", "libmp3lame", "-q:a", "4", str(dest)], timeout=600)
    if cp.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg failed: {cp.stderr[-1000:]}")
    return dest


# -----------------------------
# Final SRT from script/audio
# -----------------------------

def script_to_final_srt(script: str, duration: Optional[float]) -> str:
    script = ensure_myanmar_punctuation(script)
    sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s+|\n+", script) if s.strip()]
    # Combine short sentences into subtitle-friendly chunks.
    chunks: List[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) < 115:
            buf = f"{buf} {s}".strip()
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    if not chunks:
        chunks = [script[:200] or " "]
    total_chars = sum(max(1, len(c)) for c in chunks)
    if not duration or duration <= 0:
        duration = max(4.0, total_chars / 14.0)
    cur = 0.0
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        share = max(1, len(chunk)) / total_chars
        dur = max(1.4, duration * share)
        if i == len(chunks):
            end = duration
        else:
            end = min(duration, cur + dur)
        blocks.append({"index": str(i), "start": seconds_to_srt_time(cur), "end": seconds_to_srt_time(end), "text": chunk})
        cur = end
    return compose_srt(blocks)


# -----------------------------
# Routes
# -----------------------------

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    return resp


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "success": True,
        "ok": True,
        "name": "Video2Audio Pro Backend",
        "version": APP_VERSION,
        "endpoints": [
            "GET /health",
            "POST /download",
            "POST /extract-srt",
            "POST /process-upload",
            "POST /translate-srt",
            "POST /rewrite",
            "POST /rewrite-options",
            "POST /tts",
            "POST /debug-iamhc-model-test",
            "GET /audio/<filename>",
            "GET /srt/<filename>",
            "GET /script/<filename>",
        ],
        "rewrite_engine": {
            "primary": os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
            "pro_model": os.getenv("IAMHC_PRO_MODEL", os.getenv("IAMHC_FINAL_POLISH_MODEL", "DeepSeek-V4-Pro")),
            "fast_model": os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
            "enabled": env_bool("USE_IAMHC_REWRITE", True),
            "key_configured": bool(os.getenv("IAMHC_API_KEY")),
        },
        "output_quality_modes": {
            "basic": "Google Translate + IAMHC Qwen/DeepSeek-Flash rewrite + Edge TTS",
            "premium": "IAMHC AI translation + Qwen rewrite + DeepSeek-Pro polish + optional stepaudio TTS",
        },
        "tts_engines": {
            "default": "edge_tts",
            "premium_stepaudio_enabled": env_bool("USE_IAMHC_TTS", False),
            "stepaudio_model": os.getenv("IAMHC_TTS_MODEL", "stepaudio-2.5-tts"),
        },
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"success": True, "ok": True, "version": APP_VERSION, "time": now_stamp()})


@app.route("/audio/<path:filename>", methods=["GET"])
def get_audio(filename):
    fp = resolve_existing_file(DOWNLOAD_DIR, filename)
    if not fp:
        return file_not_found_error("Audio", DOWNLOAD_DIR, filename)
    download_name = request.args.get("download_name")
    return send_file(fp, as_attachment=bool(download_name), download_name=download_name or fp.name, mimetype="audio/mpeg")


@app.route("/srt/<path:filename>", methods=["GET"])
def get_srt(filename):
    fp = resolve_existing_file(SRT_DIR, filename)
    if not fp:
        return file_not_found_error("SRT", SRT_DIR, filename)
    download_name = request.args.get("download_name")
    return send_file(fp, as_attachment=bool(download_name), download_name=download_name or fp.name, mimetype="text/plain; charset=utf-8")


@app.route("/script/<path:filename>", methods=["GET"])
def get_script(filename):
    fp = resolve_existing_file(SCRIPT_DIR, filename)
    if not fp:
        return file_not_found_error("Script", SCRIPT_DIR, filename)
    download_name = request.args.get("download_name")
    return send_file(fp, as_attachment=bool(download_name), download_name=download_name or fp.name, mimetype="text/plain; charset=utf-8")


@app.route("/download", methods=["POST", "OPTIONS"])
def download_audio_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.form.get("url") or "").strip()
    if not url:
        return json_error("URL is required", 400)
    try:
        with tempfile.TemporaryDirectory() as td:
            audio_tmp = youtube_download_audio(url, Path(td))
            final_name = f"download_{now_stamp()}_{uid()}.mp3"
            final_path = DOWNLOAD_DIR / final_name
            shutil.copyfile(audio_tmp, final_path)
        return send_file(final_path, as_attachment=True, download_name="audio.mp3", mimetype="audio/mpeg")
    except Exception as exc:
        return json_error(
            "Audio download failed. The source may be restricted. Use SRT/manual upload flow to continue.",
            502,
            details=str(exc)[:1000],
            needs_manual_upload=True,
        )


@app.route("/extract-srt", methods=["POST", "OPTIONS"])
def extract_srt_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    language = data.get("language") or "auto"
    mode = data.get("mode") or "caption_first"
    if not url:
        return json_error("url is required", 400)

    # Caption first for URL input. Downsub/SubDown are assisted manual fallback only.
    caption_meta: Dict[str, object] = {}
    if mode in {"caption_first", "auto", "subtitles"}:
        srt_text, caption_meta = try_caption_first_srt(url, language=language)
        if srt_text:
            fname = f"{uid()}_captions.srt"
            write_text_file(SRT_DIR / fname, srt_text)
            return jsonify({
                "success": True,
                "ok": True,
                "source": "caption_first",
                "srt_text": srt_text,
                "srt_url": public_url("srt", fname, "original.srt"),
                "filename": fname,
                "caption": caption_meta,
            })

    # If YouTube already returned an anti-bot/cookies error during caption-first,
    # do not waste time downloading audio for Whisper. Return assisted manual fallback.
    if is_youtube_bot_check_error(caption_meta):
        return manual_srt_fallback_response(url, caption_meta, reason="youtube_bot_check")

    # Whisper fallback can be disabled if Railway resource is tight.
    if env_bool("ENABLE_URL_WHISPER_FALLBACK", True):
        try:
            with tempfile.TemporaryDirectory() as td:
                audio = youtube_download_audio(url, Path(td))
                srt_text, whisper_meta = transcribe_audio_to_srt(audio, language=language)
                fname = f"{uid()}_whisper.srt"
                write_text_file(SRT_DIR / fname, srt_text)
                return jsonify({
                    "success": True,
                    "ok": True,
                    "source": "whisper_fallback",
                    "srt_text": srt_text,
                    "srt_url": public_url("srt", fname, "original.srt"),
                    "filename": fname,
                    "whisper": whisper_meta,
                    "caption_attempt": caption_meta,
                })
        except Exception as exc:
            whisper_error = str(exc)[:1000]
            if is_youtube_bot_check_error(whisper_error):
                return manual_srt_fallback_response(url, caption_meta, whisper_error=whisper_error, reason="youtube_bot_check")
    else:
        whisper_error = "URL Whisper fallback disabled"

    return manual_srt_fallback_response(url, caption_meta, whisper_error=whisper_error, reason="auto_extract_failed")


@app.route("/process-upload", methods=["POST", "OPTIONS"])
def process_upload_route():
    if request.method == "OPTIONS":
        return ("", 204)
    target_language = request.form.get("target_language") or request.form.get("language") or "my"
    source_language = request.form.get("source_language") or "auto"
    media = request.files.get("file")
    srt_file = request.files.get("srt_file") or request.files.get("srt")

    audio_url = ""
    audio_filename = ""
    srt_text = ""
    whisper_meta = None

    try:
        if media:
            original_name = safe_name(media.filename, ".bin")
            upload_path = UPLOAD_DIR / f"{uid()}_{original_name}"
            media.save(upload_path)
            audio_filename = f"upload_audio_{now_stamp()}_{uid()}.mp3"
            audio_path = DOWNLOAD_DIR / audio_filename
            extract_audio_from_upload(upload_path, audio_path)
            audio_url = public_url("audio", audio_filename, "original-audio.mp3")
        if srt_file:
            raw = srt_file.read().decode("utf-8", errors="ignore")
            srt_text = vtt_to_srt(raw) if (srt_file.filename or "").lower().endswith(".vtt") else raw
        elif media:
            srt_text, whisper_meta = transcribe_audio_to_srt(DOWNLOAD_DIR / audio_filename, language=source_language)
        else:
            return json_error("file or srt_file is required", 400)

        srt_name = f"upload_{uid()}.srt"
        write_text_file(SRT_DIR / srt_name, srt_text)

        translated_text = ""
        translated_url = ""
        translation_meta = None
        if srt_text:
            blocks = parse_srt_blocks(srt_text)
            if blocks:
                items = [b["text"] for b in blocks]
                translated_items, stats = translate_items_google(items, source_language, target_language)
                translated_text = compose_srt(blocks, translated_items)
                translation_meta = {"engine": "google_translate", "source_language": source_language, "target_language": target_language, **stats}
            else:
                translated_items, stats = translate_items_google([clean_srt_to_text(srt_text)], source_language, target_language)
                translated_text = translated_items[0]
                translation_meta = {"engine": "google_translate", "source_language": source_language, "target_language": target_language, **stats}
            translated_name = f"translated_{target_language}_{uid()}.srt"
            write_text_file(SRT_DIR / translated_name, translated_text)
            translated_url = public_url("srt", translated_name, "translated.srt")

        return jsonify({
            "success": True,
            "ok": True,
            "audio_url": audio_url,
            "audio_filename": audio_filename,
            "srt_text": srt_text,
            "srt_url": public_url("srt", srt_name, "original.srt"),
            "translated_srt_text": translated_text,
            "translated_srt_url": translated_url,
            "translation": translation_meta,
            "whisper": whisper_meta,
        })
    except Exception as exc:
        return json_error("Upload processing failed", 500, details=str(exc)[:1000])


@app.route("/translate-srt", methods=["POST", "OPTIONS"])
def translate_srt_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    srt_text = data.get("srt_text") or data.get("text") or ""
    source_language = data.get("source_language") or "auto"
    target_language = data.get("target_language") or data.get("language") or "my"
    quality_mode = normalize_quality_mode(data.get("output_quality") or data.get("quality_mode"))
    use_ai_translation = bool(data.get("use_ai_translation")) or (quality_mode == "premium" and env_bool("USE_AI_TRANSLATION", True))
    if not str(srt_text).strip():
        return json_error("srt_text is required", 400)
    try:
        blocks = parse_srt_blocks(srt_text)
        if blocks:
            items = [b["text"] for b in blocks]
            if use_ai_translation:
                translated_items, stats = translate_items_iamhc(items, source_language, target_language, quality_mode=quality_mode)
                if translated_items is None:
                    translated_items, stats = translate_items_google(items, source_language, target_language)
                    stats = {"engine": "google_translate", "ai_translation_failed": True, **stats}
            else:
                translated_items, stats = translate_items_google(items, source_language, target_language)
                stats = {"engine": "google_translate", **stats}
            translated_srt_text = compose_srt(blocks, translated_items)
            segments = len(blocks)
        else:
            plain = clean_srt_to_text(srt_text)
            if use_ai_translation:
                translated_items, stats = translate_items_iamhc([plain], source_language, target_language, quality_mode=quality_mode)
                if translated_items is None:
                    translated_items, stats = translate_items_google([plain], source_language, target_language)
                    stats = {"engine": "google_translate", "ai_translation_failed": True, **stats}
            else:
                translated_items, stats = translate_items_google([plain], source_language, target_language)
                stats = {"engine": "google_translate", **stats}
            translated_srt_text = translated_items[0]
            segments = 1
        cleaned_translated_text = clean_srt_to_text(translated_srt_text)
        fname = f"translated_{target_language}_{now_stamp()}_{uid()}.srt"
        write_text_file(SRT_DIR / fname, translated_srt_text)
        warning = bool(stats.get("quality_warning")) or stats.get("google_error_segments_removed", 0) > 0 or stats.get("possibly_untranslated_segments", 0) > 0
        engine = stats.get("engine", "iamhc_ai_translation" if use_ai_translation else "google_translate")
        return jsonify({
            "success": True,
            "ok": True,
            "output_quality": quality_mode,
            "translation_engine": engine,
            "translated_srt_text": translated_srt_text,
            "translatedSrtText": translated_srt_text,
            "cleaned_translated_text": cleaned_translated_text,
            "cleanedTranslatedText": cleaned_translated_text,
            "translated_srt_url": public_url("srt", fname, "translated.srt"),
            "filename": fname,
            "translation": {
                "engine": engine,
                "ai_translation": bool(use_ai_translation and "iamhc" in str(engine)),
                "source_language": source_language,
                "target_language": target_language,
                "segments": segments,
                "quality_warning": warning,
                "block_count_matched": True,
                **stats,
            },
        })
    except Exception as exc:
        return json_error("Translation failed", 500, details=str(exc)[:1000])


@app.route("/rewrite", methods=["POST", "OPTIONS"])
def rewrite_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    text = data.get("text") or data.get("srt_text") or ""
    language = data.get("language") or "my"
    style = data.get("style") or "concise_natural_tts"
    cleaned = clean_srt_to_text(text)
    if not cleaned:
        return json_error("No subtitle text available to rewrite", 400)
    quality_mode = normalize_quality_mode(data.get("output_quality") or data.get("quality_mode"))
    default_ratio = "0.30" if quality_mode == "premium" else os.getenv("REWRITE_TARGET_RATIO_CONCISE", "0.35")
    target_ratio = float(data.get("target_length_ratio") or default_ratio)
    option_id = "emotional_tts" if "emotion" in style else "natural_accurate"
    opt = make_rewrite_option(cleaned, option_id, "Rewritten Script", target_ratio, quality_mode=quality_mode)
    script = opt["script"]
    return jsonify({
        "success": True,
        "ok": True,
        "script": script,
        "text": script,
        "rewrittenScript": script,
        "rewritten_script": script,
        "rewrittenText": script,
        "source": opt["source"],
        "quality": opt["quality"],
        "tts_safe": opt["tts_safe"],
        "needs_retry": opt["needs_retry"],
        "script_url": opt["script_url"],
        "output_quality": quality_mode,
        "language": language,
    })


@app.route("/rewrite-options", methods=["POST", "OPTIONS"])
def rewrite_options_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    text = data.get("text") or data.get("translated_srt_text") or data.get("srt_text") or ""
    cleaned = clean_srt_to_text(text)
    if not cleaned or len(cleaned) < 20:
        return json_error("No usable text available for rewrite", 400)
    quality_mode = normalize_quality_mode(data.get("output_quality") or data.get("quality_mode"))
    default_ratio = "0.30" if quality_mode == "premium" else os.getenv("REWRITE_TARGET_RATIO_CONCISE", "0.35")
    target_ratio = float(data.get("target_length_ratio") or default_ratio)
    natural = make_rewrite_option(cleaned, "natural_accurate", "Natural Accurate", target_ratio, quality_mode=quality_mode)
    emotional = make_rewrite_option(cleaned, "emotional_tts", "Emotional TTS", target_ratio, quality_mode=quality_mode)
    # If model returned duplicate scripts, mark for retry.
    if natural["script"].strip() == emotional["script"].strip():
        natural["duplicate_warning"] = True
        emotional["duplicate_warning"] = True
        emotional["needs_retry"] = True
        emotional["tts_safe"] = False
    return jsonify({
        "success": True,
        "ok": True,
        "version": APP_VERSION,
        "output_quality": quality_mode,
        "options": [natural, emotional],
        "naturalScript": natural["script"],
        "emotionalScript": emotional["script"],
        "naturalOption": natural,
        "emotionalOption": emotional,
        # Flat aliases for older Lovable code.
        "script": natural["script"],
        "text": natural["script"],
        "rewrittenScript": natural["script"],
        "quality": natural["quality"],
        "source": natural["source"],
        "tts_safe": natural.get("tts_safe"),
        "needs_retry": natural.get("needs_retry"),
        "cleaned_input_chars": len(cleaned),
        "quality_gate": {
            "fallback_is_preview_only": True,
            "final_tts_requires_tts_safe": True,
            "must_include_ending": True,
        },
    })




def _json_audio_to_bytes(obj) -> Tuple[Optional[bytes], Optional[str]]:
    """Best-effort extractor for OpenAI-compatible audio JSON responses."""
    if not isinstance(obj, dict):
        return None, None
    candidates = []
    # OpenAI-style: choices[0].message.audio.data
    try:
        candidates.append(obj.get("choices", [{}])[0].get("message", {}).get("audio", {}).get("data"))
    except Exception:
        pass
    # Other common shapes
    for path in [
        ("audio",), ("audio_data",), ("data",), ("b64_json",), ("base64",),
        ("result", "audio"), ("output", "audio"), ("response", "audio"),
    ]:
        cur = obj
        try:
            for key in path:
                cur = cur.get(key)
            candidates.append(cur)
        except Exception:
            pass
    for item in candidates:
        if not item or not isinstance(item, str):
            continue
        raw = item.strip()
        if raw.startswith("data:audio") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw), "base64_json"
        except Exception:
            continue
    return None, None


def iamhc_stepaudio_save(text: str, output_path: Path, voice: Optional[str] = None, style: Optional[str] = None) -> Dict[str, object]:
    """Experimental stepaudio-2.5-tts support via IAMHC OpenAI-compatible gateway.

    IAMHC's exact TTS schema may differ by account/model. This function tries the
    two most common OpenAI-compatible shapes and returns structured errors if the
    model/provider rejects them. It never silently falls back to Edge TTS.
    """
    api_key = os.getenv("IAMHC_API_KEY", "").strip()
    base_url = os.getenv("IAMHC_BASE_URL", "https://api.iamhc.cn/v1").rstrip("/")
    model = os.getenv("IAMHC_TTS_MODEL", "stepaudio-2.5-tts")
    timeout = int(os.getenv("IAMHC_TTS_TIMEOUT", os.getenv("IAMHC_TIMEOUT", "240")))
    voice = voice or os.getenv("IAMHC_TTS_VOICE", "default")
    if not api_key:
        return {"ok": False, "error": "IAMHC_API_KEY is not set", "model": model}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    attempts = []

    # Attempt 1: OpenAI /audio/speech style, binary audio response.
    payload1 = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    if style:
        payload1["instructions"] = str(style)
    try:
        r = requests.post(f"{base_url}/audio/speech", headers=headers, json=payload1, timeout=timeout)
        ctype = r.headers.get("content-type", "")
        attempts.append({"endpoint": "/audio/speech", "status": r.status_code, "content_type": ctype, "raw": r.text[:600] if "json" in ctype or "text" in ctype else "<binary>"})
        if r.ok and r.content:
            if "audio" in ctype or not r.content.lstrip().startswith(b"{"):
                output_path.write_bytes(r.content)
                return {"ok": True, "model": model, "voice": voice, "endpoint": "/audio/speech", "attempts": attempts}
            try:
                obj = r.json()
                audio_bytes, shape = _json_audio_to_bytes(obj)
                if audio_bytes:
                    output_path.write_bytes(audio_bytes)
                    return {"ok": True, "model": model, "voice": voice, "endpoint": "/audio/speech", "shape": shape, "attempts": attempts}
            except Exception:
                pass
    except Exception as exc:
        attempts.append({"endpoint": "/audio/speech", "error": str(exc)[:600]})

    # Attempt 2: chat/completions with audio modality.
    payload2 = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a natural Myanmar text to speech engine. Return audio only."},
            {"role": "user", "content": text},
        ],
        "modalities": ["text", "audio"],
        "audio": {"voice": voice, "format": "mp3"},
    }
    try:
        r = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload2, timeout=timeout)
        ctype = r.headers.get("content-type", "")
        attempts.append({"endpoint": "/chat/completions", "status": r.status_code, "content_type": ctype, "raw": r.text[:600]})
        if r.ok:
            obj = r.json()
            audio_bytes, shape = _json_audio_to_bytes(obj)
            if audio_bytes:
                output_path.write_bytes(audio_bytes)
                return {"ok": True, "model": model, "voice": voice, "endpoint": "/chat/completions", "shape": shape, "attempts": attempts}
    except Exception as exc:
        attempts.append({"endpoint": "/chat/completions", "error": str(exc)[:600]})

    return {"ok": False, "model": model, "voice": voice, "error": "stepaudio_tts_attempts_failed", "attempts": attempts[-4:]}

async def edge_tts_save(text: str, voice: str, output_path: Path, rate: str = "-10%", pitch: str = "-1Hz") -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(str(output_path))


@app.route("/tts", methods=["POST", "OPTIONS"])
def tts_route():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    text = data.get("text") or data.get("script") or ""
    if not str(text).strip():
        return json_error("text is required", 400)
    if contains_bad_text(text):
        return json_error("This script contains translation or AI instruction errors. Please retry rewrite.", 400)

    output_quality = normalize_quality_mode(data.get("output_quality") or data.get("quality_mode"))
    engine = data.get("engine")
    if not engine:
        engine = "iamhc_stepaudio" if (output_quality == "premium" and env_bool("PREMIUM_TTS_USE_STEPAUDIO", False)) else "edge_tts"
    language = data.get("language") or "my"
    gender = (data.get("gender") or "male").lower()
    style = data.get("style") or "concise_narrative_summary"

    final_script = ensure_myanmar_punctuation(text)
    script_name = f"final_script_{now_stamp()}_{uid()}.txt"
    write_text_file(SCRIPT_DIR / script_name, final_script)

    if engine in {"iamhc_stepaudio", "stepaudio", "stepaudio_tts", "iamhc_tts"}:
        if not env_bool("USE_IAMHC_TTS", False):
            return json_error(
                "IAMHC stepaudio TTS is installed in this backend but disabled. Set USE_IAMHC_TTS=true after testing the model schema.",
                501,
                final_script_url=public_url("script", script_name, "final-script.txt"),
                engine=engine,
                model=os.getenv("IAMHC_TTS_MODEL", "stepaudio-2.5-tts"),
            )
        audio_name = f"final_stepaudio_{now_stamp()}_{uid()}.mp3"
        audio_path = DOWNLOAD_DIR / audio_name
        meta = iamhc_stepaudio_save(
            final_script,
            audio_path,
            voice=data.get("voice") or data.get("iamhc_voice") or os.getenv("IAMHC_TTS_VOICE", "default"),
            style=data.get("tone") or data.get("style") or "natural Myanmar narration",
        )
        if not meta.get("ok"):
            return json_error("IAMHC stepaudio TTS failed", 502, details=meta, final_script_url=public_url("script", script_name, "final-script.txt"))
        duration = audio_duration_seconds(audio_path)
        final_srt_text = script_to_final_srt(final_script, duration)
        srt_name = f"final_stepaudio_{now_stamp()}_{uid()}.srt"
        write_text_file(SRT_DIR / srt_name, final_srt_text)
        return jsonify({
            "success": True,
            "ok": True,
            "engine": "iamhc_stepaudio",
            "model": os.getenv("IAMHC_TTS_MODEL", "stepaudio-2.5-tts"),
            "voice": meta.get("voice"),
            "provider_meta": meta,
            "audio_url": public_url("audio", audio_name),
            "download_url": public_url("audio", audio_name, "final-audio.mp3"),
            "audio_download_url": public_url("audio", audio_name, "final-audio.mp3"),
            "filename": audio_name,
            "final_srt_text": final_srt_text,
            "final_srt_url": public_url("srt", srt_name, "final.srt"),
            "final_script_text": final_script,
            "final_script_url": public_url("script", script_name, "final-script.txt"),
            "audio_duration_seconds": duration,
            "output_quality": output_quality,
        })

    if engine in {"gemini_tts_app", "gemini_tts_user_key", "gemini_tts"}:
        return json_error(
            "Gemini TTS is not enabled in this backend package yet. Use Edge TTS or deploy the Gemini TTS extension later.",
            501,
            final_script_url=public_url("script", script_name, "final-script.txt"),
            engine=engine,
        )

    if language in {"my", "mm", "myanmar", "burmese"}:
        male_voice = os.getenv("TTS_VOICE_MY_MALE", "my-MM-ThihaNeural")
        female_voice = os.getenv("TTS_VOICE_MY_FEMALE", "my-MM-NilarNeural")
    else:
        male_voice = os.getenv("TTS_VOICE_EN_MALE", "en-US-GuyNeural")
        female_voice = os.getenv("TTS_VOICE_EN_FEMALE", "en-US-JennyNeural")
    voice = data.get("voice") or (female_voice if gender == "female" else male_voice)
    rate = data.get("rate") or (os.getenv("TTS_RATE_EMOTIONAL", "-10%") if "emotion" in style else os.getenv("TTS_RATE_CONCISE", "-10%"))
    pitch = data.get("pitch") or (os.getenv("TTS_PITCH_EMOTIONAL", "-1Hz") if "emotion" in style else os.getenv("TTS_PITCH_CONCISE", "-1Hz"))

    audio_name = f"final_audio_{now_stamp()}_{uid()}.mp3"
    audio_path = DOWNLOAD_DIR / audio_name
    try:
        asyncio.run(edge_tts_save(final_script, voice, audio_path, rate=rate, pitch=pitch))
    except Exception as exc:
        return json_error("TTS generation failed", 500, details=str(exc)[:1000])

    duration = audio_duration_seconds(audio_path)
    final_srt_text = script_to_final_srt(final_script, duration)
    srt_name = f"final_{now_stamp()}_{uid()}.srt"
    write_text_file(SRT_DIR / srt_name, final_srt_text)

    return jsonify({
        "success": True,
        "ok": True,
        "engine": "edge_tts",
        "voice": voice,
        "rate": rate,
        "pitch": pitch,
        "audio_url": public_url("audio", audio_name),
        "download_url": public_url("audio", audio_name, "final-audio.mp3"),
        "audio_download_url": public_url("audio", audio_name, "final-audio.mp3"),
        "filename": audio_name,
        "final_srt_text": final_srt_text,
        "final_srt_url": public_url("srt", srt_name, "final.srt"),
        "final_script_text": final_script,
        "final_script_url": public_url("script", script_name, "final-script.txt"),
        "audio_duration_seconds": duration,
        "output_quality": output_quality,
    })




@app.route("/debug-iamhc-model-test", methods=["POST", "OPTIONS"])
def debug_iamhc_model_test():
    """Temporary Railway-only model test helper.

    Enable with ENABLE_AI_DEBUG_ROUTES=true. Disable again after testing.
    Optional security: set DEBUG_AI_TEST_TOKEN and pass X-Debug-Token header.
    """
    if request.method == "OPTIONS":
        return ("", 204)
    if not env_bool("ENABLE_AI_DEBUG_ROUTES", False):
        return json_error("Debug AI test route is disabled. Set ENABLE_AI_DEBUG_ROUTES=true temporarily to use it.", 403)
    token = os.getenv("DEBUG_AI_TEST_TOKEN", "").strip()
    if token and request.headers.get("X-Debug-Token") != token:
        return json_error("Missing or invalid debug token", 403)

    data = request.get_json(silent=True) or {}
    test_text = data.get("text") or "A poor family enters a rich family house by pretending to be skilled workers."
    system_prompt = "You are a professional Myanmar voice-over script writer. Output only natural Myanmar narration. No English explanation."
    user_prompt = f"မြန်မာ voice-over narration အဖြစ် သဘာဝကျကျ ပြန်ရေးပါ။ မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။\n\n{test_text}"
    models = data.get("models") or [
        os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
        os.getenv("IAMHC_PRO_MODEL", os.getenv("IAMHC_FINAL_POLISH_MODEL", "DeepSeek-V4-Pro")),
        os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
    ]
    results = []
    for model in models:
        out, meta = iamhc_chat(str(model), system_prompt, user_prompt, max_tokens=int(os.getenv("DEBUG_AI_TEST_MAX_TOKENS", "1000")))
        results.append({
            "model": model,
            "ok": bool(out and not bad_llm_output(out)),
            "preview": (out or "")[:400],
            "meta": meta,
        })

    tts_result = None
    if data.get("test_tts") is True:
        audio_name = f"debug_stepaudio_{now_stamp()}_{uid()}.mp3"
        audio_path = DOWNLOAD_DIR / audio_name
        tts_result = iamhc_stepaudio_save(
            data.get("tts_text") or "မင်္ဂလာပါ။ ဒီအသံက စမ်းသပ်ထားတဲ့ မြန်မာအသံဖြစ်ပါတယ်။",
            audio_path,
            voice=data.get("voice") or os.getenv("IAMHC_TTS_VOICE", "default"),
            style="natural Myanmar narration",
        )
        if tts_result.get("ok"):
            tts_result["audio_url"] = public_url("audio", audio_name, "debug-stepaudio.mp3")

    return jsonify({
        "success": True,
        "ok": True,
        "version": APP_VERSION,
        "models": results,
        "stepaudio_tts": tts_result,
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
