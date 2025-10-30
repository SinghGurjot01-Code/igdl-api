import os
import uuid
import logging
import tempfile
import shutil
import io
import zipfile
import re
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

def get_ydl_opts(download=False, output_dir=None, format_spec=None):
    """Get yt-dlp options with enhanced Instagram support"""
    opts = {
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        },
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'extractor_args': {
            'instagram': {
                'format_types': ['image', 'video', 'carousel'],
                'post_data': 'full',
            }
        },
        'ignoreerrors': True,
        'no_overwrites': True,
    }
    
    if download and output_dir:
        opts['outtmpl'] = os.path.join(output_dir, '%(title).100s_%(playlist_index)s.%(ext)s')
        # Enhanced format selection for Instagram
        opts['format'] = format_spec or 'best[height<=1080]/best'
        opts['merge_output_format'] = 'mp4'
        # Add retry options for problematic posts
        opts['retry_sleep'] = 'exp=1:10'
    
    if COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH):
        opts['cookiefile'] = COOKIES_FILE_PATH
        logger.info("✓ Using cookies for request")
    else:
        logger.warning("⚠️ No cookies - private content may fail")
    
    return opts

def get_media_info_ytdlp(url):
    """Get media information with enhanced carousel support"""
    try:
        ydl_opts = get_ydl_opts()
        ydl_opts.update({
            'extract_flat': False,
            'force_json': True,
            'ignoreerrors': True,
            'extract_flat': 'in_playlist',
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
            
            # Enhanced carousel detection
            is_carousel = info.get('_type') == 'playlist'
            entries = info.get('entries', [])
            media_count = 1
            
            if is_carousel and entries:
                media_count = len(entries)
                # Filter out None entries
                entries = [e for e in entries if e]
                media_count = len(entries)
                logger.info(f"📸 Carousel detected with {media_count} items")
            
            # Get available formats for download options
            available_formats = []
            if not is_carousel and info.get('formats'):
                # Single media - collect available formats
                formats = info.get('formats', [])
                video_formats = [f for f in formats if f.get('vcodec') != 'none']
                if video_formats:
                    # Get unique quality options
                    quality_map = {}
                    for fmt in video_formats:
                        height = fmt.get('height', 0)
                        if height and height not in quality_map:
                            quality_map[height] = fmt
                    
                    for height in sorted(quality_map.keys(), reverse=True):
                        fmt = quality_map[height]
                        available_formats.append({
                            'format_id': fmt.get('format_id'),
                            'quality': f"{height}p",
                            'height': height,
                            'ext': fmt.get('ext', 'mp4')
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
                'available_formats': available_formats,
                'webpage_url': info.get('webpage_url', url),
            }
            
            logger.info(f"✓ Info retrieved: {result['title']} ({media_count} items, {len(available_formats)} formats)")
            return result
            
    except Exception as e:
        logger.error(f"❌ Error getting media info: {str(e)}")
        raise e

def download_media_to_buffer(url, format_spec=None):
    """Download media to memory buffer with enhanced Instagram support"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        ydl_opts = get_ydl_opts(download=True, output_dir=temp_dir, format_spec=format_spec)
        
        # Add specific Instagram extractor options
        ydl_opts.update({
            'extractor_retries': 5,
            'ignore_no_formats_error': False,
        })
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"📥 Downloading from: {url} with format: {format_spec or 'best'}")
            info = ydl.extract_info(url, download=True)
            
            if not info:
                raise Exception("No media found")
            
            # Enhanced file discovery for carousels
            downloaded_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if any(file.endswith(ext) for ext in ['.mp4', '.jpg', '.jpeg', '.png', '.webp', '.mkv', '.m4a']):
                        full_path = os.path.join(root, file)
                        # Skip very small files that might be fragments
                        if os.path.getsize(full_path) > 1024:  # At least 1KB
                            downloaded_files.append(full_path)
                        else:
                            logger.warning(f"Skipping small file: {file} ({os.path.getsize(full_path)} bytes)")
            
            if not downloaded_files:
                # If no files found, try alternative approach
                logger.warning("No files found with standard search, trying alternative...")
                # Look for any files in temp directory
                all_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) 
                           if os.path.isfile(os.path.join(temp_dir, f))]
                downloaded_files = [f for f in all_files if os.path.getsize(f) > 1024]
            
            if not downloaded_files:
                raise Exception("No valid files downloaded - possible format extraction issue")
            
            logger.info(f"📦 Found {len(downloaded_files)} valid files")
            
            # If single file, return it directly
            if len(downloaded_files) == 1:
                file_path = downloaded_files[0]
                file_size = os.path.getsize(file_path)
                
                with open(file_path, 'rb') as f:
                    file_data = io.BytesIO(f.read())
                
                filename = os.path.basename(file_path)
                # Clean filename
                safe_filename = re.sub(r'[^\w\s\.\-_]', '', filename)
                safe_filename = safe_filename.replace(' ', '_')
                
                # Determine MIME type
                if file_path.endswith(('.mp4', '.mkv', '.m4v')):
                    file_type = 'video/mp4'
                elif file_path.endswith(('.jpg', '.jpeg')):
                    file_type = 'image/jpeg'
                elif file_path.endswith('.png'):
                    file_type = 'image/png'
                elif file_path.endswith('.webp'):
                    file_type = 'image/webp'
                else:
                    file_type = 'application/octet-stream'
                
                logger.info(f"✓ Single file downloaded: {safe_filename} ({file_size / (1024*1024):.2f} MB)")
                
                return {
                    'file_data': file_data,
                    'filename': safe_filename,
                    'mimetype': file_type,
                    'size': file_size,
                    'is_zip': False
                }
            
            # If multiple files (carousel), create a ZIP file
            else:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    total_size = 0
                    for i, file_path in enumerate(downloaded_files, 1):
                        file_ext = os.path.splitext(file_path)[1]
                        safe_filename = f"instagram_{i:02d}{file_ext}"
                        zip_file.write(file_path, safe_filename)
                        file_size = os.path.getsize(file_path)
                        total_size += file_size
                        logger.info(f"  - Added to ZIP: {safe_filename} ({file_size / 1024:.1f} KB)")
                
                zip_buffer.seek(0)
                
                # Create zip filename
                uploader = info.get('uploader', 'instagram').replace(' ', '_')
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                zip_filename = f"instagram_{uploader}_{timestamp}.zip"
                safe_zip_filename = re.sub(r'[^\w\s\.\-_]', '', zip_filename)
                
                logger.info(f"✓ Carousel downloaded: {len(downloaded_files)} files in {safe_zip_filename} ({total_size / (1024*1024):.2f} MB)")
                
                return {
                    'file_data': zip_buffer,
                    'filename': safe_zip_filename,
                    'mimetype': 'application/zip',
                    'size': len(zip_buffer.getvalue()),
                    'is_zip': True
                }
            
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"❌ yt-dlp download error: {str(e)}")
        # Try fallback approach for problematic posts
        if "No video formats found" in str(e):
            logger.info("🔄 Trying fallback download approach...")
            return download_fallback(url, temp_dir)
        raise e
    except Exception as e:
        logger.error(f"❌ Download error: {str(e)}")
        raise e
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"Could not clean up temp dir: {str(e)}")

def download_fallback(url, temp_dir):
    """Fallback download method for problematic Instagram posts"""
    try:
        # Try with different format specifications
        format_options = [
            'best[height<=720]',
            'best[height<=480]', 
            'worst',
            'best'
        ]
        
        for format_spec in format_options:
            try:
                ydl_opts = get_ydl_opts(download=True, output_dir=temp_dir, format_spec=format_spec)
                ydl_opts['ignoreerrors'] = True
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    logger.info(f"🔄 Fallback attempt with format: {format_spec}")
                    ydl.download([url])
                    
                    # Check for downloaded files
                    downloaded_files = []
                    for root, dirs, files in os.walk(temp_dir):
                        for file in files:
                            if any(file.endswith(ext) for ext in ['.mp4', '.jpg', '.jpeg', '.png', '.webp']):
                                file_path = os.path.join(root, file)
                                if os.path.getsize(file_path) > 1024:
                                    downloaded_files.append(file_path)
                    
                    if downloaded_files:
                        logger.info(f"✓ Fallback successful with {format_spec}")
                        # Process files as in main function
                        if len(downloaded_files) == 1:
                            file_path = downloaded_files[0]
                            with open(file_path, 'rb') as f:
                                file_data = io.BytesIO(f.read())
                            
                            filename = f"instagram_media{os.path.splitext(file_path)[1]}"
                            return {
                                'file_data': file_data,
                                'filename': filename,
                                'mimetype': 'video/mp4' if file_path.endswith('.mp4') else 'image/jpeg',
                                'size': os.path.getsize(file_path),
                                'is_zip': False
                            }
            except Exception as fallback_error:
                logger.warning(f"Fallback {format_spec} failed: {str(fallback_error)}")
                continue
        
        raise Exception("All download attempts failed - this post may not be accessible")
    except Exception as e:
        raise e

# ==================== ROUTES ====================

@app.route('/')
def index():
    """Serve the main HTML page"""
    try:
        if os.path.exists('index.html'):
            return send_from_directory('.', 'index.html')
        else:
            return "IGDL API is running"
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
    """Download media file with format support"""
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
        format_spec = data.get('format')  # Get optional format specification
        
        logger.info(f"⬇️ Download request from {client_ip} for: {url} (format: {format_spec or 'best'})")
        
        # Download to memory
        result = download_media_to_buffer(url, format_spec)
        
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
    logger.info(f"📦 Carousel Support: ✓ Enhanced with fallback")
    logger.info("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
