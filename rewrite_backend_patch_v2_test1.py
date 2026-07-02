from pathlib import Path
import re
from datetime import datetime

APP = Path('app.py')
REQ = Path('requirements.txt')

NEW_CALL = r'''
def clean_srt_to_text(text: str) -> str:
    raw = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    raw = re.sub(r'(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}', ' ', raw)
    out, seen = [], set()
    for line in raw.split('\n'):
        line = (line or '').strip()
        if not line:
            continue
        upper = line.upper()
        if upper in {'WEBVTT', 'STYLE', 'REGION'} or upper.startswith(('NOTE', 'KIND:', 'LANGUAGE:')):
            continue
        if re.fullmatch(r'\d+', line):
            continue
        if 'SRT_TIMESTAMP_RE' in globals() and SRT_TIMESTAMP_RE.match(line):
            continue
        line = re.sub(r'<[^>]+>', '', line)
        line = re.sub(r'\{[^}]+\}', '', line)
        line = re.sub(r'\s+', ' ', line).strip()
        if not line:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return '\n'.join(out).strip()


def _strip_ai_wrapping(text: str) -> str:
    text = (text or '').strip()
    text = re.sub(r'^```(?:[a-zA-Z0-9_-]+)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip().strip('"').strip()


def call_openrouter_rewrite(
    text: str = '',
    language: str = 'my',
    style: str = 'natural_myanmar_tts_from_original',
    original_text: str = '',
    translated_text: str = '',
    fallback_text: str = '',
) -> str:
    api_key = os.getenv('OPENROUTER_API_KEY')
    model = os.getenv('OPENROUTER_MODEL', 'openrouter/free')
    if not api_key:
        raise RuntimeError('OPENROUTER_API_KEY is missing')

    original = clean_srt_to_text(original_text)
    translated = clean_srt_to_text(translated_text)
    fallback = clean_srt_to_text(fallback_text or text)
    if not original and not translated and not fallback:
        raise ValueError('No readable subtitle text found after cleanup')
    if not original:
        original = fallback
    if not translated:
        translated = fallback

    system_prompt = """You are a professional English-to-Myanmar translator and Myanmar TTS script editor.
Use the original English text as the source of truth.
Use the Myanmar translation only as a rough reference.
Return only clean Myanmar text. No markdown, no JSON, no English notes, no SRT numbers, no timestamps.
For song lyrics and emotional dialogue, make the Myanmar sound soft, natural, emotional, culturally fitting, and easy to speak aloud.
Do not translate word-for-word. Do not add new facts.
Prefer natural Myanmar expressions such as ကိုယ်, မင်း, သူမ, အသည်း, လက်လွှတ်လိုက်ပါ, အဆင်ပြေသွားမှာပါ when context fits.
Keep sentences short and TTS-friendly."""

    reference_style = """Reference style from the user's test.mp4 and test1.mp4. Match this quality and tone:

TEST.MP4 style:
So, please, don't break my heart => ဒါကြောင့် ကျေးဇူးပြုပြီး ကိုယ့်အသည်းကို မခွဲပါနဲ့။
Don't tear me apart => ကိုယ့်ကို အပိုင်းအစတွေ ဖြစ်အောင် မလုပ်ပါနဲ့။
I know how it starts => အစက ဘယ်လိုစတတ်တယ်ဆိုတာ ကိုယ်သိပါတယ်။
Trust me, I've been broken before => ယုံပါ၊ ကိုယ်အရင်ကလည်း အသည်းကွဲဖူးပါတယ်။
Don't break me again => ကိုယ့်အသည်းကို ထပ်ပြီး မခွဲပါနဲ့နော်။
I am delicate => ကိုယ်က အသည်းနုသူမို့လို့။

TEST1.MP4 style:
I know you love her, but it's over, mate => မင်းသူမကို ချစ်နေမှန်း သိပေမယ့် အရာအားလုံး ပြီးသွားပြီလေ။
It doesn't matter, put the phone away => အရေးမကြီးတော့ပါဘူး။ ဖုန်းကိုချပြီး အဆက်အသွယ်ဖြတ်လိုက်ပါတော့။
It's never easy to walk away => ထွက်သွားဖို့ ဘယ်တော့မှ မလွယ်မှန်း သိပါတယ်။
Let her go => သူမကို လက်လွှတ်လိုက်ပါ။
It will be all right => အဆင်ပြေသွားမှာပါ။

Do not copy these examples unless the original text says the same thing.
Use them only as the target Myanmar fluency/style."""

    user_prompt = f"""Language: {language}
Style: {style}

Original English text:
{original or '(not provided)'}

Rough Myanmar translation:
{translated or '(not provided)'}

{reference_style}

Task:
Rewrite into natural Myanmar for TTS.
Preserve the original meaning, emotion, and tone.
Return only the final Myanmar script."""

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': os.getenv('APP_PUBLIC_URL', 'https://video-audio-tool-production.up.railway.app'),
        'X-Title': os.getenv('APP_NAME', 'Video2Audio Pro'),
    }
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': float(os.getenv('OPENROUTER_TEMPERATURE', '0.35')),
        'max_tokens': int(os.getenv('OPENROUTER_MAX_TOKENS', '1200')),
    }
    response = requests.post(OPENROUTER_CHAT_URL, headers=headers, json=payload, timeout=int(os.getenv('OPENROUTER_TIMEOUT', '60')))
    if not response.ok:
        raise RuntimeError(f'OpenRouter error {response.status_code}: {response.text[:1200]}')
    data = response.json()
    result = _strip_ai_wrapping(data.get('choices', [{}])[0].get('message', {}).get('content', ''))
    if not result:
        raise RuntimeError('OpenRouter returned empty result')
    if '-->' in result or 'WEBVTT' in result.upper():
        result = clean_srt_to_tts_script(result, language=language)
    if not result:
        raise RuntimeError('Rewrite result was empty after cleanup')
    return result
'''

NEW_ROUTE = r'''
@app.route('/rewrite', methods=['POST', 'OPTIONS'])
def rewrite_script():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True, 'success': True}), 200
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get('text') or payload.get('srt_text') or payload.get('translated_srt_text') or request.form.get('text') or ''
        original_text = payload.get('original_text') or payload.get('originalText') or payload.get('source_text') or ''
        translated_text = payload.get('translated_text') or payload.get('translatedText') or payload.get('translated_srt_text') or text
        language = payload.get('language') or payload.get('target_language') or request.form.get('language') or 'my'
        style = payload.get('style') or request.form.get('style') or 'natural_myanmar_tts_from_original'

        cleaned_original = clean_srt_to_text(str(original_text))
        cleaned_translated = clean_srt_to_text(str(translated_text))
        cleaned_fallback = clean_srt_to_text(str(text))
        if not cleaned_original and not cleaned_translated and not cleaned_fallback:
            return json_error('No readable subtitle text found after cleanup', 400)

        try:
            script = call_openrouter_rewrite(
                original_text=cleaned_original,
                translated_text=cleaned_translated,
                fallback_text=cleaned_fallback,
                language=language,
                style=style,
            )
        except Exception as ai_error:
            print(f'OpenRouter rewrite failed: {ai_error}', flush=True)
            return jsonify({'ok': False, 'success': False, 'error': str(ai_error), 'language': language, 'style': style, 'source': 'openrouter_free_ai_failed'}), 500

        return jsonify({
            'ok': True,
            'success': True,
            'rewritten_text': script,
            'rewrittenText': script,
            'rewritten_script': script,
            'rewrittenScript': script,
            'script': script,
            'text': script,
            'language': language,
            'style': style,
            'source': 'openrouter_free_ai',
        })
    except Exception as exc:
        print(f'rewrite error: {exc}', flush=True)
        return json_error(str(exc), 500)
'''

def replace_between(src, start_pat, end_pat, replacement, label):
    m = re.search(start_pat, src, flags=re.S)
    if not m:
        raise SystemExit(f'Could not find start for {label}')
    n = re.search(end_pat, src[m.start():], flags=re.S)
    if not n:
        raise SystemExit(f'Could not find end for {label}')
    end = m.start() + n.start()
    return src[:m.start()].rstrip() + '\n\n' + replacement.strip() + '\n\n' + src[end:].lstrip()

if not APP.exists():
    raise SystemExit('app.py not found. Put this file next to app.py in the backend repo.')
src = APP.read_text(encoding='utf-8', errors='replace')
backup = APP.with_name('app.py.backup_rewrite_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
backup.write_text(src, encoding='utf-8')

src = replace_between(src, r'def call_openrouter_rewrite\s*\(', r'\n@app\.get\(["\']/', NEW_CALL, 'call_openrouter_rewrite')
src = replace_between(src, r'@app\.route\(["\']/rewrite["\'].*?\ndef rewrite_script\s*\(', r'\n@app\.post\(["\']/upload["\']\)', NEW_ROUTE, 'rewrite_script')
compile(src, 'app.py', 'exec')
APP.write_text(src, encoding='utf-8')

if REQ.exists():
    req = REQ.read_text(encoding='utf-8', errors='replace')
    if not re.search(r'^\s*requests\b', req, flags=re.M):
        REQ.write_text(req.rstrip() + '\nrequests>=2.31.0\n', encoding='utf-8')

print('✅ Rewrite quality patch applied.')
print('Backup:', backup.name)
print('Commit app.py and requirements.txt, then Railway will redeploy.')
