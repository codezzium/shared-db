#!/usr/bin/env python3
"""
Quick database creator for new projects.
Usage: docker exec -it shared-pgbackup python /app/mkdb.py <database name>
"""
import os
import sys
import subprocess

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")


def run(cmd, check=True, capture=False):
    """Execute command with PGPASSWORD in environment"""
    env = os.environ.copy()
    if PGPASSWORD:
        env["PGPASSWORD"] = PGPASSWORD
    
    result = subprocess.run(
        cmd,
        env=env,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True
    )
    return result


def db_exists(dbname: str) -> bool:
    """Check if database already exists"""
    sql = f"SELECT 1 FROM pg_database WHERE datname='{dbname}'"
    result = run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres", "-tAc", sql],
        capture=True,
        check=False
    )
    return result.stdout.strip() == "1"


def create_database(dbname: str):
    """Create new database with UTF8 encoding"""
    if db_exists(dbname):
        print(f"[INFO] Database '{dbname}' already exists")
        return
    
    print(f"[CREATE] Creating database: {dbname}")
    run([
        "psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres",
        "-c", f"CREATE DATABASE {dbname} ENCODING 'UTF8' TEMPLATE template1;"
    ])
    print(f"[OK] Database '{dbname}' created successfully")


def print_connection_info(dbname: str):
    """Print connection string and environment variables"""
    conn_string = f"postgresql://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{dbname}"
    
    print("\n" + "="*60)
    print("DATABASE CONNECTION INFO")
    print("="*60)
    print(f"\n📦 Database: {dbname}")
    print(f"🔗 Connection String:\n   {conn_string}")
    print("\n🔧 Environment Variables (.env):")
    print(f"   PGHOST={PGHOST}")
    print(f"   PGPORT={PGPORT}")
    print(f"   PGDATABASE={dbname}")
    print(f"   PGUSER={PGUSER}")
    print(f"   PGPASSWORD={PGPASSWORD}")
    print("\n" + "="*60 + "\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python mkdb.py <database_name>")
        print("\nExample:")
        print("  docker-compose exec pgbackup python /app/mkdb.py my_django_db")
        sys.exit(1)
    
    dbname = sys.argv[1]
    
    # Validate database name (basic check)
    if not dbname.replace("_", "").replace("-", "").isalnum():
        print(f"[ERROR] Invalid database name: {dbname}")
        print("[ERROR] Use only letters, numbers, underscores, and hyphens")
        sys.exit(1)
    
    try:
        create_database(dbname)
        print_connection_info(dbname)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to create database: {e}")
        if e.stderr:
            print(e.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

