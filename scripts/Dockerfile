FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install PostgreSQL client, cron, rclone, and timezone data
RUN apk add --no-cache \
    postgresql-client \
    busybox-suid \
    rclone \
    tzdata \
 && ln -snf /usr/share/zoneinfo/Europe/Istanbul /etc/localtime \
 && echo "Europe/Istanbul" > /etc/timezone

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all scripts
COPY backup.py mkdb.py restore.py ./

# Configure cron: Daily at 02:00 Istanbul time (output to stdout for docker logs)
RUN echo '0 2 * * * python /app/backup.py 2>&1' > /etc/crontabs/root

# Create necessary directories
RUN mkdir -p /root/.config/rclone

# Start cron daemon (log level 2 = show job starts/ends)
CMD ["crond", "-f", "-l", "2"]
