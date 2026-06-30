from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import os

app = Flask(__name__)
CORS(app)  # Loveable က ခေါ်လို့ရအောင်

# Audio သိမ်းမယ့် Folder လုပ်ပေး
os.makedirs('downloads', exist_ok=True)

@app.route('/download', methods=['POST'])
def download_audio():
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL မပါဘူး'}), 400
        
        # yt-dlp နဲ့ Audio ဆွဲ
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': 'downloads/audio.%(ext)s',
            'quiet': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # အောင်မြင်ရင် Audio URL ပြန်
        audio_url = '/downloads/audio.mp3'
        return jsonify({
            'success': True,
            'audio_url': audio_url,
            'message': 'Audio ဆွဲချပြီးပါပြီ'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def home():
    return jsonify({'message': 'Video-to-Audio API is running!'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
