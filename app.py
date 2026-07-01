from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import re
import time
import subprocess

app = Flask(__name__)
CORS(app)

os.makedirs('downloads', exist_ok=True)

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

        # YouTube Shorts URL ကို ပြောင်းပါ
        if 'shorts/' in url:
            video_id = url.split('shorts/')[1].split('?')[0]
            url = f'https://www.youtube.com/watch?v={video_id}'
            print(f"Converted Shorts URL to: {url}")

        # yt-dlp options (MP4 အဖြစ် ဆွဲမယ်)
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'downloads/audio.%(ext)s',
            'quiet': True,
            'no_check_certificate': True,
            'ignoreerrors': True,
            'geo_bypass': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls'],
                    'player_client': ['android', 'web'],
                }
            }
        }

        # yt-dlp ကို Python ကနေ ခေါ်ပါ
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            print(f"Downloaded: {info.get('title', 'Unknown')}")

        # ဖိုင်ကိုရှာပါ
        time.sleep(1)
        audio_file = None
        for f in os.listdir('downloads'):
            if f.endswith(('.mp4', '.webm', '.m4a')):
                audio_file = f
                break

        if not audio_file:
            files = os.listdir('downloads')
            return jsonify({'error': f'Audio ဖိုင် မတွေ့ဘူး။ Files: {files}'}), 500

        # MP4 ကနေ MP3 ကို FFmpeg နဲ့ ပြောင်းပါ
        input_path = os.path.join('downloads', audio_file)
        output_filename = audio_file.rsplit('.', 1)[0] + '.mp3'
        output_path = os.path.join('downloads', output_filename)

        cmd = [
            'ffmpeg', '-i', input_path,
            '-q:a', '0', '-map', 'a',
            output_path, '-y'
        ]
        subprocess.run(cmd, capture_output=True, check=True)

        # MP4 ဖိုင်ကို ဖျက်ပါ
        try:
            os.remove(input_path)
        except:
            pass

        return jsonify({
            'success': True,
            'audio_url': f'/downloads/{output_filename}',
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
