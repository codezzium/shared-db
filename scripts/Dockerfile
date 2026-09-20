FROM python:3.14.7-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install PostgreSQL client, cron, rclone, and timezone data
RUN apk add --no-cache \
    postgresql18-client \
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
COPY backup.py backupctl.py bootstrap.py clone.py common.py garage_admin.py garage_init.py metadb.py mkdb.py mkredis.py mks3.py restore.py rmdb.py rmredis.py rms3.py ./

# Configure cron: Daily at 02:00 Istanbul time (output to stdout for docker logs)
RUN echo '0 2 * * * python /app/backup.py 2>&1' > /etc/crontabs/root

# Create necessary directories
RUN mkdir -p /root/.config/rclone

# Start cron daemon (log level 2 = show job starts/ends)
CMD ["crond", "-f", "-l", "2"]
