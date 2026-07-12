from __future__ import annotations

import os
from typing import Any

import style12_rewrite_engine as engine


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


calls: list[str] = []


def fake_post(url: str, **_: Any) -> FakeResponse:
    calls.append(url)
    if "gemini-2.5-flash" in url:
        return FakeResponse(
            404,
            {"error": {"message": "This model is no longer available to new users."}},
        )
    return FakeResponse(
        200,
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "ဒီဇာတ်လမ်းမှာတော့ စမ်းသပ်မှု အောင်မြင်သွားပါတယ်။"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        },
    )


old_post = engine.requests.post
old_env = os.environ.get("GEMINI_REWRITE_MODEL_PREMIUM")
try:
    os.environ["GEMINI_REWRITE_MODEL_PREMIUM"] = "gemini-2.5-flash"
    models, tier = engine.select_models({"output_quality": "premium"})
    assert tier == "premium"
    assert models[0] == "gemini-3.5-flash", models
    assert "gemini-2.5-flash" not in models, models

    engine.requests.post = fake_post
    text, meta = engine.call_gemini_text(
        api_key="fake-key",
        models=["gemini-2.5-flash", "gemini-3.5-flash"],
        system="Return Myanmar only.",
        prompt="Test",
        temperature=0.4,
    )
    assert text
    assert meta["model_used"] == "gemini-3.5-flash", meta
    assert meta["model_fallback_used"] is True, meta
    assert meta["temperature_applied"] == 1.0, meta
    print("V25.2 model selection and fallback test: PASS")
finally:
    engine.requests.post = old_post
    if old_env is None:
        os.environ.pop("GEMINI_REWRITE_MODEL_PREMIUM", None)
    else:
        os.environ["GEMINI_REWRITE_MODEL_PREMIUM"] = old_env
