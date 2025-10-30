import os
import uuid
import logging
import tempfile
import shutil
import io
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp
from flask_cors import CORS

# Flask app
app = Flask(__name__, static_folder='.', static_url_path='')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Enable CORS for all routes - CRITICAL for localhost frontend
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "expose_headers": ["Content-Disposition"]
    }
})

# Configuration
LOG_FOLDER = 'logs'
MAX_FILE_SIZE_MB = 100
RATE_LIMIT_PER_HOUR = 50

# Cookie file paths
COOKIES_FILE_PATHS = [
    '/etc/secrets/cookies.txt',
    './cookies.txt',
    '/tmp/cookies.txt'
]

def get_cookies_file_path():
    """Find cookies file and copy to writable location if needed"""
    writable_path = '/tmp/cookies.txt'
    
    for path in COOKIES_FILE_PATHS:
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
                if size > 0:
                    logger.info(f"✓ Found cookies file: {path} ({size} bytes)")
                    
                    if path.startswith('/etc/secrets/'):
                        try:
                            shutil.copy2(path, writable_path)
                            logger.info(f"✓ Copied cookies to: {writable_path}")
                            return writable_path
                        except Exception as e:
                            logger.error(f"Failed to copy cookies: {str(e)}")
                            return path  # Try to use read-only version
                    else:
                        return path
            except Exception as e:
                logger.error(f"Error checking cookies {path}: {str(e)}")
    
    logger.warning("⚠️ No valid cookies file found")
    return None

# Create directories
os.makedirs(LOG_FOLDER, exist_ok=True)

# Setup logging
log_file = os.path.join(LOG_FOLDER, 'igdl.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('IGDL')

# Initialize cookies
COOKIES_FILE_PATH = get_cookies_file_path()

# Rate limiting
download_tracker = {}

def check_rate_limit(ip_address):
    """Rate limiting per IP"""
    current_time = datetime.now()
    
    if ip_address not in download_tracker:
        download_tracker[ip_address] = []
    
    download_tracker[ip_address] = [
        timestamp for timestamp in download_tracker[ip_address]
        if current_time - timestamp < timedelta(hours=1)
    ]
    
    if len(download_tracker[ip_address]) >= RATE_LIMIT_PER_HOUR:
        return False
    
    download_tracker[ip_address].append(current_time)
    return True

def get_ydl_opts(download=False, output_dir=None):
    """Get yt-dlp options"""
    opts = {
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
            'Referer': 'https://www.instagram.com/',
        },
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
    }
    
    if download and output_dir:
        opts['outtmpl'] = os.path.join(output_dir, '%(title)s.%(ext)s')
        opts['format'] = 'best[filesize<?100M]/best'
        opts['merge_output_format'] = 'mp4'
    
    if COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH):
        opts['cookiefile'] = COOKIES_FILE_PATH
        logger.info("✓ Using cookies for request")
    else:
        logger.warning("⚠️ No cookies - private content may fail")
    
    return opts

def get_media_info_ytdlp(url):
    """Get media information"""
    try:
        ydl_opts = get_ydl_opts()
        ydl_opts.update({
            'extract_flat': False,
            'force_json': True,
            'ignoreerrors': True,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"📥 Fetching info for: {url}")
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("No media found at this URL")
            
            # Parse upload date
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Check for carousel
            is_carousel = info.get('_type') == 'playlist'
            media_count = len(info.get('entries', [])) if is_carousel else 1
            
            result = {
                'title': info.get('title', 'Instagram Media'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'upload_date': upload_date,
                'like_count': info.get('like_count', 0),
                'comment_count': info.get('comment_count', 0),
                'description': info.get('description', ''),
                'duration': info.get('duration', 0),
                'is_carousel': is_carousel,
                'media_count': media_count,
            }
            
            logger.info(f"✓ Info retrieved: {result['title']} ({media_count} items)")
            return result
            
    except Exception as e:
        logger.error(f"❌ Error getting media info: {str(e)}")
        raise e

def download_media_to_buffer(url):
    """Download media to memory buffer"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        with yt_dlp.YoutubeDL(get_ydl_opts(download=True, output_dir=temp_dir)) as ydl:
            logger.info(f"📥 Downloading from: {url}")
            info = ydl.extract_info(url, download=True)
            
            if not info:
                raise Exception("No media found")
            
            # Find downloaded file
            downloaded_file = None
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith(('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mkv')):
                        downloaded_file = os.path.join(root, file)
                        break
                if downloaded_file:
                    break
            
            if not downloaded_file:
                raise Exception("No files downloaded")
            
            file_size = os.path.getsize(downloaded_file)
            logger.info(f"📦 File size: {file_size / (1024*1024):.2f} MB")
            
            # Read file into memory
            with open(downloaded_file, 'rb') as f:
                file_data = io.BytesIO(f.read())
            
            filename = os.path.basename(downloaded_file)
            
            # Sanitize filename
            safe_filename = "".join(c for c in filename if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
            
            file_type = 'video/mp4' if downloaded_file.endswith(('.mp4', '.mkv')) else 'image/jpeg'
            
            logger.info(f"✓ Successfully downloaded: {safe_filename}")
            
            return {
                'file_data': file_data,
                'filename': safe_filename,
                'mimetype': file_type,
                'size': file_size
            }
            
    except Exception as e:
        logger.error(f"❌ Download error: {str(e)}")
        raise e
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass

# ==================== ROUTES ====================

@app.route('/')
def index():
    """Serve the main HTML page"""
    try:
        if os.path.exists('index.html'):
            return send_from_directory('.', 'index.html')
        else:
            return f"""
<!DOCTYPE html>
<html>
<head>
    <title>IGDL API - Running</title>
    <style>
        body {{ font-family: Arial; max-width: 800px; margin: 50px auto; padding: 20px; }}
        .status {{ padding: 15px; background: #e8f5e9; border-left: 4px solid #4caf50; margin: 20px 0; }}
        .endpoint {{ background: #f5f5f5; padding: 15px; margin: 10px 0; border-radius: 5px; }}
        code {{ background: #333; color: #fff; padding: 2px 6px; border-radius: 3px; }}
        .copy-btn {{ padding: 8px 15px; background: #833ab4; color: white; border: none; border-radius: 5px; cursor: pointer; }}
    </style>
</head>
<body>
    <h1>🚀 IGDL API is Running!</h1>
    <div class="status">
        <strong>✓ Status:</strong> API operational<br>
        <strong>📍 API URL:</strong> <span id="api-url">{request.url_root.rstrip('/')}</span>
        <button class="copy-btn" onclick="copyUrl()">Copy URL</button>
    </div>
    
    <h2>📡 Available Endpoints:</h2>
    
    <div class="endpoint">
        <strong>GET /health</strong><br>
        Health check - Test if API is working
    </div>
    
    <div class="endpoint">
        <strong>POST /api/media/info</strong><br>
        Get media information<br>
        Body: <code>{{"url": "instagram_url"}}</code>
    </div>
    
    <div class="endpoint">
        <strong>POST /api/download</strong><br>
        Download media file<br>
        Body: <code>{{"url": "instagram_url"}}</code>
    </div>
    
    <h2>🔧 Setup Instructions:</h2>
    <ol>
        <li>Copy the API URL above</li>
        <li>Open the frontend HTML file</li>
        <li>Paste the API URL in the configuration box</li>
        <li>Click "Save" and start downloading!</li>
    </ol>
    
    <p><strong>Cookies:</strong> {'✓ Configured' if COOKIES_FILE_PATH else '✗ Missing'}</p>
    <p><strong>CORS:</strong> ✓ Enabled for all origins (works with localhost)</p>
    
    <script>
        function copyUrl() {{
            const url = document.getElementById('api-url').textContent;
            navigator.clipboard.writeText(url);
            alert('API URL copied to clipboard!');
        }}
    </script>
</body>
</html>
            """
    except Exception as e:
        logger.error(f"Error serving index: {str(e)}")
        return jsonify({'error': 'Failed to load page'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    cookies_ok = COOKIES_FILE_PATH is not None and os.path.exists(COOKIES_FILE_PATH)
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cookies_configured': cookies_ok,
        'api_url': request.url_root.rstrip('/'),
        'cors_enabled': True
    })

@app.route('/api/media/info', methods=['POST', 'OPTIONS'])
def get_media_info():
    """Get media information"""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        logger.info(f"📱 Info request for: {url}")
        
        media_info = get_media_info_ytdlp(url)
        
        return jsonify({
            'status': 'ok', 
            'media_info': media_info
        })

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Media info error: {error_msg}")
        
        if 'private' in error_msg.lower() or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private or requires authentication'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Media not found or unavailable'}), 404
        else:
            return jsonify({'status': 'error', 'message': error_msg}), 500

@app.route('/api/download', methods=['POST', 'OPTIONS'])
def download_media():
    """Download media file"""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        # Rate limiting
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()

        if not check_rate_limit(client_ip):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Try again later.'}), 429

        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        logger.info(f"⬇️ Download request from {client_ip} for: {url}")
        
        # Download to memory
        result = download_media_to_buffer(url)
        
        logger.info(f"✓ Sending file: {result['filename']} ({result['size'] / (1024*1024):.2f} MB)")
        
        response = send_file(
            result['file_data'],
            as_attachment=True,
            download_name=result['filename'],
            mimetype=result['mimetype']
        )
        
        # Add CORS headers explicitly
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Disposition'
        
        return response

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Download error: {error_msg}")
        
        if 'private' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Media not found'}), 404
        elif 'timeout' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Request timed out. Try again.'}), 504
        else:
            return jsonify({'status': 'error', 'message': 'Download failed: ' + error_msg}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({'status': 'error', 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal error: {str(e)}")
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("🚀 Instagram Downloader API Starting")
    logger.info(f"📁 Cookies: {'✓ Configured' if COOKIES_FILE_PATH else '✗ Missing'}")
    logger.info(f"🌐 CORS: ✓ Enabled (works with localhost)")
    logger.info(f"💾 Storage: Memory buffer (no persistent files)")
    logger.info("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
