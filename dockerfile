# Use Python 3.11 slim image based on Debian
FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Install system dependencies including ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Create downloads directory and cache directory
RUN mkdir -p downloads .cache

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser --no-create-home
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Set environment variables for yt-dlp cache
ENV XDG_CACHE_HOME=/app/.cache
ENV HOME=/app

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5000/ || exit 1

# Run the application with explicit cache directory for yt-dlp
CMD ["python", "app.py"]