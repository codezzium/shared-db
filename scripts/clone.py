#!/usr/bin/env python3
"""
Database cloning tool.
Clones a source database to a target database by piping pg_dump output to psql.
Usage: python clone.py <source_db> <target_db>
"""
import sys
import os
import subprocess

import psycopg2

import metadb
from common import connect, role_exists, validate_name
from mkdb import ProjectError, create_project, print_connection_info, reassign_ownership, retarget_policies

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
        print("[RUN]", " ".join(cmd))

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
        check=False,
        quiet=True
    )
    return result.stdout.strip() == "1"


def create_db(dbname: str) -> str:
    """Create a new database with Turkish ICU collation settings"""
    print(f"[CREATE] Creating database: {dbname}")
    return create_project(dbname, [])


def clone_db(source: str, target: str):
    """Clone source db to target db using pipe"""
    print(f"[CLONE] Cloning {source} to {target}...")

    env = os.environ.copy()
    if PGPASSWORD:
        env["PGPASSWORD"] = PGPASSWORD

    # Command: pg_dump source | psql target
    dump_cmd = ["pg_dump", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, source]
    restore_cmd = ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-X", "-q", "-v", "ON_ERROR_STOP=1", target]

    # We use subprocess.Popen to handle the pipe
    p1 = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, env=env)
    p2 = subprocess.Popen(restore_cmd, stdin=p1.stdout, stdout=subprocess.DEVNULL, env=env)
    p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits

    output = p2.communicate()[0]

    if p2.returncode != 0:
        raise subprocess.CalledProcessError(p2.returncode, restore_cmd)

    if p1.wait() != 0:
         raise subprocess.CalledProcessError(p1.returncode, dump_cmd)

    print(f"[SUCCESS] Cloned {source} to {target}")


def adopt_clone(source: str, target: str):
    reassign_ownership(target, target)
    conn = connect(target)
    try:
        with conn.cursor() as cur:
            if role_exists(cur, source):
                retarget_policies(cur, source, target)
    finally:
        conn.close()


def main():
    if len(sys.argv) != 3:
        print("Usage: python clone.py <source_db> <target_db>")
        sys.exit(1)

    source_db = sys.argv[1]
    target_db = sys.argv[2]

    error = validate_name(target_db)
    if error:
        print(f"[ERROR] Invalid target database name: {target_db}")
        print(f"[ERROR] {error}")
        sys.exit(1)

    # Check source exists
    if not db_exists(source_db):
        print(f"[ERROR] Source database '{source_db}' does not exist.")
        sys.exit(1)

    # Check target does not exist
    if db_exists(target_db):
        print(f"[ERROR] Target database '{target_db}' already exists. Aborting.")
        sys.exit(1)

    try:
        # Create target DB
        password = create_db(target_db)

        # Clone data
        clone_db(source_db, target_db)
        adopt_clone(source_db, target_db)

    except (subprocess.CalledProcessError, ProjectError, metadb.MetaError, psycopg2.Error) as e:
        print(f"[ERROR] Cloning failed: {str(e).strip()}")
        # Cleanup: verify if we should drop the half-created DB?
        # For safety, we might leave it or delete it.
        # User requirement didn't specify cleanup on failure, keeping it simple.
        if db_exists(target_db):
            print(f"[HINT] Remove the partial clone with: python /app/rmdb.py {target_db} --yes")
        sys.exit(1)

    print_connection_info(target_db, password)


if __name__ == "__main__":
    main()
