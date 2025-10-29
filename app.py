import os
import uuid
import logging
import tempfile
import shutil
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp
from flask_cors import CORS

# Flask app with proper proxy configuration for production
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app, resources={r"/*": {"origins": "*"}})

# Configuration
DOWNLOAD_FOLDER = 'downloads'
LOG_FOLDER = 'logs'
MAX_FILE_AGE_HOURS = 1
MAX_FILE_SIZE_MB = 100
RATE_LIMIT_PER_HOUR = 50

# Cookie file paths (checked in order)
COOKIES_FILE_PATHS = [
    '/etc/secrets/cookies.txt',  # Render Secret Files (primary)
    './cookies.txt',             # Local development
    '/tmp/cookies.txt'           # Temporary fallback
]

def get_cookies_file_path():
    """Find and validate cookies file"""
    for path in COOKIES_FILE_PATHS:
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
                if size > 0:
                    logger.info(f"✓ Found valid cookies file: {path} ({size} bytes)")
                    return path
                else:
                    logger.warning(f"⚠️ Cookies file is empty: {path}")
            except Exception as e:
                logger.error(f"Error checking cookies file {path}: {str(e)}")
    
    logger.warning("⚠️ No valid cookies file found")
    logger.warning(f"Searched paths: {COOKIES_FILE_PATHS}")
    return None

# Create necessary directories
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# Setup logging
log_file = os.path.join(LOG_FOLDER, 'igdl.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('IGDL')

# Initialize cookies path
COOKIES_FILE_PATH = get_cookies_file_path()

# Rate limiting storage
download_tracker = {}

def sanitize_filename(filename):
    """Enhanced filename sanitization with unique ID"""
    keepchars = (' ', '.', '_', '-')
    filename = "".join(c for c in filename if c.isalnum() or c in keepchars).rstrip()
    name, ext = os.path.splitext(filename)
    unique_id = str(uuid.uuid4())[:8]
    return f"{name}_{unique_id}{ext}"

def check_rate_limit(ip_address):
    """Rate limiting per IP"""
    current_time = datetime.now()
    
    if ip_address not in download_tracker:
        download_tracker[ip_address] = []
    
    # Remove entries older than 1 hour
    download_tracker[ip_address] = [
        timestamp for timestamp in download_tracker[ip_address]
        if current_time - timestamp < timedelta(hours=1)
    ]
    
    if len(download_tracker[ip_address]) >= RATE_LIMIT_PER_HOUR:
        return False
    
    download_tracker[ip_address].append(current_time)
    return True

def cleanup_old_files():
    """Remove files older than MAX_FILE_AGE_HOURS"""
    try:
        current_time = datetime.now().timestamp()
        deleted_count = 0
        
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(file_path):
                file_age_hours = (current_time - os.path.getmtime(file_path)) / 3600
                if file_age_hours > MAX_FILE_AGE_HOURS:
                    os.remove(file_path)
                    deleted_count += 1
                    logger.info(f"Auto-deleted old file: {filename}")
        
        if deleted_count > 0:
            logger.info(f"Cleanup: Deleted {deleted_count} old files")
            
        return deleted_count
    except Exception as e:
        logger.error(f"Cleanup error: {str(e)}")
        return 0

def get_ydl_opts(download=False, output_dir=None):
    """Get yt-dlp options with or without cookies"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        },
        'socket_timeout': 30,
        'retries': 3,
    }
    
    if download and output_dir:
        opts['outtmpl'] = os.path.join(output_dir, '%(title)s.%(ext)s')
        opts['format'] = 'best[filesize<?100M]'
    
    # Add cookies if available
    if COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH):
        opts['cookiefile'] = COOKIES_FILE_PATH
        logger.debug("Using cookies for request")
    else:
        logger.warning("No cookies available - private content may fail")
    
    return opts

def get_media_info_ytdlp(url):
    """Get media information without downloading"""
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            logger.info(f"Fetching info for: {url}")
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("No media found")
            
            # Parse upload date
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Check if carousel
            is_carousel = False
            media_count = 1
            carousel_media = []
            
            if '_type' in info and info['_type'] == 'playlist':
                is_carousel = True
                entries = info.get('entries', [])
                media_count = len(entries)
                
                for entry in entries:
                    carousel_media.append({
                        'id': entry.get('id', ''),
                        'title': entry.get('title', ''),
                        'thumbnail': entry.get('thumbnail', ''),
                        'duration': entry.get('duration', 0),
                        'width': entry.get('width', 0),
                        'height': entry.get('height', 0)
                    })
            
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
                'carousel_media': carousel_media,
                'url': url
            }
            
            logger.info(f"Info retrieved: {result['title']}")
            return result
            
    except Exception as e:
        logger.error(f"Error getting media info: {str(e)}")
        raise e

def download_media_ytdlp(url):
    """Download media using yt-dlp"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        with yt_dlp.YoutubeDL(get_ydl_opts(download=True, output_dir=temp_dir)) as ydl:
            logger.info(f"Downloading from: {url}")
            info = ydl.extract_info(url, download=True)
            
            if not info:
                raise Exception("No media found")
            
            # Parse metadata
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Find downloaded files
            downloaded_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith(('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mkv', '.avi')):
                        downloaded_files.append(os.path.join(root, file))
            
            if not downloaded_files:
                raise Exception("No files downloaded")
            
            # Process files
            results = []
            for downloaded_file in downloaded_files:
                filename = os.path.basename(downloaded_file)
                file_size = os.path.getsize(downloaded_file)
                
                if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    logger.warning(f"File too large: {file_size / (1024*1024):.2f}MB")
                    continue
                
                safe_filename = sanitize_filename(filename)
                final_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
                shutil.move(downloaded_file, final_path)
                
                file_type = 'video' if downloaded_file.endswith(('.mp4', '.mkv', '.avi')) else 'image'
                
                results.append({
                    'filename': safe_filename,
                    'file_size': file_size,
                    'type': file_type,
                    'download_url': f'/download/{safe_filename}'
                })
            
            shutil.rmtree(temp_dir)
            
            if not results:
                raise Exception("No valid files downloaded")
            
            logger.info(f"Successfully downloaded {len(results)} file(s)")
            
            return {
                'status': 'success',
                'type': 'multiple' if len(results) > 1 else results[0]['type'],
                'files': results,
                'count': len(results),
                'title': info.get('title', 'Instagram Media'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'upload_date': upload_date,
                'like_count': info.get('like_count', 0),
                'comment_count': info.get('comment_count', 0),
                'description': info.get('description', ''),
                'duration': info.get('duration', 0)
            }
            
    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        raise e

@app.before_request
def before_request():
    """Periodic cleanup"""
    if not hasattr(app, '_last_cleanup'):
        app._last_cleanup = datetime.now()
    
    if datetime.now() - app._last_cleanup > timedelta(minutes=30):
        cleanup_old_files()
        app._last_cleanup = datetime.now()

# ==================== ROUTES ====================

@app.route('/')
def index():
    """API root endpoint"""
    cookies_ok = COOKIES_FILE_PATH is not None and os.path.exists(COOKIES_FILE_PATH)
    return jsonify({
        'status': 'online',
        'service': 'Instagram Downloader API',
        'version': '2.0',
        'cookies_configured': cookies_ok,
        'endpoints': {
            'health': '/health',
            'download': '/api/download',
            'media_info': '/api/media/info',
            'stats': '/api/stats',
            'debug': '/debug/info'
        }
    })

@app.route('/health')
def health_check():
    """Health check for monitoring"""
    cookies_ok = COOKIES_FILE_PATH is not None and os.path.exists(COOKIES_FILE_PATH)
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cookies_configured': cookies_ok,
        'cookies_path': COOKIES_FILE_PATH if cookies_ok else None
    })

@app.route('/debug/info')
def debug_info():
    """Debug information endpoint"""
    info = {
        'timestamp': datetime.now().isoformat(),
        'cookies': {
            'configured': COOKIES_FILE_PATH is not None,
            'path': COOKIES_FILE_PATH,
            'exists': os.path.exists(COOKIES_FILE_PATH) if COOKIES_FILE_PATH else False
        },
        'paths_checked': COOKIES_FILE_PATHS,
        'directories': {
            'downloads': os.path.exists(DOWNLOAD_FOLDER),
            'logs': os.path.exists(LOG_FOLDER)
        }
    }
    
    # Add file info if cookies exist
    if COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH):
        try:
            info['cookies']['size'] = os.path.getsize(COOKIES_FILE_PATH)
            with open(COOKIES_FILE_PATH, 'r') as f:
                first_lines = [f.readline().strip() for _ in range(3)]
            info['cookies']['preview'] = first_lines
        except Exception as e:
            info['cookies']['error'] = str(e)
    
    return jsonify(info)

@app.route('/api/media/info', methods=['POST'])
def get_media_info():
    """Get media information without downloading"""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        if not url.startswith(('https://www.instagram.com/', 'https://instagram.com/')):
            return jsonify({'status': 'error', 'message': 'Invalid Instagram URL'}), 400

        if not COOKIES_FILE_PATH or not os.path.exists(COOKIES_FILE_PATH):
            logger.warning("Attempting to fetch info without cookies")

        media_info = get_media_info_ytdlp(url)
        return jsonify({'status': 'ok', 'media_info': media_info})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Media info error: {error_msg}")
        
        if 'private' in error_msg.lower() or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private or requires authentication'}), 401
        elif 'not found' in error_msg.lower() or '404' in error_msg:
            return jsonify({'status': 'error', 'message': 'Media not found'}), 404
        else:
            return jsonify({'status': 'error', 'message': 'Failed to fetch media information'}), 500

@app.route('/api/download', methods=['POST'])
def download_media():
    """Download Instagram media"""
    try:
        # Rate limiting
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()

        if not check_rate_limit(client_ip):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Try again later.'}), 429

        # Validate input
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        if not url.startswith(('https://www.instagram.com/', 'https://instagram.com/')):
            return jsonify({'status': 'error', 'message': 'Invalid Instagram URL'}), 400

        if not COOKIES_FILE_PATH or not os.path.exists(COOKIES_FILE_PATH):
            logger.warning("Attempting download without cookies")

        # Download
        result = download_media_ytdlp(url)
        return jsonify({
            'status': 'ok',
            'type': result['type'],
            'files': result.get('files', []),
            'count': result.get('count', 1),
            'preview_url': result.get('thumbnail', ''),
            'title': result.get('title', ''),
            'uploader': result.get('uploader', 'Unknown'),
            'upload_date': result.get('upload_date', ''),
            'like_count': result.get('like_count', 0),
            'comment_count': result.get('comment_count', 0)
        })

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")
        
        if 'private' in error_msg.lower() or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Media not found'}), 404
        elif 'timeout' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Request timed out'}), 504
        else:
            return jsonify({'status': 'error', 'message': 'Download failed'}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """Serve downloaded file"""
    try:
        safe_filename = sanitize_filename(filename)
        file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)

        if not os.path.exists(file_path):
            # Try to find similar filename
            base_name = filename.rsplit('_', 1)[0] if '_' in filename else filename
            matching_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(base_name)]
            if matching_files:
                file_path = os.path.join(DOWNLOAD_FOLDER, matching_files[0])
            else:
                return jsonify({'status': 'error', 'message': 'File not found or expired'}), 404

        logger.info(f"Serving file: {filename}")
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))

    except Exception as e:
        logger.error(f"Error serving file {filename}: {str(e)}")
        return jsonify({'status': 'error', 'message': 'File not available'}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get service statistics"""
    try:
        download_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if os.path.isfile(os.path.join(DOWNLOAD_FOLDER, f))]
        total_size = sum(os.path.getsize(os.path.join(DOWNLOAD_FOLDER, f)) for f in download_files)
        
        return jsonify({
            'status': 'ok',
            'stats': {
                'cached_files': len(download_files),
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'max_age_hours': MAX_FILE_AGE_HOURS,
                'rate_limit_per_hour': RATE_LIMIT_PER_HOUR,
                'cookies_configured': COOKIES_FILE_PATH is not None
            }
        })
    except Exception as e:
        logger.error(f"Stats error: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get recent logs"""
    try:
        lines = request.args.get('lines', 100, type=int)
        if not os.path.exists(log_file):
            return jsonify({'status': 'error', 'message': 'No logs available'}), 404
        
        with open(log_file, 'r') as f:
            log_lines = f.readlines()[-lines:]
        
        return jsonify({
            'status': 'ok',
            'logs': log_lines,
            'count': len(log_lines)
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'status': 'error', 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {str(e)}")
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({'status': 'error', 'message': 'File too large'}), 413

# ==================== MAIN ====================

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("Instagram Downloader API Starting")
    logger.info(f"Cookies configured: {COOKIES_FILE_PATH is not None}")
    logger.info(f"Download folder: {DOWNLOAD_FOLDER}")
    logger.info(f"Log folder: {LOG_FOLDER}")
    logger.info("=" * 50)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
