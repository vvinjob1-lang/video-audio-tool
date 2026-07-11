"""VoiceCraft Myanmar V25 production wrapper.

Drop-in wrapper around the existing Flask backend. It preserves all existing
routes, replaces only the public status responses and /tts dispatch behavior,
and adds direct Gemini 2.5 Pro TTS support with explicit paid-tier errors.

Expected Procfile:
    web: gunicorn production_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 600

The wrapper imports VC_BASE_MODULE when set, otherwise tries contract_app and
then app. The imported module must expose a Flask object named ``app``.
"""
from __future__ import annotations

import base64
import importlib
import io
import sys
from array import array
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

CONTRACT_VERSION = "v25.2-edge-local-normalization-fix"
PRO_MODEL = os.getenv("GEMINI_PRO_TTS_MODEL", "gemini-2.5-pro-preview-tts")
FLASH_MODEL = os.getenv("GEMINI_FLASH_TTS_MODEL", "gemini-2.5-flash-preview-tts")
OUTPUT_ROOT = Path(os.getenv("VOICECRAFT_OUTPUT_ROOT", "/tmp/voicecraft_v25"))
AUDIO_DIR = OUTPUT_ROOT / "audio"
SRT_DIR = OUTPUT_ROOT / "srt"
SCRIPT_DIR = OUTPUT_ROOT / "script"
MAX_SCRIPT_CHARS = int(os.getenv("VOICECRAFT_MAX_SCRIPT_CHARS", "30000"))
MAX_CHUNK_CHARS = int(os.getenv("GEMINI_TTS_MAX_CHUNK_CHARS", "3500"))
GOOGLE_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TTS_TIMEOUT_SECONDS", "240"))
OUTPUT_TTL_SECONDS = int(os.getenv("VOICECRAFT_OUTPUT_TTL_SECONDS", "86400"))
CHUNK_GAP_MS = int(os.getenv("GEMINI_TTS_CHUNK_GAP_MS", "180"))
TARGET_PEAK_DBFS = float(os.getenv("VOICECRAFT_TARGET_PEAK_DBFS", "-3.0"))
NORMALIZE_EDGE_AUDIO = _EDGE_NORMALIZE_DEFAULT = os.getenv("VOICECRAFT_NORMALIZE_EDGE_AUDIO", "true").strip().lower() in {"1", "true", "yes", "on"}
EDGE_TARGET_LUFS = float(os.getenv("VOICECRAFT_EDGE_TARGET_LUFS", "-17.0"))
EDGE_TARGET_TRUE_PEAK = float(os.getenv("VOICECRAFT_EDGE_TARGET_TRUE_PEAK", "-2.0"))
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://v15r.vercel.app,http://localhost:3000,http://localhost:5173",
    ).split(",")
    if origin.strip()
}

for directory in (AUDIO_DIR, SRT_DIR, SCRIPT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

LOGGER = logging.getLogger("voicecraft_v25")

MODEL_ALIASES = {
    "gemini-2.5-pro-tts": PRO_MODEL,
    "gemini-2.5-pro-preview-tts": PRO_MODEL,
    "gemini-2.5-flash-tts": FLASH_MODEL,
    "gemini-2.5-flash-preview-tts": FLASH_MODEL,
}

VALID_GEMINI_VOICES = {
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede",
    "Autonoe", "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome",
    "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus",
    "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
    "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi",
}


class GeminiTtsError(RuntimeError):
    def __init__(self, *, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _import_base_module():
    candidates: List[str] = []
    configured = os.getenv("VC_BASE_MODULE", "").strip()
    if configured:
        candidates.append(configured)
    candidates.extend(["contract_app", "app"])

    last_error: Optional[Exception] = None
    for module_name in candidates:
        if module_name == __name__:
            continue
        try:
            module = importlib.import_module(module_name)
            candidate_app = getattr(module, "app", None)
            if isinstance(candidate_app, Flask):
                return module_name, module, candidate_app
        except Exception as exc:  # pragma: no cover - deployment diagnostics
            last_error = exc
            LOGGER.warning("Could not import base module %s: %s", module_name, exc)
    raise RuntimeError(
        "Could not import the existing Flask backend. Set VC_BASE_MODULE to the "
        "module that exposes app. Last error: %r" % (last_error,)
    )


BASE_MODULE_NAME, BASE_MODULE, app = _import_base_module()


def _find_endpoint(path: str, method: str = "GET") -> Optional[str]:
    for rule in app.url_map.iter_rules():
        if rule.rule == path and method.upper() in rule.methods:
            return rule.endpoint
    return None


def _replace_view(path: str, method: str, view: Callable[..., Any]) -> Optional[Callable[..., Any]]:
    endpoint = _find_endpoint(path, method)
    if not endpoint:
        return None
    original = app.view_functions.get(endpoint)
    app.view_functions[endpoint] = view
    return original


ORIGINAL_TTS_ENDPOINT = _find_endpoint("/tts", "POST")
if not ORIGINAL_TTS_ENDPOINT:
    raise RuntimeError("Existing backend does not expose POST /tts")
ORIGINAL_TTS_VIEW = app.view_functions[ORIGINAL_TTS_ENDPOINT]


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "allow"}


def _canonical_model(value: Any, tier: str) -> str:
    raw = str(value or "").strip()
    if raw in MODEL_ALIASES:
        return MODEL_ALIASES[raw]
    if raw:
        return raw
    return PRO_MODEL if tier == "pro" else FLASH_MODEL


def _resolve_api_source(payload: Dict[str, Any]) -> str:
    source = str(payload.get("api_source") or payload.get("api_mode") or "app").lower()
    return "user" if source in {"user", "user_api", "my_gemini_api", "byok"} else "app"


def _resolve_api_key(payload: Dict[str, Any], source: str) -> Optional[str]:
    if source == "user":
        for field in ("user_api_key", "gemini_api_key", "api_key"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_APP_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _resolve_voice(payload: Dict[str, Any]) -> str:
    candidate = str(payload.get("voice_name") or payload.get("voice") or "Kore").strip()
    if candidate in VALID_GEMINI_VOICES:
        return candidate
    # Common typo seen in QA recordings.
    if candidate.lower() == "koge":
        return "Kore"
    return "Kore"


def _is_pro_request(payload: Dict[str, Any]) -> bool:
    tier = str(payload.get("output_quality") or payload.get("quality_tier") or "").lower()
    model = _canonical_model(payload.get("model") or payload.get("voice_model"), tier)
    engine = str(payload.get("engine") or payload.get("voice_engine") or "").lower()
    return tier == "pro" or model == PRO_MODEL or ("pro" in model and engine == "gemini_tts")


def _is_gemini_request(payload: Dict[str, Any]) -> bool:
    tier = str(payload.get("output_quality") or payload.get("quality_tier") or "").lower()
    engine = str(payload.get("engine") or payload.get("voice_engine") or "").lower()
    model = str(payload.get("model") or payload.get("voice_model") or "").lower()
    return engine in {"gemini_tts", "gemini", "gemini_tts_app"} or "gemini" in model or tier == "pro"


def _fallback_policy(payload: Dict[str, Any]) -> str:
    policy = str(payload.get("fallback_policy") or "none").strip().lower()
    if policy in {"premium", "edge", "none"}:
        return policy
    if _bool(payload.get("allow_fallback"), False):
        return "premium"
    return "none"


def _style_instruction(speed: str, tone: str) -> str:
    speed_map = {
        "slow": "Read at a deliberately slow pace with clear pauses.",
        "fast": "Read at a brisk but intelligible narration pace.",
        "normal": "Read at a balanced natural narration pace.",
    }
    tone_map = {
        "documentary": "Use a calm, mature documentary narrator tone.",
        "warm": "Use a warm, approachable storytelling tone.",
        "cinematic": "Use a cinematic, suspenseful tone without exaggeration.",
        "neutral": "Use a clear neutral narration tone.",
    }
    return f"{tone_map.get(tone, tone_map['documentary'])} {speed_map.get(speed, speed_map['normal'])}"


def _clean_script(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"```(?:\w+)?", "", value)
    value = re.sub(r"WEBVTT", "", value, flags=re.I)
    value = re.sub(r"^\s*\d+\s*$", "", value, flags=re.M)
    value = re.sub(
        r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}",
        "",
        value,
    )
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _split_long_text(text: str, max_chars: int) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            # Myanmar full stop and common punctuation aware sentence split.
            sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s*", paragraph) if s.strip()]
        else:
            sentences = [paragraph]
        for sentence in sentences:
            if len(sentence) > max_chars:
                flush()
                for start in range(0, len(sentence), max_chars):
                    chunks.append(sentence[start : start + max_chars].strip())
                continue
            candidate = f"{current}\n\n{sentence}".strip() if current else sentence
            if len(candidate) > max_chars:
                flush()
                current = sentence
            else:
                current = candidate
    flush()
    return chunks or [text]


def _google_error(response: requests.Response, model: str) -> GeminiTtsError:
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:1000]}
    error = data.get("error") if isinstance(data, dict) else None
    message = "Gemini TTS request failed."
    google_status = ""
    if isinstance(error, dict):
        message = str(error.get("message") or message)
        google_status = str(error.get("status") or "")

    status = response.status_code
    lowered = message.lower()
    detail_blob = json.dumps(data, ensure_ascii=False).lower()
    is_pro = model == PRO_MODEL

    if status in {400, 401} and any(token in lowered for token in ("api key", "key not valid", "invalid key")):
        code = "GEMINI_API_KEY_INVALID"
        user_message = "The Gemini API key is invalid or no longer active. Replace the key and retry."
        mapped_status = 401
    elif status == 429 and is_pro:
        code = "PRO_TTS_PAID_TIER_OR_QUOTA_REQUIRED"
        user_message = (
            "Gemini 2.5 Pro TTS has no free tier. Use a billed Gemini API project "
            "with available Pro TTS quota, or explicitly choose Premium Flash TTS."
        )
        mapped_status = 402
    elif status == 429 or "quota" in lowered or "rate limit" in lowered:
        code = "GEMINI_TTS_QUOTA_EXCEEDED"
        user_message = "Gemini TTS quota is unavailable or exhausted for this API project."
        mapped_status = 429
    elif status == 403 and is_pro:
        code = "PRO_TTS_PAID_TIER_OR_PERMISSION_REQUIRED"
        user_message = (
            "Gemini 2.5 Pro TTS requires a billed project with permission for this model. "
            "Enable billing or explicitly choose Premium Flash TTS."
        )
        mapped_status = 402
    elif status == 403 or "permission" in lowered or "service_disabled" in detail_blob:
        code = "GEMINI_TTS_PERMISSION_DENIED"
        user_message = "This Gemini API project does not have permission to use the selected TTS model."
        mapped_status = 403
    elif status == 404 or "not found" in lowered:
        code = "GEMINI_TTS_MODEL_NOT_AVAILABLE"
        user_message = "The selected Gemini TTS model is not available to this API project or region."
        mapped_status = 404
    elif status == 400:
        code = "GEMINI_TTS_BAD_REQUEST"
        user_message = "Gemini TTS rejected the request. Check the model, voice, and script length."
        mapped_status = 400
    else:
        code = "GEMINI_TTS_UPSTREAM_ERROR"
        user_message = "Gemini TTS could not generate audio. Please retry later."
        mapped_status = status
    return GeminiTtsError(
        status=mapped_status,
        code=code,
        message=user_message,
        details={"google_status": google_status, "upstream_message": message},
    )


def _extract_audio_bytes(data: Dict[str, Any]) -> Tuple[bytes, str]:
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise GeminiTtsError(
            status=502,
            code="PRO_TTS_NO_CANDIDATE",
            message="Gemini Pro TTS returned no audio candidate.",
            details=data,
        )
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            encoded = inline.get("data")
            if isinstance(encoded, str) and encoded:
                try:
                    return base64.b64decode(encoded), str(inline.get("mimeType") or inline.get("mime_type") or "")
                except Exception as exc:
                    raise GeminiTtsError(
                        status=502,
                        code="PRO_TTS_INVALID_AUDIO",
                        message="Gemini Pro TTS returned invalid audio data.",
                        details=str(exc),
                    ) from exc
    raise GeminiTtsError(
        status=502,
        code="PRO_TTS_NO_AUDIO",
        message="Gemini Pro TTS returned a response without audio.",
        details=data,
    )


def _call_gemini_tts(*, model: str, key: str, text: str, voice: str, speed: str, tone: str) -> Tuple[bytes, str]:
    instruction = _style_instruction(speed, tone)
    prompt = (
        f"{instruction}\n"
        "Read the Myanmar narration below exactly as written. Do not add, translate, summarize, "
        "or omit words. Preserve Myanmar punctuation and natural paragraph pauses.\n\n"
        f"{text}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }
    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=GOOGLE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise GeminiTtsError(
            status=504,
            code="PRO_TTS_TIMEOUT",
            message="Gemini Pro TTS timed out. Retry with a shorter script or Premium Flash TTS.",
            details=str(exc),
        ) from exc
    except requests.RequestException as exc:
        raise GeminiTtsError(
            status=502,
            code="PRO_TTS_NETWORK_ERROR",
            message="The backend could not reach Gemini Pro TTS.",
            details=str(exc),
        ) from exc

    if not response.ok:
        raise _google_error(response, model)
    try:
        data = response.json()
    except Exception as exc:
        raise GeminiTtsError(
            status=502,
            code="PRO_TTS_INVALID_RESPONSE",
            message="Gemini Pro TTS returned a non-JSON response.",
            details=response.text[:1000],
        ) from exc
    return _extract_audio_bytes(data)


def _audio_to_pcm(audio_bytes: bytes, mime_type: str) -> Tuple[bytes, int, int, int]:
    # Some SDK/API versions return a WAV container; REST commonly returns raw PCM.
    if audio_bytes[:4] == b"RIFF" or "wav" in mime_type.lower():
        with wave.open(io.BytesIO(audio_bytes), "rb") as source:
            channels = source.getnchannels()
            rate = source.getframerate()
            width = source.getsampwidth()
            frames = source.readframes(source.getnframes())
        if channels != 1 or rate != 24000 or width != 2:
            raise GeminiTtsError(
                status=502,
                code="PRO_TTS_UNEXPECTED_AUDIO_FORMAT",
                message="Gemini Pro TTS returned an unsupported WAV format.",
                details={"channels": channels, "rate": rate, "sample_width": width},
            )
        return frames, channels, rate, width
    # Official REST examples describe 16-bit, mono, 24 kHz PCM output.
    return audio_bytes, 1, 24000, 2


def _write_wav(path: Path, pcm: bytes, channels: int = 1, rate: int = 24000, width: int = 2) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(pcm)


def _normalize_pcm16(pcm: bytes, target_dbfs: float = -3.0) -> Tuple[bytes, float]:
    if not pcm:
        return pcm, 1.0
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    if peak <= 0:
        return pcm, 1.0
    target_peak = max(1, int(32767 * (10 ** (target_dbfs / 20.0))))
    gain = max(0.25, min(4.0, target_peak / float(peak)))
    if abs(gain - 1.0) < 0.01:
        return pcm, 1.0
    for index, value in enumerate(samples):
        scaled = int(round(value * gain))
        samples[index] = max(-32768, min(32767, scaled))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes(), round(gain, 4)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _script_cues(text: str) -> List[str]:
    pieces = [piece.strip() for piece in re.split(r"(?<=[။.!?])\s+|\n+", text) if piece.strip()]
    cues: List[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) > 140 and current:
            cues.append(current)
            current = piece
        else:
            current = candidate
    if current:
        cues.append(current)
    return cues or [text.strip()]


def _build_estimated_srt(text: str, duration: float) -> str:
    cues = _script_cues(text)
    weights = [max(1, len(re.sub(r"\s+", "", cue))) for cue in cues]
    total_weight = sum(weights)
    current = 0.0
    lines: List[str] = []
    for index, (cue, weight) in enumerate(zip(cues, weights), start=1):
        if index == len(cues):
            end = duration
        else:
            end = current + duration * (weight / total_weight)
        lines.extend([str(index), f"{_srt_time(current)} --> {_srt_time(end)}", cue, ""])
        current = end
    return "\n".join(lines).strip() + "\n"


def _cleanup_old_outputs() -> None:
    cutoff = time.time() - OUTPUT_TTL_SECONDS
    for directory in (AUDIO_DIR, SRT_DIR, SCRIPT_DIR):
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _generate_gemini_output(payload: Dict[str, Any], *, model: str, fallback_used: bool = False, fallback_reason: Optional[str] = None) -> Dict[str, Any]:
    raw_text = payload.get("text") or payload.get("final_script") or ""
    text = _clean_script(str(raw_text))
    if len(text) < 20:
        raise GeminiTtsError(status=400, code="TEXT_REQUIRED", message="A final script of at least 20 characters is required.")
    if len(text) > MAX_SCRIPT_CHARS:
        raise GeminiTtsError(
            status=413,
            code="SCRIPT_TOO_LONG",
            message=f"Script is too long. Maximum supported length is {MAX_SCRIPT_CHARS} characters.",
        )

    source = _resolve_api_source(payload)
    key = _resolve_api_key(payload, source)
    if not key:
        code = "USER_GEMINI_KEY_REQUIRED" if source == "user" else "APP_GEMINI_KEY_MISSING"
        message = "A Gemini API key is required for this request."
        raise GeminiTtsError(status=400, code=code, message=message)

    voice = _resolve_voice(payload)
    speed = str(payload.get("speed") or "normal").lower()
    tone = str(payload.get("tone") or "documentary").lower()
    chunks = _split_long_text(text, MAX_CHUNK_CHARS)
    all_pcm = bytearray()
    chunk_gap = b"\x00\x00" * max(0, int(24000 * CHUNK_GAP_MS / 1000.0))
    for chunk_index, chunk in enumerate(chunks):
        raw_audio, mime = _call_gemini_tts(
            model=model,
            key=key,
            text=chunk,
            voice=voice,
            speed=speed,
            tone=tone,
        )
        pcm, channels, rate, width = _audio_to_pcm(raw_audio, mime)
        if (channels, rate, width) != (1, 24000, 2):
            raise GeminiTtsError(
                status=502,
                code="PRO_TTS_UNEXPECTED_AUDIO_FORMAT",
                message="Gemini TTS returned incompatible audio chunks.",
            )
        if chunk_index > 0 and chunk_gap:
            all_pcm.extend(chunk_gap)
        all_pcm.extend(pcm)

    normalized_pcm, normalization_gain = _normalize_pcm16(bytes(all_pcm), TARGET_PEAK_DBFS)

    identifier = uuid.uuid4().hex[:16]
    audio_name = f"final_audio_{identifier}.wav"
    srt_name = f"final_{identifier}.srt"
    script_name = f"final_script_{identifier}.txt"
    audio_path = AUDIO_DIR / audio_name
    srt_path = SRT_DIR / srt_name
    script_path = SCRIPT_DIR / script_name

    _write_wav(audio_path, normalized_pcm)
    duration = len(normalized_pcm) / float(24000 * 2)
    srt_text = _build_estimated_srt(text, duration)
    srt_path.write_text(srt_text, encoding="utf-8")
    script_path.write_text(text, encoding="utf-8")
    _cleanup_old_outputs()

    requested_model = _canonical_model(payload.get("model") or payload.get("voice_model"), str(payload.get("output_quality") or "pro"))
    return {
        "ok": True,
        "success": True,
        "contract_version": CONTRACT_VERSION,
        "engine_requested": "gemini_tts",
        "engine_used": "gemini_tts",
        "model_requested": requested_model,
        "model_used": model,
        "voice_requested": str(payload.get("voice_name") or payload.get("voice") or "Kore"),
        "voice_used": voice,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "speed_requested": speed,
        "speed_applied": False,
        "speed_applied_value": "prompt-directed",
        "speed_note": "Gemini TTS pace is prompt-directed; exact speed is not guaranteed.",
        "tone_requested": tone,
        "audio_format": "wav",
        "audio_duration_seconds": round(duration, 3),
        "audio_url": f"/v25-audio/{audio_name}",
        "download_url": f"/v25-audio/{audio_name}",
        "final_srt_url": f"/v25-srt/{srt_name}",
        "final_script_url": f"/v25-script/{script_name}",
        "final_srt_text": srt_text,
        "final_script_text": text,
        "chunk_count": len(chunks),
        "chunk_gap_ms": CHUNK_GAP_MS,
        "audio_normalized": True,
        "normalization_gain": normalization_gain,
        "target_peak_dbfs": TARGET_PEAK_DBFS,
        "api_source_used": source,
        "pro_paid_tier_required": model == PRO_MODEL,
    }


def _call_original_with_payload(payload: Dict[str, Any]):
    old_json = getattr(request, "_cached_json", None)
    old_data = getattr(request, "_cached_data", None)
    try:
        request._cached_json = (payload, payload)  # Flask caches normal/silent get_json separately.
        request._cached_data = json.dumps(payload).encode("utf-8")
        return ORIGINAL_TTS_VIEW()
    finally:
        request._cached_json = old_json
        request._cached_data = old_data


def _edge_truth_contract(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    requested_engine = str(payload.get("engine") or payload.get("voice_engine") or "edge_tts")
    requested_model = payload.get("model") or payload.get("voice_model")
    actual_engine = data.get("engine_used") or data.get("engine") or "edge_tts"
    actual_model = data.get("model_used") or data.get("model")
    actual_format = str(data.get("audio_format") or "mp3").lower()
    data.update({
        "contract_version": CONTRACT_VERSION,
        "engine_requested": requested_engine,
        "model_requested": requested_model,
        "engine_used": actual_engine,
        "model_used": actual_model,
        "audio_format": actual_format,
        "actual_format": actual_format,
        "fallback_used": bool(data.get("fallback_used", False)),
    })
    return data


def _fetch_generated_audio(url: str) -> Tuple[bytes, str]:
    """Read generated audio without making a public self-request.

    Existing Edge routes may return either a relative URL or an absolute URL
    pointing back to this same Railway service. Calling the public Railway URL
    from inside the same request can deadlock a single-worker deployment and
    previously caused a 60-second read timeout. Same-service URLs are now
    resolved through Flask's internal test client; only truly external URLs use
    requests.get().
    """
    if not url:
        raise RuntimeError("Missing generated audio URL")

    parsed = urlparse(url)
    internal_path = ""

    if url.startswith("/"):
        internal_path = url
    elif parsed.scheme in {"http", "https"} and parsed.netloc:
        request_host = (request.host or "").lower()
        forwarded_host = str(request.headers.get("X-Forwarded-Host") or "").lower()
        public_host = str(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").lower()
        candidate_host = parsed.netloc.lower()

        same_service_hosts = {host for host in (request_host, forwarded_host, public_host) if host}
        # Railway may expose RAILWAY_PUBLIC_DOMAIN without a scheme. Also
        # accept the exact current request host to cover custom domains.
        if candidate_host in same_service_hosts:
            internal_path = parsed.path or "/"
            if parsed.query:
                internal_path += "?" + parsed.query

    if internal_path:
        with app.test_client() as client:
            response = client.get(internal_path)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Could not read generated audio internally: HTTP {response.status_code}"
                )
            return bytes(response.data), str(response.mimetype or "audio/mpeg")

    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content, str(response.headers.get("Content-Type") or "audio/mpeg")


def _normalize_edge_result(raw_result: Any, payload: Dict[str, Any]):
    response = app.make_response(raw_result)
    data = response.get_json(silent=True)
    if response.status_code >= 400 or not isinstance(data, dict):
        return raw_result

    data = _edge_truth_contract(data, payload)
    if not NORMALIZE_EDGE_AUDIO:
        data["audio_normalized"] = False
        data["normalization_note"] = "Edge normalization disabled by configuration."
        return jsonify(data), response.status_code

    ffmpeg_path = shutil.which("ffmpeg")
    audio_url = str(data.get("audio_url") or data.get("download_url") or "")
    if not ffmpeg_path or not audio_url:
        data["audio_normalized"] = False
        data["normalization_note"] = "ffmpeg or generated audio URL unavailable; original Edge audio returned."
        return jsonify(data), response.status_code

    try:
        audio_bytes, _mime = _fetch_generated_audio(audio_url)
        identifier = uuid.uuid4().hex[:16]
        output_name = f"final_audio_{identifier}.mp3"
        output_path = AUDIO_DIR / output_name
        with tempfile.TemporaryDirectory(prefix="voicecraft_edge_") as temp_dir:
            source_path = Path(temp_dir) / "source_audio"
            source_path.write_bytes(audio_bytes)
            command = [
                ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source_path),
                "-af", f"loudnorm=I={EDGE_TARGET_LUFS}:TP={EDGE_TARGET_TRUE_PEAK}:LRA=7",
                "-ar", "24000", "-ac", "1", "-b:a", "64k",
                str(output_path),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if completed.returncode != 0 or not output_path.exists():
                raise RuntimeError((completed.stderr or "ffmpeg normalization failed")[-1000:])

        data.update({
            "audio_url": f"/v25-audio/{output_name}",
            "download_url": f"/v25-audio/{output_name}",
            "audio_format": "mp3",
            "actual_format": "mp3",
            "audio_normalized": True,
            "normalization_note": f"Edge audio normalized to approximately {EDGE_TARGET_LUFS} LUFS.",
            "normalization_target_lufs": EDGE_TARGET_LUFS,
            "normalization_true_peak_dbfs": EDGE_TARGET_TRUE_PEAK,
        })
    except Exception as exc:
        LOGGER.warning("Edge normalization skipped: %s", exc)
        data["audio_normalized"] = False
        data["normalization_note"] = "Edge normalization failed; original generated audio returned."
        data["normalization_error"] = str(exc)[:300]
    return jsonify(data), response.status_code


def _explicit_edge_fallback(payload: Dict[str, Any], reason: str, requested_model: str):
    edge_payload = dict(payload)
    edge_payload.update({
        "engine": "edge_tts",
        "voice_engine": "edge_tts",
        "output_quality": "basic",
        "quality_tier": "basic",
        "model": None,
        "voice_model": None,
    })
    normalized = _normalize_edge_result(_call_original_with_payload(edge_payload), edge_payload)
    response = app.make_response(normalized)
    data = response.get_json(silent=True)
    if response.status_code < 400 and isinstance(data, dict):
        data.update({
            "contract_version": CONTRACT_VERSION,
            "engine_requested": "gemini_tts",
            "model_requested": requested_model,
            "engine_used": data.get("engine_used") or data.get("engine") or "edge_tts",
            "model_used": data.get("model_used") or data.get("model"),
            "fallback_used": True,
            "fallback_reason": reason,
            "fallback_confirmed_by_user": True,
        })
        return jsonify(data), response.status_code
    return normalized


def production_tts():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "success": False, "error_code": "INVALID_JSON", "error": "JSON object required."}), 400

    # Edge remains the stable base-engine path; V25.2 normalizes via internal file routing.
    if not _is_gemini_request(payload):
        return _normalize_edge_result(ORIGINAL_TTS_VIEW(), payload)

    tier = str(payload.get("output_quality") or payload.get("quality_tier") or "premium").lower()
    requested_model = _canonical_model(payload.get("model") or payload.get("voice_model"), tier)
    primary_model = PRO_MODEL if _is_pro_request(payload) else FLASH_MODEL
    policy = _fallback_policy(payload)

    try:
        result = _generate_gemini_output(payload, model=primary_model)
        return jsonify(result), 200
    except GeminiTtsError as primary_error:
        # Pro may explicitly fall back to Premium Flash. Premium has no same-tier retry fallback.
        if primary_model == PRO_MODEL and policy == "premium":
            try:
                result = _generate_gemini_output(
                    payload,
                    model=FLASH_MODEL,
                    fallback_used=True,
                    fallback_reason=primary_error.code,
                )
                return jsonify(result), 200
            except GeminiTtsError as flash_error:
                return jsonify({
                    "ok": False,
                    "success": False,
                    "contract_version": CONTRACT_VERSION,
                    "error_code": flash_error.code,
                    "error": flash_error.message,
                    "details": flash_error.details,
                    "engine_requested": "gemini_tts",
                    "model_requested": requested_model,
                    "fallback_attempted": "premium",
                    "fallback_used": False,
                }), flash_error.status

        if policy == "edge":
            return _explicit_edge_fallback(payload, primary_error.code, requested_model)

        allowed = ["edge"] if primary_model == FLASH_MODEL else ["premium", "edge"]
        return jsonify({
            "ok": False,
            "success": False,
            "contract_version": CONTRACT_VERSION,
            "error_code": primary_error.code,
            "error": primary_error.message,
            "details": primary_error.details,
            "engine_requested": "gemini_tts",
            "model_requested": requested_model,
            "fallback_used": False,
            "fallback_allowed": allowed,
            "requires_user_confirmation": True,
            "suggested_action": (
                "Retry Gemini, or explicitly choose Edge fallback."
                if primary_model == FLASH_MODEL
                else "Enable billing, retry Pro, or explicitly choose Premium/Edge fallback."
            ),
        }), primary_error.status


# Replace existing POST /tts implementation.
app.view_functions[ORIGINAL_TTS_ENDPOINT] = production_tts


@app.get("/v25-health")
def v25_health():
    return jsonify({
        "ok": True,
        "success": True,
        "name": "VoiceCraft Myanmar Backend",
        "version": CONTRACT_VERSION,
        "base_module": BASE_MODULE_NAME,
        "tts_wrapped": True,
        "pro_model": PRO_MODEL,
        "flash_model": FLASH_MODEL,
        "app_key_configured": bool(_resolve_api_key({}, "app")),
        "silent_fallback": False,
        "premium_flash_wrapped": True,
        "pro_tts_wrapped": True,
        "edge_normalization_enabled": NORMALIZE_EDGE_AUDIO,
        "ffmpeg_found": bool(shutil.which("ffmpeg")),
        "edge_internal_audio_fetch_fix": True,
    })


@app.get("/v25-capabilities")
def v25_capabilities():
    return jsonify({
        "ok": True,
        "version": CONTRACT_VERSION,
        "engines": {
            "basic": {"engine": "edge_tts", "format": "mp3", "status": "stable"},
            "premium": {"engine": "gemini_tts", "model": FLASH_MODEL, "format": "wav", "free_tier_possible": True},
            "pro": {
                "engine": "gemini_tts",
                "model": PRO_MODEL,
                "format": "wav",
                "paid_tier_required": True,
                "free_tier_available": False,
                "status": "requires_billed_project",
            },
        },
        "fallback": {
            "default": "none",
            "requires_user_confirmation": True,
            "supported_policies": ["none", "premium", "edge"],
        },
        "voices": sorted(VALID_GEMINI_VOICES),
        "byok_supported": True,
        "audio_processing": {
            "chunk_gap_ms": CHUNK_GAP_MS,
            "normalization": True,
            "target_peak_dbfs": TARGET_PEAK_DBFS,
            "edge_normalization_enabled": NORMALIZE_EDGE_AUDIO,
            "edge_target_lufs": EDGE_TARGET_LUFS,
            "edge_true_peak_dbfs": EDGE_TARGET_TRUE_PEAK,
        },
    })


@app.get("/v25-audio/<path:filename>")
def v25_audio(filename: str):
    mimetype = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
    return send_from_directory(AUDIO_DIR, filename, mimetype=mimetype, as_attachment=False, max_age=0)


@app.get("/v25-srt/<path:filename>")
def v25_srt(filename: str):
    return send_from_directory(SRT_DIR, filename, mimetype="application/x-subrip", as_attachment=False, max_age=0)


@app.get("/v25-script/<path:filename>")
def v25_script(filename: str):
    return send_from_directory(SCRIPT_DIR, filename, mimetype="text/plain; charset=utf-8", as_attachment=False, max_age=0)


def production_root():
    return jsonify({
        "ok": True,
        "success": True,
        "name": "VoiceCraft Myanmar Backend",
        "version": CONTRACT_VERSION,
        "status": "production-candidate",
        "endpoints": [
            "GET /health",
            "GET /v25-health",
            "GET /v25-capabilities",
            "POST /extract-srt",
            "POST /translate-srt",
            "POST /rewrite-options",
            "POST /tts",
        ],
    })


def production_health():
    return v25_health()


_replace_view("/", "GET", production_root)
_replace_view("/health", "GET", production_health)


if not _bool(os.getenv("ENABLE_DEBUG_ENDPOINTS"), False):
    def disabled_debug_endpoint(*_args: Any, **_kwargs: Any):
        return jsonify({"ok": False, "error": "Debug endpoints are disabled in production."}), 404

    _replace_view("/debug-iamhc-model-test", "POST", disabled_debug_endpoint)


@app.after_request
def production_headers(response: Response):
    origin = request.headers.get("Origin", "")
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith("/v25-"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
