"""
URL to MP3 / MP4 Converter — Flask Backend
Requirements:
    pip install flask yt-dlp flask-cors
    sudo apt install ffmpeg   (or: brew install ffmpeg on macOS)

Run:
    python app.py
Then open http://localhost:5000 in your browser.
"""

import os
import re
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

FFMPEG_PATH = r"C:\Users\TNC\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR = Path(os.path.expanduser("~/Downloads/MediaConverter"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = Path(__file__).parent / "cookies.txt"

# Track active jobs  {job_id: {status, progress, filename, error}}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

class YDLDebugLogger:
    """Redirects yt-dlp internal logs to the console for better debugging."""
    def debug(self, msg):
        if msg.startswith('[debug] '): print(msg)
    def warning(self, msg): print(f"WARNING: {msg}")
    def error(self, msg): print(f"ERROR: {msg}")


def format_error(exc: Exception) -> str:
    """Maps technical yt-dlp errors to user-friendly messages."""
    msg = str(exc)
    if "Unsupported URL" in msg:
        return "This site is not natively supported. Try searching the Network tab for '.m3u8' or '.mpd' stream links and paste that instead."
    if "Failed to extract media information" in msg:
        return "Extraction failed: The website's video player is either encrypted (DRM) or uses a format that cannot be scraped directly. Please try a different source."
    if "Invalid data found" in msg or "Error opening input files" in msg or "unable to obtain file audio codec" in msg:
        return "The video stream is unreadable or has expired. This often happens with temporary links. Please get a fresh link from the Network tab and try again immediately."
    if "DPAPI" in msg:
        return "Cookie access failed: Please close Google Chrome and try again, or export cookies to a text file."
    if "video is unavailable" in msg.lower():
        return "The video is unavailable. It might be private or deleted."
    return msg


def safe_filename(name: str) -> str:
    """Strip characters that are problematic in file names."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def build_output_template(job_id: str) -> str:
    """Prefix outputs with the job id so we can reliably find the final file."""
    return str(DOWNLOAD_DIR / f"{job_id}__%(title)s.%(ext)s")


def safe_extract_info(ydl, url, download=False):
    """
    A robust wrapper for extract_info that handles cookie locks and 
    falls back to the generic extractor if necessary.
    """
    info = None
    try:
        # Attempt 1: Standard extraction
        info = ydl.extract_info(url, download=download)
    except Exception as e:
        # Step 1: Handle Cookie Lock (DPAPI) or common cookie errors
        if "DPAPI" in str(e) or "cookie" in str(e).lower():
            ydl.params.pop('cookiesfrombrowser', None)
            try:
                info = ydl.extract_info(url, download=download)
            except Exception as e2: e = e2
        
        # Step 2: Handle Unsupported URL (Force Generic Scraper)
        if info is None and "Unsupported URL" in str(e):
            ydl.params['force_generic_extractor'] = True
            info = ydl.extract_info(url, download=download)
        
        if info is None: raise e
    return info


def find_downloaded_file(job_id: str, ext: str) -> Path | None:
    """Locate the actual converted file created for this job."""
    matches = sorted(
        DOWNLOAD_DIR.glob(f"{job_id}__*.{ext}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def make_progress_hook(job_id: str):
    def hook(d):
        with jobs_lock:
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "0%").strip().rstrip("%")
                try:
                    jobs[job_id]["progress"] = float(pct)
                except ValueError:
                    pass
                jobs[job_id]["speed"] = d.get("_speed_str", "")
                jobs[job_id]["eta"] = d.get("_eta_str", "")
            elif d["status"] == "finished":
                jobs[job_id]["progress"] = 99
                jobs[job_id]["status"] = "processing"
    return hook


def run_download(job_id: str, url: str, fmt: str, quality: str):
    """Runs in a background thread."""
    try:
        ydl_opts = build_ydl_opts(job_id, url, fmt, quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = safe_extract_info(ydl, url, download=True)

            if info is None:
                raise Exception("Failed to extract media information.")

            ext = "mp3" if fmt == "mp3" else "mp4"
            actual_path = find_downloaded_file(job_id, ext)
            title = safe_filename(info.get("title", "media"))
            filename = f"{title}.{ext}"

            if actual_path is None:
                raise FileNotFoundError(
                    f"Converted file was not found in {DOWNLOAD_DIR}"
                )

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["filename"] = filename
            jobs[job_id]["filepath"] = str(actual_path)

    except Exception as exc:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = format_error(exc)


def get_base_ydl_opts(url: str = None) -> dict:
    """Common configuration for yt-dlp."""
    return {
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "noplaylist": True,
        "logger": YDLDebugLogger(),
        "nocheckcertificate": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        # Smart Referer: If it's a player API, use the main site as referer
        "referer": (
            "https://www.sabayflix.net/" 
            if url and "sf-api.net" in url 
            else (url if url and url.startswith("http") else "https://www.google.com/")
        ),
        "http_headers": {
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
            "Connection": "keep-alive",
        },
        # Use cookies.txt if it exists, otherwise try Chrome (fallback logic handles the lock)
        "cookiefile": str(COOKIES_FILE) if COOKIES_FILE.exists() else None,
        "cookiesfrombrowser": None if COOKIES_FILE.exists() else ("chrome",),
        
        "extractor_args": {"generic": ["impersonate", "navigate"]}, # More aggressive browser spoofing
        "extract_flat": "in_playlist",
        "playlist_items": "1",
        "check_formats": "cached",
        "geo_bypass": True,
        "socket_timeout": 30,
        "ignoreerrors": False,
        "wait_for_video": (1, 5),
    }


def build_ydl_opts(job_id: str, url: str, fmt: str, quality: str) -> dict:
    outtmpl = build_output_template(job_id)

    if fmt == "mp3":
        audio_quality = {
            "best": "0",
            "high": "2",
            "medium": "5",
            "low": "7",
        }.get(quality, "2")

        opts = get_base_ydl_opts(url)
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "progress_hooks": [make_progress_hook(job_id)],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
            "postprocessor_args": {
                "ffmpeg": ["-analyzeduration", "1000000", "-probesize", "1000000"]
            },
        })
        return opts
    else:  # mp4
        opts = get_base_ydl_opts(url)
        opts.update({
            "format": "bestvideo+bestaudio/best",
            "outtmpl": outtmpl,
            "progress_hooks": [make_progress_hook(job_id)],
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
            "merge_output_format": "mp4",
            "postprocessor_args": {
                "ffmpeg": ["-analyzeduration", "1000000", "-probesize", "1000000"]
            },
        })
        return opts


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the HTML frontend (place index.html next to app.py)."""
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h2>Put index.html next to app.py and restart.</h2>", 404


@app.route("/api/info", methods=["POST"])
def get_info():
    """Fetch video title & available formats without downloading."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        with yt_dlp.YoutubeDL(get_base_ydl_opts(url)) as ydl:
            info = safe_extract_info(ydl, url, download=False)

        if info is None:
            raise Exception("Failed to extract video information.")

        return jsonify({
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration_string") or str(info.get("duration", "")),
            "uploader": info.get("uploader", ""),
        })
    except Exception as exc:
        return jsonify({"error": format_error(exc)}), 400


@app.route("/api/convert", methods=["POST"])
def convert():
    """Start a background download/conversion job."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    fmt = data.get("format", "mp3").lower()
    quality = data.get("quality", "high").lower()

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Format must be mp3 or mp4"}), 400

    import uuid
    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "filename": None,
            "filepath": None,
            "speed": "",
            "eta": "",
            "error": None,
        }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, fmt, quality),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    """Poll conversion status."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/download/<job_id>")
def download(job_id: str):
    """Stream the converted file to the browser."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    filepath = Path(job["filepath"])
    if not filepath.exists():
        return jsonify({"error": "File missing on disk"}), 404

    mime = "audio/mpeg" if filepath.suffix == ".mp3" else "video/mp4"
    return send_file(
        filepath,
        mimetype=mime,
        as_attachment=True,
        download_name=job.get("filename") or filepath.name,
    )


@app.route("/api/save-location")
def save_location():
    """Return the local save directory path."""
    return jsonify({"path": str(DOWNLOAD_DIR)})


if __name__ == "__main__":
    print(f"\n✅  Files will be saved to: {DOWNLOAD_DIR}\n")
    print("🚀  Server running at http://localhost:5000\n")
    app.run(debug=True, port=5000)
