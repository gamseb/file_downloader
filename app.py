#!/usr/bin/env python3
import os
import time
import threading
import requests
import hashlib
from flask import Flask, render_template_string, request, jsonify, send_file
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


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>File Downloader</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            margin-bottom: 30px;
        }
        .input-group {
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
        }
        input[type="text"] {
            flex: 1;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
        }
        button {
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        .btn-primary {
            background-color: #007bff;
            color: white;
        }
        .btn-primary:hover {
            background-color: #0056b3;
        }
        .btn-success {
            background-color: #28a745;
            color: white;
        }
        .btn-danger {
            background-color: #dc3545;
            color: white;
        }
        .btn-danger:hover {
            background-color: #c82333;
        }
        .file-list {
            margin-top: 20px;
        }
        .file-item {
            background-color: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 4px;
            padding: 15px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .file-info {
            flex: 1;
        }
        .file-name {
            font-weight: bold;
            margin-bottom: 5px;
        }
        .file-status {
            font-size: 14px;
            color: #666;
        }
        .file-actions {
            display: flex;
            gap: 10px;
        }
        .progress-bar {
            width: 100%;
            height: 20px;
            background-color: #e9ecef;
            border-radius: 4px;
            margin-top: 5px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            background-color: #007bff;
            transition: width 0.3s;
        }
        .status-downloading {
            color: #007bff;
        }
        .status-completed {
            color: #28a745;
        }
        .status-failed {
            color: #dc3545;
        }
        .empty-state {
            text-align: center;
            color: #666;
            padding: 40px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>File Downloader</h1>

        <div class="input-group">
            <input type="text" id="urlInput" placeholder="Paste download URL here..." />
            <button class="btn-primary" onclick="startDownload()">Download</button>
        </div>

        <div class="file-list" id="fileList">
            <div class="empty-state">No files yet. Paste a URL above to start downloading.</div>
        </div>
    </div>

    <script>
        let downloads = {};

        async function startDownload() {
            const urlInput = document.getElementById('urlInput');
            const url = urlInput.value.trim();

            if (!url) {
                alert('Please enter a URL');
                return;
            }

            try {
                const response = await fetch('/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ url: url })
                });

                const data = await response.json();

                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    urlInput.value = '';
                    updateFileList();
                }
            } catch (error) {
                alert('Error starting download: ' + error);
            }
        }

        async function deleteFile(downloadId) {
            if (!confirm('Are you sure you want to delete this file?')) {
                return;
            }

            try {
                const response = await fetch(`/delete/${downloadId}`, {
                    method: 'DELETE'
                });

                const data = await response.json();

                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    updateFileList();
                }
            } catch (error) {
                alert('Error deleting file: ' + error);
            }
        }

        async function updateFileList() {
            try {
                const response = await fetch('/status');
                const data = await response.json();
                downloads = data.downloads;

                const fileList = document.getElementById('fileList');

                if (Object.keys(downloads).length === 0) {
                    fileList.innerHTML = '<div class="empty-state">No files yet. Paste a URL above to start downloading.</div>';
                    return;
                }

                let html = '';
                for (const [id, file] of Object.entries(downloads)) {
                    html += `
                        <div class="file-item">
                            <div class="file-info">
                                <div class="file-name">${file.filename}</div>
                                <div class="file-status status-${file.status}">
                                    Status: ${file.status}
                                    ${file.status === 'downloading' ? `(${file.progress}%)` : ''}
                                    ${file.error ? `- ${file.error}` : ''}
                                </div>
                                ${file.status === 'downloading' ? `
                                    <div class="progress-bar">
                                        <div class="progress-fill" style="width: ${file.progress}%"></div>
                                    </div>
                                ` : ''}
                            </div>
                            <div class="file-actions">
                                ${file.status === 'completed' ? `
                                    <button class="btn-success" onclick="window.location.href='/download/${id}'">Download</button>
                                ` : ''}
                                <button class="btn-danger" onclick="deleteFile('${id}')">Delete</button>
                            </div>
                        </div>
                    `;
                }

                fileList.innerHTML = html;
            } catch (error) {
                console.error('Error updating file list:', error);
            }
        }

        // Update file list every 2 seconds
        setInterval(updateFileList, 2000);

        // Initial update
        updateFileList();

        // Allow Enter key to start download
        document.getElementById('urlInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                startDownload();
            }
        });
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


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