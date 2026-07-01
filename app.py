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

# ===== LAZY LOADING =====
_whisper_model = None
_tts_model = None
_tts_tokenizer = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model("base")
    return _whisper_model

def get_tts():
    global _tts_model, _tts_tokenizer
    if _tts_model is None:
        import torch
        from transformers import VitsModel, AutoTokenizer
        _tts_model = VitsModel.from_pretrained("facebook/mms-tts-mya")
        _tts_tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-mya")
    return _tts_model, _tts_tokenizer

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

@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

@app.route('/process', methods=['POST'])
def process_video():
    try:
        data = request.get_json()
        url = data.get('url')
        target_lang = data.get('target_language', 'my')

        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400

        # Download video
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

        video_path = 'downloads/video.mp4'

        # Extract audio
        audio_path = 'downloads/audio.wav'
        cmd = [
            'ffmpeg', '-i', video_path,
            '-q:a', '0', '-map', 'a',
            '-ac', '1', '-ar', '16000',
            audio_path, '-y'
        ]
        subprocess.run(cmd, capture_output=True, check=True)

        # Whisper transcribe
        model = get_whisper()
        result = model.transcribe(audio_path, task="translate", verbose=False)

        # Save text
        text_path = 'srt/translated.txt'
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(result['text'])

        # Translate if needed
        if target_lang != 'en':
            from googletrans import Translator
            translator = Translator()
            with open(text_path, 'r', encoding='utf-8') as f:
                content = f.read()
            translated = translator.translate(content, dest=target_lang).text
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(translated)

        # TTS
        with open(text_path, 'r', encoding='utf-8') as f:
            text = f.read()[:200]

        model_tts, tokenizer = get_tts()
        import torch
        import scipy.io.wavfile
        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            output = model_tts(**inputs).waveform

        filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        filepath = os.path.join('downloads', filename)
        scipy.io.wavfile.write(filepath, rate=model_tts.config.sampling_rate, data=output)

        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{filename}',
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

        filename = secure_filename(file.filename)
        unique_id = uuid.uuid4().hex[:8]
        saved_filename = f"{unique_id}_{filename}"
        filepath = os.path.join('uploads', saved_filename)
        file.save(filepath)

        # Extract audio from video if needed
        ext = filename.rsplit('.', 1)[1].lower()
        if ext in ['mp4', 'mov', 'avi', 'mkv', 'webm']:
            audio_path = 'downloads/audio.wav'
            cmd = [
                'ffmpeg', '-i', filepath,
                '-q:a', '0', '-map', 'a',
                '-ac', '1', '-ar', '16000',
                audio_path, '-y'
            ]
            subprocess.run(cmd, capture_output=True, check=True)
        else:
            audio_path = filepath

        # Transcribe
        model = get_whisper()
        result = model.transcribe(audio_path, task="translate", verbose=False)

        text_path = 'srt/uploaded.txt'
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(result['text'])

        # Translate to Myanmar
        from googletrans import Translator
        translator = Translator()
        with open(text_path, 'r', encoding='utf-8') as f:
            content = f.read()
        translated = translator.translate(content, dest='my').text
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(translated)

        # TTS
        with open(text_path, 'r', encoding='utf-8') as f:
            text = f.read()[:200]

        model_tts, tokenizer = get_tts()
        import torch
        import scipy.io.wavfile
        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            output = model_tts(**inputs).waveform

        tts_filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        tts_path = os.path.join('downloads', tts_filename)
        scipy.io.wavfile.write(tts_path, rate=model_tts.config.sampling_rate, data=output)

        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{tts_filename}',
            'message': 'Upload processing complete!'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
