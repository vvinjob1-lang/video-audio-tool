"""
VoiceCraft Myanmar — V22.7 Backend Contract Wrapper

Drop-in wrapper for an existing Flask backend app.py.
It imports the existing Flask `app`, preserves all existing endpoints,
and normalizes POST /tts responses so the frontend can truthfully display:
- requested voice engine/model
- actual engine/model used
- fallback status and reason
- speed requested/applied status

Usage:
1) Keep existing app.py unchanged.
2) Add this file as contract_app.py.
3) Change Procfile to: web: gunicorn contract_app:app
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any, Dict, Optional, Tuple

from flask import jsonify, request

# Import existing backend without modifying it.
try:
    import app as legacy_app_module  # existing app.py
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "Could not import existing app.py. Keep contract_app.py beside app.py. "
        f"Original import error: {exc}"
    ) from exc

app = legacy_app_module.app

V22_CONTRACT_VERSION = "v22.7-edge-gemini-truth-contract-wrapper"


def _safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _requested_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    output_quality = _safe_lower(payload.get("output_quality") or payload.get("quality") or payload.get("voice_tier"))
    engine = _safe_lower(payload.get("engine") or payload.get("voice_engine"))
    model = str(payload.get("model") or payload.get("tts_model") or "").strip()

    # Infer requested tier/engine/model when frontend sends partial legacy payloads.
    if not output_quality:
        if "pro" in model:
            output_quality = "pro"
        elif "gemini" in model or engine == "gemini_tts":
            output_quality = "premium"
        else:
            output_quality = "basic"

    if not engine:
        engine = "gemini_tts" if output_quality in {"premium", "pro"} or "gemini" in model.lower() else "edge_tts"

    if not model:
        if engine == "edge_tts" or output_quality == "basic":
            model = "edge_tts"
        elif output_quality == "pro":
            model = "gemini-2.5-pro-tts"
        else:
            model = "gemini-2.5-flash-tts"

    speed = _safe_lower(
        payload.get("speed")
        or payload.get("voice_speed")
        or payload.get("voiceSpeed")
        or payload.get("rate")
        or "normal"
    )
    if speed not in {"slow", "normal", "fast"}:
        speed = "normal"

    api_source = _safe_lower(payload.get("api_source") or payload.get("apiSource")) or None

    return {
        "voice_tier_requested": output_quality,
        "engine_requested": engine,
        "model_requested": model,
        "speed_requested": speed,
        "api_source_requested": api_source,
        "voice_name_requested": payload.get("voice_name") or payload.get("voice") or payload.get("gender"),
    }


def _infer_actual_engine(data: Dict[str, Any]) -> str:
    combined = " ".join(
        _safe_lower(data.get(key))
        for key in ["engine", "source", "provider", "engine_used", "actual_engine", "voice_engine"]
    )
    audio_url = _safe_lower(data.get("audio_url") or data.get("download_url"))

    if "gemini" in combined or "/gemini-" in audio_url or "/gemini_" in audio_url or "gemini" in audio_url:
        return "gemini_tts"
    if "edge" in combined or "my-mm-" in combined or "edge" in audio_url:
        return "edge_tts"

    # Some legacy responses only contain an Edge voice name.
    voice = _safe_lower(data.get("voice") or data.get("voice_name"))
    if voice.startswith("my-mm-"):
        return "edge_tts"

    return "unknown"


def _infer_actual_model(data: Dict[str, Any], actual_engine: str) -> Optional[str]:
    model = data.get("model") or data.get("model_used") or data.get("actual_model") or data.get("tts_model")
    if model:
        return str(model)
    if actual_engine == "edge_tts":
        return "edge_tts"
    if actual_engine == "gemini_tts":
        return "gemini_tts"
    return None


def _infer_actual_voice(data: Dict[str, Any]) -> Optional[str]:
    voice = data.get("voice") or data.get("voice_name") or data.get("actual_voice") or data.get("voice_used")
    if voice:
        return str(voice)
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "fallback"}


def _normalize_tts_contract(data: Dict[str, Any], status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    requested = _requested_from_payload(payload)
    actual_engine = _infer_actual_engine(data)
    actual_model = _infer_actual_model(data, actual_engine)
    actual_voice = _infer_actual_voice(data)

    requested_engine = requested["engine_requested"]
    explicit_fallback = any(
        _boolish(data.get(key))
        for key in ["fallback", "fallback_used", "used_fallback", "edge_fallback"]
    )
    inferred_fallback = requested_engine == "gemini_tts" and actual_engine == "edge_tts"
    fallback_used = bool(explicit_fallback or inferred_fallback)

    fallback_reason = (
        data.get("fallback_reason")
        or data.get("fallback_message")
        or data.get("warning")
        or None
    )
    if fallback_used and not fallback_reason:
        fallback_reason = "Gemini TTS was requested, but the backend generated the audio with Edge TTS fallback."

    # Speed/rate is only considered applied if the backend explicitly says so.
    speed_applied = any(
        _boolish(data.get(key))
        for key in ["speed_applied", "rate_applied", "voice_speed_applied"]
    )
    speed_applied_value = data.get("speed_applied_value") or data.get("rate_used") or None

    success = data.get("success")
    if success is None:
        success = data.get("ok")
    if success is None:
        success = 200 <= status_code < 300

    contract = {
        "contract_version": V22_CONTRACT_VERSION,
        "success": bool(success),
        "ok": bool(success),
        **requested,
        "engine_used": actual_engine,
        "actual_engine": actual_engine,
        "model_used": actual_model,
        "actual_model": actual_model,
        "voice_used": actual_voice,
        "actual_voice": actual_voice,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "speed_applied": speed_applied,
        "speed_applied_value": speed_applied_value,
        "speed_note": (
            "Speed/rate was applied by the backend."
            if speed_applied
            else "Speed is currently recorded as a UI preference unless the backend explicitly reports speed_applied=true."
        ),
        "frontend_truth_hint": {
            "show_requested_engine": requested_engine,
            "show_actual_engine": actual_engine,
            "show_fallback_badge": fallback_used,
            "show_speed_applied": speed_applied,
        },
    }

    # Preserve all original response fields, but standardized contract fields win when names overlap.
    merged = dict(data)
    merged.update(contract)
    return merged


def _response_to_json_and_status(raw_response: Any) -> Tuple[Dict[str, Any], int]:
    response = app.make_response(raw_response)
    status_code = getattr(response, "status_code", 200) or 200
    data: Optional[Dict[str, Any]] = None

    try:
        data = response.get_json(silent=True)
    except Exception:
        data = None

    if not isinstance(data, dict):
        text = response.get_data(as_text=True) if hasattr(response, "get_data") else ""
        try:
            parsed = json.loads(text) if text else {}
            data = parsed if isinstance(parsed, dict) else {"raw_response": parsed}
        except Exception:
            data = {"raw_response": text}

    return data, status_code


def _find_original_tts_view():
    for rule in list(app.url_map.iter_rules()):
        if rule.rule == "/tts" and "POST" in rule.methods:
            endpoint = rule.endpoint
            original = app.view_functions.get(endpoint)
            return endpoint, original
    return None, None


_ORIGINAL_TTS_ENDPOINT, _ORIGINAL_TTS_VIEW = _find_original_tts_view()


if _ORIGINAL_TTS_VIEW is not None:

    def v22_contract_tts_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        try:
            original_raw = _ORIGINAL_TTS_VIEW(*args, **kwargs)
            data, status_code = _response_to_json_and_status(original_raw)
            normalized = _normalize_tts_contract(data, status_code, payload)
            return jsonify(normalized), status_code
        except Exception as exc:
            # Return a clean, frontend-safe contract even when legacy /tts crashes.
            error_payload = {
                "success": False,
                "ok": False,
                "error": "tts_contract_wrapper_error",
                "message": "The backend failed while generating voice-over audio.",
                "detail": str(exc),
                "contract_version": V22_CONTRACT_VERSION,
                **_requested_from_payload(payload),
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
                "debug_trace": traceback.format_exc() if os.getenv("V22_DEBUG_TRACE") == "true" else None,
            }
            return jsonify(error_payload), 500

    # Replace only the existing /tts view function. All other routes remain unchanged.
    app.view_functions[_ORIGINAL_TTS_ENDPOINT] = v22_contract_tts_wrapper


@app.get("/v22-capabilities")
def v22_capabilities():
    """Small capabilities endpoint for the V22 frontend."""
    return jsonify(
        {
            "ok": True,
            "success": True,
            "contract_version": V22_CONTRACT_VERSION,
            "backend_wrapper": "contract_app.py",
            "legacy_app_imported": True,
            "tts_wrapped": _ORIGINAL_TTS_VIEW is not None,
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
                "basic": {"engine": "edge_tts", "speed_supported": False},
                "premium": {"engine": "gemini_tts", "fallback": "edge_tts", "speed_supported": False},
                "pro": {"engine": "gemini_tts", "fallback": "edge_tts", "speed_supported": False},
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
                "This wrapper normalizes /tts response metadata only.",
                "It does not change /extract-srt, /translate-srt, or /rewrite-options.",
                "Speed is not reported as applied unless the legacy backend explicitly confirms it.",
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
            "tts_wrapped": _ORIGINAL_TTS_VIEW is not None,
            "original_tts_endpoint": _ORIGINAL_TTS_ENDPOINT,
        }
    )
