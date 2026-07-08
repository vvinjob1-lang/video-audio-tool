"""
VoiceCraft Myanmar — V22.8 Edge + Gemini TTS Backend Wrapper

Drop-in wrapper for an existing Flask backend app.py.
It imports the existing Flask `app`, preserves all existing endpoints, and replaces
POST /tts with a clean Edge/Gemini-only TTS implementation + truthful response contract.

Deploy:
1) Keep existing app.py unchanged.
2) Add this file as contract_app.py beside app.py.
3) Procfile: web: gunicorn contract_app:app
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import subprocess
import traceback
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from flask import jsonify, request, send_from_directory

try:
    import app as legacy_app_module  # existing app.py
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "Could not import existing app.py. Keep contract_app.py beside app.py. "
        f"Original import error: {exc}"
    ) from exc

app = legacy_app_module.app

V22_CONTRACT_VERSION = "v22.8-edge-gemini-tts-speed-wrapper"

BASE_DIR = Path(getattr(legacy_app_module, "BASE_DIR", Path.cwd()))
TTS_DIR = Path(getattr(legacy_app_module, "TTS_DIR", BASE_DIR / "tts"))
SRT_DIR = Path(getattr(legacy_app_module, "SRT_DIR", BASE_DIR / "srt"))
SCRIPT_DIR = Path(getattr(legacy_app_module, "SCRIPT_DIR", BASE_DIR / "script"))
for _d in [TTS_DIR, SRT_DIR, SCRIPT_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://video-audio-tool-production.up.railway.app").rstrip("/")


def _safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _public_url(path: str) -> str:
    try:
        build_public_url = getattr(legacy_app_module, "build_public_url", None)
        if callable(build_public_url):
            return build_public_url(path)
    except Exception:
        pass
    if not path.startswith("/"):
        path = "/" + path
    return f"{DEFAULT_BASE_URL}{path}"


def _sanitize_filename(name: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    return name or fallback


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    ms_total = int(round(seconds * 1000))
    ms = ms_total % 1000
    total_seconds = ms_total // 1000
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean_script_for_srt(text: str) -> str:
    text = str(text or "").replace("\ufeff", "")
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"(?im)^\s*(WEBVTT|NOTE|STYLE)\s*$", "", text)
    text = re.sub(r"(?m)^\s*\d+\s*$", "", text)
    text = re.sub(r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->.*$", "", text)
    text = re.sub(r"(?im)^\s*(ORIGINAL SOURCE TEXT|ROUGH MYANMAR TRANSLATION|IMPORTANT|BEGINNING|MIDDLE|ENDING)\s*[:：-].*$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_script_into_lines(script: str) -> list[str]:
    text = _clean_script_for_srt(script)
    if not text:
        return []
    build_lines = getattr(legacy_app_module, "split_script_into_srt_cues", None)
    if callable(build_lines):
        try:
            lines = build_lines(text)
            if isinstance(lines, list) and lines:
                return [str(x).strip() for x in lines if str(x).strip()]
        except Exception:
            pass
    pieces = [p.strip() for p in re.split(r"(?<=[။.!?])\s+|\n+", text) if p.strip()]
    lines: list[str] = []
    for piece in pieces:
        if len(piece) <= 95:
            lines.append(piece)
            continue
        words = piece.split()
        cur = ""
        for word in words:
            if cur and len(cur) + 1 + len(word) > 85:
                lines.append(cur.strip())
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            lines.append(cur.strip())
    return lines


def _build_final_srt(script: str, total_duration: Optional[float]) -> str:
    legacy_builder = getattr(legacy_app_module, "build_final_srt_from_script", None)
    if callable(legacy_builder):
        try:
            srt_text = legacy_builder(script, total_duration=total_duration)
            if isinstance(srt_text, str) and srt_text.strip():
                return srt_text
        except Exception:
            pass

    lines = _split_script_into_lines(script)
    if not lines:
        return ""
    weights = [max(18, len(line)) for line in lines]
    total_weight = sum(weights) or len(lines)
    if not total_duration or total_duration <= 0:
        durations = [max(2.0, min(7.0, len(line) / 18.0)) for line in lines]
        total_duration = sum(durations)
    else:
        raw = [float(total_duration) * (w / total_weight) for w in weights]
        durations = [max(1.4, min(8.0, x)) for x in raw]
        scale = float(total_duration) / max(0.001, sum(durations))
        durations = [d * scale for d in durations]
    blocks = []
    start = 0.0
    for idx, (line, duration) in enumerate(zip(lines, durations), start=1):
        end = start + duration
        if idx == len(lines) and total_duration and total_duration > 0:
            end = total_duration
        blocks.append(f"{idx}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{line}\n")
        start = end
    return "\n".join(blocks).strip() + "\n"


def _duration_seconds(path: Path) -> Optional[float]:
    legacy_duration = getattr(legacy_app_module, "get_audio_duration_seconds", None)
    if callable(legacy_duration):
        try:
            val = legacy_duration(path)
            if val and float(val) > 0:
                return float(val)
        except Exception:
            pass
    try:
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or 24000
                return frames / float(rate)
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            val = float((result.stdout or "").strip())
            return val if val > 0 else None
    except Exception:
        pass
    return None


def _requested_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    output_quality = _safe_lower(payload.get("output_quality") or payload.get("quality") or payload.get("voice_tier"))
    engine = _safe_lower(payload.get("engine") or payload.get("voice_engine"))
    model = str(payload.get("model") or payload.get("tts_model") or "").strip()

    if not output_quality:
        if "pro" in model.lower():
            output_quality = "pro"
        elif "gemini" in model.lower() or engine == "gemini_tts":
            output_quality = "premium"
        else:
            output_quality = "basic"

    if not engine:
        engine = "gemini_tts" if output_quality in {"premium", "pro"} or "gemini" in model.lower() else "edge_tts"

    if not model:
        if engine == "edge_tts" or output_quality == "basic":
            model = "edge_tts"
        elif output_quality == "pro":
            model = "gemini-2.5-pro-preview-tts"
        else:
            model = "gemini-2.5-flash-preview-tts"

    speed = _safe_lower(payload.get("speed") or payload.get("voice_speed") or payload.get("voiceSpeed") or payload.get("rate") or "normal")
    if speed not in {"slow", "normal", "fast"}:
        speed = "normal"

    return {
        "voice_tier_requested": output_quality,
        "engine_requested": engine,
        "model_requested": model,
        "speed_requested": speed,
        "api_source_requested": _safe_lower(payload.get("api_source") or payload.get("apiSource")) or None,
        "voice_name_requested": payload.get("voice_name") or payload.get("voice") or payload.get("gender"),
    }


def _select_edge_voice(payload: Dict[str, Any]) -> str:
    requested_voice = str(payload.get("voice") or payload.get("voice_name") or "").strip()
    if requested_voice.startswith("my-MM-"):
        return requested_voice
    gender = _safe_lower(payload.get("gender") or requested_voice or "male")
    if gender == "female":
        return "my-MM-NilarNeural"
    return "my-MM-ThihaNeural"


def _edge_rate_for_speed(speed: str) -> str:
    return {"slow": "-15%", "normal": "+0%", "fast": "+15%"}.get(speed, "+0%")


async def _edge_tts_generate(text: str, voice: str, output_path: Path, rate: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch="+0Hz", volume="+0%")
    await communicate.save(str(output_path))


def _save_text_file(text: str, prefix: str, suffix: str, directory: Path) -> Tuple[str, str]:
    filename = f"{_sanitize_filename(prefix, 'file')}_{uuid.uuid4().hex[:10]}{suffix}"
    path = directory / filename
    path.write_text(text or "", encoding="utf-8")
    route = "script" if suffix == ".txt" else "srt"
    return filename, _public_url(f"/{route}/{filename}")


def _write_pcm_wav(path: Path, pcm_data: bytes, sample_rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


def _extract_inline_audio(response_json: Dict[str, Any]) -> Tuple[bytes, str]:
    stack = [response_json]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            inline = item.get("inlineData") or item.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return base64.b64decode(inline["data"]), str(inline.get("mimeType") or inline.get("mime_type") or "")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    raise RuntimeError("Gemini TTS response did not contain inline audio data")


def _normalize_gemini_model(model: str, quality: str) -> str:
    m = (model or "").strip()
    alias_map = {
        "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-tts": "gemini-2.5-pro-preview-tts",
    }
    if m in alias_map:
        return alias_map[m]
    if m:
        return m
    return "gemini-2.5-pro-preview-tts" if quality == "pro" else "gemini-2.5-flash-preview-tts"


def _gemini_api_key(payload: Dict[str, Any]) -> Optional[str]:
    api_source = _safe_lower(payload.get("api_source") or payload.get("apiSource"))
    if api_source == "user":
        return str(payload.get("user_api_key") or payload.get("gemini_api_key") or "").strip() or None
    return (
        os.getenv("GEMINI_APP_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or None
    )


def _gemini_speed_instruction(speed: str) -> str:
    if speed == "slow":
        return "Read at a slower, clear documentary pace with gentle pauses."
    if speed == "fast":
        return "Read at a slightly faster recap pace while keeping the Myanmar pronunciation clear."
    return "Read at a balanced natural narration speed."


def _generate_gemini_tts(text: str, payload: Dict[str, Any], requested: Dict[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    api_key = _gemini_api_key(payload)
    if not api_key:
        raise RuntimeError("Gemini API key is not configured. Set GEMINI_APP_API_KEY or use User API key mode.")

    quality = requested["voice_tier_requested"]
    model_requested = requested["model_requested"]
    model_used = _normalize_gemini_model(model_requested, quality)
    voice_name = str(payload.get("voice_name") or payload.get("voice") or "Kore").strip() or "Kore"
    speed = requested["speed_requested"]

    prompt = (
        "You are generating Myanmar narrative voice-over audio. "
        f"Voice direction: {_gemini_speed_instruction(speed)} "
        "Use a polished movie recap/documentary narration tone. "
        "Speak only the provided Myanmar narration text; do not add introductions or extra words.\n\n"
        f"Narration text:\n{text}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_used}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name}
                }
            },
        },
    }
    timeout = int(os.getenv("GEMINI_TTS_TIMEOUT", "300"))
    resp = requests.post(url, params={"key": api_key}, json=body, timeout=timeout)
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise RuntimeError(f"Gemini TTS API error {resp.status_code}: {detail}")
    data = resp.json()
    audio_bytes, mime_type = _extract_inline_audio(data)

    ext = ".wav"
    if "mpeg" in mime_type or "mp3" in mime_type:
        ext = ".mp3"
    elif "wav" in mime_type or audio_bytes[:4] == b"RIFF":
        ext = ".wav"
    elif "l16" in mime_type.lower() or "pcm" in mime_type.lower() or not mime_type:
        ext = ".wav"

    filename = f"gemini_tts_{uuid.uuid4().hex[:12]}{ext}"
    output_path = TTS_DIR / filename
    if ext == ".wav" and audio_bytes[:4] != b"RIFF":
        _write_pcm_wav(output_path, audio_bytes, sample_rate=24000)
    else:
        output_path.write_bytes(audio_bytes)

    meta = {
        "model_used": model_used,
        "voice_used": voice_name,
        "audio_format": ext.lstrip("."),
        "gemini_mime_type": mime_type,
        "speed_prompted": True,
        "speed_applied": False,
        "speed_note": "Gemini speed is requested through the narration prompt; exact speed is not guaranteed by the backend.",
    }
    return output_path, meta


def _generate_edge_tts(text: str, payload: Dict[str, Any], requested: Dict[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    voice = _select_edge_voice(payload)
    speed = requested["speed_requested"]
    rate = _edge_rate_for_speed(speed)
    filename = f"edge_tts_{uuid.uuid4().hex[:12]}.mp3"
    output_path = TTS_DIR / filename
    try:
        asyncio.run(_edge_tts_generate(text, voice, output_path, rate=rate))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_edge_tts_generate(text, voice, output_path, rate=rate))
        finally:
            loop.close()
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("Edge TTS audio file was not created")
    return output_path, {
        "model_used": "edge_tts",
        "voice_used": voice,
        "audio_format": "mp3",
        "edge_rate": rate,
        "speed_applied": True,
        "speed_applied_value": speed,
        "speed_note": f"Edge TTS rate applied: {rate}",
    }


def _build_tts_response(
    *,
    payload: Dict[str, Any],
    requested: Dict[str, Any],
    output_path: Path,
    text: str,
    engine_used: str,
    model_used: str,
    voice_used: Optional[str],
    audio_format: str,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    speed_applied: bool = False,
    speed_applied_value: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    duration = _duration_seconds(output_path)
    final_srt_text = _build_final_srt(text, total_duration=duration)
    srt_filename, srt_url = _save_text_file(final_srt_text, "final", ".srt", SRT_DIR)
    script_filename, script_url = _save_text_file(text, "final_script", ".txt", SCRIPT_DIR)
    audio_url = _public_url(f"/tts/{output_path.name}")

    contract = {
        "success": True,
        "ok": True,
        "contract_version": V22_CONTRACT_VERSION,
        **requested,
        "engine_used": engine_used,
        "actual_engine": engine_used,
        "model_used": model_used,
        "actual_model": model_used,
        "voice_used": voice_used,
        "actual_voice": voice_used,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "speed_applied": speed_applied,
        "speed_applied_value": speed_applied_value,
        "speed_note": (extra or {}).get("speed_note") or (
            "Speed/rate was applied by the backend."
            if speed_applied else
            "Speed is currently recorded as a UI preference unless the backend explicitly reports speed_applied=true."
        ),
        "frontend_truth_hint": {
            "show_requested_engine": requested["engine_requested"],
            "show_actual_engine": engine_used,
            "show_fallback_badge": fallback_used,
            "show_speed_applied": speed_applied,
        },
        "engine": engine_used,
        "source": engine_used,
        "model": model_used,
        "voice": voice_used,
        "voice_name": voice_used,
        "audio_url": audio_url,
        "tts_audio_url": audio_url,
        "download_url": f"{audio_url}?download_name=final-audio.{audio_format}",
        "audio_filename": output_path.name,
        "filename": output_path.name,
        "audio_format": audio_format,
        "audio_duration_seconds": duration,
        "final_srt_text": final_srt_text,
        "final_srt_url": srt_url,
        "final_srt_filename": srt_filename,
        "final_script_text": text,
        "final_script_url": script_url,
        "final_script_filename": script_filename,
    }
    if extra:
        contract.update(extra)
    return contract


def _error_contract(payload: Dict[str, Any], requested: Dict[str, Any], message: str, status_code: int = 500) -> Tuple[Dict[str, Any], int]:
    return {
        "success": False,
        "ok": False,
        "error": message,
        "message": message,
        "contract_version": V22_CONTRACT_VERSION,
        **requested,
        "engine_used": "unknown",
        "actual_engine": "unknown",
        "model_used": None,
        "actual_model": None,
        "voice_used": None,
        "actual_voice": None,
        "fallback_used": False,
        "fallback_reason": None,
        "speed_applied": False,
        "speed_applied_value": None,
        "frontend_truth_hint": {
            "show_requested_engine": requested.get("engine_requested"),
            "show_actual_engine": "unknown",
            "show_fallback_badge": False,
            "show_speed_applied": False,
        },
    }, status_code


def _v22_tts_handler() -> Tuple[Dict[str, Any], int]:
    payload = request.get_json(silent=True) or {}
    text = _clean_script_for_srt(payload.get("text") or payload.get("script") or payload.get("finalScript") or "")
    requested = _requested_from_payload(payload)

    if not text:
        return _error_contract(payload, requested, "Text is required for TTS.", 400)
    if len(text) < 5:
        return _error_contract(payload, requested, "Text is too short for TTS.", 400)

    requested_engine = requested["engine_requested"]
    try:
        if requested_engine == "gemini_tts":
            try:
                output_path, meta = _generate_gemini_tts(text, payload, requested)
                return _build_tts_response(
                    payload=payload,
                    requested=requested,
                    output_path=output_path,
                    text=text,
                    engine_used="gemini_tts",
                    model_used=meta.get("model_used") or requested["model_requested"],
                    voice_used=meta.get("voice_used"),
                    audio_format=meta.get("audio_format") or output_path.suffix.lstrip(".") or "wav",
                    fallback_used=False,
                    fallback_reason=None,
                    speed_applied=bool(meta.get("speed_applied")),
                    speed_applied_value=meta.get("speed_applied_value"),
                    extra=meta,
                ), 200
            except Exception as gemini_exc:
                # Production-safe fallback: generate usable Edge audio and truthfully report fallback.
                output_path, meta = _generate_edge_tts(text, {**payload, "gender": payload.get("gender") or "male"}, requested)
                fallback_reason = f"Gemini TTS failed, so Edge TTS fallback was used. Reason: {str(gemini_exc)[:240]}"
                return _build_tts_response(
                    payload=payload,
                    requested=requested,
                    output_path=output_path,
                    text=text,
                    engine_used="edge_tts",
                    model_used=meta.get("model_used") or "edge_tts",
                    voice_used=meta.get("voice_used"),
                    audio_format=meta.get("audio_format") or "mp3",
                    fallback_used=True,
                    fallback_reason=fallback_reason,
                    speed_applied=bool(meta.get("speed_applied")),
                    speed_applied_value=meta.get("speed_applied_value"),
                    extra=meta,
                ), 200

        # Basic / default Edge path.
        output_path, meta = _generate_edge_tts(text, payload, requested)
        return _build_tts_response(
            payload=payload,
            requested=requested,
            output_path=output_path,
            text=text,
            engine_used="edge_tts",
            model_used=meta.get("model_used") or "edge_tts",
            voice_used=meta.get("voice_used"),
            audio_format=meta.get("audio_format") or "mp3",
            fallback_used=False,
            fallback_reason=None,
            speed_applied=bool(meta.get("speed_applied")),
            speed_applied_value=meta.get("speed_applied_value"),
            extra=meta,
        ), 200
    except Exception as exc:
        result, code = _error_contract(payload, requested, "The backend failed while generating voice-over audio.", 500)
        result["detail"] = str(exc)
        result["debug_trace"] = traceback.format_exc() if os.getenv("V22_DEBUG_TRACE") == "true" else None
        return result, code


def _find_original_tts_view():
    for rule in list(app.url_map.iter_rules()):
        if rule.rule == "/tts" and "POST" in rule.methods:
            endpoint = rule.endpoint
            original = app.view_functions.get(endpoint)
            return endpoint, original
    return None, None


_ORIGINAL_TTS_ENDPOINT, _ORIGINAL_TTS_VIEW = _find_original_tts_view()


def v22_tts_route(*args, **kwargs):  # type: ignore[no-untyped-def]
    data, status_code = _v22_tts_handler()
    return jsonify(data), status_code


if _ORIGINAL_TTS_ENDPOINT:
    app.view_functions[_ORIGINAL_TTS_ENDPOINT] = v22_tts_route
else:
    app.add_url_rule("/tts", "v22_tts_route", v22_tts_route, methods=["POST"])


# Add safe static routes only when legacy app lacks them.
def _has_route(rule_path: str) -> bool:
    return any(rule.rule == rule_path for rule in app.url_map.iter_rules())


if not _has_route("/tts/<path:filename>"):
    @app.get("/tts/<path:filename>")
    def v22_serve_tts(filename: str):
        safe_filename = Path(filename).name
        mime = mimetypes.guess_type(safe_filename)[0] or "audio/mpeg"
        return send_from_directory(TTS_DIR, safe_filename, mimetype=mime, as_attachment=True, download_name=safe_filename, max_age=0)

if not _has_route("/srt/<path:filename>"):
    @app.get("/srt/<path:filename>")
    def v22_serve_srt(filename: str):
        safe_filename = Path(filename).name
        return send_from_directory(SRT_DIR, safe_filename, mimetype="text/plain; charset=utf-8", as_attachment=True, download_name=safe_filename, max_age=0)

if not _has_route("/script/<path:filename>"):
    @app.get("/script/<path:filename>")
    def v22_serve_script(filename: str):
        safe_filename = Path(filename).name
        return send_from_directory(SCRIPT_DIR, safe_filename, mimetype="text/plain; charset=utf-8", as_attachment=True, download_name=safe_filename, max_age=0)


@app.get("/v22-capabilities")
def v22_capabilities():
    return jsonify(
        {
            "ok": True,
            "success": True,
            "contract_version": V22_CONTRACT_VERSION,
            "backend_wrapper": "contract_app.py",
            "legacy_app_imported": True,
            "tts_wrapped": True,
            "providers_allowed": ["edge_tts", "gemini_tts"],
            "providers_removed_from_product_ui": [
                "iamhc",
                "openrouter",
                "qwen",
                "deepseek",
                "stepaudio",
                "voice_clone",
                "ollama",
            ],
            "voice_tiers": {
                "basic": {"engine": "edge_tts", "speed_supported": True, "speed_mode": "edge_rate"},
                "premium": {"engine": "gemini_tts", "fallback": "edge_tts", "speed_supported": "prompt_only"},
                "pro": {"engine": "gemini_tts", "fallback": "edge_tts", "speed_supported": "prompt_only"},
            },
            "gemini": {
                "app_key_configured": bool(os.getenv("GEMINI_APP_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
                "flash_model_default": os.getenv("GEMINI_TTS_FLASH_MODEL", "gemini-2.5-flash-preview-tts"),
                "pro_model_default": os.getenv("GEMINI_TTS_PRO_MODEL", "gemini-2.5-pro-preview-tts"),
                "voice_default": os.getenv("GEMINI_TTS_VOICE_NAME", "Kore"),
            },
            "truth_contract_fields": [
                "engine_requested",
                "engine_used",
                "model_requested",
                "model_used",
                "voice_used",
                "fallback_used",
                "fallback_reason",
                "speed_requested",
                "speed_applied",
            ],
            "notes": [
                "V22.8 replaces /tts with Edge/Gemini-only generation.",
                "Edge speed is applied with edge-tts rate control.",
                "Gemini speed is requested through prompt direction; exact speed is not guaranteed.",
                "It does not change /extract-srt, /translate-srt, or /rewrite-options.",
            ],
        }
    )


@app.get("/v22-health")
def v22_health():
    return jsonify(
        {
            "ok": True,
            "success": True,
            "contract_version": V22_CONTRACT_VERSION,
            "legacy_app_imported": True,
            "tts_wrapped": True,
            "original_tts_endpoint": _ORIGINAL_TTS_ENDPOINT,
            "gemini_app_key_configured": bool(os.getenv("GEMINI_APP_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        }
    )
