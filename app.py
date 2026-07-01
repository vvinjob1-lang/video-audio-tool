from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import time
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('downloads', exist_ok=True)

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mp3', 'wav', 'm4a', 'webm', 'mkv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ===== URL DOWNLOAD =====
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

# ===== FILE UPLOAD =====
@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'File မပါဘူး'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'File မရွေးထားဘူး'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'ဒီ File အမျိုးအစားကို မထောက်ပံ့ပါဘူး'}), 400
        
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        # File ကို သိမ်းပြီးရင် Audio ထုတ်ဖို့
        # ဒီနေရာမှာ Transcribe → Translate → Rewrite → TTS ဆက်လုပ်လို့ရတယ်
        # ဒါပေမယ့် အခု အတွက် File Path ကိုပဲ ပြန်ပေးထားတယ်
        
        return jsonify({
            'success': True,
            'file_path': filepath,
            'filename': filename,
            'message': 'File တင်ပြီးပါပြီ'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== SERVE FILES =====
@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

@app.route('/')
def home():
    return jsonify({'message': 'Video-to-Audio API is running!'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
