from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import time
import subprocess
from werkzeug.utils import secure_filename

# ===== Whisper, TTS, Translation =====
import whisper
import torch
import scipy.io.wavfile
from transformers import VitsModel, AutoTokenizer
from googletrans import Translator
import ollama

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

# ==========================================
# 1. DOWNLOAD VIDEO FROM URL
# ==========================================
def download_video(url):
    try:
        ydl_opts = {
            'format': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
            'outtmpl': 'downloads/video.%(ext)s',
            'quiet': True,
            'no_check_certificate': True,
            'ignoreerrors': True,
            'geo_bypass': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_path = 'downloads/video.mp4'
            return video_path
    except Exception as e:
        raise Exception(f"Video download failed: {str(e)}")

# ==========================================
# 2. EXTRACT AUDIO FROM VIDEO
# ==========================================
def extract_audio(video_path):
    audio_path = 'downloads/audio.wav'
    try:
        cmd = [
            'ffmpeg', '-i', video_path,
            '-q:a', '0', '-map', 'a',
            '-ac', '1', '-ar', '16000',
            audio_path, '-y'
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return audio_path
    except Exception as e:
        raise Exception(f"Audio extraction failed: {str(e)}")

# ==========================================
# 3. VIDEO → SRT (Whisper)
# ==========================================
def video_to_srt(video_path):
    try:
        audio_path = extract_audio(video_path)
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, task="translate", verbose=False)
        
        srt_path = 'srt/output.srt'
        with open(srt_path, 'w', encoding='utf-8') as f:
            for i, segment in enumerate(result['segments']):
                start = segment['start']
                end = segment['end']
                text = segment['text'].strip()
                
                start_str = f"{int(start//3600):02d}:{int((start%3600)//60):02d}:{int(start%60):02d},{int((start%1)*1000):03d}"
                end_str = f"{int(end//3600):02d}:{int((end%3600)//60):02d}:{int(end%60):02d},{int((end%1)*1000):03d}"
                
                f.write(f"{i+1}\n{start_str} --> {end_str}\n{text}\n\n")
        
        return srt_path
    except Exception as e:
        raise Exception(f"SRT extraction failed: {str(e)}")

# ==========================================
# 4. FALLBACK: Audio → Text (No timestamps)
# ==========================================
def audio_to_text(video_path):
    try:
        audio_path = extract_audio(video_path)
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, task="translate", verbose=False)
        text = result['text']
        
        text_path = 'srt/fallback_text.txt'
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(text)
        
        return text_path
    except Exception as e:
        raise Exception(f"Audio to text failed: {str(e)}")

# ==========================================
# 5. TRANSLATE SRT/TEXT
# ==========================================
def translate_content(file_path, target_lang):
    try:
        translator = Translator()
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Translate
        translated = translator.translate(content, dest=target_lang).text
        
        # Save translated
        ext = 'srt' if file_path.endswith('.srt') else 'txt'
        translated_path = f'srt/translated.{ext}'
        with open(translated_path, 'w', encoding='utf-8') as f:
            f.write(translated)
        
        return translated_path
    except Exception as e:
        raise Exception(f"Translation failed: {str(e)}")

# ==========================================
# 6. REWRITE SCRIPT (Ollama)
# ==========================================
def rewrite_script(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # If SRT, remove timestamps
        if file_path.endswith('.srt'):
            lines = [line for line in content.split('\n') if line.strip() and not line[0].isdigit() and '-->' not in line]
            content = ' '.join(lines)
        
        # Ollama
        response = ollama.chat(model='llama3', messages=[{
            'role': 'user',
            'content': f'Rewrite this text to be more concise, engaging, and natural sounding for a video narration: {content[:2000]}'
        }])
        
        rewritten = response['message']['content']
        
        rewritten_path = 'srt/rewritten_script.txt'
        with open(rewritten_path, 'w', encoding='utf-8') as f:
            f.write(rewritten)
        
        return rewritten_path
    except Exception as e:
        raise Exception(f"Rewriting failed: {str(e)}")

# ==========================================
# 7. TTS (Myanmar - Facebook MMS)
# ==========================================
def generate_tts(text_file, target_lang):
    try:
        with open(text_file, 'r', encoding='utf-8') as f:
            text = f.read()
        
        # If text is too long, take first 200 chars
        if len(text) > 200:
            text = text[:200]
        
        # Load model
        model = VitsModel.from_pretrained("facebook/mms-tts-mya")
        tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-mya")
        
        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            output = model(**inputs).waveform
        
        filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        filepath = os.path.join('downloads', filename)
        scipy.io.wavfile.write(filepath, rate=model.config.sampling_rate, data=output)
        
        return filepath, filename
    except Exception as e:
        raise Exception(f"TTS generation failed: {str(e)}")

# ==========================================
# 8. MAIN PROCESSING ENDPOINT
# ==========================================
@app.route('/process', methods=['POST'])
def process_video():
    try:
        data = request.get_json()
        url = data.get('url')
        target_lang = data.get('target_language', 'my')
        
        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400
        
        print(f"Processing: {url} → Language: {target_lang}")
        
        # Step 1: Download video
        video_path = download_video(url)
        print("✅ Video downloaded")
        
        # Step 2: Try SRT first
        try:
            srt_path = video_to_srt(video_path)
            print("✅ SRT extracted")
            content_path = srt_path
            is_srt = True
        except Exception as e:
            print(f"⚠️ SRT failed: {e}")
            print("🔄 Falling back to Audio → Text")
            content_path = audio_to_text(video_path)
            is_srt = False
            print("✅ Audio → Text complete")
        
        # Step 3: Translate
        translated_path = translate_content(content_path, target_lang)
        print("✅ Translation complete")
        
        # Step 4: Rewrite script
        rewritten_path = rewrite_script(translated_path)
        print("✅ Script rewritten")
        
        # Step 5: TTS
        tts_path, tts_filename = generate_tts(rewritten_path, target_lang)
        print("✅ TTS generated")
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{tts_filename}',
            'srt_used': is_srt,
            'message': 'Processing complete!'
        })
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ==========================================
# 9. FILE UPLOAD ENDPOINT
# ==========================================
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
        
        # Save uploaded file
        original_filename = secure_filename(file.filename)
        unique_id = uuid.uuid4().hex[:8]
        saved_filename = f"{unique_id}_{original_filename}"
        filepath = os.path.join('uploads', saved_filename)
        file.save(filepath)
        
        # Check if video or audio
        ext = original_filename.rsplit('.', 1)[1].lower()
        
        if ext in ['mp4', 'mov', 'avi', 'mkv', 'webm']:
            # Video → SRT
            try:
                srt_path = video_to_srt(filepath)
                content_path = srt_path
                is_srt = True
            except:
                # Fallback: Audio → Text
                content_path = audio_to_text(filepath)
                is_srt = False
        else:
            # Audio only → transcribe
            model = whisper.load_model("base")
            result = model.transcribe(filepath, task="translate")
            text_path = 'srt/fallback_text.txt'
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(result['text'])
            content_path = text_path
            is_srt = False
        
        # Translate
        translated_path = translate_content(content_path, 'my')
        
        # Rewrite
        rewritten_path = rewrite_script(translated_path)
        
        # TTS
        tts_path, tts_filename = generate_tts(rewritten_path, 'my')
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{tts_filename}',
            'message': 'Upload processing complete!'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==========================================
# 10. SERVE FILES
# ==========================================
@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

@app.route('/srt/<filename>')
def serve_srt(filename):
    return send_from_directory('srt', filename)

@app.route('/')
def home():
    return jsonify({'message': 'Video-to-Audio API is running!'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
