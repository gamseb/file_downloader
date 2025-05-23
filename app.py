#!/usr/bin/env python3
import os
import time
import threading
import requests
import hashlib
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = 'downloads'
CHUNK_SIZE = 8192  # 8KB chunks for downloading large files
REAL_DEBRID_API_KEY = os.getenv('REAL_DEBRID_API_KEY')
REAL_DEBRID_BASE_URL = 'https://api.real-debrid.com/rest/1.0'

# Ensure download directory exists
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Store download status and file information
downloads = {}
download_lock = threading.Lock()


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

    def process_magnet(self):
        """Process magnet link through Real-Debrid"""
        try:
            self.status = 'processing'

            # Add magnet to Real-Debrid
            result = self.rd_manager.add_magnet(self.magnet_link)
            self.torrent_id = result['id']

            # Check status indefinitely - no timeout for long downloads
            check_count = 0
            while True:
                try:
                    torrent_info = self.rd_manager.get_torrent_info(self.torrent_id)
                    check_count += 1

                    # Update status message based on torrent status
                    torrent_status = torrent_info.get('status', 'unknown')

                    if torrent_status == 'downloaded':
                        # Select all files
                        self.rd_manager.select_files(self.torrent_id)

                        # Get updated torrent info with links
                        torrent_info = self.rd_manager.get_torrent_info(self.torrent_id)
                        self.files = torrent_info.get('files', [])

                        # Extract filename from torrent
                        if self.files:
                            self.filename = torrent_info.get('filename', f"magnet_{self.download_id}")

                        # Get unrestricted links
                        links = torrent_info.get('links', [])
                        for link in links:
                            unrestricted = self.rd_manager.unrestrict_link(link)
                            self.download_links.append({
                                'filename': unrestricted.get('filename', 'unknown'),
                                'download': unrestricted.get('download'),
                                'filesize': unrestricted.get('filesize', 0)
                            })

                        self.status = 'ready'
                        self.progress = 100
                        break
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
                        # Auto-select all files
                        self.rd_manager.select_files(self.torrent_id)
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
                    else:
                        # Unknown status, continue waiting
                        self.error = f"Status: {torrent_status} - Check #{check_count}"

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

            # Try to establish connection with retries
            max_attempts = 120  # 10 minutes max (5 seconds between attempts)
            response = None

            for attempt in range(max_attempts):
                try:
                    response = requests.get(self.url, stream=True, timeout=30)
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
            self.file_size = int(response.headers.get('content-length', 0))
            self.error = None  # Clear any connection attempt messages

            # Download with timeout handling
            last_progress_time = time.time()
            stall_timeout = 300  # 5 minutes without progress

            with open(self.filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        self.downloaded_size += len(chunk)
                        if self.file_size > 0:
                            self.progress = int((self.downloaded_size / self.file_size) * 100)

                        # Reset stall timer on progress
                        last_progress_time = time.time()
                    else:
                        # Check for stalled download
                        if time.time() - last_progress_time > stall_timeout:
                            raise Exception("Download stalled - no progress for 5 minutes")

            self.status = 'completed'
            self.progress = 100

        except requests.exceptions.RequestException as e:
            self.status = 'failed'
            self.error = str(e)
            # Clean up partial file
            if os.path.exists(self.filepath):
                os.remove(self.filepath)
        except Exception as e:
            self.status = 'failed'
            self.error = f"Unexpected error: {str(e)}"
            if os.path.exists(self.filepath):
                os.remove(self.filepath)

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
            response = requests.get(self.url, stream=True, timeout=30)
            response.raise_for_status()

            # Get file size if available
            self.file_size = int(response.headers.get('content-length', 0))

            with open(self.filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        self.downloaded_size += len(chunk)
                        if self.file_size > 0:
                            self.progress = int((self.downloaded_size / self.file_size) * 100)

            self.status = 'completed'
            self.progress = 100

        except requests.exceptions.RequestException as e:
            self.status = 'failed'
            self.error = str(e)
            # Clean up partial file
            if os.path.exists(self.filepath):
                os.remove(self.filepath)
        except Exception as e:
            self.status = 'failed'
            self.error = f"Unexpected error: {str(e)}"
            if os.path.exists(self.filepath):
                os.remove(self.filepath)


def start_download(url):
    """Start a new download in a separate thread"""
    download_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:12]

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

    # Start download in background thread
    thread = threading.Thread(target=manager.download)
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
    """Check if Real-Debrid API is configured"""
    return jsonify({
        'real_debrid_configured': bool(REAL_DEBRID_API_KEY)
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


@app.route('/delete/<download_id>', methods=['DELETE'])
def delete_file(download_id):
    with download_lock:
        if download_id not in downloads:
            return jsonify({'error': 'Download not found'}), 404

        manager = downloads[download_id]['manager']

        # Delete file if it exists
        if os.path.exists(manager.filepath):
            try:
                os.remove(manager.filepath)
            except Exception as e:
                return jsonify({'error': f'Failed to delete file: {str(e)}'}), 500

        # Remove from downloads dict
        del downloads[download_id]

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

        # Start monitoring in background thread
        thread = threading.Thread(target=manager.process_magnet)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # For production on Debian, you might want to use gunicorn instead
    # Example: gunicorn -w 4 -b 0.0.0.0:8000 app:app
    app.run(debug=True, host='0.0.0.0', port=5000)