import os
import uuid
import logging
import tempfile
import shutil
import json
import re
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
    """Get yt-dlp options with enhanced Instagram support"""
    opts = {
        'quiet': True,
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
        'ignoreerrors': True,
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
        # Enhanced yt-dlp options for Instagram
        ydl_opts = get_ydl_opts()
        ydl_opts.update({
            'extract_flat': False,
            'force_json': True,
            'ignoreerrors': True,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Fetching info for: {url}")
            
            # Test if URL is accessible first
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                logger.error(f"yt-dlp extraction failed: {error_msg}")
                
                # More specific error messages
                if "Private" in error_msg or "login" in error_msg:
                    raise Exception("This content is private or requires login. Make sure you're using valid cookies.")
                elif "not found" in error_msg.lower() or "removed" in error_msg.lower():
                    raise Exception("This content is not available or has been removed from Instagram.")
                elif "URL could be wrong" in error_msg:
                    raise Exception("Invalid Instagram URL. Please check the URL and try again.")
                elif "Unsupported URL" in error_msg:
                    raise Exception("This Instagram URL format is not supported.")
                elif "No video formats found" in error_msg:
                    raise Exception("This post contains content that cannot be downloaded. Try a different post.")
                else:
                    raise Exception(f"Instagram returned an error: {error_msg}")
            
            # CRITICAL FIX: Check if info is None before processing
            if info is None:
                raise Exception("Could not extract media information. This post format may not be supported.")
            
            # Parse upload date
            upload_date = info.get('upload_date', '')
            if upload_date:
                try:
                    upload_date = datetime.strptime(upload_date, '%Y%m%d').strftime('%Y-%m-%d')
                except:
                    upload_date = 'Unknown'
            
            # Enhanced carousel detection with robust error handling
            is_carousel = False
            media_count = 1
            carousel_media = []
            
            # Check for playlist (carousel)
            if info.get('_type') == 'playlist':
                is_carousel = True
                entries = info.get('entries', [])
                # Filter out None entries and count valid ones
                valid_entries = [entry for entry in entries if entry is not None]
                media_count = len(valid_entries) if valid_entries else 1
                
                if valid_entries:
                    for i, entry in enumerate(valid_entries):
                        # Get the best available URL for each media item
                        media_url = None
                        if entry.get('url'):
                            media_url = entry.get('url')
                        elif entry.get('formats'):
                            # Get the best format URL
                            formats = entry.get('formats', [])
                            if formats:
                                best_format = formats[-1]  # Usually the last one is best
                                media_url = best_format.get('url')
                        
                        carousel_media.append({
                            'id': entry.get('id', f'item_{i}'),
                            'title': entry.get('title', f'Media {i+1}'),
                            'thumbnail': entry.get('thumbnail', info.get('thumbnail', '')),
                            'duration': entry.get('duration', 0),
                            'width': entry.get('width', 0),
                            'height': entry.get('height', 0),
                            'url': media_url,
                            'index': i,
                            'is_video': entry.get('duration', 0) > 0,
                            'ext': entry.get('ext', 'mp4' if entry.get('duration', 0) > 0 else 'jpg')
                        })
            else:
                # Single media item
                media_url = info.get('url')
                if not media_url and info.get('formats'):
                    formats = info.get('formats', [])
                    if formats:
                        best_format = formats[-1]
                        media_url = best_format.get('url')
                
                carousel_media.append({
                    'id': info.get('id', 'single'),
                    'title': info.get('title', 'Instagram Media'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'width': info.get('width', 0),
                    'height': info.get('height', 0),
                    'url': media_url,
                    'index': 0,
                    'is_video': info.get('duration', 0) > 0,
                    'ext': info.get('ext', 'mp4' if info.get('duration', 0) > 0 else 'jpg')
                })
                media_count = 1
                is_carousel = False
            
            # If no carousel media was added (all entries were None), create a basic response
            if not carousel_media:
                carousel_media.append({
                    'id': info.get('id', 'single'),
                    'title': info.get('title', 'Instagram Media'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'width': info.get('width', 0),
                    'height': info.get('height', 0),
                    'url': url,
                    'index': 0,
                    'is_video': info.get('duration', 0) > 0,
                    'ext': 'mp4' if info.get('duration', 0) > 0 else 'jpg'
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
            
            logger.info(f"Info retrieved: {result['title']} - {media_count} media items")
            return result
            
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"yt-dlp download error: {error_msg}")
        
        # Enhanced error handling for Instagram
        if "No video formats found" in error_msg:
            raise Exception("This post format is not supported. The post might contain content that cannot be downloaded (like some carousels or restricted content). Try a different post.")
        elif "not available" in error_msg.lower():
            raise Exception("This content is not available or has been deleted")
        elif "private" in error_msg.lower():
            raise Exception("This content is private or requires login")
        elif "rate limit" in error_msg.lower():
            raise Exception("Rate limit exceeded. Please try again later.")
        else:
            raise Exception(f"Failed to fetch media: {error_msg}")
    except Exception as e:
        logger.error(f"Error getting media info: {str(e)}")
        raise e

def download_media_ytdlp(url, item_index=None):
    """Download media using yt-dlp with enhanced error handling"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        
        # Enhanced yt-dlp options for better compatibility
        ydl_opts = get_ydl_opts(download=True, output_dir=temp_dir)
        ydl_opts.update({
            'format': 'best[filesize<?50M]/best',
            'ignoreerrors': True,
            'no_overwrites': True,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Downloading from: {url}")
            info = ydl.extract_info(url, download=True)
            
            # CRITICAL FIX: Check if info is None
            if info is None:
                raise Exception("Could not download media. This post format may not be supported.")
            
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
                raise Exception("No files downloaded. This post format might not be supported.")
            
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
                    'download_url': f'/download/{safe_filename}',
                    'original_name': filename,
                    'available': True
                })
            
            shutil.rmtree(temp_dir)
            
            if not results:
                raise Exception("No valid files downloaded. The post format might not be supported.")
            
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
                'duration': info.get('duration', 0),
                'is_carousel': info.get('_type') == 'playlist'
            }
            
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"yt-dlp download error: {error_msg}")
        
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            
        # Enhanced error handling
        if "No video formats found" in error_msg:
            raise Exception("This post format is not supported. Try a different post or check if the content is available. Some carousel posts may not be downloadable.")
        elif "private" in error_msg.lower():
            raise Exception("This content is private or requires login")
        elif "not available" in error_msg.lower():
            raise Exception("This content is not available or has been removed")
        else:
            raise Exception(f"Download failed: {error_msg}")
    except Exception as e:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        logger.error(f"Download error: {str(e)}")
        raise e

@app.before_request
def before_request():
    """Periodic cleanup"""
    if not hasattr(app, '_last_cleanup'):
        app._last_cleanup = datetime.now()
    
    if datetime.now() - app._last_cleanup > timedelta(minutes=30):
        cleanup_old_files()
        app._last_cleanup = datetime.now()

# ==================== API ROUTES ====================

@app.route('/')
def home():
    """API home page"""
    return jsonify({
        'status': 'running',
        'service': 'IGDL API',
        'version': '1.0.0',
        'endpoints': {
            'media_info': '/api/media/info',
            'download': '/api/download',
            'health': '/health',
            'stats': '/api/stats'
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
        elif 'not supported' in error_msg.lower() or 'no video formats' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This post format is not supported. Try a different post.'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Failed to fetch media information'}), 500

@app.route('/api/download', methods=['POST', 'OPTIONS'])
def download_media():
    """Download Instagram media"""
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

        # Download
        result = download_media_ytdlp(url, item_index)
        
        # If specific item is requested, filter results
        if item_index is not None and result.get('files'):
            item_index = int(item_index)
            if 0 <= item_index < len(result['files']):
                # Return only the requested item
                single_file = result['files'][item_index]
                return jsonify({
                    'status': 'ok',
                    'type': single_file['type'],
                    'files': [single_file],
                    'count': 1,
                    'preview_url': result.get('thumbnail', ''),
                    'title': result.get('title', ''),
                    'uploader': result.get('uploader', 'Unknown'),
                    'upload_date': result.get('upload_date', ''),
                    'like_count': result.get('like_count', 0),
                    'comment_count': result.get('comment_count', 0),
                    'is_carousel': result.get('is_carousel', False)
                })

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
            'comment_count': result.get('comment_count', 0),
            'is_carousel': result.get('is_carousel', False)
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
        elif 'not supported' in error_msg.lower() or 'no video formats' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'This post format is not supported. Try a different post.'}), 400
        elif 'rate limit' in error_msg.lower():
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded. Please try again later.'}), 429
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

# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'status': 'error', 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {str(e)}")
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

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
