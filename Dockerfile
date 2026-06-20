FROM python:3.12-slim

# Install sqlite3 and CA certificates (required for secure connections)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create the persistent directory for data (mounted as a volume in Fly.io)
RUN mkdir -p /data

# Set environmental default variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV DB_PATH=/data/garmin_data.db
ENV GARMIN_TOKEN_STORE=/data/.garmin_tokens
ENV BACKUP_DIR=/data/backups

# Expose port
EXPOSE 8080

# Run the telegram bot with its built-in scheduler
CMD ["python", "bot.py"]
