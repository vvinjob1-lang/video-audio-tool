"""Drop-in Style 1 / Style 2 wrapper for an existing VoiceCraft backend.

Deployment does not require manually merging app.py.  Gunicorn imports this
module, which imports the existing app.py and replaces only POST /rewrite and
POST /rewrite-options at runtime.  All unrelated extraction, translation, TTS
and download routes remain owned by the existing backend.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from flask import jsonify, request, make_response

# Preserve the currently deployed backend wrapper when it exists.
# VoiceCraft deployments commonly start with contract_app:app, which adds
# CORS, requested-vs-actual TTS truth fields, Gemini/Edge routing, and speed
# metadata.  Importing app.py directly would bypass those production fixes.
try:
    import contract_app as legacy  # preferred production base
    BASE_APP_MODULE = "contract_app"
except ImportError:
    import app as legacy  # backward-compatible fallback
    BASE_APP_MODULE = "app"

from style12_rewrite_engine import (
    ENGINE_VERSION,
    generate_single_style,
    generate_style_options,
)


app = legacy.app


# Explicit browser CORS protection for the dynamically replaced rewrite routes.
# Some legacy deployments applied CORS as a decorator on the original view
# function. Replacing app.view_functions then removes that decorator even
# though Railway curl tests still pass. These hooks guarantee that browser
# OPTIONS preflight and the final POST response both carry CORS headers.
_STYLE12_CORS_PATHS = {
    "/rewrite",
    "/rewrite-options",
    "/rewrite-capabilities",
    "/style12-health",
}


def _configured_cors_origins() -> set[str]:
    raw = (
        os.getenv("STYLE12_CORS_ORIGINS")
        or os.getenv("CORS_ALLOWED_ORIGINS")
        or "https://v15r.vercel.app,http://localhost:5173,http://localhost:3000"
    )
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def _origin_is_allowed(origin: str) -> bool:
    if not origin:
        return False
    normalized = origin.strip().rstrip("/")
    allowed = _configured_cors_origins()
    if "*" in allowed or normalized in allowed:
        return True
    # Local development ports are allowed without requiring every port in env.
    return normalized.startswith("http://localhost:") or normalized.startswith("http://127.0.0.1:")


@app.before_request
def style12_explicit_preflight():
    if request.method == "OPTIONS" and request.path in _STYLE12_CORS_PATHS:
        return make_response("", 204)
    return None



def _json_error(message: str, status: int = 400, **extra: Any):
    helper = getattr(legacy, "json_error", None)
    if callable(helper):
        return helper(message, status, **extra)
    return jsonify({"ok": False, "success": False, "error": message, **extra}), status


def _save_text(script: str, base_name: str) -> tuple[str, str]:
    helper = getattr(legacy, "save_text_response", None)
    if callable(helper):
        return helper(script, base_name)

    # Conservative compatibility fallback for backend versions where the
    # helper was renamed.  It uses the existing SCRIPT_DIR when available.
    import uuid

    script_dir = Path(getattr(legacy, "SCRIPT_DIR", Path(__file__).resolve().parent / "scripts"))
    script_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{base_name}_{uuid.uuid4().hex[:8]}.txt"
    (script_dir / filename).write_text(script or "", encoding="utf-8")
    build_public_url = getattr(legacy, "build_public_url", None)
    url = build_public_url(f"/script/{filename}") if callable(build_public_url) else f"/script/{filename}"
    return filename, url


def _raw_text(payload: dict[str, Any]) -> str:
    return str(
        payload.get("text")
        or payload.get("srt_text")
        or payload.get("translated_srt_text")
        or payload.get("translated_text")
        or ""
    )


def style12_rewrite():
    try:
        payload = request.get_json(silent=True) or {}
        raw_text = _raw_text(payload)
        if not raw_text.strip():
            return _json_error("Missing text to rewrite", 400, version=ENGINE_VERSION)

        raw_style = str(payload.get("style") or "style1").strip().lower()
        style_id = "style2" if raw_style in {
            "style2", "style_2", "emotional", "emotional_tts",
            "emotional_storytelling", "storytelling", "dramatic",
        } else "style1"
        result = generate_single_style(raw_text, payload, style_id)
        option = result["option"]
        script = str(option.get("script") or "").strip()
        script_url = ""
        if script:
            filename, script_url = _save_text(script, style_id)
            option.update({"filename": filename, "script_url": script_url, "download_url": script_url})

        response = {
            "ok": bool(script),
            "success": bool(script),
            "version": ENGINE_VERSION,
            "script": script,
            "text": script,
            "result": script,
            "output": script,
            "rewrittenScript": script,
            "rewritten_script": script,
            "rewrittenText": script,
            "rewritten_text": script,
            "script_url": script_url,
            "download_url": script_url,
            "style": style_id,
            "style_title": option.get("title"),
            "source": option.get("source"),
            "quality": option.get("quality"),
            "rewrite_quality": option.get("rewrite_quality"),
            "tts_safe": bool(option.get("tts_safe")),
            "needs_retry": bool(option.get("needs_retry")),
            "validation_reasons": option.get("validation_reasons") or [],
            "rewrite": option,
            "provider": "gemini",
            "model": result.get("model"),
            "model_requested": result.get("model_requested"),
            "model_candidates": result.get("model_candidates") or [],
            "models_used": result.get("models_used") or [],
            "api_source": result.get("api_source"),
            "output_quality": result.get("output_quality"),
            "target": result.get("target"),
            "cleaned_text": result.get("cleaned_text"),
            "source_compacted": result.get("source_compacted"),
        }
        return jsonify(response), (200 if script else 502)
    except ValueError as exc:
        return _json_error(str(exc), 400, version=ENGINE_VERSION)
    except RuntimeError as exc:
        return _json_error(str(exc), 503, version=ENGINE_VERSION)
    except Exception as exc:
        print(f"style12 rewrite error: {exc}", flush=True)
        return _json_error("Gemini rewrite failed. Please retry.", 500, version=ENGINE_VERSION)


def style12_rewrite_options():
    try:
        payload = request.get_json(silent=True) or {}
        raw_text = _raw_text(payload)
        if not raw_text.strip():
            return _json_error("Missing text to rewrite", 400, version=ENGINE_VERSION)

        result = generate_style_options(raw_text, payload)
        options = result.get("options") or []
        for option in options:
            script = str(option.get("script") or "").strip()
            if not script:
                option.update({"script_url": "", "download_url": ""})
                continue
            filename, url = _save_text(script, option.get("id") or "rewrite")
            option.update({"filename": filename, "script_url": url, "download_url": url})

        style1 = next((x for x in options if x.get("id") == "style1"), options[0] if options else {})
        style2 = next((x for x in options if x.get("id") == "style2"), options[1] if len(options) > 1 else {})
        style1_script = str(style1.get("script") or "")
        style2_script = str(style2.get("script") or "")
        primary = style1_script or style2_script
        usable_count = sum(1 for option in options if str(option.get("script") or "").strip())
        safe_count = sum(1 for option in options if option.get("tts_safe"))

        response = {
            "ok": usable_count > 0,
            "success": usable_count > 0,
            "version": ENGINE_VERSION,
            "script": primary,
            "text": primary,
            "result": primary,
            "output": primary,
            "rewrittenScript": primary,
            "rewritten_script": primary,
            "rewrittenText": primary,
            "rewritten_text": primary,
            "style1_script": style1_script,
            "style1Script": style1_script,
            "style1_script_url": style1.get("script_url") or "",
            "style1_tts_safe": bool(style1.get("tts_safe")),
            "style2_script": style2_script,
            "style2Script": style2_script,
            "style2_script_url": style2.get("script_url") or "",
            "style2_tts_safe": bool(style2.get("tts_safe")),
            "naturalScript": style1_script,
            "natural_script": style1_script,
            "natural_script_url": style1.get("script_url") or "",
            "naturalScriptUrl": style1.get("script_url") or "",
            "emotionalScript": style2_script,
            "emotional_script": style2_script,
            "emotional_script_url": style2.get("script_url") or "",
            "emotionalScriptUrl": style2.get("script_url") or "",
            "options": options,
            "rewrites": {
                "style1": style1_script,
                "style2": style2_script,
                "movie_recap_documentary": style1_script,
                "emotional_storytelling": style2_script,
                "natural_accurate": style1_script,
                "emotional_tts": style2_script,
            },
            "quality": "ai_rewrite" if safe_count else "needs_retry",
            "rewrite_quality": "ai_rewrite" if safe_count else "needs_retry",
            "tts_safe": safe_count > 0,
            "needs_retry": safe_count == 0,
            "all_styles_safe": safe_count == 2,
            "any_style_safe": safe_count > 0,
            "safe_count": safe_count,
            "usable_count": usable_count,
            "ai_rewrite_configured": True,
            "provider": "gemini",
            "model": result.get("model"),
            "model_requested": result.get("model_requested"),
            "model_candidates": result.get("model_candidates") or [],
            "models_used": result.get("models_used") or [],
            "api_source": result.get("api_source"),
            "output_quality": result.get("output_quality"),
            "target": result.get("target"),
            "cleaned_text": result.get("cleaned_text"),
            "source_compacted": result.get("source_compacted"),
            "prepared_source_chars": result.get("prepared_source_chars"),
            "compatibility": "style12-gemini-production-contract",
        }
        return jsonify(response), (200 if usable_count > 0 else 502)
    except ValueError as exc:
        return _json_error(str(exc), 400, version=ENGINE_VERSION)
    except RuntimeError as exc:
        return _json_error(str(exc), 503, version=ENGINE_VERSION)
    except Exception as exc:
        print(f"style12 rewrite-options error: {exc}", flush=True)
        return _json_error("Gemini Style 1/2 rewrite failed. Please retry.", 500, version=ENGINE_VERSION)


def style12_capabilities():
    return jsonify({
        "ok": True,
        "version": ENGINE_VERSION,
        "provider": "gemini",
        "base_app_module": BASE_APP_MODULE,
        "contract_wrapper_preserved": BASE_APP_MODULE == "contract_app",
        "styles": [
            {"id": "style1", "title": "Style 1 — Movie Recap / Documentary"},
            {"id": "style2", "title": "Style 2 — Emotional Storytelling"},
        ],
        "tiers": {
            "basic": os.getenv("GEMINI_REWRITE_MODEL_BASIC", "gemini-3.1-flash-lite"),
            "premium": os.getenv("GEMINI_REWRITE_MODEL_PREMIUM", "gemini-3.5-flash"),
            "pro": os.getenv("GEMINI_REWRITE_MODEL_PRO", "gemini-3.1-pro-preview"),
        },
        "model_fallback_enabled": True,
        "legacy_2_5_rewrite_models_skipped": os.getenv("GEMINI_REWRITE_ALLOW_LEGACY_25", "false").strip().lower() not in {"1", "true", "yes", "on"},
        "recommended_models": {
            "basic": ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-flash-latest"],
            "premium": ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-flash-lite"],
            "pro": ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-flash-latest"],
        },
        "app_api_configured": bool(
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GEMINI_APP_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        ),
        "repair_enabled": os.getenv("GEMINI_REWRITE_ENABLE_REPAIR", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        "routes_replaced": ["POST /rewrite", "POST /rewrite-options"],
    })


def _replace_existing_route(path: str, view: Callable[..., Any], method: str = "POST") -> bool:
    for rule in app.url_map.iter_rules():
        if rule.rule == path and method in rule.methods:
            app.view_functions[rule.endpoint] = view
            return True
    return False


if not _replace_existing_route("/rewrite", style12_rewrite):
    app.add_url_rule("/rewrite", "style12_rewrite", style12_rewrite, methods=["POST"])
if not _replace_existing_route("/rewrite-options", style12_rewrite_options):
    app.add_url_rule(
        "/rewrite-options",
        "style12_rewrite_options",
        style12_rewrite_options,
        methods=["POST"],
    )

if not any(rule.rule == "/rewrite-capabilities" for rule in app.url_map.iter_rules()):
    app.add_url_rule(
        "/rewrite-capabilities",
        "style12_capabilities",
        style12_capabilities,
        methods=["GET"],
    )
if not any(rule.rule == "/style12-health" for rule in app.url_map.iter_rules()):
    app.add_url_rule(
        "/style12-health",
        "style12_health",
        style12_capabilities,
        methods=["GET"],
    )


@app.after_request
def add_style12_version_and_cors_headers(response):
    response.headers["X-Rewrite-Engine-Version"] = ENGINE_VERSION

    if request.path in _STYLE12_CORS_PATHS:
        origin = request.headers.get("Origin", "").strip()
        if _origin_is_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin.rstrip("/")
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "false"

        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With, X-Gemini-Key, X-API-Key"
        )
        response.headers["Access-Control-Expose-Headers"] = "X-Rewrite-Engine-Version"
        response.headers["Access-Control-Max-Age"] = "86400"

    return response
