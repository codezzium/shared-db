"""ASGI config for SharedPanel project."""
from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sharedpanel.settings")

application = get_asgi_application()
