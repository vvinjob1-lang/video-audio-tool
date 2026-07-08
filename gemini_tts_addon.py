# gemini_tts_addon.py
# V20.4 Gemini TTS add-on for Video2Audio Pro / AI Voice-over Script Generator.
#
# This module is intentionally self-contained:
# - It does not replace your existing /extract-srt, /translate-srt, /rewrite-options, or Edge TTS logic.
# - It only handles /tts requests when engine is gemini_tts_app or gemini_tts_user_key.
# - If engine is anything else, it returns None so your existing /tts code continues normally.

import base64
import json
import math
import mimetypes
import os
import re
import subprocess
import time
import uuid
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from flask import Response, jsonify, request, send_from_directory


GEMINI_ADDON_VERSION = "v20.4-gemini-tts-direct-script-addon"

# Gemini REST TTS endpoint.
GEMINI_REST_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Gemini TTS currently returns raw PCM bytes in examples:
# 24kHz, 1 channel, signed 16-bit little-endian PCM.
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2

OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", "outputs")).resolve()
GEMINI_AUDIO_DIR = OUTPUT_ROOT / "gemini_audio"
GEMINI_SRT_DIR = OUTPUT_ROOT / "gemini_srt"
GEMINI_SCRIPT_DIR = OUTPUT_ROOT / "gemini_script"

for _d in (GEMINI_AUDIO_DIR, GEMINI_SRT_DIR, GEMINI_SCRIPT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _json_error(message: str, status: int = 400, **extra):
    payload = {
        "success": False,
        "ok": False,
        "version": GEMINI_ADDON_VERSION,
        "engine": extra.pop("engine", "gemini_tts"),
        "error": message,
        **extra,
    }
    return jsonify(payload), status


def _base_url() -> str:
    # Works behind Railway/proxies well enough for public URLs.
    return request.host_url.rstrip("/")


def _is_gemini_engine(engine: str) -> bool:
    return str(engine or "").strip() in {"gemini_tts_app", "gemini_tts_user_key"}


def _get_api_key(engine: str, data: Dict) -> Tuple[Optional[str], str]:
    if engine == "gemini_tts_user_key":
        key = str(data.get("user_gemini_api_key") or "").strip()
        return (key or None), "user_key"

    # App/shared key. Keep this only in Railway Variables.
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"):
        key = os.getenv(name)
        if key:
            return key.strip(), name
    return None, "missing"


def _clean_script_for_tts(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = text.replace("```", "")
    text = re.sub(r"^\s*(STYLE\s*1|STYLE\s*2)\s*[—:-].*$", "", text, flags=re.I | re.M)
    text = re.sub(r"^\s*(Movie\s*Recap\s*/?\s*Documentary|Emotional\s*Storytelling)\s*[:：-]?\s*$", "", text, flags=re.I | re.M)
    # Remove common CTA/channel text.
    cta_patterns = [
        r"\bsubscribe\b.*",
        r"\blike and share\b.*",
        r"\bthanks for watching\b.*",
        r"\bnotification\b.*",
        r"စာရင်းသွင်း.*",
        r"ကြည့်ရှု.*ကျေးဇူးတင်.*",
    ]
    for p in cta_patterns:
        text = re.sub(p, "", text, flags=re.I)
    # Normalize blank lines and spacing.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _hard_tts_errors(text: str) -> List[str]:
    problems = []
    if not text.strip():
        problems.append("empty_script")
    if len(text.strip()) < 20:
        problems.append("script_under_20_chars")
    if re.search(r"\d{2}:\d{2}:\d{2}[,.]\d{2,3}", text):
        problems.append("srt_timestamp_detected")
    if "-->" in text:
        problems.append("srt_arrow_detected")
    if re.search(r"\bWEBVTT\b", text, flags=re.I):
        problems.append("webvtt_detected")
    bad_phrases = [
        "Error 500",
        "Server Error",
        "That’s an error",
        "That's an error",
        "ORIGINAL SOURCE TEXT:",
        "ROUGH MYANMAR TRANSLATION:",
        "IMPORTANT:",
    ]
    for phrase in bad_phrases:
        if phrase.lower() in text.lower():
            problems.append(f"bad_text:{phrase}")
    return problems


def _prompt_for_style(script: str, style: str, tone: str, pace: str) -> str:
    style = str(style or "Movie Recap / Documentary")
    tone = str(tone or "documentary")
    pace = str(pace or "natural_narration")

    tone_map = {
        "documentary": "calm professional documentary narrator",
        "emotional": "warm emotional storytelling narrator",
        "cinematic": "cinematic suspenseful Myanmar narrator",
    }
    pace_map = {
        "slow_cinematic": "slow cinematic pace with natural pauses",
        "natural_narration": "natural narration pace",
        "fast_recap": "slightly energetic recap pace, still clear",
    }

    tone_text = tone_map.get(tone, tone)
    pace_text = pace_map.get(pace, pace)

    # Clear preamble is important: Google docs warn vague prompts may be rejected
    # or model may read style notes aloud.
    return (
        "Synthesize speech only. Do not read these director notes aloud.\n"
        "Language: Burmese / Myanmar (my).\n"
        f"Voice direction: {tone_text}.\n"
        f"Pace direction: {pace_text}.\n"
        f"Script style: {style}.\n"
        "Pronounce the following as natural Myanmar narration. "
        "Use the punctuation and paragraph breaks as pauses. "
        "Do not translate, summarize, add commentary, or add extra words.\n\n"
        "SPOKEN TRANSCRIPT START\n"
        f"{script.strip()}\n"
        "SPOKEN TRANSCRIPT END"
    )


def _split_text_for_gemini(text: str, max_chars: int = 2200) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    # Prefer paragraph boundaries, then sentence boundaries.
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for para in paras:
        if len(para) > max_chars:
            flush()
            # Split long paragraph by Myanmar full stop or common punctuation.
            sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s+", para) if s.strip()]
            sub = ""
            for sent in sentences:
                if len(sub) + len(sent) + 1 > max_chars:
                    if sub.strip():
                        chunks.append(sub.strip())
                    sub = sent
                else:
                    sub = (sub + " " + sent).strip()
            if sub.strip():
                chunks.append(sub.strip())
            continue

        if len(current) + len(para) + 2 > max_chars:
            flush()
            current = para
        else:
            current = (current + "\n\n" + para).strip() if current else para

    flush()
    return chunks or [text]


def _gemini_voice_name(data: Dict) -> str:
    # Gemini voice names are not male/female-specific. These are stable prebuilt names.
    # Use env override if the user wants to test a preferred voice.
    explicit = str(data.get("gemini_voice_name") or data.get("voice_name") or "").strip()
    if explicit:
        return explicit

    gender = str(data.get("gender") or "").lower()
    tone = str(data.get("tone") or "").lower()

    if os.getenv("GEMINI_TTS_VOICE_NAME"):
        return os.getenv("GEMINI_TTS_VOICE_NAME").strip()

    # Sensible defaults from Google voice list.
    if "emotional" in tone or "cinematic" in tone:
        return "Sulafat"  # warm
    if gender == "female":
        return "Kore"     # firm/clear
    if gender == "male":
        return "Charon"   # informative
    return "Kore"


def _extract_inline_audio_data(resp_json: Dict) -> Optional[bytes]:
    try:
        parts = resp_json["candidates"][0]["content"]["parts"]
    except Exception:
        return None

    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            try:
                return base64.b64decode(inline["data"])
            except Exception:
                return None
    return None


def _call_gemini_tts_once(
    api_key: str,
    model: str,
    prompt: str,
    voice_name: str,
    timeout: int = 120,
) -> Tuple[Optional[bytes], Optional[str], Optional[Dict]]:
    url = GEMINI_REST_URL_TEMPLATE.format(model=model)
    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice_name
                    }
                }
            }
        },
        "model": model,
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        return None, f"request_exception:{type(e).__name__}:{str(e)}", None

    try:
        resp_json = resp.json()
    except Exception:
        resp_json = {"raw_text": resp.text[:1000]}

    if resp.status_code >= 400:
        message = resp_json.get("error", {}).get("message") if isinstance(resp_json, dict) else None
        return None, f"gemini_http_{resp.status_code}:{message or resp.text[:300]}", resp_json

    audio = _extract_inline_audio_data(resp_json)
    if not audio:
        return None, "no_audio_inline_data_returned", resp_json

    return audio, None, resp_json


def _write_pcm_to_wav(pcm_bytes: bytes, wav_path: Path) -> float:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH)
        wf.setframerate(PCM_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)

    frames = len(pcm_bytes) / (PCM_CHANNELS * PCM_SAMPLE_WIDTH)
    return frames / PCM_SAMPLE_RATE


def _try_convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", "128k", str(mp3_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        return result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0
    except Exception:
        return False


def _format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    s = total % 60
    m = (total // 60) % 60
    h = total // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_script_to_srt_blocks(script: str, max_chars: int = 115) -> List[str]:
    script = re.sub(r"\n{2,}", "\n", script.strip())
    sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s+|\n+", script) if s.strip()]
    blocks = []
    cur = ""
    for s in sentences:
        if len(cur) + len(s) + 1 > max_chars and cur:
            blocks.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s).strip() if cur else s
    if cur:
        blocks.append(cur.strip())
    return blocks or [script.strip()]


def _make_srt(script: str, duration: float) -> str:
    blocks = _split_script_to_srt_blocks(script)
    weights = [max(1, len(b)) for b in blocks]
    total_w = sum(weights)
    t = 0.0
    lines = []
    for i, (block, w) in enumerate(zip(blocks, weights), start=1):
        seg_dur = max(1.2, duration * (w / total_w))
        start = t
        end = min(duration, t + seg_dur)
        if i == len(blocks):
            end = duration
        lines.append(str(i))
        lines.append(f"{_format_srt_time(start)} --> {_format_srt_time(end)}")
        lines.append(block)
        lines.append("")
        t = end
    return "\n".join(lines).strip() + "\n"


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def register_gemini_tts_routes(app):
    """Register static file routes and debug route. Safe to call more than once."""
    if getattr(app, "_gemini_tts_routes_registered", False):
        return app

    @app.route("/gemini-audio/<path:filename>", methods=["GET"])
    def gemini_audio_file(filename):
        return send_from_directory(GEMINI_AUDIO_DIR, filename, as_attachment=False)

    @app.route("/gemini-srt/<path:filename>", methods=["GET"])
    def gemini_srt_file(filename):
        return send_from_directory(GEMINI_SRT_DIR, filename, as_attachment=True)

    @app.route("/gemini-script/<path:filename>", methods=["GET"])
    def gemini_script_file(filename):
        return send_from_directory(GEMINI_SCRIPT_DIR, filename, as_attachment=True)

    @app.route("/debug-gemini-tts-health", methods=["GET"])
    def debug_gemini_tts_health():
        key_configured = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_AI_API_KEY"))
        return jsonify({
            "ok": True,
            "success": True,
            "version": GEMINI_ADDON_VERSION,
            "gemini_tts": {
                "enabled": os.getenv("GEMINI_TTS_ENABLED", "true").lower() != "false",
                "app_key_configured": key_configured,
                "model": os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"),
                "fallback_model": os.getenv("GEMINI_TTS_FALLBACK_MODEL", "gemini-2.5-flash-preview-tts"),
                "max_chunk_chars": int(os.getenv("GEMINI_TTS_MAX_CHARS", "2200")),
                "output_root": str(OUTPUT_ROOT),
            }
        })

    app._gemini_tts_routes_registered = True
    return app


def try_handle_gemini_tts():
    """Return a Flask response for Gemini engines, or None for non-Gemini engines.

    Insert this at the top of your existing /tts route:
        gemini_response = try_handle_gemini_tts()
        if gemini_response is not None:
            return gemini_response
    """
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}

    engine = str(data.get("engine") or "").strip()
    if not _is_gemini_engine(engine):
        return None

    if os.getenv("GEMINI_TTS_ENABLED", "true").lower() == "false":
        return _json_error("Gemini TTS is disabled by GEMINI_TTS_ENABLED=false.", 403, engine=engine)

    raw_text = str(data.get("text") or "").strip()
    script = _clean_script_for_tts(raw_text)
    problems = _hard_tts_errors(script)
    if problems:
        return _json_error(
            "Direct Script is not ready for Gemini TTS.",
            400,
            engine=engine,
            problems=problems,
            fallback_available=True,
        )

    api_key, key_source = _get_api_key(engine, data)
    if not api_key:
        return _json_error(
            "Gemini API key is missing. Add GEMINI_API_KEY in Railway Variables or use engine gemini_tts_user_key with user_gemini_api_key.",
            401,
            engine=engine,
            fallback_available=True,
        )

    model = str(data.get("gemini_model") or os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")).strip()
    fallback_model = str(os.getenv("GEMINI_TTS_FALLBACK_MODEL", "gemini-2.5-flash-preview-tts")).strip()
    max_chars = int(data.get("max_chunk_chars") or os.getenv("GEMINI_TTS_MAX_CHARS", "2200"))
    timeout = int(os.getenv("GEMINI_TTS_TIMEOUT_SECONDS", "120"))

    voice_style = data.get("voice_style") or data.get("style") or "Movie Recap / Documentary"
    tone = data.get("tone") or "documentary"
    pace = data.get("pace") or "natural_narration"
    voice_name = _gemini_voice_name(data)

    chunks = _split_text_for_gemini(script, max_chars=max_chars)
    pcm_parts: List[bytes] = []
    chunk_reports = []

    start_time = time.time()

    for idx, chunk in enumerate(chunks, start=1):
        prompt = _prompt_for_style(chunk, voice_style, tone, pace)

        audio = None
        err = None
        used_model = model
        attempts = []

        # Attempt primary model twice.
        for attempt in (1, 2):
            audio, err, raw = _call_gemini_tts_once(api_key, model, prompt, voice_name, timeout=timeout)
            attempts.append({"model": model, "attempt": attempt, "ok": bool(audio), "error": err})
            if audio:
                break
            time.sleep(1.0)

        # Optional fallback model once if configured and different.
        if not audio and fallback_model and fallback_model != model:
            used_model = fallback_model
            audio, err, raw = _call_gemini_tts_once(api_key, fallback_model, prompt, voice_name, timeout=timeout)
            attempts.append({"model": fallback_model, "attempt": 1, "ok": bool(audio), "error": err})

        if not audio:
            return _json_error(
                f"Gemini TTS failed on chunk {idx} of {len(chunks)}.",
                502,
                engine=engine,
                chunk_index=idx,
                chunk_count=len(chunks),
                attempts=attempts,
                fallback_available=True,
                fallback_engine="edge_tts",
            )

        pcm_parts.append(audio)
        chunk_reports.append({
            "chunk": idx,
            "chars": len(chunk),
            "model": used_model,
            "attempts": attempts,
            "bytes": len(audio),
        })

    combined_pcm = b"".join(pcm_parts)
    job_id = f"gemini_tts_{uuid.uuid4().hex[:10]}"
    wav_path = GEMINI_AUDIO_DIR / f"{job_id}.wav"
    mp3_path = GEMINI_AUDIO_DIR / f"{job_id}.mp3"
    srt_path = GEMINI_SRT_DIR / f"{job_id}.srt"
    script_path = GEMINI_SCRIPT_DIR / f"{job_id}.txt"

    duration = _write_pcm_to_wav(combined_pcm, wav_path)
    mp3_ok = _try_convert_wav_to_mp3(wav_path, mp3_path)
    audio_filename = mp3_path.name if mp3_ok else wav_path.name
    audio_path = mp3_path if mp3_ok else wav_path
    audio_format = "mp3" if mp3_ok else "wav"

    srt_text = _make_srt(script, duration)
    _write_text_file(srt_path, srt_text)
    _write_text_file(script_path, script)

    elapsed = round(time.time() - start_time, 2)
    base = _base_url()

    return jsonify({
        "success": True,
        "ok": True,
        "version": GEMINI_ADDON_VERSION,
        "engine": engine,
        "source": "gemini_tts",
        "model": model,
        "fallback_model": fallback_model,
        "voice": voice_name,
        "voice_name": voice_name,
        "tone": tone,
        "pace": pace,
        "voice_style": voice_style,
        "audio_format": audio_format,
        "audio_duration_seconds": round(duration, 3),
        "duration_seconds": round(duration, 3),
        "chunk_count": len(chunks),
        "estimated_requests_used": len(chunks),
        "processing_seconds": elapsed,
        "key_source": key_source,
        "audio_url": f"{base}/gemini-audio/{audio_filename}",
        "download_url": f"{base}/gemini-audio/{audio_filename}",
        "final_srt_url": f"{base}/gemini-srt/{srt_path.name}",
        "srt_url": f"{base}/gemini-srt/{srt_path.name}",
        "final_script_url": f"{base}/gemini-script/{script_path.name}",
        "script_url": f"{base}/gemini-script/{script_path.name}",
        "final_srt_text": srt_text,
        "final_script_text": script,
        "debug": {
            "chunks": chunk_reports,
            "pcm_bytes": len(combined_pcm),
            "audio_file": str(audio_path),
        },
        "fallback_available": True,
        "fallback_engine": "edge_tts",
    })
