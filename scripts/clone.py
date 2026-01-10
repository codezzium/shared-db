#!/usr/bin/env python3
"""
Database cloning tool.
Clones a source database to a target database by dumping and piping to psql.
Usage: python clone.py <source_db> <target_db>
"""
import os
import sys
import subprocess
import time

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")


def run(cmd, check=True, capture=False, quiet=False):
    """Execute command with PGPASSWORD in environment"""
    env = os.environ.copy()
    if PGPASSWORD:
        env["PGPASSWORD"] = PGPASSWORD

    if not quiet:
        print(f"[RUN] {' '.join(cmd)}")

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
    """Check if database exists"""
    sql = f"SELECT 1 FROM pg_database WHERE datname='{dbname}'"
    result = run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres", "-tAc", sql],
        capture=True,
        check=False,
        quiet=True
    )
    return result.stdout.strip() == "1"


def create_db(dbname: str):
    """Create new database"""
    print(f"[CREATE] Creating target database: {dbname}")
    run([
        "psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres",
        "-c", f"CREATE DATABASE {dbname} ENCODING 'UTF8' TEMPLATE template1;"
    ])


def clone_database(source_db: str, target_db: str):
    """
    Clone source_db to target_db using pg_dump | psql.
    """
    if not db_exists(source_db):
        raise ValueError(f"Source database '{source_db}' does not exist")

    if db_exists(target_db):
        raise ValueError(f"Target database '{target_db}' already exists")

    # Create target database
    create_db(target_db)

    print(f"[CLONE] Copying data from {source_db} to {target_db}...")
    start_time = time.time()

    # Pipe pg_dump to psql
    # We use subprocess.Popen for piping
    env = os.environ.copy()
    if PGPASSWORD:
        env["PGPASSWORD"] = PGPASSWORD

    dump_cmd = [
        "pg_dump",
        "-h", PGHOST,
        "-p", PGPORT,
        "-U", PGUSER,
        "-d", source_db,
        "--no-owner",  # Skip ownership to avoid permission issues if users differ
        "--no-acl",    # Skip privileges
    ]

    restore_cmd = [
        "psql",
        "-h", PGHOST,
        "-p", PGPORT,
        "-U", PGUSER,
        "-d", target_db,
        "-q",
    ]

    try:
        p1 = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, env=env)
        p2 = subprocess.Popen(restore_cmd, stdin=p1.stdout, env=env)
        p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits
        output = p2.communicate()

        if p2.returncode != 0:
            raise subprocess.CalledProcessError(p2.returncode, restore_cmd)

        duration = time.time() - start_time
        print(f"[OK] Clone completed in {duration:.2f}s")

    except Exception as e:
        print(f"[ERROR] Clone failed: {e}")
        # Cleanup target db? Maybe better to leave it for inspection or manual cleanup
        # But if it's half-baked, maybe drop it?
        # For now, we leave it.
        raise


def main():
    if len(sys.argv) != 3:
        print("Usage: python clone.py <source_db> <target_db>")
        sys.exit(1)

    source = sys.argv[1]
    target = sys.argv[2]

    try:
        clone_database(source, target)
    except Exception as e:
        sys.stderr.write(f"Fatal error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
