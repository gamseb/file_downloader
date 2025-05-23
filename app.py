#!/usr/bin/env python3
import os
import time
import threading
import requests
import hashlib
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
from urllib.parse import urlparse, unquote

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = 'downloads'
CHUNK_SIZE = 8192  # 8KB chunks for downloading large files

# Ensure download directory exists
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Store download status and file information
downloads = {}
download_lock = threading.Lock()


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
            'manager': manager
        }

    # Start download in background thread
    thread = threading.Thread(target=manager.download)
    thread.daemon = True
    thread.start()

    return download_id


@app.route('/')
def index():
    return render_template('index.html')


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


@app.route('/status')
def get_status():
    with download_lock:
        status_data = {}
        for download_id, info in downloads.items():
            manager = info['manager']
            status_data[download_id] = {
                'id': download_id,
                'filename': info['filename'],
                'status': manager.status,
                'progress': manager.progress,
                'error': manager.error,
                'start_time': info['start_time']
            }
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


if __name__ == '__main__':
    # For production on Debian, you might want to use gunicorn instead
    # Example: gunicorn -w 4 -b 0.0.0.0:8000 app:app
    app.run(debug=True, host='0.0.0.0', port=5000)