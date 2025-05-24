# File Downloader

A Flask-based web application for downloading files from multiple sources

## Supported Sources

- YouTube (videos, playlists, channels)
- Vimeo
- Stream.cz
- Direct HTTP/HTTPS links
- Magnet links (via Real-Debrid)

## Quick Start with Docker

1. **Clone and setup**:
   ```bash
   git clone <repository>
   cd <directory>
   ```

2. **Create environment file** (optional for Real-Debrid):
   ```bash
   echo "REAL_DEBRID_API_KEY=your_api_key_here" > .env
   ```

3. **Run with Docker Compose**:
   ```bash
   sudo docker compose up -d
   ```

4. **Access the application**:
   Open http://localhost:5000
