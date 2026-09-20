import hashlib
import os
import re
from zoneinfo import ZoneInfo

import psycopg2

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB") or PGUSER

BACKUP_ROLE = "backup"
BACKUP_PASSWORD = os.getenv("BACKUP_PASSWORD")

PGBOUNCER_AUTH_ROLE = "pgbouncer_auth"
PGBOUNCER_ADMIN_ROLE = "pgbouncer_admin"
PGBOUNCER_STATS_ROLE = "pgbouncer_stats"
SERVICE_ROLES = (BACKUP_ROLE, PGBOUNCER_AUTH_ROLE, PGBOUNCER_ADMIN_ROLE, PGBOUNCER_STATS_ROLE)

META_DB = "backup_meta"
SERVER_NAME = os.getenv("SERVER_NAME", "default")
TIMEZONE = ZoneInfo("Europe/Istanbul")

STAGING_PREFIX = "_restore_"
REPLACED_PREFIX = "_replaced_"
TRANSIENT_PREFIXES = (STAGING_PREFIX, REPLACED_PREFIX)

APP_HOST = "shared-pgbouncer"
APP_PORT = "5432"

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}")
RESERVED_NAMES = {
    "postgres",
    "template0",
    "template1",
    "pgbouncer",
    "public",
    "none",
    "current_user",
    "current_role",
    "session_user",
    META_DB,
    *SERVICE_ROLES,
}


def validate_name(name: str) -> str | None:
    if not NAME_PATTERN.fullmatch(name):
        return "Use only lowercase letters, digits and underscores, starting with a letter (max 63 characters)"
    if name in RESERVED_NAMES or name.startswith("pg_"):
        return f"'{name}' is reserved"
    return None


def transient_name(prefix: str, dbname: str) -> str:
    name = f"{prefix}{dbname}"
    if len(name) <= 63:
        return name
    return f"{name[:54]}_{hashlib.sha256(dbname.encode()).hexdigest()[:8]}"


def local_time(value) -> str:
    return f"{value.astimezone(TIMEZONE):%Y-%m-%d %H:%M}"


def connect(dbname: str = "postgres", user: str | None = None, password: str | None = None):
    conn = psycopg2.connect(
        host=PGHOST,
        port=PGPORT,
        user=user or PGUSER,
        password=password if user else PGPASSWORD,
        dbname=dbname,
        connect_timeout=10,
    )
    conn.autocommit = True
    return conn


def connect_as_backup(dbname: str = "postgres"):
    return connect(dbname, BACKUP_ROLE, BACKUP_PASSWORD)


def role_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def db_exists(cur, dbname: str) -> bool:
    """Check if database already exists"""
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return cur.fetchone() is not None
