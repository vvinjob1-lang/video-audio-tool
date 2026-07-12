"""Gemini-backed Style 1 / Style 2 rewrite engine for VoiceCraft Myanmar.

This module intentionally contains no Flask routes.  app.py owns HTTP handling,
file downloads and CORS, while this module owns prompt construction, Gemini API
calls, output cleaning, adaptive length rules and quality validation.
"""

from __future__ import annotations

import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests


ENGINE_VERSION = "v25.2-style12-gemini3-model-fallback"
GEMINI_API_ROOT = os.getenv(
    "GEMINI_API_ROOT",
    "https://generativelanguage.googleapis.com/v1beta",
).rstrip("/")

_SRT_TS_RE = re.compile(
    r"(?P<a>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<b>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})"
)
_SINGLE_TS_LINE_RE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{3}.*$"
)
_PROMPT_LABEL_RE = re.compile(
    r"^\s*(?:STYLE\s*[12]|BEGINNING|MIDDLE|ENDING|INTRO|OUTRO|"
    r"ORIGINAL\s+SOURCE\s+TEXT|ROUGH\s+MYANMAR\s+TRANSLATION|"
    r"INPUT\s+SUBTITLE\s+TEXT|FINAL\s+SCRIPT|REWRITTEN\s+SCRIPT|"
    r"MOVIE\s+RECAPS?|DOCUMENTARY|EMOTIONAL\s+STORYTELLING|"
    r"NARRATIVE\s+REPAIR)\s*[:：\-]?\s*",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]+)?|```")
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_CTA_PATTERNS = [
    re.compile(r"\b(?:subscribe|like and share|thanks for watching|turn on notifications?)\b.*", re.I),
    re.compile(r"စာရင်းသွင်း[^။.!?]*(?:[။.!?]|$)"),
    re.compile(r"ကြည့်ရှု[^။.!?]*ကျေးဇူးတင်[^။.!?]*(?:[။.!?]|$)"),
    re.compile(r"notification[^။.!?]*(?:[။.!?]|$)", re.I),
]
_LEAK_PHRASES = (
    "return only",
    "input subtitle text",
    "preserve names",
    "target output length",
    "this is segment",
    "critical rules",
    "you are a senior",
    "original source text",
    "rough myanmar translation",
)
_SERVICE_ERROR_PHRASES = (
    "error 500",
    "internal server error",
    "server error",
    "that’s an error",
    "that's an error",
    "please try again later",
    "failed to fetch",
    "bad gateway",
)


@dataclass(frozen=True)
class TargetProfile:
    mode: str
    min_ratio: float
    ideal_ratio: float
    max_ratio: float
    duration_seconds: float | None
    min_chars: int
    ideal_chars: int
    max_chars: int


def _timestamp_seconds(value: str) -> float:
    raw = value.replace(",", ".").strip()
    pieces = raw.split(":")
    if len(pieces) == 3:
        hours, minutes, seconds = pieces
    else:
        hours = "0"
        minutes, seconds = pieces
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def infer_srt_duration_seconds(raw_text: str) -> float | None:
    latest: float | None = None
    for match in _SRT_TS_RE.finditer(raw_text or ""):
        try:
            end = _timestamp_seconds(match.group("b"))
        except (TypeError, ValueError):
            continue
        latest = max(latest or 0.0, end)
    return latest


def clean_source_text(raw_text: str) -> str:
    """Convert SRT/VTT or rough text into a clean story source.

    This is only source preparation.  It is not used as a fake rewrite.
    """
    value = html.unescape(str(raw_text or ""))
    value = value.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    recent: list[str] = []
    for raw_line in value.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if line.isdigit():
            continue
        if _SINGLE_TS_LINE_RE.match(line) or "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\{\\[^}]+\}", " ", line)
        line = re.sub(r"^\s*[-–—>*#]+\s*", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        key = line.casefold()
        if key in recent[-5:]:
            continue
        recent.append(key)
        lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"\s+([၊။,.!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _balanced_source(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    # Preserve the actual beginning, middle and ending instead of taking only
    # the first N characters.  The prompt makes clear that omitted portions
    # must not be invented.
    begin_n = int(max_chars * 0.36)
    middle_n = int(max_chars * 0.28)
    end_n = max_chars - begin_n - middle_n
    midpoint = len(text) // 2
    middle_start = max(begin_n, midpoint - middle_n // 2)
    middle_end = min(len(text) - end_n, middle_start + middle_n)
    compact = (
        text[:begin_n].rstrip()
        + "\n\n[...source middle excerpt...]\n\n"
        + text[middle_start:middle_end].strip()
        + "\n\n[...source ending excerpt...]\n\n"
        + text[-end_n:].lstrip()
    )
    return compact, True


def _coerce_duration(payload: dict[str, Any], raw_text: str) -> float | None:
    for key in (
        "duration_seconds",
        "source_duration_seconds",
        "video_duration_seconds",
        "duration",
    ):
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return infer_srt_duration_seconds(raw_text)


def target_profile(source_chars: int, duration_seconds: float | None, payload: dict[str, Any]) -> TargetProfile:
    explicit = payload.get("target_length_ratio") or payload.get("targetLengthRatio")
    if explicit is not None:
        try:
            ideal = max(0.20, min(1.20, float(explicit)))
            low = max(0.16, ideal * 0.75)
            high = min(1.40, ideal * 1.30)
            mode = "custom_ratio"
        except (TypeError, ValueError):
            explicit = None
    if explicit is None:
        if duration_seconds is not None and duration_seconds < 120:
            mode, low, ideal, high = "full_narrative", 0.72, 1.00, 1.30
        elif duration_seconds is not None and duration_seconds <= 480:
            mode, low, ideal, high = "narrative_condensed", 0.45, 0.68, 0.95
        elif duration_seconds is not None and duration_seconds > 480:
            mode, low, ideal, high = "full_story_recap", 0.25, 0.43, 0.70
        elif source_chars <= 1800:
            mode, low, ideal, high = "full_narrative", 0.65, 0.95, 1.30
        elif source_chars <= 9000:
            mode, low, ideal, high = "narrative_condensed", 0.42, 0.66, 0.95
        else:
            mode, low, ideal, high = "full_story_recap", 0.25, 0.43, 0.72

    # Very small sources need a practical absolute floor, while very long
    # sources should not force unbounded output.
    min_chars = max(180 if source_chars < 600 else 420, int(source_chars * low))
    ideal_chars = max(350 if source_chars < 800 else 700, int(source_chars * ideal))
    # Character counts expand when English/romanized subtitles become natural
    # Myanmar.  Keep the semantic ratio target but avoid false rejection on
    # short clips merely because Myanmar Unicode uses more characters.
    practical_short_ceiling = 900 if source_chars < 800 else 0
    max_chars = max(min_chars + 160, int(source_chars * high), practical_short_ceiling)
    max_chars = min(max_chars, int(os.getenv("GEMINI_REWRITE_MAX_OUTPUT_CHARS", "18000")))
    ideal_chars = min(ideal_chars, max_chars)
    min_chars = min(min_chars, max_chars)
    return TargetProfile(
        mode=mode,
        min_ratio=low,
        ideal_ratio=ideal,
        max_ratio=high,
        duration_seconds=duration_seconds,
        min_chars=min_chars,
        ideal_chars=ideal_chars,
        max_chars=max_chars,
    )


def resolve_api_key(payload: dict[str, Any]) -> tuple[str, str]:
    mode = str(payload.get("api_mode") or payload.get("api_source") or "app").strip().lower()
    user_modes = {"user", "user_api", "own", "own_api", "my_gemini_api", "byok"}
    user_key = str(
        payload.get("gemini_api_key")
        or payload.get("user_api_key")
        or payload.get("api_key")
        or ""
    ).strip()
    if mode in user_modes:
        if not user_key:
            raise ValueError("My Gemini API mode was selected, but no Gemini API key was provided.")
        return user_key, "user_api"

    app_key = str(
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GEMINI_APP_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()
    if not app_key:
        raise RuntimeError("Gemini App API key is not configured on the backend.")
    return app_key, "app_api"


def _split_model_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _legacy_25_rewrite_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized in {
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    } or normalized.startswith("gemini-2.5-flash-preview") or normalized.startswith("gemini-2.5-pro-preview")


def select_models(payload: dict[str, Any]) -> tuple[list[str], str]:
    """Return an ordered, de-duplicated model fallback chain.

    Gemini 2.5 text models can return 404 for newly provisioned API projects even
    before the published global shutdown date.  V25.2 therefore defaults to the
    current Gemini 3 family and automatically skips legacy 2.5 rewrite model
    variables unless GEMINI_REWRITE_ALLOW_LEGACY_25=true is explicitly set.
    """
    tier = str(
        payload.get("output_quality")
        or payload.get("quality_tier")
        or payload.get("tier")
        or payload.get("quality")
        or "premium"
    ).strip().lower()
    aliases = {"default": "premium", "standard": "basic", "flash": "premium"}
    tier = aliases.get(tier, tier)
    if tier not in {"basic", "premium", "pro"}:
        tier = "premium"

    single_env = {
        "basic": "GEMINI_REWRITE_MODEL_BASIC",
        "premium": "GEMINI_REWRITE_MODEL_PREMIUM",
        "pro": "GEMINI_REWRITE_MODEL_PRO",
    }[tier]
    list_env = {
        "basic": "GEMINI_REWRITE_MODELS_BASIC",
        "premium": "GEMINI_REWRITE_MODELS_PREMIUM",
        "pro": "GEMINI_REWRITE_MODELS_PRO",
    }[tier]
    defaults = {
        "basic": [
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-flash-latest",
        ],
        "premium": [
            "gemini-3.5-flash",
            "gemini-flash-latest",
            "gemini-3.1-flash-lite",
        ],
        "pro": [
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash",
            "gemini-flash-latest",
        ],
    }[tier]

    explicit = str(
        payload.get("rewrite_model")
        or payload.get("gemini_rewrite_model")
        or ""
    ).strip()
    candidates = (
        ([explicit] if explicit else [])
        + _split_model_list(os.getenv(list_env, ""))
        + ([str(os.getenv(single_env, "")).strip()] if os.getenv(single_env) else [])
        + defaults
    )
    allow_legacy = os.getenv("GEMINI_REWRITE_ALLOW_LEGACY_25", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    result: list[str] = []
    for model in candidates:
        model = str(model or "").strip()
        if not model or model in result:
            continue
        if not allow_legacy and _legacy_25_rewrite_model(model):
            continue
        result.append(model)
    if not result:
        result = list(defaults)
    return result, tier


def select_model(payload: dict[str, Any]) -> tuple[str, str]:
    """Backward-compatible helper returning the primary candidate."""
    models, tier = select_models(payload)
    return models[0], tier


def _style_spec(style_id: str) -> dict[str, Any]:
    if style_id == "style2":
        return {
            "id": "style2",
            "title": "Style 2 — Emotional Storytelling",
            "short_title": "Emotional Storytelling",
            "temperature": float(os.getenv("GEMINI_STYLE2_TEMPERATURE", "0.72")),
            "rules": """
STYLE 2 — EMOTIONAL STORYTELLING
- Tell the story with cinematic emotion, suspense and human feeling.
- Let the listener feel the character's fear, hope, loss, conflict or surprise.
- Use natural Myanmar spoken rhythm, meaningful pauses and smooth transitions.
- Build tension gradually.  Do not exaggerate, become poetic for its own sake,
  or change facts merely to make the story dramatic.
- It must clearly sound different from Style 1: warmer, more immersive and
  emotionally driven, while still accurate and easy to understand.
""".strip(),
        }
    return {
        "id": "style1",
        "title": "Style 1 — Movie Recap / Documentary",
        "short_title": "Movie Recap / Documentary",
        "temperature": float(os.getenv("GEMINI_STYLE1_TEMPERATURE", "0.42")),
        "rules": """
STYLE 1 — MOVIE RECAP / DOCUMENTARY
- Write like a calm, mature and professional Myanmar recap narrator.
- Make events, motivations and cause/effect easy to follow.
- Use clear active sentences and smooth factual transitions.
- Keep the narration engaging, but do not over-dramatize or sound literary.
- It must clearly sound different from Style 2: steadier, clearer and more
  documentary-like, while still natural enough for voice-over.
""".strip(),
    }


def _source_zones(source: str) -> tuple[str, str, str]:
    if not source:
        return "", "", ""
    zone_len = min(1300, max(350, len(source) // 8))
    mid = len(source) // 2
    return (
        source[:zone_len].strip(),
        source[max(0, mid - zone_len // 2): mid + zone_len // 2].strip(),
        source[-zone_len:].strip(),
    )


def build_generation_prompt(source: str, profile: TargetProfile, style_id: str, source_was_compacted: bool) -> tuple[str, str]:
    style = _style_spec(style_id)
    beginning, middle, ending = _source_zones(source)
    system = """
You are a senior Myanmar narrative voice-over script writer.
Return only the finished Myanmar narration script.  Never return analysis,
headings, markdown, source labels, instructions, JSON, bullet points or notes.

Accuracy is mandatory:
- Preserve the source's actual facts, story order, motivations and outcome.
- Do not invent scenes, explanations, relationships, rescue, death, victory or
  a happy ending that the source does not contain.
- Do not omit the source ending when the source includes one.
- Translate non-Myanmar meaning naturally as part of the rewrite.
- Keep proper names when needed, but translate ordinary English words.

Voice-over quality is mandatory:
- Write natural Myanmar spoken prose in paragraphs, not subtitle fragments.
- Remove timestamps, numbering, repeated lines, music markers, channel CTA,
  subscribe/like/thanks phrases and machine/service error text.
- Use clear Myanmar punctuation and sentence rhythm suitable for TTS.
- Avoid repeated conclusions and duplicate sentences.
""".strip()

    compact_note = (
        "The source was exceptionally long and contains balanced beginning, middle and ending excerpts. "
        "Do not invent events from omitted portions; connect only facts supported by the supplied source."
        if source_was_compacted
        else "The full prepared source is supplied. Cover its beginning, middle and ending faithfully."
    )
    prompt = f"""
{style['rules']}

REWRITE MODE: {profile.mode}
TARGET LENGTH: approximately {profile.ideal_chars} Myanmar characters.
ACCEPTABLE RANGE: {profile.min_chars} to {profile.max_chars} characters.
{compact_note}

Coverage anchors (use them only to verify coverage; never print these labels):
Beginning excerpt:
{beginning}

Middle excerpt:
{middle}

Ending excerpt:
{ending}

Prepared source text:
{source}

Write the finished Myanmar narration now. Return the script only.
""".strip()
    return system, prompt


def _extract_gemini_text(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or "no_candidates"
        raise RuntimeError(f"Gemini returned no candidate: {reason}")
    candidate = candidates[0] or {}
    parts = ((candidate.get("content") or {}).get("parts") or [])
    text = "\n".join(str(part.get("text") or "") for part in parts if part.get("text")).strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty text (finishReason={candidate.get('finishReason')}).")
    return text, {
        "finish_reason": candidate.get("finishReason"),
        "usage": data.get("usageMetadata") or {},
        "safety_ratings": candidate.get("safetyRatings") or [],
    }


def call_gemini_text(
    *,
    api_key: str,
    models: list[str] | tuple[str, ...] | str,
    system: str,
    prompt: str,
    temperature: float,
) -> tuple[str, dict[str, Any]]:
    """Call Gemini with automatic current-model fallback.

    A 404/403/400 on one model advances to the next candidate. Transient
    429/5xx/network failures retry the current model first, then advance. A 401
    is treated as an invalid key and is not hidden by model fallback.
    """
    candidates = [models] if isinstance(models, str) else list(models)
    candidates = [str(model).strip() for model in candidates if str(model).strip()]
    if not candidates:
        raise RuntimeError("No Gemini rewrite model is configured.")

    max_output_tokens = int(os.getenv("GEMINI_REWRITE_MAX_OUTPUT_TOKENS", "8192"))
    timeout = int(os.getenv("GEMINI_REWRITE_TIMEOUT", "300"))
    retries = max(1, int(os.getenv("GEMINI_REWRITE_HTTP_ATTEMPTS", "2")))
    failures: list[str] = []

    for model_index, model in enumerate(candidates):
        # Google recommends the default temperature of 1.0 for Gemini 3.x.
        effective_temperature = 1.0 if model.startswith("gemini-3") else temperature
        request_json = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": effective_temperature,
                "topP": float(os.getenv("GEMINI_REWRITE_TOP_P", "0.90")),
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "text/plain",
            },
        }
        url = f"{GEMINI_API_ROOT}/models/{model}:generateContent"

        for attempt in range(1, retries + 1):
            try:
                response = requests.post(
                    url,
                    headers={
                        "x-goog-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json=request_json,
                    timeout=timeout,
                )
                if response.status_code >= 400:
                    try:
                        detail = response.json()
                        detail_message = ((detail.get("error") or {}).get("message") or "").strip()
                    except Exception:
                        detail_message = (response.text or "")[:500].strip()
                    message = f"{model} HTTP {response.status_code}: {detail_message or 'request failed'}"
                    if response.status_code == 401:
                        raise RuntimeError(message)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                        time.sleep(min(4, attempt * 2))
                        continue
                    failures.append(message)
                    break

                text, meta = _extract_gemini_text(response.json())
                meta.update({
                    "http_attempt": attempt,
                    "model": model,
                    "model_used": model,
                    "model_requested": candidates[0],
                    "model_fallback_used": model_index > 0,
                    "model_candidates": candidates,
                    "temperature_applied": effective_temperature,
                })
                return text, meta
            except requests.RequestException as exc:
                message = f"{model} network error: {exc}"
                if attempt < retries:
                    time.sleep(min(4, attempt * 2))
                    continue
                failures.append(message)
                break
            except RuntimeError as exc:
                # Authentication failures should be surfaced immediately.
                if "HTTP 401" in str(exc):
                    raise
                failures.append(str(exc))
                break

    summary = "; ".join(failures[-6:]) or "all model candidates failed"
    raise RuntimeError(f"All Gemini rewrite models failed: {summary}")


def sanitize_script(raw: str) -> str:
    value = html.unescape(str(raw or ""))
    value = _THINK_RE.sub("", value)
    value = _CODE_FENCE_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    for raw_line in value.split("\n"):
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        line = _PROMPT_LABEL_RE.sub("", line).strip()
        line = re.sub(r"^\s*[-*#>]+\s*", "", line).strip()
        if not line:
            continue
        if line.isdigit() or _SINGLE_TS_LINE_RE.match(line) or "-->" in line:
            continue
        for pattern in _CTA_PATTERNS:
            line = pattern.sub("", line).strip()
        if line:
            cleaned_lines.append(line)

    value = "\n".join(cleaned_lines)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s+([၊။,.!?])", r"\1", value)
    value = re.sub(r"([။!?])\s*", r"\1\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)

    # Remove exact and near-exact consecutive duplicate sentences/paragraphs.
    result: list[str] = []
    recent_keys: list[str] = []
    for unit in value.split("\n"):
        unit = unit.strip()
        if not unit:
            if result and result[-1] != "":
                result.append("")
            continue
        key = re.sub(r"[^\w\u1000-\u109f]+", "", unit.casefold())
        if key and key in recent_keys[-8:]:
            continue
        recent_keys.append(key)
        result.append(unit)
    value = "\n".join(result)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value


def _latin_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\u1000-\u109f]", text or "")
    if not letters:
        return 0.0
    latin = sum(1 for char in letters if "A" <= char <= "Z" or "a" <= char <= "z")
    return latin / len(letters)


def _duplicate_sentence_ratio(text: str) -> float:
    sentences = [
        re.sub(r"[^\w\u1000-\u109f]+", "", x.casefold())
        for x in re.split(r"[။.!?]+|\n+", text or "")
        if x.strip()
    ]
    sentences = [x for x in sentences if len(x) >= 10]
    if not sentences:
        return 0.0
    return 1.0 - (len(set(sentences)) / len(sentences))


def validate_script(script: str, source: str, profile: TargetProfile) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    lower = (script or "").lower()
    chars = len(script or "")
    if not script.strip():
        reasons.append("empty_output")
    if "-->" in script or _SRT_TS_RE.search(script):
        reasons.append("srt_artifacts")
    if any(phrase in lower for phrase in _LEAK_PHRASES):
        reasons.append("prompt_instruction_leakage")
    if any(phrase in lower for phrase in _SERVICE_ERROR_PHRASES):
        reasons.append("service_error_leakage")
    if script and not re.search(r"[\u1000-\u109f]", script):
        reasons.append("no_myanmar_text")
    latin = _latin_ratio(script)
    if chars > 400 and latin > float(os.getenv("GEMINI_REWRITE_MAX_LATIN_RATIO", "0.28")):
        reasons.append(f"too_much_english:{latin:.2f}")
    if chars < profile.min_chars:
        reasons.append(f"too_short:{chars}<{profile.min_chars}")
    if chars > profile.max_chars:
        reasons.append(f"too_long:{chars}>{profile.max_chars}")
    sentence_count = len([x for x in re.split(r"[။.!?]+|\n+", script) if x.strip()])
    paragraph_count = len([x for x in re.split(r"\n\s*\n", script) if x.strip()])
    if len(source) > 3000 and sentence_count < 7:
        reasons.append("too_few_sentences")
    duplicate_ratio = _duplicate_sentence_ratio(script)
    if sentence_count >= 8 and duplicate_ratio > float(os.getenv("GEMINI_REWRITE_MAX_DUPLICATE_RATIO", "0.22")):
        reasons.append(f"repetition:{duplicate_ratio:.2f}")
    metrics = {
        "input_chars": len(source),
        "output_chars": chars,
        "target_min_chars": profile.min_chars,
        "target_ideal_chars": profile.ideal_chars,
        "target_max_chars": profile.max_chars,
        "duration_seconds": profile.duration_seconds,
        "rewrite_mode": profile.mode,
        "sentence_count": sentence_count,
        "paragraph_count": paragraph_count,
        "latin_ratio": round(latin, 4),
        "duplicate_sentence_ratio": round(duplicate_ratio, 4),
    }
    return not reasons, reasons, metrics


def _repair_prompt(source: str, draft: str, profile: TargetProfile, style_id: str, reasons: list[str]) -> tuple[str, str]:
    style = _style_spec(style_id)
    system = """
You repair Myanmar narrative voice-over scripts. Return only the repaired final
Myanmar script. Keep every fact grounded in the source. Never invent an ending,
rescue, death, relationship, scene or explanation. Remove prompt labels,
subtitles, CTA, repetition and English instructions.
""".strip()
    prompt = f"""
{style['rules']}

The draft failed quality validation for: {', '.join(reasons)}.
Repair it using the source below.
- Preserve the real beginning, middle and ending in story order.
- Keep the style distinct and natural for TTS.
- Output {profile.min_chars} to {profile.max_chars} characters; aim for {profile.ideal_chars}.
- Do not mention the validation, source or repair process.

SOURCE:
{source}

DRAFT TO REPAIR:
{draft}

Return only the repaired Myanmar narration.
""".strip()
    return system, prompt


def generate_style(
    *,
    style_id: str,
    source: str,
    source_was_compacted: bool,
    profile: TargetProfile,
    api_key: str,
    models: list[str],
) -> dict[str, Any]:
    style = _style_spec(style_id)
    attempts: list[dict[str, Any]] = []
    try:
        system, prompt = build_generation_prompt(source, profile, style_id, source_was_compacted)
        raw, call_meta = call_gemini_text(
            api_key=api_key,
            models=models,
            system=system,
            prompt=prompt,
            temperature=style["temperature"],
        )
        model_used = str(call_meta.get("model_used") or models[0])
        script = sanitize_script(raw)
        safe, reasons, metrics = validate_script(script, source, profile)
        attempts.append({"type": "generation", "safe": safe, "reasons": reasons, **call_meta})
        source_name = "gemini_style_rewrite"
        repaired = False

        if not safe and os.getenv("GEMINI_REWRITE_ENABLE_REPAIR", "true").strip().lower() not in {"0", "false", "no", "off"}:
            repair_system, repair_prompt = _repair_prompt(source, script, profile, style_id, reasons)
            repaired_raw, repair_meta = call_gemini_text(
                api_key=api_key,
                models=[call_meta.get("model_used") or models[0]] + [m for m in models if m != (call_meta.get("model_used") or models[0])],
                system=repair_system,
                prompt=repair_prompt,
                temperature=max(0.25, style["temperature"] - 0.12),
            )
            repaired_script = sanitize_script(repaired_raw)
            repaired_safe, repaired_reasons, repaired_metrics = validate_script(repaired_script, source, profile)
            attempts.append({
                "type": "repair",
                "safe": repaired_safe,
                "reasons": repaired_reasons,
                **repair_meta,
            })
            if repaired_safe or (len(repaired_reasons) < len(reasons) and repaired_script):
                script, safe, reasons, metrics = repaired_script, repaired_safe, repaired_reasons, repaired_metrics
                model_used = str(repair_meta.get("model_used") or model_used)
                source_name = "gemini_style_rewrite_repaired"
                repaired = True

        return {
            "id": style["id"],
            "title": style["title"],
            "short_title": style["short_title"],
            "script": script,
            "text": script,
            "result": script,
            "source": source_name,
            "quality": "ai_rewrite" if safe else "needs_retry",
            "rewrite_quality": "ai_rewrite" if safe else "needs_retry",
            "tts_safe": safe,
            "needs_retry": not safe,
            "validation_reasons": reasons,
            "metrics": metrics,
            "model": model_used,
            "model_used": model_used,
            "model_requested": models[0],
            "model_candidates": models,
            "model_fallback_used": model_used != models[0],
            "repaired": repaired,
            "attempts": attempts,
        }
    except Exception as exc:
        return {
            "id": style["id"],
            "title": style["title"],
            "short_title": style["short_title"],
            "script": "",
            "text": "",
            "result": "",
            "source": "gemini_rewrite_error",
            "quality": "error",
            "rewrite_quality": "error",
            "tts_safe": False,
            "needs_retry": True,
            "validation_reasons": ["api_error"],
            "metrics": {
                "input_chars": len(source),
                "output_chars": 0,
                "target_min_chars": profile.min_chars,
                "target_ideal_chars": profile.ideal_chars,
                "target_max_chars": profile.max_chars,
                "duration_seconds": profile.duration_seconds,
                "rewrite_mode": profile.mode,
            },
            "model": models[0] if models else "",
            "model_used": "",
            "model_requested": models[0] if models else "",
            "model_candidates": models,
            "model_fallback_used": False,
            "repaired": False,
            "attempts": attempts,
            "error": str(exc),
        }


def generate_style_options(raw_text: str, payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = clean_source_text(raw_text)
    if not cleaned:
        raise ValueError("Missing usable text to rewrite.")
    duration = _coerce_duration(payload, raw_text)
    profile = target_profile(len(cleaned), duration, payload)
    max_input_chars = int(os.getenv("GEMINI_REWRITE_MAX_INPUT_CHARS", "90000"))
    prepared, compacted = _balanced_source(cleaned, max_input_chars)
    api_key, api_source = resolve_api_key(payload)
    models, tier = select_models(payload)

    # Intentionally make independent calls so Style 2 cannot become a renamed
    # copy of Style 1 and each style can be repaired independently. Run both
    # styles concurrently so the endpoint does not take twice as long.
    common = {
        "source": prepared,
        "source_was_compacted": compacted,
        "profile": profile,
        "api_key": api_key,
        "models": models,
    }
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="style12") as pool:
        future1 = pool.submit(generate_style, style_id="style1", **common)
        future2 = pool.submit(generate_style, style_id="style2", **common)
        style1 = future1.result()
        style2 = future2.result()
    options = [style1, style2]
    usable = [option for option in options if option.get("script")]
    safe = [option for option in options if option.get("tts_safe")]
    return {
        "engine_version": ENGINE_VERSION,
        "cleaned_text": cleaned,
        "prepared_source_chars": len(prepared),
        "source_compacted": compacted,
        "api_source": api_source,
        "output_quality": tier,
        "model": next((str(option.get("model_used") or "") for option in options if option.get("model_used")), models[0]),
        "model_requested": models[0],
        "model_candidates": models,
        "models_used": sorted({str(option.get("model_used")) for option in options if option.get("model_used")}),
        "target": {
            "mode": profile.mode,
            "duration_seconds": profile.duration_seconds,
            "min_chars": profile.min_chars,
            "ideal_chars": profile.ideal_chars,
            "max_chars": profile.max_chars,
        },
        "options": options,
        "usable_count": len(usable),
        "safe_count": len(safe),
        "all_styles_safe": len(safe) == 2,
        "any_style_safe": bool(safe),
    }


def generate_single_style(raw_text: str, payload: dict[str, Any], style_id: str) -> dict[str, Any]:
    cleaned = clean_source_text(raw_text)
    if not cleaned:
        raise ValueError("Missing usable text to rewrite.")
    duration = _coerce_duration(payload, raw_text)
    profile = target_profile(len(cleaned), duration, payload)
    max_input_chars = int(os.getenv("GEMINI_REWRITE_MAX_INPUT_CHARS", "90000"))
    prepared, compacted = _balanced_source(cleaned, max_input_chars)
    api_key, api_source = resolve_api_key(payload)
    models, tier = select_models(payload)
    option = generate_style(
        style_id=style_id,
        source=prepared,
        source_was_compacted=compacted,
        profile=profile,
        api_key=api_key,
        models=models,
    )
    return {
        "engine_version": ENGINE_VERSION,
        "cleaned_text": cleaned,
        "prepared_source_chars": len(prepared),
        "source_compacted": compacted,
        "api_source": api_source,
        "output_quality": tier,
        "model": option.get("model_used") or models[0],
        "model_requested": models[0],
        "model_candidates": models,
        "models_used": [option.get("model_used")] if option.get("model_used") else [],
        "target": {
            "mode": profile.mode,
            "duration_seconds": profile.duration_seconds,
            "min_chars": profile.min_chars,
            "ideal_chars": profile.ideal_chars,
            "max_chars": profile.max_chars,
        },
        "option": option,
    }
