import os
import uuid
from flask import Flask, request, jsonify, send_file, render_template
import yt_dlp
import tempfile
import shutil
from urllib.parse import urlparse
import requests

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = 'downloads'
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def sanitize_filename(filename):
    """Basic filename sanitization"""
    keepchars = (' ', '.', '_', '-')
    return "".join(c for c in filename if c.isalnum() or c in keepchars).rstrip()

def download_media_ytdlp(url):
    """Download media using yt-dlp"""
    try:
        # Create temp directory for this download
        temp_dir = tempfile.mkdtemp()
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            if not info:
                raise Exception("No media found")
            
            # Find the downloaded file
            downloaded_files = [f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
            if not downloaded_files:
                raise Exception("No file downloaded")
            
            downloaded_file = os.path.join(temp_dir, downloaded_files[0])
            
            # Generate safe filename
            safe_filename = sanitize_filename(downloaded_files[0])
            final_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
            
            # Move file to downloads folder
            shutil.move(downloaded_file, final_path)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir)
            
            return {
                'status': 'success',
                'type': 'video' if info.get('duration') else 'image',
                'filename': safe_filename,
                'file_size': os.path.getsize(final_path),
                'title': info.get('title', ''),
                'thumbnail': info.get('thumbnail', '')
            }
            
    except Exception as e:
        # Clean up on error
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        raise e

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def download_media():
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400
        
        url = data['url']
        
        # Basic URL validation
        if not url.startswith(('https://www.instagram.com/', 'https://instagram.com/')):
            return jsonify({'status': 'error', 'message': 'Invalid Instagram URL'}), 400
        
        # Download media
        result = download_media_ytdlp(url)
        
        return jsonify({
            'status': 'ok',
            'type': result['type'],
            'download_url': f'/download/{result["filename"]}',
            'preview_url': result.get('thumbnail', ''),
            'file_size': result.get('file_size', 0),
            'title': result.get('title', '')
        })
        
    except Exception as e:
        error_msg = str(e)
        if 'Private' in error_msg or 'login' in error_msg:
            return jsonify({'status': 'error', 'message': 'This content may be private or require login'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Media not found'}), 404
        else:
            return jsonify({'status': 'error', 'message': f'Download failed: {error_msg}'}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """Serve downloaded files"""
    try:
        # Security check
        safe_filename = sanitize_filename(filename)
        file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
        
        if not os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'File not found'}), 404
        
        return send_file(file_path, as_attachment=True, download_name=safe_filename)
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/cleanup', methods=['POST'])
def cleanup_files():
    """Clean up downloaded files (optional)"""
    try:
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        return jsonify({'status': 'ok', 'message': 'Cleanup completed'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)