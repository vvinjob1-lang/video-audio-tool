import asyncio
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

APP_VERSION = "v16-iamhc-qwen-rewrite-quality"

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


def public_url(prefix: str, filename: str, download_name: Optional[str] = None) -> str:
    url = f"{PUBLIC_BASE_URL}/{prefix.strip('/')}/{filename}"
    if download_name:
        url += f"?download_name={requests.utils.quote(download_name)}"
    return url


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
    if n < 300:
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
    if ratio > 0.55 or ratio < 0.12:
        tts_safe = False
    if cov.get("end") is False:
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


def rewrite_with_iamhc(text: str, option: str, target_ratio: float) -> Tuple[Optional[str], Dict[str, object]]:
    if not env_bool("USE_IAMHC_REWRITE", True):
        return None, {"ok": False, "error": "USE_IAMHC_REWRITE disabled"}
    models = []
    for name in [
        os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
        os.getenv("IAMHC_FINAL_POLISH_MODEL", "Qwen3.5-397B-A17B"),
        os.getenv("IAMHC_FAST_MODEL", "DeepSeek-V4-Flash"),
        os.getenv("IAMHC_FALLBACK_MODEL", "DeepSeek-V4-Flash"),
    ]:
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
                "မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။ English explanation မထည့်ပါနဲ့။\n"
                "SRT timestamp, numbering, arrow မထည့်ပါနဲ့။\n"
                "ဇာတ်လမ်းအစဉ်အလိုက် အဓိကဖြစ်ရပ်တွေမပျောက်စေပါနဲ့။\n\n"
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
        if len(chunks) > 1 or len(merged) > int(len(text) * (target_ratio + 0.1)):
            polish_prompt = (
                "အောက်က အပိုင်းလိုက်ရေးထားတဲ့ မြန်မာ narration ကို ဇာတ်လမ်းအစ-အလယ်-အဆုံးမပျောက်စေဘဲ "
                f"တစ်ပုဒ်တည်းသော concise voice-over script အဖြစ်ပြန်စီပါ။ Target length က မူရင်းစာရဲ့ {int(target_ratio*100)}% ဝန်းကျင်။\n"
                "အဆုံးပိုင်း/ဖြေရှင်းချက် မဖြတ်ပါနဲ့။ မြန်မာစာတစ်ခုတည်းသာပြန်ပါ။ English မထည့်ပါနဲ့။\n\n"
                f"Draft:\n{merged}"
            )
            polished, pmeta = iamhc_chat(model, system_prompt, polish_prompt, max_tokens=int(os.getenv("IAMHC_POLISH_MAX_TOKENS", "3000")))
            all_model_meta.append(pmeta)
            if polished and not bad_llm_output(polished):
                merged = polished.strip()
        merged = ensure_myanmar_punctuation(sanitize_bad_service_text(merged))
        q = script_quality(text, merged, source="iamhc_ai")
        # Allow a little flexibility; mark unsafe if too long/coverage fail but still return.
        if bad_llm_output(merged):
            continue
        return merged, {"ok": True, "model": model, "meta": all_model_meta, "quality": q}

    return None, {"ok": False, "error": "all_models_failed", "models_tried": models, "meta": all_model_meta[-4:]}


def make_rewrite_option(text: str, option_id: str, title: str, target_ratio: float) -> Dict[str, object]:
    script, meta = rewrite_with_iamhc(text, option_id, target_ratio)
    source = "iamhc_qwen_ai"
    quality = "ai_rewrite"
    if not script:
        script = distributed_fallback_summary(text, target_ratio)
        source = "LOCAL_SANITIZED_SUMMARY_FALLBACK"
        quality = "local_sanitized_summary_fallback"
    q = script_quality(text, script, source=source)
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
        "cookiefile": str(BASE_DIR / "cookies.txt") if (BASE_DIR / "cookies.txt").exists() else None,
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


def try_caption_first_srt(url: str) -> Tuple[Optional[str], Dict[str, object]]:
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
            "noplaylist": True,
            "cookiefile": str(BASE_DIR / "cookies.txt") if (BASE_DIR / "cookies.txt").exists() else None,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            return None, {"caption_error": str(exc)}
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
                    }
            except Exception:
                continue
    return None, {"caption_error": "No subtitle file returned"}


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
            "GET /audio/<filename>",
            "GET /srt/<filename>",
            "GET /script/<filename>",
        ],
        "rewrite_engine": {
            "primary": os.getenv("IAMHC_REWRITE_MODEL", "Qwen3.5-397B-A17B"),
            "enabled": env_bool("USE_IAMHC_REWRITE", True),
            "key_configured": bool(os.getenv("IAMHC_API_KEY")),
        },
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"success": True, "ok": True, "version": APP_VERSION, "time": now_stamp()})


@app.route("/audio/<path:filename>", methods=["GET"])
def get_audio(filename):
    fp = DOWNLOAD_DIR / safe_name(filename)
    if not fp.exists():
        return json_error("Audio file not found", 404)
    download_name = request.args.get("download_name")
    return send_file(fp, as_attachment=bool(download_name), download_name=download_name or fp.name, mimetype="audio/mpeg")


@app.route("/srt/<path:filename>", methods=["GET"])
def get_srt(filename):
    fp = SRT_DIR / safe_name(filename)
    if not fp.exists():
        return json_error("SRT file not found", 404)
    download_name = request.args.get("download_name")
    return send_file(fp, as_attachment=bool(download_name), download_name=download_name or fp.name, mimetype="text/plain; charset=utf-8")


@app.route("/script/<path:filename>", methods=["GET"])
def get_script(filename):
    fp = SCRIPT_DIR / safe_name(filename)
    if not fp.exists():
        return json_error("Script file not found", 404)
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
        srt_text, caption_meta = try_caption_first_srt(url)
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
    else:
        whisper_error = "URL Whisper fallback disabled"

    encoded = requests.utils.quote(url, safe="")
    return jsonify({
        "success": False,
        "ok": False,
        "accepted_mode": True,
        "needs_manual_srt_upload": True,
        "needs_upload": True,
        "message": "Automatic extraction did not work from our server. Open Downsub/SubDown and upload the SRT here to continue.",
        "open_downsub_url": f"https://downsub.com/?url={encoded}",
        "open_subdown_url": f"https://subdown.org/youtube-subtitle-downloader?url={encoded}",
        "caption_attempt": caption_meta,
        "whisper_error": whisper_error,
    }), 200


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
    if not str(srt_text).strip():
        return json_error("srt_text is required", 400)
    try:
        blocks = parse_srt_blocks(srt_text)
        if blocks:
            items = [b["text"] for b in blocks]
            translated_items, stats = translate_items_google(items, source_language, target_language)
            translated_srt_text = compose_srt(blocks, translated_items)
            segments = len(blocks)
        else:
            plain = clean_srt_to_text(srt_text)
            translated_items, stats = translate_items_google([plain], source_language, target_language)
            translated_srt_text = translated_items[0]
            segments = 1
        fname = f"translated_{target_language}_{now_stamp()}_{uid()}.srt"
        write_text_file(SRT_DIR / fname, translated_srt_text)
        warning = stats.get("google_error_segments_removed", 0) > 0 or stats.get("possibly_untranslated_segments", 0) > 0
        return jsonify({
            "success": True,
            "ok": True,
            "translated_srt_text": translated_srt_text,
            "translatedSrtText": translated_srt_text,
            "translated_srt_url": public_url("srt", fname, "translated.srt"),
            "filename": fname,
            "translation": {
                "engine": "google_translate",
                "source_language": source_language,
                "target_language": target_language,
                "segments": segments,
                "quality_warning": warning,
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
    target_ratio = float(data.get("target_length_ratio") or os.getenv("REWRITE_TARGET_RATIO_CONCISE", "0.28"))
    option_id = "emotional_tts" if "emotion" in style else "natural_accurate"
    opt = make_rewrite_option(cleaned, option_id, "Rewritten Script", target_ratio)
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
    target_ratio = float(data.get("target_length_ratio") or os.getenv("REWRITE_TARGET_RATIO_CONCISE", "0.28"))
    natural = make_rewrite_option(cleaned, "natural_accurate", "Natural Accurate", target_ratio)
    emotional = make_rewrite_option(cleaned, "emotional_tts", "Emotional TTS", target_ratio)
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
        "cleaned_input_chars": len(cleaned),
        "quality_gate": {
            "fallback_is_preview_only": True,
            "final_tts_requires_tts_safe": True,
            "must_include_ending": True,
        },
    })


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

    engine = data.get("engine") or "edge_tts"
    language = data.get("language") or "my"
    gender = (data.get("gender") or "male").lower()
    style = data.get("style") or "concise_narrative_summary"

    final_script = ensure_myanmar_punctuation(text)
    script_name = f"final_script_{now_stamp()}_{uid()}.txt"
    write_text_file(SCRIPT_DIR / script_name, final_script)

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
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
