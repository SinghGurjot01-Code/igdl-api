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

# API-only Flask app (no template rendering)
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app)

# Configuration
DOWNLOAD_FOLDER = 'downloads'
LOG_FOLDER = 'logs'
MAX_FILE_AGE_HOURS = 1
MAX_FILE_SIZE_MB = 100
RATE_LIMIT_PER_HOUR = 50
COOKIES_FILE_PATH = '/etc/secrets/cookies.txt'

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

# Rate limiting storage (in-memory)
download_tracker = {}

# Check if cookies file exists
if os.path.exists(COOKIES_FILE_PATH):
    logger.info("✓ Instagram cookies loaded from secret file")
else:
    logger.warning("⚠️ No cookies.txt found — Instagram private content may fail")

def sanitize_filename(filename):
    """Enhanced filename sanitization with unique ID"""
    keepchars = (' ', '.', '_', '-')
    filename = "".join(c for c in filename if c.isalnum() or c in keepchars).rstrip()
    name, ext = os.path.splitext(filename)
    unique_id = str(uuid.uuid4())[:8]
    return f"{name}_{unique_id}{ext}"

def check_rate_limit(ip_address):
    """Simple rate limiting per IP"""
    current_time = datetime.now()
    
    if ip_address not in download_tracker:
        download_tracker[ip_address] = []
    
    # Remove old entries (older than 1 hour)
    download_tracker[ip_address] = [
        timestamp for timestamp in download_tracker[ip_address]
        if current_time - timestamp < timedelta(hours=1)
    ]
    
    # Check if limit exceeded
    if len(download_tracker[ip_address]) >= RATE_LIMIT_PER_HOUR:
        return False
    
    # Add current download
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

def log_download_attempt(url, success=True, error_msg=None, file_type=None, username=None):
    """Log download attempts"""
    if success:
        logger.info(f"Download successful - URL: {url}, Type: {file_type}")
    else:
        logger.error(f"Download failed - URL: {url}, Error: {error_msg}")

def get_media_info_ytdlp(url):
    """Get media information without downloading"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'cookiefile': COOKIES_FILE_PATH,
            'extract_flat': False,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            },
            'socket_timeout': 30,
            'retries': 3,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Getting info for: {url}")
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("No media found")
            
            # Enhanced metadata extraction
            uploader = info.get('uploader', 'Unknown')
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Determine if it's a carousel (multiple media)
            is_carousel = False
            media_count = 1
            carousel_media = []
            
            if '_type' in info and info['_type'] == 'playlist':
                is_carousel = True
                media_count = len(info.get('entries', []))
                # Extract carousel media info
                for entry in info.get('entries', []):
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
                'uploader': uploader,
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
            
            logger.info(f"Media info retrieved: {result['title']}")
            return result
            
    except Exception as e:
        logger.error(f"Error getting media info: {str(e)}")
        raise e

def get_stories_highlights_info(username):
    """Get stories and highlights information"""
    try:
        # This is a simplified implementation
        # In a real scenario, you'd use Instagram's API or yt-dlp to get this info
        result = {
            'username': username,
            'stories': {
                'available': True,
                'count': 1,  # Placeholder
                'content': [
                    {
                        'id': 'story_1',
                        'title': f'{username}\'s Story',
                        'thumbnail': '',  # You'd get this from actual API
                        'timestamp': datetime.now().isoformat()
                    }
                ]
            },
            'highlights': {
                'available': True,
                'count': 1,  # Placeholder
                'content': [
                    {
                        'id': 'highlight_1',
                        'title': f'{username}\'s Highlight',
                        'thumbnail': '',  # You'd get this from actual API
                        'timestamp': datetime.now().isoformat()
                    }
                ]
            }
        }
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting stories/highlights info: {str(e)}")
        raise e

def download_media_ytdlp(url):
    """Download media using yt-dlp with authentication"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'cookiefile': COOKIES_FILE_PATH,
            'extract_flat': False,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            },
            'format': 'best[filesize<?100M]',
            'socket_timeout': 30,
            'retries': 3,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Downloading from: {url}")
            info = ydl.extract_info(url, download=True)
            
            if not info:
                raise Exception("No media found")
            
            # Enhanced metadata extraction
            uploader = info.get('uploader', 'Unknown')
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Find all downloaded files
            downloaded_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith(('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mkv', '.avi')) and not file.endswith('.txt'):
                        downloaded_files.append(os.path.join(root, file))
            
            if not downloaded_files:
                raise Exception("No file downloaded")
            
            # Process files
            results = []
            for downloaded_file in downloaded_files:
                filename = os.path.basename(downloaded_file)
                
                # Check file size
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
            
            result = {
                'status': 'success',
                'type': 'multiple' if len(results) > 1 else results[0]['type'],
                'files': results,
                'count': len(results),
                'title': info.get('title', 'Instagram Media'),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': uploader,
                'upload_date': upload_date,
                'like_count': info.get('like_count', 0),
                'comment_count': info.get('comment_count', 0),
                'description': info.get('description', ''),
                'duration': info.get('duration', 0)
            }
            
            log_download_attempt(url, success=True, file_type=result['type'])
            return result
            
    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        log_download_attempt(url, success=False, error_msg=str(e))
        raise e

def download_stories_highlights_ytdlp(username, content_type='stories'):
    """Download Instagram stories or highlights"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        if content_type == 'stories':
            url = f'https://www.instagram.com/stories/{username}/'
            outtmpl = os.path.join(temp_dir, f'%(uploader)s_%(title)s.%(ext)s')
        else:
            url = f'https://www.instagram.com/stories/highlights/{username}/'
            outtmpl = os.path.join(temp_dir, f'%(uploader)s_highlight_%(title)s.%(ext)s')
        
        ydl_opts = {
            'outtmpl': outtmpl,
            'quiet': True,
            'no_warnings': True,
            'cookiefile': COOKIES_FILE_PATH,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Downloading {content_type} for user: {username}")
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception(f"No {content_type} found for user {username}")
            
            ydl.download([url])
            
            # Find downloaded files
            downloaded_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith(('.mp4', '.jpg', '.jpeg', '.png', '.webp')) and not file.endswith('.txt'):
                        downloaded_files.append(os.path.join(root, file))
            
            if not downloaded_files:
                raise Exception(f"No {content_type} files downloaded")
            
            # Move files
            results = []
            for file_path in downloaded_files:
                filename = os.path.basename(file_path)
                safe_filename = sanitize_filename(filename)
                final_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)
                
                shutil.move(file_path, final_path)
                
                results.append({
                    'filename': safe_filename,
                    'file_size': os.path.getsize(final_path),
                    'type': 'video' if filename.endswith('.mp4') else 'image',
                    'download_url': f'/download/{safe_filename}'
                })
            
            shutil.rmtree(temp_dir)
            
            log_download_attempt(url, success=True, file_type=content_type, username=username)
            
            return {
                'status': 'success',
                'content_type': content_type,
                'username': username,
                'files': results,
                'count': len(results)
            }
            
    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        log_download_attempt(f"{content_type} for {username}", success=False, error_msg=str(e), username=username)
        raise e

@app.before_request
def before_request():
    """Run cleanup check before requests (every 30 minutes)"""
    if not hasattr(app, '_last_cleanup'):
        app._last_cleanup = datetime.now()
    
    if datetime.now() - app._last_cleanup > timedelta(minutes=30):
        cleanup_old_files()
        app._last_cleanup = datetime.now()

@app.route('/')
def index():
    # API-only root to avoid TemplateNotFound errors when frontend is served separately
    logger.info("Homepage (API) accessed")
    return jsonify({
        'message': 'Backend running. Serve your frontend separately (local or CDN).',
        'cookies_configured': os.path.exists(COOKIES_FILE_PATH)
    })

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cookies_configured': os.path.exists(COOKIES_FILE_PATH)
    })

# ============================
# NEW ENDPOINTS FOR FRONTEND COMPATIBILITY
# ============================

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

        if not os.path.exists(COOKIES_FILE_PATH):
            return jsonify({
                'status': 'error',
                'message': 'Service not properly configured. Please contact administrator.'
            }), 503

        media_info = get_media_info_ytdlp(url)
        return jsonify({'status': 'ok', 'media_info': media_info})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Media info error: {error_msg}")
        if 'Private' in error_msg or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private or requires authentication'}), 401
        elif 'not found' in error_msg.lower() or '404' in error_msg:
            return jsonify({'status': 'error', 'message': 'Media not found or no longer available'}), 404
        else:
            return jsonify({'status': 'error', 'message': 'Failed to fetch media information'}), 500


@app.route('/api/stories-highlights/info', methods=['POST'])
def get_stories_highlights_info_endpoint():
    """Get stories and highlights information"""
    try:
        data = request.get_json()
        if not data or 'username' not in data:
            return jsonify({'status': 'error', 'message': 'Username is required'}), 400

        username = data['username'].strip().lstrip('@')
        if not username:
            return jsonify({'status': 'error', 'message': 'Valid username is required'}), 400

        info = get_stories_highlights_info(username)
        return jsonify({
            'status': 'ok',
            'username': username,
            'stories': info['stories'],
            'highlights': info['highlights']
        })
    except Exception as e:
        logger.error(f"Stories/highlights info error: {str(e)}")
        return jsonify({'status': 'error', 'message': 'Failed to fetch stories and highlights information'}), 500


# ============================
# EXISTING ENDPOINTS
# ============================

@app.route('/api/download', methods=['POST'])
def download_media():
    try:
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()

        if not check_rate_limit(client_ip):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'}), 429

        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        if not url.startswith(('https://www.instagram.com/', 'https://instagram.com/')):
            return jsonify({'status': 'error', 'message': 'Invalid Instagram URL'}), 400

        if not os.path.exists(COOKIES_FILE_PATH):
            return jsonify({'status': 'error', 'message': 'Service not properly configured. Please contact administrator.'}), 503

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
        if 'Private' in error_msg or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private or requires authentication'}), 401
        elif 'not found' in error_msg.lower() or '404' in error_msg:
            return jsonify({'status': 'error', 'message': 'Media not found or no longer available'}), 404
        elif 'timeout' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Request timed out. Please try again.'}), 504
        else:
            return jsonify({'status': 'error', 'message': 'Download failed. Please try again later.'}), 500


@app.route('/api/stories-highlights/download', methods=['POST'])
def download_stories_highlights():
    try:
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()

        if not check_rate_limit(client_ip):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'}), 429

        data = request.get_json()
        if not data or 'username' not in data or 'content_type' not in data:
            return jsonify({'status': 'error', 'message': 'Username and content type are required'}), 400

        username = data['username'].strip().lstrip('@')
        content_type = data['content_type']
        if content_type not in ['stories', 'highlights']:
            return jsonify({'status': 'error', 'message': 'Content type must be "stories" or "highlights"'}), 400

        result = download_stories_highlights_ytdlp(username, content_type)
        return jsonify({
            'status': 'ok',
            'content_type': content_type,
            'username': username,
            'files': result['files'],
            'count': result['count']
        })

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Stories/highlights download error: {error_msg}")
        if 'Private' in error_msg or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private or requires authentication'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': f'No {data.get("content_type")} found for this user'}), 404
        else:
            return jsonify({'status': 'error', 'message': 'Download failed. Please try again later.'}), 500


@app.route('/download/<filename>')
def download_file(filename):
    try:
        safe_filename = sanitize_filename(filename)
        file_path = os.path.join(DOWNLOAD_FOLDER, safe_filename)

        if not os.path.exists(file_path):
            base_name = filename.rsplit('_', 1)[0] if '_' in filename else filename
            matching_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(base_name)]
            if matching_files:
                file_path = os.path.join(DOWNLOAD_FOLDER, matching_files[0])
            else:
                return jsonify({'status': 'error', 'message': 'File not found or expired'}), 404

        logger.info(f"File served: {filename}")
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))

    except Exception as e:
        logger.error(f"Error serving file {filename}: {str(e)}")
        return jsonify({'status': 'error', 'message': 'File not available'}), 500


@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        download_files = [f for f in os.listdir(DOWNLOAD_FOLDER) if os.path.isfile(os.path.join(DOWNLOAD_FOLDER, f))]
        total_size = sum(os.path.getsize(os.path.join(DOWNLOAD_FOLDER, f)) for f in download_files)
        return jsonify({
            'status': 'ok',
            'stats': {
                'cached_files': len(download_files),
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'max_age_hours': MAX_FILE_AGE_HOURS,
                'rate_limit_per_hour': RATE_LIMIT_PER_HOUR
            }
        })
    except Exception as e:
        logger.error(f"Stats error: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/logs', methods=['GET'])
def get_logs():
    try:
        lines = request.args.get('lines', 100, type=int)
        if not os.path.exists(log_file):
            return jsonify({'status': 'error', 'message': 'No logs available'}), 404
        with open(log_file, 'r') as f:
            log_lines = f.readlines()[-lines:]
        return jsonify({'status': 'ok', 'logs': log_lines, 'count': len(log_lines)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ============================
# ERROR HANDLERS
# ============================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'status': 'error', 'message': 'Endpoint not found'}), 404


@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {str(e)}")
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({'status': 'error', 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {str(e)}")
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

if __name__ == '__main__':
    logger.info("Starting Instagram Downloader in production mode")
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
