#!/usr/bin/env python3
import os
import time
import threading
import requests
import hashlib
import json
import re
import subprocess
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = 'downloads'
INDEX_FILE = os.path.join(DOWNLOAD_FOLDER, 'index.json')
CHUNK_SIZE = 8192  # 8KB chunks for downloading large files
REAL_DEBRID_API_KEY = os.getenv('REAL_DEBRID_API_KEY')
REAL_DEBRID_BASE_URL = 'https://api.real-debrid.com/rest/1.0'

# Ensure download directory exists
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Store download status and file information
downloads = {}
download_lock = threading.Lock()


def is_youtube_url(url):
    """Check if URL is from YouTube"""
    youtube_patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(?:https?://)?(?:www\.)?youtu\.be/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/c/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/channel/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/@[\w-]+',
        r'(?:https?://)?(?:www\.)?stream\.cz/[\w-]+',  # add support for streamCZ
        r'(?:https?://)?(?:www\.)?vimeo\.com/[\w-]+',  # add support for Vimeo
    ]

    for pattern in youtube_patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False


def check_yt_dlp_installed():
    """Check if yt-dlp is installed and accessible"""
    try:
        result = subprocess.run(['yt-dlp', '--version'],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def save_downloads_index():
    """Save downloads dictionary to index file"""
    try:
        with download_lock:
            # Create serializable version of downloads (without manager objects)
            serializable_downloads = {}
            for download_id, info in downloads.items():
                manager = info['manager']
                serializable_downloads[download_id] = {
                    'id': download_id,
                    'url': info['url'],
                    'filename': info['filename'],
                    'status': manager.status,
                    'progress': manager.progress,
                    'error': manager.error,
                    'start_time': info['start_time'],
                    'type': info.get('type', 'direct'),
                    'file_size': getattr(manager, 'file_size', 0),
                    'downloaded_size': getattr(manager, 'downloaded_size', 0),
                    'filepath': getattr(manager, 'filepath', ''),
                    # Magnet-specific fields
                    'torrent_id': getattr(manager, 'torrent_id', None),
                    'download_links': getattr(manager, 'download_links', []),
                    'files': getattr(manager, 'files', []),
                    # YouTube-specific fields
                    'youtube_files': getattr(manager, 'youtube_files', []),
                    'video_title': getattr(manager, 'video_title', ''),
                    'video_duration': getattr(manager, 'video_duration', ''),
                }

            with open(INDEX_FILE, 'w') as f:
                json.dump(serializable_downloads, f, indent=2, default=str)

    except Exception as e:
        print(f"Error saving downloads index: {e}")


def load_downloads_index():
    """Load downloads dictionary from index file"""
    global downloads

    if not os.path.exists(INDEX_FILE):
        return

    try:
        with open(INDEX_FILE, 'r') as f:
            saved_downloads = json.load(f)

        with download_lock:
            for download_id, info in saved_downloads.items():
                # Recreate manager objects based on type and status
                if info.get('type') == 'magnet':
                    manager = MagnetDownloadManager(info['url'], download_id)
                    manager.torrent_id = info.get('torrent_id')
                    manager.download_links = info.get('download_links', [])
                    manager.files = info.get('files', [])
                elif info.get('type') == 'youtube':
                    manager = YouTubeDownloadManager(info['url'], download_id)
                    manager.youtube_files = info.get('youtube_files', [])
                    manager.video_title = info.get('video_title', '')
                    manager.video_duration = info.get('video_duration', '')
                else:
                    manager = DownloadManager(info['url'], download_id)
                    manager.file_size = info.get('file_size', 0)
                    manager.downloaded_size = info.get('downloaded_size', 0)

                # Restore manager state
                manager.status = info['status']
                manager.progress = info['progress']
                manager.error = info.get('error')
                manager.filename = info['filename']
                if info.get('filepath'):
                    manager.filepath = info['filepath']

                # Restore start time
                try:
                    manager.start_time = datetime.fromisoformat(info['start_time'])
                except:
                    manager.start_time = datetime.now()

                downloads[download_id] = {
                    'id': download_id,
                    'url': info['url'],
                    'filename': info['filename'],
                    'status': manager.status,
                    'progress': manager.progress,
                    'start_time': info['start_time'],
                    'manager': manager,
                    'type': info.get('type', 'direct')
                }

                # Resume incomplete downloads automatically
                if manager.status in ['downloading', 'processing', 'pending']:
                    print(f"Resuming {info['type']} download: {info['filename']}")
                    if info.get('type') == 'magnet':
                        thread = threading.Thread(target=manager.process_magnet)
                    elif info.get('type') == 'youtube':
                        thread = threading.Thread(target=manager.download_youtube)
                    else:
                        thread = threading.Thread(target=manager.download)
                    thread.daemon = True
                    thread.start()

        print(f"Loaded {len(saved_downloads)} downloads from index")

    except Exception as e:
        print(f"Error loading downloads index: {e}")


class RealDebridManager:
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {'Authorization': f'Bearer {api_key}'}

    def check_availability(self, magnet_link):
        """Check if torrent is available on Real-Debrid"""
        try:
            response = requests.post(
                f'{REAL_DEBRID_BASE_URL}/torrents/instantAvailability',
                headers=self.headers,
                data={'hash': self._extract_hash_from_magnet(magnet_link)}
            )
            response.raise_for_status()
            result = response.json()
            # If result is not empty, torrent is cached
            return len(result) > 0
        except:
            # If check fails, proceed anyway
            return None

    def _extract_hash_from_magnet(self, magnet_link):
        """Extract info hash from magnet link"""
        import re
        match = re.search(r'btih:([a-fA-F0-9]+)', magnet_link)
        return match.group(1).lower() if match else None

    def add_magnet(self, magnet_link):
        """Add magnet link to Real-Debrid"""
        response = requests.post(
            f'{REAL_DEBRID_BASE_URL}/torrents/addMagnet',
            headers=self.headers,
            data={'magnet': magnet_link}
        )
        response.raise_for_status()
        return response.json()

    def get_torrent_info(self, torrent_id):
        """Get information about a torrent"""
        response = requests.get(
            f'{REAL_DEBRID_BASE_URL}/torrents/info/{torrent_id}',
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def select_files(self, torrent_id, file_ids='all'):
        """Select files from torrent to download"""
        response = requests.post(
            f'{REAL_DEBRID_BASE_URL}/torrents/selectFiles/{torrent_id}',
            headers=self.headers,
            data={'files': file_ids}
        )
        response.raise_for_status()

    def unrestrict_link(self, link):
        """Unrestrict a download link"""
        response = requests.post(
            f'{REAL_DEBRID_BASE_URL}/unrestrict/link',
            headers=self.headers,
            data={'link': link}
        )
        response.raise_for_status()
        return response.json()


class YouTubeDownloadManager:
    def __init__(self, url, download_id):
        self.url = url
        self.download_id = download_id
        self.filename = f"youtube_{download_id}"
        self.status = 'pending'
        self.progress = 0
        self.error = None
        self.start_time = datetime.now()
        self.youtube_files = []
        self.video_title = ''
        self.video_duration = ''
        self.filepath = None

    def download_youtube(self):
        """Download YouTube video in both MP4 and MP3 formats"""
        try:
            self.status = 'processing'
            self.error = "Getting video information..."
            save_downloads_index()

            # First, get video information
            info_cmd = [
                'yt-dlp',
                '--print', '%(title)s',
                '--print', '%(duration_string)s',
                '--print', '%(id)s',
                self.url
            ]

            result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                raise Exception(f"Failed to get video info: {result.stderr}")

            lines = result.stdout.strip().split('\n')
            if len(lines) >= 3:
                self.video_title = lines[0]
                self.video_duration = lines[1]
                video_id = lines[2]
                # Clean filename for filesystem
                clean_title = "".join(c for c in self.video_title if c.isalnum() or c in (' ', '-', '_')).rstrip()
                self.filename = f"{clean_title} ({video_id})"

            save_downloads_index()

            mp4_success = False
            mp3_success = False
            download_errors = []

            # Download MP4 (video)
            self.error = "Downloading MP4..."
            self.progress = 25
            save_downloads_index()

            try:
                mp4_filename = f"{self.download_id}_video.%(ext)s"
                mp4_filepath = os.path.join(DOWNLOAD_FOLDER, mp4_filename)

                # Try different video format selectors for better compatibility
                mp4_cmd = [
                    'yt-dlp',
                    '-f', 'best[height<=1080][ext=mp4]/best[height<=720][ext=mp4]/best[ext=mp4]/best',
                    '-o', mp4_filepath,
                    '--no-playlist',
                    '--merge-output-format', 'mp4',
                    self.url
                ]

                print(f"Running MP4 command: {' '.join(mp4_cmd)}")
                mp4_process = subprocess.run(mp4_cmd, capture_output=True, text=True, timeout=1800)

                print(f"MP4 return code: {mp4_process.returncode}")
                if mp4_process.stdout:
                    print(f"MP4 stdout: {mp4_process.stdout}")
                if mp4_process.stderr:
                    print(f"MP4 stderr: {mp4_process.stderr}")

                if mp4_process.returncode == 0:
                    # Find the actual downloaded file
                    mp4_actual_path = None
                    for file in os.listdir(DOWNLOAD_FOLDER):
                        if file.startswith(f"{self.download_id}_video."):
                            mp4_actual_path = os.path.join(DOWNLOAD_FOLDER, file)
                            break

                    if mp4_actual_path and os.path.exists(mp4_actual_path):
                        self.youtube_files.append({
                            'type': 'video',
                            'format': 'MP4',
                            'filepath': mp4_actual_path,
                            'filename': f"{clean_title}.mp4",
                            'size': os.path.getsize(mp4_actual_path)
                        })
                        mp4_success = True
                        print(f"MP4 download successful: {mp4_actual_path}")
                    else:
                        download_errors.append("MP4 file not found after download")
                else:
                    download_errors.append(
                        f"MP4 download failed with return code {mp4_process.returncode}: {mp4_process.stderr}")

            except subprocess.TimeoutExpired:
                download_errors.append("MP4 download timed out")
            except Exception as e:
                download_errors.append(f"MP4 download error: {str(e)}")

            # Update progress
            self.progress = 50
            save_downloads_index()

            # Download MP3 (audio only)
            self.error = "Downloading MP3..."
            self.progress = 75
            save_downloads_index()

            try:
                mp3_filename = f"{self.download_id}_audio.%(ext)s"
                mp3_filepath = os.path.join(DOWNLOAD_FOLDER, mp3_filename)

                mp3_cmd = [
                    'yt-dlp',
                    '-f', 'bestaudio/best',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '192K',
                    '-o', mp3_filepath,
                    '--no-playlist',
                    self.url
                ]

                print(f"Running MP3 command: {' '.join(mp3_cmd)}")
                mp3_process = subprocess.run(mp3_cmd, capture_output=True, text=True, timeout=1800)

                print(f"MP3 return code: {mp3_process.returncode}")
                if mp3_process.stdout:
                    print(f"MP3 stdout: {mp3_process.stdout}")
                if mp3_process.stderr:
                    print(f"MP3 stderr: {mp3_process.stderr}")

                if mp3_process.returncode == 0:
                    # Find the actual downloaded file
                    mp3_actual_path = None
                    for file in os.listdir(DOWNLOAD_FOLDER):
                        if file.startswith(f"{self.download_id}_audio.") and file.endswith('.mp3'):
                            mp3_actual_path = os.path.join(DOWNLOAD_FOLDER, file)
                            break

                    if mp3_actual_path and os.path.exists(mp3_actual_path):
                        self.youtube_files.append({
                            'type': 'audio',
                            'format': 'MP3',
                            'filepath': mp3_actual_path,
                            'filename': f"{clean_title}.mp3",
                            'size': os.path.getsize(mp3_actual_path)
                        })
                        mp3_success = True
                        print(f"MP3 download successful: {mp3_actual_path}")
                    else:
                        download_errors.append("MP3 file not found after download")
                else:
                    download_errors.append(
                        f"MP3 download failed with return code {mp3_process.returncode}: {mp3_process.stderr}")

            except subprocess.TimeoutExpired:
                download_errors.append("MP3 download timed out")
            except Exception as e:
                download_errors.append(f"MP3 download error: {str(e)}")

            # Determine final status
            if mp4_success and mp3_success:
                self.status = 'completed'
                self.progress = 100
                self.error = None
            elif mp4_success or mp3_success:
                self.status = 'completed'
                self.progress = 100
                self.error = f"Partial success. Errors: {'; '.join(download_errors)}"
            else:
                raise Exception(f"Both downloads failed. Errors: {'; '.join(download_errors)}")

            save_downloads_index()

        except subprocess.TimeoutExpired:
            self.status = 'failed'
            self.error = "Download timed out (30 minutes)"
            save_downloads_index()
        except Exception as e:
            self.status = 'failed'
            self.error = str(e)
            save_downloads_index()


class MagnetDownloadManager:
    def __init__(self, magnet_link, download_id):
        self.magnet_link = magnet_link
        self.download_id = download_id
        self.filename = f"magnet_{download_id}"
        self.status = 'pending'
        self.progress = 0
        self.error = None
        self.start_time = datetime.now()
        self.torrent_id = None
        self.download_links = []
        self.files = []
        self.rd_manager = RealDebridManager(REAL_DEBRID_API_KEY)
        self.filepath = None

    def process_magnet(self):
        """Process magnet link through Real-Debrid"""
        try:
            self.status = 'processing'
            save_downloads_index()  # Save state change

            # Add magnet to Real-Debrid (skip if torrent_id already exists - resuming)
            if not self.torrent_id:
                result = self.rd_manager.add_magnet(self.magnet_link)
                self.torrent_id = result['id']
                save_downloads_index()  # Save torrent_id

            # Check status indefinitely - no timeout for long downloads
            check_count = 0
            while True:
                try:
                    torrent_info = self.rd_manager.get_torrent_info(self.torrent_id)
                    check_count += 1

                    # Update status message based on torrent status
                    torrent_status = torrent_info.get('status', 'unknown')

                    if torrent_status == 'downloaded':
                        # Select all files if not already done
                        if not torrent_info.get('links'):
                            self.rd_manager.select_files(self.torrent_id)
                            # Wait a bit for the selection to take effect
                            time.sleep(2)
                            # Get updated torrent info with links
                            torrent_info = self.rd_manager.get_torrent_info(self.torrent_id)

                        self.files = torrent_info.get('files', [])

                        # Extract filename from torrent
                        if self.files:
                            self.filename = torrent_info.get('filename', f"magnet_{self.download_id}")

                        # Get unrestricted links
                        links = torrent_info.get('links', [])
                        if links:
                            self.download_links = []  # Clear existing links
                            for link in links:
                                try:
                                    unrestricted = self.rd_manager.unrestrict_link(link)
                                    self.download_links.append({
                                        'filename': unrestricted.get('filename', 'unknown'),
                                        'download': unrestricted.get('download'),
                                        'filesize': unrestricted.get('filesize', 0)
                                    })
                                except Exception as e:
                                    print(f"Failed to unrestrict link {link}: {e}")
                                    continue

                            self.status = 'ready'
                            self.progress = 100
                            save_downloads_index()  # Save completion
                            break
                        else:
                            self.error = "Torrent downloaded but no links available yet - waiting..."

                    elif torrent_status in ['magnet_error', 'error', 'virus']:
                        raise Exception(f"Torrent error: {torrent_status}")
                    elif torrent_status == 'dead':
                        # Check if it's really dead or just slow
                        seeders = torrent_info.get('seeders', 0)
                        if seeders == 0 and check_count > 12:  # No seeders after 1 minute
                            raise Exception(f"Torrent appears to be dead (no seeders)")
                        else:
                            self.error = f"Status: {torrent_status} - Seeders: {seeders} - Waiting for seeders..."
                    elif torrent_status == 'waiting_files_selection':
                        # Auto-select all files and wait for confirmation
                        self.error = f"Selecting files automatically..."
                        try:
                            self.rd_manager.select_files(self.torrent_id)
                            self.error = f"Files selected - waiting for processing to continue..."
                        except Exception as e:
                            self.error = f"Failed to select files: {e}"
                            # Continue waiting, might resolve itself
                    elif torrent_status in ['queued', 'downloading', 'compressing']:
                        # Update progress based on download progress if available
                        progress = torrent_info.get('progress', 0)
                        self.progress = int(progress)

                        # Add detailed status info
                        seeders = torrent_info.get('seeders', 0)
                        speed = torrent_info.get('speed', 0)
                        size = torrent_info.get('bytes', 0)
                        downloaded = torrent_info.get('bytes_done', 0)

                        # Format speed
                        speed_str = self._format_speed(speed)

                        # Calculate ETA if speed > 0
                        eta_str = ""
                        if speed > 0 and size > downloaded:
                            eta_seconds = (size - downloaded) / speed
                            eta_str = f" - ETA: {self._format_time(eta_seconds)}"

                        self.error = f"Status: {torrent_status} - Progress: {progress}% - Seeders: {seeders} - Speed: {speed_str}{eta_str}"

                        # Save progress periodically
                        if check_count % 5 == 0:  # Save every 5 checks
                            save_downloads_index()
                    else:
                        # Unknown status, continue waiting
                        self.error = f"Status: {torrent_status} - Check #{check_count} - Waiting..."

                    # Wait before next check - longer intervals for long downloads
                    if check_count < 12:  # First minute: check every 5 seconds
                        time.sleep(5)
                    elif check_count < 60:  # First 5 minutes: check every 10 seconds
                        time.sleep(10)
                    else:  # After 5 minutes: check every 30 seconds
                        time.sleep(30)

                except requests.exceptions.RequestException as e:
                    # API error - wait and retry
                    self.error = f"API Error (will retry): {str(e)}"
                    time.sleep(60)  # Wait 1 minute before retrying on API errors
                    continue

        except Exception as e:
            self.status = 'failed'
            self.error = str(e)
            save_downloads_index()  # Save failure state

    def _format_speed(self, bytes_per_second):
        """Format speed in human readable format"""
        if bytes_per_second == 0:
            return "0 B/s"

        units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
        unit_index = 0
        speed = float(bytes_per_second)

        while speed >= 1024 and unit_index < len(units) - 1:
            speed /= 1024
            unit_index += 1

        return f"{speed:.1f} {units[unit_index]}"

    def _format_time(self, seconds):
        """Format time in human readable format"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        elif seconds < 86400:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
        else:
            days = int(seconds // 86400)
            hours = int((seconds % 86400) // 3600)
            return f"{days}d {hours}h"


class DownloadManager:
    def __init__(self, url, download_id):
        self.url = url
        self.download_id = download_id
        self.filename = self._get_filename_from_url(url)
        self.filepath = os.path.join(DOWNLOAD_FOLDER, f"{download_id}_{self.filename}")
        self.status = 'pending'
        self.progress = 0
        self.error = None
        self.start_time = datetime.now()
        self.file_size = 0
        self.downloaded_size = 0

    def _get_filename_from_url(self, url):
        """Extract filename from URL or generate one"""
        parsed = urlparse(url)
        filename = os.path.basename(unquote(parsed.path))
        if not filename or '.' not in filename:
            # Generate filename from URL hash
            filename = f"download_{hashlib.md5(url.encode()).hexdigest()[:8]}.bin"
        return filename

    def download(self):
        """Download file in chunks with progress tracking"""
        try:
            self.status = 'downloading'
            save_downloads_index()  # Save state change

            # Check if file already exists and get its size for resuming
            resume_header = {}
            if os.path.exists(self.filepath):
                self.downloaded_size = os.path.getsize(self.filepath)
                resume_header['Range'] = f'bytes={self.downloaded_size}-'
                print(f"Resuming download from byte {self.downloaded_size}")

            # Try to establish connection with retries
            max_attempts = 120  # 10 minutes max (5 seconds between attempts)
            response = None

            for attempt in range(max_attempts):
                try:
                    response = requests.get(self.url, stream=True, timeout=30, headers=resume_header)

                    # Handle partial content response
                    if response.status_code == 206:  # Partial content
                        print("Server supports resume - continuing download")
                    elif response.status_code == 200 and resume_header:
                        # Server doesn't support resume, start over
                        print("Server doesn't support resume - starting over")
                        self.downloaded_size = 0
                        if os.path.exists(self.filepath):
                            os.remove(self.filepath)

                    response.raise_for_status()
                    break
                except requests.exceptions.ConnectionError as e:
                    self.error = f"Connection attempt {attempt + 1}/{max_attempts} failed"
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(5)
                except requests.exceptions.Timeout:
                    self.error = f"Timeout attempt {attempt + 1}/{max_attempts}"
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(5)

            if not response:
                raise Exception("Failed to connect after all attempts")

            # Get file size if available
            content_length = response.headers.get('content-length')
            if content_length:
                if response.status_code == 206:
                    # For partial content, add the range to existing downloaded size
                    content_range = response.headers.get('content-range', '')
                    if '/' in content_range:
                        self.file_size = int(content_range.split('/')[-1])
                else:
                    self.file_size = int(content_length)

            self.error = None  # Clear any connection attempt messages

            # Download with timeout handling
            last_progress_time = time.time()
            stall_timeout = 300  # 5 minutes without progress
            save_counter = 0

            # Open file in append mode if resuming, write mode if starting fresh
            mode = 'ab' if self.downloaded_size > 0 else 'wb'

            with open(self.filepath, mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        self.downloaded_size += len(chunk)
                        if self.file_size > 0:
                            self.progress = int((self.downloaded_size / self.file_size) * 100)

                        # Reset stall timer on progress
                        last_progress_time = time.time()

                        # Save progress periodically
                        save_counter += 1
                        if save_counter % 1000 == 0:  # Save every 1000 chunks
                            save_downloads_index()
                    else:
                        # Check for stalled download
                        if time.time() - last_progress_time > stall_timeout:
                            raise Exception("Download stalled - no progress for 5 minutes")

            self.status = 'completed'
            self.progress = 100
            save_downloads_index()  # Save completion

        except requests.exceptions.RequestException as e:
            self.status = 'failed'
            self.error = str(e)
            save_downloads_index()
            # Don't clean up partial file - allow resume
        except Exception as e:
            self.status = 'failed'
            self.error = f"Unexpected error: {str(e)}"
            save_downloads_index()
            # Don't clean up partial file - allow resume


def start_download(url):
    """Start a new download in a separate thread"""
    download_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:12]

    # Check if it's a YouTube URL
    if is_youtube_url(url):
        return start_youtube_download(url)

    with download_lock:
        manager = DownloadManager(url, download_id)
        downloads[download_id] = {
            'id': download_id,
            'url': url,
            'filename': manager.filename,
            'status': manager.status,
            'progress': manager.progress,
            'start_time': manager.start_time.isoformat(),
            'manager': manager,
            'type': 'direct'
        }

    save_downloads_index()  # Save new download

    # Start download in background thread
    thread = threading.Thread(target=manager.download)
    thread.daemon = True
    thread.start()

    return download_id


def start_youtube_download(url):
    """Start processing a YouTube URL"""
    if not check_yt_dlp_installed():
        raise Exception("yt-dlp is not installed. Please install it with: pip install yt-dlp")

    download_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:12]

    with download_lock:
        manager = YouTubeDownloadManager(url, download_id)
        downloads[download_id] = {
            'id': download_id,
            'url': url,
            'filename': manager.filename,
            'status': manager.status,
            'progress': manager.progress,
            'start_time': manager.start_time.isoformat(),
            'manager': manager,
            'type': 'youtube'
        }

    save_downloads_index()  # Save new download

    # Start processing in background thread
    thread = threading.Thread(target=manager.download_youtube)
    thread.daemon = True
    thread.start()

    return download_id


def start_magnet_download(magnet_link):
    """Start processing a magnet link through Real-Debrid"""
    if not REAL_DEBRID_API_KEY:
        raise Exception("Real-Debrid API key not configured")

    download_id = hashlib.md5(f"{magnet_link}{time.time()}".encode()).hexdigest()[:12]

    with download_lock:
        manager = MagnetDownloadManager(magnet_link, download_id)
        downloads[download_id] = {
            'id': download_id,
            'url': magnet_link,
            'filename': manager.filename,
            'status': manager.status,
            'progress': manager.progress,
            'start_time': manager.start_time.isoformat(),
            'manager': manager,
            'type': 'magnet'
        }

    save_downloads_index()  # Save new download

    # Start processing in background thread
    thread = threading.Thread(target=manager.process_magnet)
    thread.daemon = True
    thread.start()

    return download_id


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Check if Real-Debrid API and yt-dlp are configured"""
    return jsonify({
        'real_debrid_configured': bool(REAL_DEBRID_API_KEY),
        'yt_dlp_available': check_yt_dlp_installed()
    })


@app.route('/download', methods=['POST'])
def download_url():
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    # Basic URL validation
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL. Must start with http:// or https://'}), 400

    try:
        download_id = start_download(url)
        return jsonify({'success': True, 'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/magnet', methods=['POST'])
def download_magnet():
    data = request.get_json()
    magnet_link = data.get('magnet', '').strip()

    if not magnet_link:
        return jsonify({'error': 'Magnet link is required'}), 400

    # Basic magnet link validation
    if not magnet_link.startswith('magnet:?'):
        return jsonify({'error': 'Invalid magnet link'}), 400

    if not REAL_DEBRID_API_KEY:
        return jsonify({'error': 'Real-Debrid API key not configured'}), 500

    try:
        download_id = start_magnet_download(magnet_link)
        return jsonify({'success': True, 'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/rd-links/<download_id>')
def get_rd_links(download_id):
    """Get Real-Debrid download links for a processed magnet"""
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        download_info = downloads[download_id]
        if download_info.get('type') != 'magnet':
            return jsonify({'error': 'Not a magnet download'}), 400

        manager = download_info['manager']
        if manager.status != 'ready':
            return jsonify({'error': 'Magnet not ready yet'}), 400

        return jsonify({
            'filename': manager.filename,
            'links': manager.download_links
        })


@app.route('/youtube-files/<download_id>')
def get_youtube_files(download_id):
    """Get YouTube download files for a completed download"""
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        download_info = downloads[download_id]
        if download_info.get('type') != 'youtube':
            return jsonify({'error': 'Not a YouTube download'}), 400

        manager = download_info['manager']
        if manager.status != 'completed':
            return jsonify({'error': 'YouTube download not completed yet'}), 400

        return jsonify({
            'video_title': manager.video_title,
            'video_duration': manager.video_duration,
            'files': manager.youtube_files
        })


@app.route('/status')
def get_status():
    with download_lock:
        status_data = {}
        for download_id, info in downloads.items():
            manager = info['manager']
            data = {
                'id': download_id,
                'filename': info['filename'],
                'status': manager.status,
                'progress': manager.progress,
                'error': manager.error,
                'start_time': info['start_time'],
                'type': info.get('type', 'direct')
            }

            # Add Real-Debrid specific info for magnets
            if info.get('type') == 'magnet' and hasattr(manager, 'download_links'):
                data['download_links'] = manager.download_links
                data['files_count'] = len(manager.download_links)

            # Add YouTube specific info
            if info.get('type') == 'youtube' and hasattr(manager, 'youtube_files'):
                data['youtube_files'] = manager.youtube_files
                data['video_title'] = getattr(manager, 'video_title', '')
                data['video_duration'] = getattr(manager, 'video_duration', '')

            status_data[download_id] = data
    return jsonify({'downloads': status_data})


@app.route('/download/<download_id>')
def download_file(download_id):
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        manager = downloads[download_id]['manager']
        if manager.status != 'completed':
            return jsonify({'error': 'Download not completed'}), 400

        if not os.path.exists(manager.filepath):
            return jsonify({'error': 'File not found'}), 404

    return send_file(manager.filepath, as_attachment=True, download_name=manager.filename)


@app.route('/download-youtube/<download_id>/<file_type>')
def download_youtube_file(download_id, file_type):
    """Download specific YouTube file (mp3 or mp4)"""
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        download_info = downloads[download_id]
        if download_info.get('type') != 'youtube':
            return jsonify({'error': 'Not a YouTube download'}), 400

        manager = download_info['manager']
        if manager.status != 'completed':
            return jsonify({'error': 'YouTube download not completed'}), 400

        # Find the requested file type
        target_file = None
        for file_info in manager.youtube_files:
            if file_type == 'mp3' and file_info['type'] == 'audio':
                target_file = file_info
                break
            elif file_type == 'mp4' and file_info['type'] == 'video':
                target_file = file_info
                break

        if not target_file:
            return jsonify({'error': f'{file_type.upper()} file not found'}), 404

        if not os.path.exists(target_file['filepath']):
            return jsonify({'error': 'File not found on disk'}), 404

    return send_file(target_file['filepath'], as_attachment=True, download_name=target_file['filename'])


@app.route('/delete/<download_id>', methods=['DELETE'])
def delete_file(download_id):
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        manager = downloads[download_id]['manager']

        # Delete files based on download type
        if downloads[download_id].get('type') == 'youtube':
            # Delete all YouTube files
            for file_info in getattr(manager, 'youtube_files', []):
                if os.path.exists(file_info['filepath']):
                    try:
                        os.remove(file_info['filepath'])
                    except Exception as e:
                        print(f"Failed to delete YouTube file {file_info['filepath']}: {e}")
        else:
            # Delete regular file if it exists
            if manager.filepath is not None:
                if os.path.exists(manager.filepath):
                    try:
                        os.remove(manager.filepath)
                    except Exception as e:
                        return jsonify({'error': f'Failed to delete file: {str(e)}'}), 500

        # Remove from downloads dict
        del downloads[download_id]

    save_downloads_index()  # Save deletion
    return jsonify({'success': True})


@app.route('/torrents/active')
def get_active_torrents():
    """Get list of active torrents from Real-Debrid"""
    if not REAL_DEBRID_API_KEY:
        return jsonify({'error': 'Real-Debrid API key not configured'}), 500

    try:
        rd_manager = RealDebridManager(REAL_DEBRID_API_KEY)
        response = requests.get(
            f'{REAL_DEBRID_BASE_URL}/torrents',
            headers=rd_manager.headers,
            params={'limit': 100}
        )
        response.raise_for_status()
        torrents = response.json()

        # Filter and format active torrents
        active_torrents = []
        for torrent in torrents:
            if torrent['status'] in ['downloading', 'queued', 'waiting_files_selection']:
                active_torrents.append({
                    'id': torrent['id'],
                    'filename': torrent.get('filename', 'Unknown'),
                    'status': torrent['status'],
                    'progress': torrent.get('progress', 0),
                    'seeders': torrent.get('seeders', 0),
                    'size': torrent.get('bytes', 0),
                    'added': torrent.get('added', '')
                })

        return jsonify({'torrents': active_torrents})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/resume/<torrent_id>', methods=['POST'])
def resume_torrent_monitoring(torrent_id):
    """Resume monitoring an existing Real-Debrid torrent"""
    if not REAL_DEBRID_API_KEY:
        return jsonify({'error': 'Real-Debrid API key not configured'}), 500

    try:
        # Create a new download entry for this torrent
        download_id = hashlib.md5(f"resume_{torrent_id}_{time.time()}".encode()).hexdigest()[:12]

        with download_lock:
            # Create a special manager that resumes monitoring
            manager = MagnetDownloadManager(f"resumed_torrent_{torrent_id}", download_id)
            manager.torrent_id = torrent_id  # Set the existing torrent ID

            downloads[download_id] = {
                'id': download_id,
                'url': f"Real-Debrid Torrent: {torrent_id}",
                'filename': f"torrent_{torrent_id}",
                'status': 'processing',
                'progress': 0,
                'start_time': datetime.now().isoformat(),
                'manager': manager,
                'type': 'magnet'
            }

        save_downloads_index()  # Save resumed download

        # Start monitoring in background thread
        thread = threading.Thread(target=manager.process_magnet)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # Load existing downloads on startup
    print("Loading downloads index...")
    load_downloads_index()

    # Check if yt-dlp is available
    if check_yt_dlp_installed():
        print("yt-dlp is available - YouTube downloads enabled")
    else:
        print("yt-dlp not found - YouTube downloads will be disabled")
        print("To enable YouTube downloads, install yt-dlp with: pip install yt-dlp")

    # For production on Debian, you might want to use gunicorn instead
    # Example: gunicorn -w 4 -b 0.0.0.0:8000 app:app
    app.run(debug=True, host='0.0.0.0', port=5000)