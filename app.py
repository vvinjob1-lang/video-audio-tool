from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import subprocess
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

# ===== FOLDERS =====
os.makedirs('downloads', exist_ok=True)
os.makedirs('uploads', exist_ok=True)
os.makedirs('srt', exist_ok=True)

# ===== ALLOWED EXTENSIONS =====
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mp3', 'wav', 'm4a', 'webm', 'mkv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ===== LAZY LOADING (Model တွေကို လိုမှသာ Load လုပ်မယ်) =====
_whisper_model = None
_tts_model = None
_tts_tokenizer = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        print("🔄 Loading Whisper model...")
        _whisper_model = whisper.load_model("base")
        print("✅ Whisper model loaded!")
    return _whisper_model

def get_tts():
    global _tts_model, _tts_tokenizer
    if _tts_model is None:
        import torch
        from transformers import VitsModel, AutoTokenizer
        print("🔄 Loading TTS model...")
        _tts_model = VitsModel.from_pretrained("facebook/mms-tts-mya")
        _tts_tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-mya")
        print("✅ TTS model loaded!")
    return _tts_model, _tts_tokenizer

# ==========================================
# DOWNLOAD VIDEO FROM URL
# ==========================================
def download_video(url):
    ydl_opts = {
        'format': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        'outtmpl': 'downloads/video.%(ext)s',
        'quiet': True,
        'no_check_certificate': True,
        'ignoreerrors': True,
        'geo_bypass': True,
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    return 'downloads/video.mp4'

# ==========================================
# EXTRACT AUDIO
# ==========================================
def extract_audio(video_path):
    audio_path = 'downloads/audio.wav'
    cmd = [
        'ffmpeg', '-i', video_path,
        '-q:a', '0', '-map', 'a',
        '-ac', '1', '-ar', '16000',
        audio_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return audio_path

# ==========================================
# VIDEO → SRT
# ==========================================
def video_to_srt(video_path):
    audio_path = extract_audio(video_path)
    model = get_whisper()
    result = model.transcribe(audio_path, task="translate", verbose=False)
    
    srt_path = 'srt/output.srt'
    with open(srt_path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(result['segments']):
            start = seg['start']
            end = seg['end']
            text = seg['text'].strip()
            f.write(f"{i+1}\n")
            f.write(f"{int(start//3600):02d}:{int((start%3600)//60):02d}:{int(start%60):02d},{int((start%1)*1000):03d} --> ")
            f.write(f"{int(end//3600):02d}:{int((end%3600)//60):02d}:{int(end%60):02d},{int((end%1)*1000):03d}\n")
            f.write(f"{text}\n\n")
    return srt_path

# ==========================================
# FALLBACK: Audio → Text
# ==========================================
def audio_to_text(video_path):
    audio_path = extract_audio(video_path)
    model = get_whisper()
    result = model.transcribe(audio_path, task="translate", verbose=False)
    
    text_path = 'srt/fallback_text.txt'
    with open(text_path, 'w', encoding='utf-8') as f:
        f.write(result['text'])
    return text_path

# ==========================================
# TRANSLATE
# ==========================================
def translate_content(file_path, target_lang):
    from googletrans import Translator
    translator = Translator()
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    translated = translator.translate(content, dest=target_lang).text
    ext = 'srt' if file_path.endswith('.srt') else 'txt'
    translated_path = f'srt/translated.{ext}'
    
    with open(translated_path, 'w', encoding='utf-8') as f:
        f.write(translated)
    return translated_path

# ==========================================
# REWRITE SCRIPT
# ==========================================
def rewrite_script(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if file_path.endswith('.srt'):
        lines = [line for line in content.split('\n') if line.strip() and not line[0].isdigit() and '-->' not in line]
        content = ' '.join(lines)
    
    try:
        import ollama
        response = ollama.chat(model='llama3', messages=[{
            'role': 'user',
            'content': f'Rewrite this text to be more concise and engaging: {content[:1500]}'
        }])
        rewritten = response['message']['content']
    except:
        # Ollama မရှိရင် မူရင်းအတိုင်းထားမယ်
        rewritten = content
    
    rewritten_path = 'srt/rewritten_script.txt'
    with open(rewritten_path, 'w', encoding='utf-8') as f:
        f.write(rewritten)
    return rewritten_path

# ==========================================
# TTS (Myanmar)
# ==========================================
def generate_tts(text_file):
    import torch
    import scipy.io.wavfile
    
    with open(text_file, 'r', encoding='utf-8') as f:
        text = f.read()
    
    if len(text) > 200:
        text = text[:200]
    
    model, tokenizer = get_tts()
    inputs = tokenizer(text, return_tensors="pt")
    
    with torch.no_grad():
        output = model(**inputs).waveform
    
    filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
    filepath = os.path.join('downloads', filename)
    scipy.io.wavfile.write(filepath, rate=model.config.sampling_rate, data=output)
    
    return filepath, filename

# ==========================================
# ENDPOINTS
# ==========================================

@app.route('/')
def home():
    return jsonify({'message': 'Video-to-Audio API is running!'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})

@app.route('/download', methods=['POST'])
def download_audio():
    try:
        data = request.get_json()
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }],
            'outtmpl': 'downloads/audio.%(ext)s',
            'quiet': True,
            'no_check_certificate': True,
            'ignoreerrors': True,
            'geo_bypass': True,
            'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'sleep_interval': 5,
            'max_sleep_interval': 10,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        
        audio_file = None
        for f in os.listdir('downloads'):
            if f.endswith('.mp3'):
                audio_file = f
                break
        
        if not audio_file:
            return jsonify({'error': 'Audio ဖိုင် မတွေ့ဘူး'}), 500
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{audio_file}',
            'message': 'Audio ဆွဲချပြီးပါပြီ'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/process', methods=['POST'])
def process_video():
    try:
        data = request.get_json()
        url = data.get('url')
        target_lang = data.get('target_language', 'my')
        
        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400
        
        print(f"Processing: {url}")
        
        video_path = download_video(url)
        print("✅ Video downloaded")
        
        try:
            srt_path = video_to_srt(video_path)
            content_path = srt_path
            srt_used = True
            print("✅ SRT extracted")
        except Exception as e:
            print(f"⚠️ SRT failed: {e}, using fallback")
            content_path = audio_to_text(video_path)
            srt_used = False
            print("✅ Audio → Text complete")
        
        translated_path = translate_content(content_path, target_lang)
        print("✅ Translation complete")
        
        rewritten_path = rewrite_script(translated_path)
        print("✅ Script rewritten")
        
        tts_path, tts_filename = generate_tts(rewritten_path)
        print("✅ TTS generated")
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{tts_filename}',
            'srt_used': srt_used,
            'message': 'Processing complete!'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File မပါဘူး'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'File မရွေးထားဘူး'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'ဒီ File အမျိုးအစားကို မထောက်ပံ့ပါဘူး'}), 400
        
        original_filename = secure_filename(file.filename)
        unique_id = uuid.uuid4().hex[:8]
        saved_filename = f"{unique_id}_{original_filename}"
        filepath = os.path.join('uploads', saved_filename)
        file.save(filepath)
        
        ext = original_filename.rsplit('.', 1)[1].lower()
        
        if ext in ['mp4', 'mov', 'avi', 'mkv', 'webm']:
            try:
                srt_path = video_to_srt(filepath)
                content_path = srt_path
            except:
                content_path = audio_to_text(filepath)
        else:
            model = get_whisper()
            result = model.transcribe(filepath, task="translate")
            text_path = 'srt/fallback_text.txt'
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(result['text'])
            content_path = text_path
        
        translated_path = translate_content(content_path, 'my')
        rewritten_path = rewrite_script(translated_path)
        tts_path, tts_filename = generate_tts(rewritten_path)
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{tts_filename}',
            'message': 'Upload processing complete!'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

@app.route('/srt/<filename>')
def serve_srt(filename):
    return send_from_directory('srt', filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
