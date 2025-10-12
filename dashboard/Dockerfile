FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=sharedpanel.settings \
    DJANGO_SCRIPTS_PATH=/app/scripts

RUN apk add --no-cache \
    postgresql-client \
    rclone \
    tzdata \
    docker-cli \
    bash \
    gcc \
    musl-dev

WORKDIR /app

COPY dashboard/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard /app
COPY scripts /app/scripts

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
