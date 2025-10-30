import os
import uuid
import logging
import tempfile
import shutil
import json
import re
import io
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp
from flask_cors import CORS

# Flask app with proper proxy configuration for production
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app, resources={r"/*": {"origins": "*"}})

# Configuration
LOG_FOLDER = 'logs'
MAX_FILE_SIZE_MB = 100
RATE_LIMIT_PER_HOUR = 50

# Cookie file paths (checked in order)
COOKIES_FILE_PATHS = [
    '/etc/secrets/cookies.txt',  # Render Secret Files (read-only)
    './cookies.txt',             # Local development
    '/tmp/cookies.txt'           # Temporary fallback
]

def get_cookies_file_path():
    """Find cookies file and copy to writable location if needed"""
    writable_path = '/tmp/cookies.txt'
    
    # Check all possible paths
    for path in COOKIES_FILE_PATHS:
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
                if size > 0:
                    logger.info(f"✓ Found cookies file: {path} ({size} bytes)")
                    
                    # If it's in read-only /etc/secrets, copy to /tmp
                    if path.startswith('/etc/secrets/'):
                        try:
                            shutil.copy2(path, writable_path)
                            logger.info(f"✓ Copied cookies to writable location: {writable_path}")
                            return writable_path
                        except Exception as e:
                            logger.error(f"Failed to copy cookies: {str(e)}")
                            return None
                    else:
                        # Already in writable location
                        return path
                else:
                    logger.warning(f"⚠️ Cookies file is empty: {path}")
            except Exception as e:
                logger.error(f"Error checking cookies file {path}: {str(e)}")
    
    logger.warning("⚠️ No valid cookies file found")
    logger.warning(f"Searched paths: {COOKIES_FILE_PATHS}")
    return None

# Create necessary directories
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

def get_ydl_opts(download=False, output_dir=None):
    """Get yt-dlp options with enhanced Instagram support"""
    opts = {
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
            'Referer': 'https://www.instagram.com/',
        },
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'extractor_args': {
            'instagram': {
                'format': 'best',
                'post_filter': 'none'
            }
        },
    }
    
    if download and output_dir:
        opts['outtmpl'] = os.path.join(output_dir, '%(title)s.%(ext)s')
        # Enhanced format selection for Instagram
        opts['format'] = 'best[filesize<?100M]/best'
        opts['merge_output_format'] = 'mp4'
    
    # Add cookies if available
    if COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH):
        opts['cookiefile'] = COOKIES_FILE_PATH
        logger.info("Using cookies for Instagram request")
    else:
        logger.warning("No cookies available - private content may fail")
    
    return opts

def is_valid_instagram_url(url):
    """Enhanced Instagram URL validation"""
    instagram_patterns = [
        r'https?://(www\.)?instagram\.com/(p|reel|stories|story)/[^/]+/?',
        r'https?://(www\.)?instagram\.com/stories/highlight/[\w_-]+/?',
        r'https?://(www\.)?instagram\.com/tv/[\w_-]+/?',
        r'https?://(www\.)?instagram\.com/reel/[\w_-]+/?'
    ]
    
    for pattern in instagram_patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False

def get_media_info_ytdlp(url):
    """Get media information without downloading with enhanced error handling"""
    try:
        # Enhanced yt-dlp options for Instagram with fallback
        ydl_opts = get_ydl_opts()
        ydl_opts.update({
            'extract_flat': False,
            'force_json': True,
            'ignoreerrors': True,
            'extractor_retries': 3,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Fetching info for: {url}")
            
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                logger.error(f"yt-dlp extraction failed: {error_msg}")
                
                # Try alternative extraction method
                if "No video formats found" in error_msg:
                    logger.info("Trying alternative extraction method...")
                    # Use different extractor args
                    ydl_opts_alt = ydl_opts.copy()
                    ydl_opts_alt['extractor_args'] = {
                        'instagram': {
                            'format': 'best',
                            'post_filter': 'image'
                        }
                    }
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts_alt) as ydl_alt:
                            info = ydl_alt.extract_info(url, download=False)
                    except Exception as alt_e:
                        raise Exception(f"Instagram extraction failed: {str(alt_e)}")
                else:
                    raise
            
            if not info:
                raise Exception("No media found at this URL. The post might be private or deleted.")
            
            # Parse upload date
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Handle carousel posts
            is_carousel = False
            media_count = 1
            carousel_media = []
            
            if info.get('_type') == 'playlist':
                is_carousel = True
                entries = info.get('entries', [])
                media_count = len(entries) if entries else 1
                
                if entries:
                    for i, entry in enumerate(entries):
                        if entry:  # Check if entry is not None
                            carousel_media.append({
                                'id': entry.get('id', f'item_{i}'),
                                'title': entry.get('title', f'Media {i+1}'),
                                'thumbnail': entry.get('thumbnail', info.get('thumbnail', '')),
                                'duration': entry.get('duration', 0),
                                'width': entry.get('width', 0),
                                'height': entry.get('height', 0),
                                'url': entry.get('url'),
                                'index': i,
                                'is_video': entry.get('duration', 0) > 0,
                                'ext': entry.get('ext', 'mp4' if entry.get('duration', 0) > 0 else 'jpg')
                            })
            else:
                # Single media item
                carousel_media.append({
                    'id': info.get('id', 'single'),
                    'title': info.get('title', 'Instagram Media'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'width': info.get('width', 0),
                    'height': info.get('height', 0),
                    'url': info.get('url'),
                    'index': 0,
                    'is_video': info.get('duration', 0) > 0,
                    'ext': info.get('ext', 'mp4' if info.get('duration', 0) > 0 else 'jpg')
                })
                media_count = 1
                is_carousel = False
            
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
            
            logger.info(f"Info retrieved: {result['title']} - {media_count} media items")
            return result
            
    except Exception as e:
        logger.error(f"Error getting media info: {str(e)}")
        raise Exception(f"Failed to fetch media information: {str(e)}")

def download_media_to_buffer(url, item_index=None):
    """Download media directly to memory buffer and return file data"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        # Try multiple format strategies
        format_strategies = [
            'best[filesize<?100M]/best',
            'best',
            'worst'  # Sometimes worst quality works when best doesn't
        ]
        
        last_error = None
        for format_strategy in format_strategies:
            try:
                ydl_opts = get_ydl_opts(download=True, output_dir=temp_dir)
                ydl_opts['format'] = format_strategy
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    logger.info(f"Downloading from: {url} with format: {format_strategy}")
                    info = ydl.extract_info(url, download=True)
                    
                    if not info:
                        continue
                    
                    # Find downloaded files
                    downloaded_files = []
                    for root, dirs, files in os.walk(temp_dir):
                        for file in files:
                            if file.endswith(('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mkv', '.avi')):
                                downloaded_files.append(os.path.join(root, file))
                    
                    if downloaded_files:
                        break
                        
            except Exception as e:
                last_error = e
                logger.warning(f"Format strategy {format_strategy} failed: {str(e)}")
                continue
        
        if not downloaded_files:
            if last_error:
                raise last_error
            else:
                raise Exception("No files downloaded with any format strategy")
        
        # Process files - read into memory
        file_data_list = []
        for downloaded_file in downloaded_files:
            filename = os.path.basename(downloaded_file)
            file_size = os.path.getsize(downloaded_file)
            
            if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                logger.warning(f"File too large: {file_size / (1024*1024):.2f}MB")
                continue
            
            # Read file into memory
            with open(downloaded_file, 'rb') as f:
                file_data = io.BytesIO(f.read())
            
            safe_filename = sanitize_filename(filename)
            file_type = 'video' if downloaded_file.endswith(('.mp4', '.mkv', '.avi')) else 'image'
            
            file_data_list.append({
                'filename': safe_filename,
                'file_data': file_data,
                'file_size': file_size,
                'type': file_type,
                'original_name': filename
            })
        
        # Clean up temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        
        if not file_data_list:
            raise Exception("No valid files downloaded")
        
        logger.info(f"Successfully downloaded {len(file_data_list)} file(s) to memory")
        
        return {
            'files': file_data_list,
            'info': info,
            'is_carousel': info.get('_type') == 'playlist' if info else False
        }
            
    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        raise e

@app.before_request
def before_request():
    """Periodic cleanup"""
    pass

# ==================== ROUTES ====================

@app.route('/')
def index():
    """Serve the main HTML page"""
    try:
        return send_from_directory('.', 'index.html')
    except FileNotFoundError:
        return """
        <!DOCTYPE html>
        <html>
        <head><title>IGDL - Instagram Downloader</title></head>
        <body>
            <h1>IGDL - Instagram Downloader</h1>
            <p>Frontend file not found. Please make sure index.html is deployed.</p>
        </body>
        </html>
        """

@app.route('/health')
def health_check():
    """Health check for monitoring"""
    cookies_ok = COOKIES_FILE_PATH is not None and os.path.exists(COOKIES_FILE_PATH)
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cookies_configured': cookies_ok,
        'cookies_path': COOKIES_FILE_PATH if cookies_ok else None,
        'storage': 'memory_buffer'
    })

@app.route('/api/media/info', methods=['POST', 'OPTIONS'])
def get_media_info():
    """Get media information without downloading"""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'status': 'error', 'message': 'URL is required'}), 400

        url = data['url'].strip()
        
        # Enhanced URL validation
        if not is_valid_instagram_url(url):
            return jsonify({
                'status': 'error', 
                'message': 'Invalid Instagram URL. Supported formats: Posts (instagram.com/p/...), Reels (instagram.com/reel/...), Stories (instagram.com/stories/...), TV (instagram.com/tv/...)'
            }), 400

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
        elif 'rate limit' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'}), 429
        elif 'not supported' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This post format is not supported. Try a different post.'}), 400
        else:
            return jsonify({'status': 'error', 'message': f'Failed to fetch media information: {error_msg}'}), 500

@app.route('/api/download', methods=['POST', 'OPTIONS'])
def download_media():
    """Download Instagram media directly to client"""
    if request.method == 'OPTIONS':
        return '', 200
        
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
        if not is_valid_instagram_url(url):
            return jsonify({'status': 'error', 'message': 'Invalid Instagram URL'}), 400

        # Check if specific item is requested
        item_index = data.get('item_index')
        
        if not COOKIES_FILE_PATH or not os.path.exists(COOKIES_FILE_PATH):
            logger.warning("Attempting download without cookies")

        # Download to memory buffer
        result = download_media_to_buffer(url, item_index)
        
        # Handle file download
        if result['files']:
            file_data = result['files'][0]
            if item_index is not None and 0 <= int(item_index) < len(result['files']):
                file_data = result['files'][int(item_index)]
            
            return send_file(
                file_data['file_data'],
                as_attachment=True,
                download_name=file_data['filename'],
                mimetype='video/mp4' if file_data['type'] == 'video' else 'image/jpeg'
            )
        else:
            raise Exception("No files available for download")

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")
        
        if 'private' in error_msg.lower() or 'login' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This content is private'}), 401
        elif 'not found' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Media not found'}), 404
        elif 'timeout' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Request timed out'}), 504
        elif 'not supported' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This post format is not supported'}), 400
        elif 'rate limit' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'}), 429
        else:
            return jsonify({'status': 'error', 'message': f'Download failed: {error_msg}'}), 500

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("Instagram Downloader API Starting")
    logger.info(f"Cookies configured: {COOKIES_FILE_PATH is not None}")
    logger.info("Storage mode: Memory buffer (no persistent files)")
    logger.info(f"Log folder: {LOG_FOLDER}")
    logger.info("=" * 50)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
