from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import yt_dlp
import os
import re
import uuid
import json
import time
import shutil
import subprocess
import requests
from urllib.parse import urlparse, quote
import traceback
import threading
from datetime import datetime

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_LOCATION = r"C:\ffmpeg\bin"  # ⚠️ CHANGE THIS TO YOUR REAL PATH

# Global job storage with detailed progress tracking
JOBS = {}
JOB_LOCK = threading.Lock()

def sanitize_url(url: str) -> str:
    """Clean and normalize URL"""
    url = (url or "").strip()
    url = re.sub(r"^x+https?://", "https://", url)
    return url


def safe_filename(name: str) -> str:
    """Create safe filename by removing invalid characters"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


def detect_platform(url: str) -> str:
    """Detect social media platform from URL"""
    url_lower = url.lower()
    
    if "instagram.com" in url_lower:
        return "instagram"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower:
        return "facebook"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "twitter.com" in url_lower or "x.com" in url_lower or "t.co" in url_lower:
        return "twitter"
    elif "linkedin.com" in url_lower:
        return "linkedin"
    elif "snapchat.com" in url_lower or "snap.com" in url_lower:
        return "snapchat"
    else:
        return "unknown"


def get_ffmpeg_path() -> str | None:
    """Find ffmpeg executable"""
    if FFMPEG_LOCATION:
        candidate = os.path.join(FFMPEG_LOCATION, "ffmpeg.exe")
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(FFMPEG_LOCATION, "ffmpeg")
        if os.path.exists(candidate):
            return candidate
    return shutil.which("ffmpeg")


def update_job_progress(job_id: str, status: str, percent: int = None, 
                       downloaded_bytes: int = None, total_bytes: int = None,
                       speed: str = None, filename: str = None, error: str = None):
    """Update job progress in a thread-safe way"""
    with JOB_LOCK:
        if job_id in JOBS:
            if percent is not None:
                JOBS[job_id]["percent"] = percent
            if status:
                JOBS[job_id]["status"] = status
            if downloaded_bytes is not None:
                JOBS[job_id]["downloaded_bytes"] = downloaded_bytes
            if total_bytes is not None:
                JOBS[job_id]["total_bytes"] = total_bytes
            if speed is not None:
                JOBS[job_id]["speed"] = speed
            if filename is not None:
                JOBS[job_id]["filename"] = filename
            if error is not None:
                JOBS[job_id]["error"] = error
            
            # Update timestamp
            JOBS[job_id]["last_update"] = datetime.now().isoformat()
            
            # Calculate speed if we have downloaded bytes
            if downloaded_bytes is not None and "start_time" in JOBS[job_id]:
                elapsed_time = time.time() - JOBS[job_id]["start_time"]
                if elapsed_time > 0:
                    current_speed = downloaded_bytes / elapsed_time
                    if current_speed > 1024 * 1024:
                        JOBS[job_id]["speed"] = f"{current_speed/(1024*1024):.1f} MB/s"
                    else:
                        JOBS[job_id]["speed"] = f"{current_speed/1024:.1f} KB/s"


def download_image_directly(url: str, info: dict, job_id: str) -> str:
    """
    Download image directly using requests
    Used for Instagram photos and other image content
    """
    update_job_progress(job_id, "downloading", 50)
    
    # Try to get the best image URL
    image_url = None
    
    # Priority order for getting image URL
    if info.get("thumbnail"):
        image_url = info["thumbnail"]
    elif info.get("thumbnails") and len(info["thumbnails"]) > 0:
        # Get the largest/last thumbnail (usually highest quality)
        thumbnails = sorted(info["thumbnails"], key=lambda x: x.get("preference", 0) or 0)
        image_url = thumbnails[-1].get("url")
    elif info.get("url"):
        image_url = info["url"]
    
    if not image_url:
        raise Exception("No image URL found in media information")
    
    update_job_progress(job_id, "downloading", 60)
    
    # Create filename
    title = info.get("title") or info.get("id") or "image"
    media_id = info.get("id", uuid.uuid4().hex[:8])
    safe_title = safe_filename(title)
    
    # Initial filename with .jpg extension
    filename = f"{safe_title}_{media_id}.jpg"
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    
    update_job_progress(job_id, "downloading", 70)
    
    # Download image with proper headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    response = requests.get(image_url, stream=True, headers=headers, timeout=30)
    response.raise_for_status()
    
    # Get total size for progress calculation
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    
    # Detect actual content type and adjust extension
    content_type = response.headers.get('content-type', '').lower()
    if 'png' in content_type:
        filepath = filepath.replace('.jpg', '.png')
        filename = filename.replace('.jpg', '.png')
    elif 'webp' in content_type:
        filepath = filepath.replace('.jpg', '.webp')
        filename = filename.replace('.jpg', '.webp')
    elif 'gif' in content_type:
        filepath = filepath.replace('.jpg', '.gif')
        filename = filename.replace('.jpg', '.gif')
    
    update_job_progress(job_id, "downloading", 80, downloaded, total_size)
    
    # Write file with progress tracking
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                # Update progress more frequently
                if total_size > 0:
                    percent = int(downloaded * 100 / total_size)
                    # Keep progress between 80-95%
                    adjusted_percent = 80 + int(percent * 0.15)
                    update_job_progress(job_id, "downloading", adjusted_percent, downloaded, total_size)
    
    update_job_progress(job_id, "downloading", 95)
    
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        raise Exception("Image download failed - file is empty")
    
    return filepath


def get_cookies_for_platform(platform: str) -> dict:
    """
    Get platform-specific cookies/headers configuration
    """
    configs = {
        "facebook": {
            "cookiefile": None,
            "extractor_args": {
                "facebook": {
                    "skip_dash_manifest": True
                }
            }
        },
        "twitter": {
            "extractor_args": {
                "twitter": {
                    "api": ["graphql"]
                }
            }
        },
        "instagram": {
            "extractor_args": {
                "instagram": {
                    "skip_dash_manifest": True
                }
            }
        }
    }
    
    return configs.get(platform, {})


def create_ydl_progress_hook(job_id: str):
    """Create a progress hook that updates job progress frequently"""
    def hook(d):
        if job_id not in JOBS:
            return

        status = d.get("status")

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes")
            
            # Calculate download speed
            speed = d.get("speed")
            speed_str = None
            if speed:
                if speed > 1024 * 1024:
                    speed_str = f"{speed/(1024*1024):.1f} MB/s"
                else:
                    speed_str = f"{speed/1024:.1f} KB/s"
            
            if total and downloaded:
                pct = int(downloaded * 100 / total)
                # Ensure we show progress from 20% to 90%
                adjusted_pct = max(20, min(90, pct))
                update_job_progress(job_id, "downloading", adjusted_pct, downloaded, total, speed_str)
            elif downloaded:
                # Even if we don't know total, update downloaded bytes
                update_job_progress(job_id, "downloading", downloaded_bytes=downloaded, speed=speed_str)
            else:
                # Fallback: increment slowly
                current_pct = JOBS[job_id].get("percent", 20)
                new_pct = min(85, current_pct + 1)
                update_job_progress(job_id, "downloading", new_pct, speed=speed_str)

        elif status == "finished":
            update_job_progress(job_id, "processing", 92)
            
        elif status == "postprocessing":
            update_job_progress(job_id, "processing", 95)
            
        elif status == "error":
            error_msg = d.get("error", "Unknown error")
            update_job_progress(job_id, "error", error=error_msg)

    return hook


def make_ydl_opts(job_id: str, media_type: str, platform: str) -> dict:
    """
    Build yt-dlp options based on platform and media type
    """
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title).150s_%(id)s.%(ext)s")

    # Base options
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [create_ydl_progress_hook(job_id)],
        "ffmpeg_location": FFMPEG_LOCATION,
        "nocheckcertificate": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        # Enable more verbose progress
        "verbose": True,
        "progress": True,
    }

    # Add platform-specific configurations
    platform_config = get_cookies_for_platform(platform)
    opts.update(platform_config)

    media_type_lower = media_type.lower()

    # ==================== AUDIO ONLY ====================
    if media_type_lower in ["audio", "audio only", "audio-only"]:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })

    # ==================== PHOTO ====================
    elif media_type_lower == "photo":
        opts.update({
            "format": "best",
            "writethumbnail": True,
            "skip_download": True,
        })

    # ==================== VIDEO / REEL / SHORTS ====================
    else:
        if platform == "youtube":
            opts.update({
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            })
        
        elif platform == "facebook":
            opts.update({
                "format": "best",
                "merge_output_format": "mp4",
            })
        
        elif platform == "twitter":
            opts.update({
                "format": "best",
            })
        
        elif platform == "instagram":
            opts.update({
                "format": "best",
            })
        
        elif platform == "linkedin":
            opts.update({
                "format": "best",
            })
        
        elif platform == "snapchat":
            opts.update({
                "format": "best",
            })
        
        else:
            opts.update({
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            })

    return opts


def find_downloaded_file(prepared_path: str, media_type: str, download_dir: str = DOWNLOAD_DIR) -> str | None:
    """
    Find the actual downloaded file (yt-dlp may change extension after processing)
    """
    media_type_lower = media_type.lower()
    
    # Check if prepared path exists
    if prepared_path and os.path.exists(prepared_path):
        return prepared_path

    # Get base name without extension
    if prepared_path:
        base, _ext = os.path.splitext(prepared_path)
    else:
        base = None

    # Define possible extensions based on media type
    if media_type_lower in ["audio", "audio only", "audio-only"]:
        extensions = [".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"]
    elif media_type_lower == "photo":
        extensions = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    else:  # video
        extensions = [".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv"]

    # Check each possible extension if we have a base path
    if base:
        for ext in extensions:
            candidate = base + ext
            if os.path.exists(candidate):
                return candidate

    # Last resort: check recent files in download directory
    try:
        import glob
        all_files = glob.glob(os.path.join(download_dir, "*"))
        
        # Filter by media type extensions
        filtered_files = [
            f for f in all_files 
            if os.path.splitext(f)[1].lower() in extensions
        ]
        
        if filtered_files:
            # Get most recent file (created within last 2 minutes)
            recent_files = [
                f for f in filtered_files
                if time.time() - os.path.getctime(f) < 120
            ]
            
            if recent_files:
                latest_file = max(recent_files, key=os.path.getctime)
                return latest_file
    except Exception as e:
        print(f"Error finding recent files: {e}")
    
    return None


@app.route("/", methods=["GET"])
def home():
    """Root endpoint - health check"""
    return jsonify({
        "status": "OK",
        "message": "mediaDL Backend is running",
        "version": "2.0",
        "endpoints": {
            "download": "/download [POST]",
            "progress": "/progress/<job_id> [GET]",
            "files": "/files/<filename> [GET]",
            "debug": "/debug/ffmpeg [GET]"
        }
    })


@app.route("/debug/ffmpeg", methods=["GET"])
def debug_ffmpeg():
    """Debug endpoint to check ffmpeg installation"""
    return jsonify({
        "configured_ffmpeg_location": FFMPEG_LOCATION,
        "ffmpeg_found": shutil.which("ffmpeg"),
        "ffprobe_found": shutil.which("ffprobe"),
        "download_directory": DOWNLOAD_DIR,
        "download_dir_exists": os.path.exists(DOWNLOAD_DIR)
    })


@app.route("/download", methods=["POST"])
def download():
    """Main download endpoint"""
    job_id = None
    try:
        data = request.json or {}
        url = sanitize_url(data.get("url", ""))
        media_type = data.get("mediaType", "Video")

        # Validation
        if not url:
            return jsonify({"status": "error", "message": "URL is required"}), 400

        if not url.startswith(("http://", "https://")):
            return jsonify({"status": "error", "message": "Invalid URL format. URL must start with http:// or https://"}), 400

        # Detect platform
        platform = detect_platform(url)
        
        if platform == "unknown":
            return jsonify({
                "status": "error", 
                "message": "Unsupported platform. Please use Instagram, Facebook, YouTube, Twitter/X, LinkedIn, or Snapchat URLs."
            }), 400

        # Create job with detailed tracking
        job_id = str(uuid.uuid4())
        with JOB_LOCK:
            JOBS[job_id] = {
                "status": "queued",
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "speed": "0 KB/s",
                "filename": None,
                "error": None,
                "platform": platform,
                "media_type": media_type,
                "start_time": time.time(),
                "last_update": datetime.now().isoformat()
            }

        print(f"\n{'='*60}")
        print(f"New Download Job: {job_id}")
        print(f"Platform: {platform}")
        print(f"Media Type: {media_type}")
        print(f"URL: {url}")
        print(f"{'='*60}\n")

        update_job_progress(job_id, "starting", 10)

        # Build yt-dlp options
        ydl_opts = make_ydl_opts(job_id, media_type, platform)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            update_job_progress(job_id, "extracting info", 20)
            
            # Extract info first (without downloading)
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as extract_error:
                error_msg = str(extract_error)
                
                # Handle specific errors
                if "private" in error_msg.lower():
                    raise Exception("This content is private and cannot be downloaded")
                elif "not available" in error_msg.lower() or "unavailable" in error_msg.lower():
                    raise Exception("This content is not available or has been removed")
                elif "login" in error_msg.lower() or "sign in" in error_msg.lower():
                    raise Exception("This content requires login. Please use public content.")
                elif "unsupported url" in error_msg.lower():
                    raise Exception(f"This URL is not supported for {platform}")
                else:
                    raise Exception(f"Cannot access this content: {error_msg}")
            
            if not info:
                raise Exception("Could not extract media information from URL")
            
            update_job_progress(job_id, "extracting info", 35)

            # ==================== PHOTO HANDLING ====================
            if media_type.lower() == "photo":
                # Check if this is actually a photo/image
                is_image = False
                
                # Multiple checks for image content
                file_ext = info.get("ext", "").lower()
                vcodec = info.get("vcodec", "")
                
                if file_ext in ["jpg", "jpeg", "png", "webp", "gif"]:
                    is_image = True
                elif vcodec == "none" and info.get("thumbnail"):
                    is_image = True
                elif platform == "instagram" and ("/p/" in url or "img" in url):
                    is_image = True
                elif info.get("thumbnail") and not info.get("url"):
                    is_image = True
                
                # Try to download image
                try:
                    final_path = download_image_directly(url, info, job_id)
                except Exception as img_error:
                    # If image download fails, it might be a video
                    if not is_image:
                        update_job_progress(job_id, "error", error="This URL contains a video, not a photo")
                        return jsonify({
                            "status": "error",
                            "message": f"This URL contains a video, not a photo. Please select 'Video' or 'Reel' instead."
                        }), 400
                    else:
                        raise Exception(f"Failed to download photo: {str(img_error)}")

            # ==================== VIDEO/AUDIO HANDLING ====================
            else:
                update_job_progress(job_id, "preparing", 45)
                
                # Download the media
                try:
                    info = ydl.extract_info(url, download=True)
                except Exception as download_error:
                    error_msg = str(download_error)
                    
                    if "format" in error_msg.lower():
                        raise Exception("No suitable video/audio format found for this content")
                    else:
                        raise Exception(f"Download failed: {error_msg}")
                
                # Get the prepared filename
                prepared = ydl.prepare_filename(info)
                
                update_job_progress(job_id, "finding file", 90)
                
                # Find the actual downloaded file
                final_path = find_downloaded_file(prepared, media_type)

                if not final_path:
                    # Try one more time with just the download directory
                    import glob
                    recent = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
                    if recent:
                        final_path = max(recent, key=os.path.getctime)
                    
                    if not final_path or not os.path.exists(final_path):
                        raise Exception(f"Download completed but {media_type} file not found. Please try again.")

            # Verify file exists and has content
            if not os.path.exists(final_path):
                raise Exception("Download completed but file not found on server")
            
            file_size = os.path.getsize(final_path)
            if file_size == 0:
                raise Exception("Downloaded file is empty")

            print(f"✓ Downloaded file: {final_path} ({file_size} bytes)")

            # Get filename and sanitize
            filename = os.path.basename(final_path)
            safe_name = safe_filename(filename)

            # Rename if needed
            if safe_name != filename:
                new_path = os.path.join(DOWNLOAD_DIR, safe_name)
                try:
                    if os.path.exists(new_path):
                        # Add timestamp to avoid conflicts
                        name, ext = os.path.splitext(safe_name)
                        safe_name = f"{name}_{int(time.time())}{ext}"
                        new_path = os.path.join(DOWNLOAD_DIR, safe_name)
                    
                    os.rename(final_path, new_path)
                    final_path = new_path
                    filename = safe_name
                except Exception as e:
                    print(f"Rename warning: {e}")
                    filename = os.path.basename(final_path)

            update_job_progress(job_id, "done", 100, filename=filename)

            print(f"✓ Job completed: {job_id}")
            print(f"✓ File ready: {filename}\n")

            return jsonify({
                "status": "success",
                "job_id": job_id,
                "filename": filename,
                "download_url": f"http://127.0.0.1:5000/files/{quote(filename, safe='')}",
                "platform": platform,
                "media_type": media_type,
                "file_size": file_size
            })

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        print(f"✗ yt-dlp error: {error_msg}")
        
        if job_id:
            update_job_progress(job_id, "error", error=error_msg)
        
        # Provide user-friendly error messages
        if "private" in error_msg.lower():
            error_msg = "This content is private and cannot be downloaded"
        elif "not available" in error_msg.lower() or "video unavailable" in error_msg.lower():
            error_msg = "This content is not available or has been removed"
        elif "login" in error_msg.lower() or "sign in" in error_msg.lower():
            error_msg = "This content requires login and cannot be downloaded"
        elif "unsupported url" in error_msg.lower():
            error_msg = f"This URL format is not supported"
        
        return jsonify({"status": "error", "message": error_msg}), 400
        
    except Exception as e:
        error_msg = str(e)
        print(f"✗ Error: {error_msg}")
        print(traceback.format_exc())
        
        if job_id:
            update_job_progress(job_id, "error", error=error_msg)
        
        return jsonify({"status": "error", "message": error_msg}), 500


@app.route("/progress/<job_id>", methods=["GET"])
def progress(job_id):
    """Server-Sent Events endpoint for download progress"""
    def event_stream():
        if job_id not in JOBS:
            yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found'})}\n\n"
            return

        last_sent = None
        start_time = time.time()
        timeout_seconds = 60 * 10  # 10 minutes timeout
        last_progress_time = time.time()
        
        # Send initial state
        job = JOBS.get(job_id)
        if job:
            initial_data = {
                "status": job["status"],
                "percent": job["percent"],
                "downloaded_bytes": job.get("downloaded_bytes", 0),
                "total_bytes": job.get("total_bytes", 0),
                "speed": job.get("speed", "0 KB/s"),
                "filename": job["filename"],
                "error": job["error"],
                "message": "Starting download..." if job["status"] == "starting" else None
            }
            yield f"data: {json.dumps(initial_data)}\n\n"
            last_sent = json.dumps(initial_data)

        while True:
            job = JOBS.get(job_id)
            if not job:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found'})}\n\n"
                break

            # Calculate estimated time if we have progress
            estimated_time = None
            if job["percent"] > 0 and job["percent"] < 100 and "start_time" in job:
                elapsed = time.time() - job["start_time"]
                if elapsed > 0 and job["percent"] > 5:
                    total_estimated = elapsed / (job["percent"] / 100)
                    remaining = total_estimated - elapsed
                    if remaining > 0:
                        if remaining > 60:
                            estimated_time = f"{int(remaining/60)}m {int(remaining%60)}s"
                        else:
                            estimated_time = f"{int(remaining)}s"

            payload = {
                "status": job["status"],
                "percent": job["percent"],
                "downloaded_bytes": job.get("downloaded_bytes", 0),
                "total_bytes": job.get("total_bytes", 0),
                "speed": job.get("speed", "0 KB/s"),
                "filename": job["filename"],
                "error": job["error"],
                "estimated_time": estimated_time,
                "message": get_status_message(job["status"], job["percent"])
            }

            encoded = json.dumps(payload)
            
            # Send update if:
            # 1. Data changed OR
            # 2. It's been more than 1 second since last update (for smooth progress)
            if encoded != last_sent or time.time() - last_progress_time > 1:
                yield f"data: {encoded}\n\n"
                last_sent = encoded
                last_progress_time = time.time()

            # Check if job is complete
            if job["status"] in ("done", "error"):
                # Send final update
                if job["status"] == "done":
                    payload["percent"] = 100
                    payload["message"] = "Download complete!"
                yield f"data: {json.dumps(payload)}\n\n"
                break

            # Check timeout
            if time.time() - start_time > timeout_seconds:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Download timeout - please try again'})}\n\n"
                break

            time.sleep(0.3)  # More frequent updates

    return Response(event_stream(), mimetype="text/event-stream")


def get_status_message(status: str, percent: int) -> str:
    """Get user-friendly status message"""
    messages = {
        "queued": "Waiting to start...",
        "starting": "Starting download...",
        "extracting info": "Extracting media information...",
        "preparing": "Preparing download...",
        "downloading": f"Downloading... {percent}%",
        "processing": "Processing media file...",
        "finding file": "Finalizing download...",
        "done": "Download complete!",
        "error": "An error occurred"
    }
    
    if status in messages:
        return messages[status]
    
    return f"Processing... {percent}%"


@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    """Serve downloaded files"""
    try:
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        
        if not os.path.exists(file_path):
            return jsonify({
                "status": "error",
                "message": "File not found"
            }), 404
        
        return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)
    
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Error serving file: {str(e)}"
        }), 500


@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return jsonify({
        "status": "error",
        "message": "Endpoint not found. Please check the API documentation.",
        "available_endpoints": {
            "root": "GET /",
            "download": "POST /download",
            "progress": "GET /progress/<job_id>",
            "files": "GET /files/<filename>",
            "debug": "GET /debug/ffmpeg"
        }
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    return jsonify({
        "status": "error",
        "message": "Internal server error. Please try again later."
    }), 500


# Cleanup old jobs periodically
def cleanup_old_jobs():
    """Remove old completed jobs from memory"""
    while True:
        time.sleep(300)  # Run every 5 minutes
        with JOB_LOCK:
            current_time = time.time()
            to_delete = []
            for job_id, job in JOBS.items():
                if job["status"] in ("done", "error"):
                    # Remove jobs older than 1 hour
                    if current_time - job.get("start_time", current_time) > 3600:
                        to_delete.append(job_id)
            
            for job_id in to_delete:
                del JOBS[job_id]
                print(f"Cleaned up old job: {job_id}")


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
cleanup_thread.start()


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print(" " * 20 + "mediaDL Backend Server")
    print("=" * 70)
    print(f"✓ Download Directory: {DOWNLOAD_DIR}")
    print(f"✓ FFmpeg Location: {FFMPEG_LOCATION}")
    print(f"✓ Server URL: http://127.0.0.1:5000")
    print(f"✓ Server URL: http://localhost:5000")
    print("=" * 70)
    print("Server is ready to accept download requests...")
    print("=" * 70 + "\n")
    
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)