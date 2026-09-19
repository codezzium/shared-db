#!/usr/bin/env python3
import argparse
import sys

import psycopg2

from mkdb import connect, db_exists, drop_project, role_exists, validate_name


def confirm(dbname: str) -> bool:
    if not sys.stdin.isatty():
        print("[ERROR] Confirmation required: run interactively or pass --yes")
        return False
    answer = input(f"Type '{dbname}' to permanently delete its database and role: ")
    return answer.strip() == dbname


def main():
    ap = argparse.ArgumentParser(
        description="Permanently delete a project's database and role",
        epilog="Example: docker-compose exec pgbackup python /app/rmdb.py my_django_db",
    )
    ap.add_argument("dbname", help="Project name, used for both the database and its role")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args()

    dbname = args.dbname
    error = validate_name(dbname)
    if error:
        print(f"[ERROR] Invalid database name: {dbname}")
        print(f"[ERROR] {error}")
        sys.exit(1)

    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                if not db_exists(cur, dbname) and not role_exists(cur, dbname):
                    print(f"[ERROR] Neither database nor role '{dbname}' exists")
                    sys.exit(1)
                if not args.yes and not confirm(dbname):
                    print("[CANCEL] Nothing was deleted")
                    sys.exit(1)
                drop_project(cur, dbname)
        finally:
            conn.close()
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to delete: {str(e).strip()}")
        sys.exit(1)

    print(f"[OK] '{dbname}' deleted")


if __name__ == "__main__":
    main()
