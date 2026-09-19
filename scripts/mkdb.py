#!/usr/bin/env python3
"""
Quick database creator for new projects.
Usage: docker exec -it shared-pgbackup python /app/mkdb.py <database name>
"""
import argparse
import os
import re
import secrets
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

from backup import BACKUP_ROLE

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")

APP_HOST = "shared-pgbouncer"
APP_PORT = "5432"

PGBOUNCER_AUTH_ROLE = "pgbouncer_auth"
PGBOUNCER_ADMIN_ROLE = "pgbouncer_admin"
PGBOUNCER_STATS_ROLE = "pgbouncer_stats"

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
    BACKUP_ROLE,
    PGBOUNCER_AUTH_ROLE,
    PGBOUNCER_ADMIN_ROLE,
    PGBOUNCER_STATS_ROLE,
}


class ProjectError(Exception):
    pass


def connect(dbname: str = "postgres"):
    conn = psycopg2.connect(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        password=PGPASSWORD,
        dbname=dbname,
    )
    conn.autocommit = True
    return conn


def validate_name(name: str) -> str | None:
    if not NAME_PATTERN.fullmatch(name):
        return "Use only lowercase letters, digits and underscores, starting with a letter (max 63 characters)"
    if name in RESERVED_NAMES or name.startswith("pg_"):
        return f"'{name}' is reserved"
    return None


def role_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def db_exists(cur, dbname: str) -> bool:
    """Check if database already exists"""
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return cur.fetchone() is not None


def unavailable_extensions(cur, extensions: list[str]) -> list[str]:
    cur.execute("SELECT name FROM pg_available_extensions WHERE name = ANY(%s)", (extensions,))
    available = {row[0] for row in cur.fetchall()}
    return [ext for ext in extensions if ext not in available]


def create_role(cur, name: str) -> str:
    password = secrets.token_urlsafe(32)
    verifier = encrypt_password(password, name, cur, "scram-sha-256")
    cur.execute(
        sql.SQL(
            "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(name), sql.Literal(verifier))
    )
    return password


def create_database(cur, dbname: str, owner: str):
    """Create new database with Turkish ICU collation settings"""
    db = sql.Identifier(dbname)
    cur.execute(
        sql.SQL(
            "CREATE DATABASE {} WITH OWNER {} LOCALE_PROVIDER icu ICU_LOCALE 'tr-TR' TEMPLATE template0"
        ).format(db, sql.Identifier(owner))
    )
    cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
    cur.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
            db, sql.Identifier(owner), sql.Identifier(BACKUP_ROLE)
        )
    )


def install_extensions(dbname: str, extensions: list[str]):
    conn = connect(dbname)
    try:
        with conn.cursor() as cur:
            for ext in extensions:
                print(f"[EXTENSION] {ext}")
                cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
    finally:
        conn.close()


def drop_project(cur, name: str):
    if db_exists(cur, name):
        print(f"[DROP] Database: {name}")
        cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    if role_exists(cur, name):
        print(f"[DROP] Role: {name}")
        cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def create_project(name: str, extensions: list[str]) -> str:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if role_exists(cur, name):
                raise ProjectError(f"Role '{name}' already exists")
            if db_exists(cur, name):
                raise ProjectError(f"Database '{name}' already exists")
            missing = unavailable_extensions(cur, extensions)
            if missing:
                raise ProjectError(f"Extension not available: {', '.join(missing)}")

            print(f"[CREATE] Role: {name}")
            password = create_role(cur, name)
            completed = False
            try:
                print(f"[CREATE] Database: {name}")
                create_database(cur, name, name)
                install_extensions(name, extensions)
                completed = True
            finally:
                if not completed:
                    print(f"[ROLLBACK] Removing partially created project: {name}")
                    drop_project(cur, name)
    finally:
        conn.close()
    return password


def print_connection_info(dbname: str, password: str):
    """Print connection string and environment variables"""
    conn_string = f"postgresql://{dbname}:{password}@{APP_HOST}:{APP_PORT}/{dbname}"

    print("\n" + "="*60)
    print("DATABASE CONNECTION INFO")
    print("="*60)
    print(f"\n📦 Database: {dbname}")
    print(f"🔗 Connection String:\n   {conn_string}")
    print("\n🔧 Environment Variables (.env):")
    print(f"   PGHOST={APP_HOST}")
    print(f"   PGPORT={APP_PORT}")
    print(f"   PGDATABASE={dbname}")
    print(f"   PGUSER={dbname}")
    print(f"   PGPASSWORD={password}")
    print("\n⚠️  This password is shown only once. Store it now.")
    print("\n" + "="*60 + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Create an isolated role and database for a project",
        epilog="Example: docker-compose exec pgbackup python /app/mkdb.py my_django_db --extension vector",
    )
    ap.add_argument("dbname", help="Project name, used for both the database and its role")
    ap.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar="NAME",
        help="Extension to install into the new database as superuser (repeatable)",
    )
    args = ap.parse_args()

    dbname = args.dbname

    # Validate database name (basic check)
    error = validate_name(dbname)
    if error:
        print(f"[ERROR] Invalid database name: {dbname}")
        print(f"[ERROR] {error}")
        sys.exit(1)

    try:
        password = create_project(dbname, args.extension)
    except (ProjectError, psycopg2.Error) as e:
        print(f"[ERROR] Failed to create database: {str(e).strip()}")
        sys.exit(1)

    print_connection_info(dbname, password)


if __name__ == "__main__":
    main()
