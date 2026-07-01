from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import subprocess
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

os.makedirs('downloads', exist_ok=True)
os.makedirs('uploads', exist_ok=True)

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mp3', 'wav', 'm4a', 'webm', 'mkv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls'],
                }
            }
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            print(f"Downloaded: {info.get('title', 'Unknown')}")

        # ဖိုင်ကိုရှာပါ
        audio_file = None
        for f in os.listdir('downloads'):
            if f.endswith('.mp3'):
                audio_file = f
                break

        if not audio_file:
            # တစ်ခါတလေ .webm ဖြစ်နေတယ်
            for f in os.listdir('downloads'):
                if f.endswith(('.webm', '.m4a')):
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
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/downloads/<filename>')
def serve_audio(filename):
    return send_from_directory('downloads', filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
