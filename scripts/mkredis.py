#!/usr/bin/env python3
import argparse
import os
import secrets
import sys

import redis

from common import NAME_PATTERN

REDIS_SOCKET = os.getenv("REDIS_SOCKET", "/run/redis/redis.sock")
REDIS_HOST = "shared-redis"
REDIS_PORT = 6379
ADMIN_USER = "admin"
RESERVED_USERS = {"default", ADMIN_USER}
BLOCKED_COMMANDS = ("flushall", "flushdb", "keys", "config", "debug")
CELERY_FANOUT_PATTERNS = ("/0.celery.pidbox", "/0.celeryev/*", "/0.celeryev/worker.*")


def connect_admin():
    password = os.getenv("REDIS_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("REDIS_ADMIN_PASSWORD is not set")
    return redis.Redis(
        unix_socket_path=REDIS_SOCKET,
        username=ADMIN_USER,
        password=password,
        decode_responses=True,
        socket_timeout=10,
    )


def acl_rules(name: str, password: str) -> list[str]:
    return [
        "reset",
        "on",
        f">{password}",
        f"~{name}:*",
        f"&{name}:*",
        *(f"&{name}:{pattern}" for pattern in CELERY_FANOUT_PATTERNS),
        "+@all",
        "-@dangerous",
        *(f"-{command}" for command in BLOCKED_COMMANDS),
    ]


def create_user(client, name: str) -> str:
    if name in client.acl_users():
        raise RuntimeError(f"Redis user '{name}' already exists")
    password = secrets.token_urlsafe(32)
    client.execute_command("ACL", "SETUSER", name, *acl_rules(name, password))
    client.execute_command("ACL", "SAVE")
    return password


def print_connection_info(name: str, password: str):
    print("\n" + "="*60)
    print("REDIS CONNECTION INFO")
    print("="*60)
    print(f"\n👤 User: {name}")
    print(f"🔑 Key and channel prefix: {name}:")
    print(f"🔗 URL:\n   redis://{name}:{password}@{REDIS_HOST}:{REDIS_PORT}/0")
    print(f"\n   Celery global_keyprefix: \"{name}:\"   Django KEY_PREFIX: \"{name}\"")
    print("\n⚠️  This password is shown only once. Store it now.")
    print("\n" + "="*60 + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Create a Redis ACL user limited to one project's keys and channels",
        epilog="Example: docker-compose exec pgbackup python /app/mkredis.py my_django_app",
    )
    ap.add_argument("name", help="Project name, used as the Redis user and the key prefix")
    args = ap.parse_args()

    if not NAME_PATTERN.fullmatch(args.name) or args.name in RESERVED_USERS:
        print(f"[ERROR] Invalid Redis user name: {args.name}")
        print("[ERROR] Use lowercase letters, digits and underscores, starting with a letter; 'default' and 'admin' are reserved")
        sys.exit(1)

    try:
        client = connect_admin()
        password = create_user(client, args.name)
    except (redis.RedisError, RuntimeError, OSError) as e:
        print(f"[ERROR] Failed to create Redis user: {e}")
        sys.exit(1)

    print(f"[CREATE] Redis user: {args.name} (keys {args.name}:*, channels {args.name}:*)")
    print_connection_info(args.name, password)


if __name__ == "__main__":
    main()
