"""Django settings for SharedPanel project."""
from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "changeme-in-production")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "controlpanel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sharedpanel.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "controlpanel" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "sharedpanel.wsgi.application"


def _postgres_database_config() -> dict[str, str]:
    """Build PostgreSQL configuration from environment variables."""
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DJANGO_DB_NAME", os.getenv("POSTGRES_DB", "postgres")),
        "USER": os.getenv("DJANGO_DB_USER", os.getenv("POSTGRES_USER", "postgres")),
        "PASSWORD": os.getenv("DJANGO_DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
        "HOST": os.getenv("DJANGO_DB_HOST", os.getenv("POSTGRES_HOST", "localhost")),
        "PORT": int(os.getenv("DJANGO_DB_PORT", os.getenv("POSTGRES_PORT", "5432"))),
    }


if os.getenv("DJANGO_DATABASE", "sqlite").lower() == "postgres":
    DATABASES = {"default": _postgres_database_config()}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "tr-tr"
TIME_ZONE = os.getenv("TZ", "Europe/Istanbul")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "controlpanel" / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Paths used by control panel services
DEFAULT_SCRIPTS_PATH = REPO_ROOT / "scripts"
SCRIPTS_PATH = Path(os.getenv("DJANGO_SCRIPTS_PATH", DEFAULT_SCRIPTS_PATH)).resolve()
