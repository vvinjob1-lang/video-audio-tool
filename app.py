from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import time
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

# ===== FOLDERS =====
os.makedirs('downloads', exist_ok=True)
os.makedirs('uploads', exist_ok=True)

# ===== ALLOWED EXTENSIONS =====
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mp3', 'wav', 'm4a', 'webm', 'mkv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ==========================================
# 1. URL DOWNLOAD ENDPOINT (ရှိပြီးသား)
# ==========================================
@app.route('/download', methods=['POST'])
def download_audio():
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400
        
        # Clean old audio files
        for f in os.listdir('downloads'):
            if f.endswith('.mp3'):
                try:
                    os.remove(os.path.join('downloads', f))
                except:
                    pass
        
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
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
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

# ==========================================
# 2. FILE UPLOAD ENDPOINT (အသစ်ထည့်မယ်)
# ==========================================
@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        # 1. File ပါမပါ စစ်
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'File မပါဘူး'}), 400
        
        file = request.files['file']
        
        # 2. File အမည်ရှိမရှိ စစ်
        if file.filename == '':
            return jsonify({'success': False, 'error': 'File မရွေးထားဘူး'}), 400
        
        # 3. File အမျိုးအစား စစ်
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'ဒီ File အမျိုးအစားကို မထောက်ပံ့ပါဘူး'}), 400
        
        # 4. File ကို သိမ်းတယ်
        original_filename = secure_filename(file.filename)
        unique_id = uuid.uuid4().hex[:8]
        saved_filename = f"{unique_id}_{original_filename}"
        filepath = os.path.join('uploads', saved_filename)
        file.save(filepath)
        
        # 5. Audio ပဲလား? Video ဆိုရင် Audio ထုတ်တယ်
        audio_path = filepath
        ext = original_filename.rsplit('.', 1)[1].lower()
        
        if ext in ['mp4', 'mov', 'avi', 'mkv', 'webm']:
            # Video ဆိုရင် Audio ထုတ်တယ် (FFmpeg သုံး)
            audio_filename = f"{unique_id}_audio.mp3"
            audio_path = os.path.join('downloads', audio_filename)
            # FFmpeg command - server မှာ ffmpeg ရှိဖို့လိုတယ်
            import subprocess
            subprocess.run([
                'ffmpeg', '-i', filepath, '-q:a', '0', '-map', 'a', audio_path, '-y'
            ], capture_output=True)
            
            # Video ဖိုင်ကို ဖျက်တယ် (နေရာလွတ်ဖို့)
            try:
                os.remove(filepath)
            except:
                pass
        else:
            # Audio ဆိုရင် downloads ထဲကို ရွှေ့တယ်
            audio_filename = f"{unique_id}_{original_filename}"
            audio_path = os.path.join('downloads', audio_filename)
            os.rename(filepath, audio_path)
        
        # 6. အောင်မြင်ကြောင်း ပြန်ပေး
        return jsonify({
            'success': True,
            'message': 'File တင်ပြီးပါပြီ',
            'audio_url': f'/downloads/{audio_filename}'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==========================================
# 3. SERVE FILES
# ==========================================
@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

@app.route('/uploads/<filename>')
def serve_upload(filename):
    return send_from_directory('uploads', filename)

@app.route('/')
def home():
    return jsonify({'message': 'Video-to-Audio API is running!'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

from transformers import VitsModel, AutoTokenizer
import torch
import scipy.io.wavfile
import uuid
import os

# ===== TTS ENDPOINT (မြန်မာလို) =====
@app.route('/tts', methods=['POST'])
def generate_tts():
    try:
        data = request.get_json()
        text = data.get('text', '')
        
        if not text:
            return jsonify({'error': 'စာသား မပါဘူး'}), 400
        
        print(f"Generating TTS for: {text[:50]}...")
        
        # မော်ဒယ်ကို ခေါ်သုံးပါ (ပထမအကြိမ် Download လုပ်ဖို့ စက္ကန့် ၃၀ လောက်ကြာနိုင်တယ်)
        model = VitsModel.from_pretrained("facebook/mms-tts-mya")
        tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-mya")
        
        inputs = tokenizer(text, return_tensors="pt")
        
        with torch.no_grad():
            output = model(**inputs).waveform
        
        # အသံဖိုင်ကို သိမ်းဆည်းပါ
        filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        filepath = os.path.join('downloads', filename)
        
        # WAV ဖိုင်အဖြစ် သိမ်းဆည်းပါ
        scipy.io.wavfile.write(filepath, rate=model.config.sampling_rate, data=output)
        
        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{filename}',
            'message': 'TTS ထုတ်လုပ်ပြီးပါပြီ'
        })
        
    except Exception as e:
        print(f"TTS Error: {e}")
        return jsonify({'error': str(e)}), 500
