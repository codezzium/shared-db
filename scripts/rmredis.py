#!/usr/bin/env python3
import argparse
import sys

import redis

from common import NAME_PATTERN
from mkredis import RESERVED_USERS, connect_admin


def confirm(name: str, keep_keys: bool) -> bool:
    if not sys.stdin.isatty():
        print("[ERROR] Confirmation required: run interactively or pass --yes")
        return False
    what = "Redis user" if keep_keys else f"Redis user and every {name}:* key"
    answer = input(f"Type '{name}' to permanently delete its {what}: ")
    return answer.strip() == name


def delete_keys(client, name: str) -> int:
    deleted = 0
    batch = []
    for key in client.scan_iter(match=f"{name}:*", count=1000):
        batch.append(key)
        if len(batch) == 1000:
            deleted += client.unlink(*batch)
            batch = []
    if batch:
        deleted += client.unlink(*batch)
    return deleted


def main():
    ap = argparse.ArgumentParser(
        description="Delete a project's Redis ACL user and its keys",
        epilog="Example: docker-compose exec pgbackup python /app/rmredis.py my_django_app",
    )
    ap.add_argument("name", help="Project name given to mkredis.py")
    ap.add_argument("--keep-keys", action="store_true", help="Delete only the user and keep its keys")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args()

    if not NAME_PATTERN.fullmatch(args.name) or args.name in RESERVED_USERS:
        print(f"[ERROR] Invalid Redis user name: {args.name}")
        sys.exit(1)

    try:
        client = connect_admin()
        exists = args.name in client.acl_users()
        has_keys = not args.keep_keys and next(client.scan_iter(match=f"{args.name}:*", count=1000), None) is not None
        if not exists and not has_keys:
            print(f"[ERROR] Neither Redis user nor keys of '{args.name}' exist")
            sys.exit(1)
        if not args.yes and not confirm(args.name, args.keep_keys):
            print("[CANCEL] Nothing was deleted")
            sys.exit(1)
        if exists:
            client.acl_deluser(args.name)
            client.execute_command("ACL", "SAVE")
            print(f"[DROP] Redis user: {args.name} (its connections were closed)")
        if not args.keep_keys:
            print(f"[DROP] Keys {args.name}:*: {delete_keys(client, args.name)}")
    except (redis.RedisError, RuntimeError, OSError) as e:
        print(f"[ERROR] Failed to delete Redis user: {e}")
        sys.exit(1)

    print(f"[OK] '{args.name}' removed from Redis")


if __name__ == "__main__":
    main()
