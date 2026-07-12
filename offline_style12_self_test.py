"""Offline smoke tests for the Style 1 / Style 2 engine.

No network request is made and no real Gemini key is required.
Run with:
    python offline_style12_self_test.py
"""

from __future__ import annotations

import os

os.environ.setdefault("GEMINI_API_KEY", "offline-test-key")

import style12_rewrite_engine as engine


SOURCE = """1
00:00:00,000 --> 00:00:20,000
A poor family lives in a cramped basement and struggles every day.

2
00:00:20,000 --> 00:00:40,000
The son receives a chance to tutor at a wealthy family's home.

3
00:00:40,000 --> 00:01:10,000
He helps each member of his family enter the house with false identities.

4
00:01:10,000 --> 00:01:35,000
A hidden secret under the house changes everything and places both families in danger.
"""

STYLE1 = (
    "ဒီဇာတ်လမ်းမှာတော့ ဆင်းရဲနွမ်းပါးတဲ့ မိသားစုတစ်စုဟာ မြေအောက်ခန်းကျဉ်းလေးထဲမှာ နေထိုင်ရင်း ဘဝကို ရုန်းကန်နေရပါတယ်။ "
    "တစ်နေ့မှာတော့ သားဖြစ်သူက ချမ်းသာတဲ့ မိသားစုအိမ်မှာ စာသင်ပေးဖို့ အခွင့်အရေးတစ်ခု ရလာပါတယ်။ "
    "အစပိုင်းမှာ ဒီအလုပ်ဟာ သာမန်ဝင်ငွေရလမ်းတစ်ခုလိုပဲ ထင်ရပေမယ့် သူက မိသားစုဝင်တွေကို ကျွမ်းကျင်သူတွေလို ဟန်ဆောင်စေပြီး အိမ်ထဲကို တစ်ယောက်ပြီးတစ်ယောက် ဝင်ရောက်စေပါတယ်။ "
    "သူတို့ရဲ့ အစီအစဉ်က အောင်မြင်နေသလို မြင်ရချိန်မှာပဲ အိမ်အောက်မှာ ဖုံးကွယ်ထားတဲ့ လျှို့ဝှက်ချက်တစ်ခု ပေါ်လာပါတယ်။ "
    "အဲဒီအရာက မိသားစုနှစ်စုလုံးရဲ့ ဘဝကို မထင်မှတ်တဲ့ အန္တရာယ်ထဲ ဆွဲခေါ်သွားပါတယ်။"
)

STYLE2 = (
    "မြေအောက်ခန်းကျဉ်းလေးထဲမှာ မနက်ဖြန်အတွက် မျှော်လင့်ချက်တောင် မသေချာတဲ့ မိသားစုတစ်စု ရှိပါတယ်။ "
    "သူတို့ထဲက သားဖြစ်သူဆီကို ချမ်းသာတဲ့အိမ်တစ်အိမ်မှာ စာသင်ပေးရမယ့် အခွင့်အရေးတစ်ခု ရောက်လာတဲ့နေ့က ဘဝပြောင်းလဲမယ့် တံခါးတစ်ချပ် ဖွင့်လိုက်သလိုပါပဲ။ "
    "သူက ဒီအခွင့်အရေးကို တစ်ယောက်တည်း မယူဘဲ မိသားစုဝင်တိုင်းကို အိမ်ထဲဝင်လာနိုင်အောင် အယုံသွင်းပြီး လမ်းခင်းပေးပါတယ်။ "
    "သူတို့ဟာ အိပ်မက်နဲ့နီးလာပြီလို့ ထင်နေကြချိန်မှာ အိမ်အောက်က တိတ်ဆိတ်နေတဲ့ နေရာတစ်ခုက ကြောက်စရာလျှို့ဝှက်ချက်ကို ဖော်ထုတ်လိုက်ပါတယ်။ "
    "အဲဒီညကစပြီး သူတို့လိုချင်ခဲ့တဲ့ ဘဝသစ်ဟာ လွတ်မြောက်ရာလမ်းမဟုတ်တော့ဘဲ ပြန်လှည့်လို့မရတဲ့ အန္တရာယ်တစ်ခု ဖြစ်လာပါတယ်။"
)


def fake_call(**kwargs):
    prompt = kwargs.get("prompt", "")
    if "STYLE 2 — EMOTIONAL STORYTELLING" in prompt:
        return STYLE2, {"finish_reason": "STOP", "model": kwargs.get("model")}
    return STYLE1, {"finish_reason": "STOP", "model": kwargs.get("model")}


def main() -> None:
    original = engine.call_gemini_text
    engine.call_gemini_text = fake_call
    try:
        result = engine.generate_style_options(
            SOURCE,
            {"output_quality": "premium", "api_mode": "app"},
        )
    finally:
        engine.call_gemini_text = original

    assert result["safe_count"] == 2, result
    assert result["options"][0]["id"] == "style1"
    assert result["options"][1]["id"] == "style2"
    assert result["options"][0]["script"] != result["options"][1]["script"]
    assert result["target"]["mode"] == "full_narrative"
    assert result["options"][0]["tts_safe"] is True
    assert result["options"][1]["tts_safe"] is True
    print("STYLE 1 / STYLE 2 OFFLINE SELF-TEST: PASS")


if __name__ == "__main__":
    main()
