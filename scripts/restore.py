#!/usr/bin/env python3
"""
Database restoration tool with safety backup functionality.
Restores databases from cloud backups with automatic rollback capability.
"""
import argparse
import os
import sys
import re
import subprocess
import pathlib
import datetime
import tempfile
import shutil
import gzip
import json

import psycopg2
from psycopg2 import sql

import metadb
# Import reusable dump function from backup.py
from backup import (
    CHECKSUM_FILE,
    DUMP_SUFFIX,
    DUMP_SUFFIXES,
    MANIFEST_FILE,
    ROLES_FILE,
    describe,
    dump_single_db,
    file_sha256,
    now_utc,
    upload_file,
)
from common import (
    META_DB,
    POSTGRES_DB,
    REPLACED_PREFIX,
    SERVICE_ROLES,
    STAGING_PREFIX,
    connect,
    local_time,
    role_exists,
    transient_name,
    validate_name,
)
from common import db_exists as database_exists
from mkdb import create_database, create_role, print_connection_info, reassign_ownership, retarget_policies

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")
SERVER_NAME = os.getenv("SERVER_NAME", "default")
SYSTEM_DATABASES = (META_DB, POSTGRES_DB)

ROLE_KEYWORDS = {"public", "current_user", "current_role", "session_user"}
ROLE_REFERENCE_PATTERNS = (
    re.compile(r"^ALTER .+ OWNER TO (.+);$"),
    re.compile(r"^(?:GRANT|REVOKE) .+ (?:TO|FROM) (.+?)(?: WITH GRANT OPTION)?(?: GRANTED BY (.+?))?;$"),
    re.compile(r"^CREATE POLICY .+ TO (.+?)(?: USING .*| WITH CHECK .*)?;$"),
    re.compile(r"^ALTER DEFAULT PRIVILEGES FOR ROLE (\S+) .* (?:TO|FROM) (.+?);$"),
    re.compile(r"^SET SESSION AUTHORIZATION '?\"?([^'\";]+)\"?'?;$"),
)


class RestoreError(Exception):
    pass


class TargetUnreachable(RestoreError):
    pass


LIST_FLAGS = ("--contimeout", "10s", "--timeout", "1m", "--low-level-retries", "3")
UNREACHABLE: dict[str, str] = {}


def mark_unreachable(target: metadb.Target, error: str):
    if target.name in UNREACHABLE:
        return
    UNREACHABLE[target.name] = error
    print(f"[WARN] Target {target.name} is unreachable and skipped: {error}")


def scan(target: metadb.Target, action):
    if target.name in UNREACHABLE:
        return None
    try:
        return action(target)
    except TargetUnreachable as e:
        mark_unreachable(target, str(e))
        return None


def unreachable_report() -> str:
    return "; ".join(f"{name}: {error}" for name, error in UNREACHABLE.items())


def run(cmd, check=True, capture=False, cwd=None, quiet=False):
    """Execute command with PGPASSWORD in environment"""
    env = os.environ.copy()
    if PGPASSWORD:
        env["PGPASSWORD"] = PGPASSWORD

    if not quiet:
        print("[RUN]", " ".join(cmd))

    return subprocess.run(
        cmd,
        check=check,
        cwd=cwd,
        stdout=(subprocess.PIPE if capture else subprocess.DEVNULL if quiet else None),
        stderr=(subprocess.STDOUT if capture else subprocess.DEVNULL if quiet else None),
        env=env,
    )


def rclone_list(args: list[str]) -> list[str] | None:
    result = subprocess.run(["rclone", "lsf", *LIST_FLAGS, *args], capture_output=True, text=True)
    if result.returncode == 3:
        return None
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise TargetUnreachable(f"rclone lsf {args[0]} failed: {lines[-1] if lines else result.returncode}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def folder_date(date_folder: str) -> datetime.date:
    year, month, day = (int(part) for part in date_folder.split("/"))
    return datetime.date(year, month, day)


def list_cloud_backups(target: metadb.Target):
    """
    List all backup folders in cloud storage (year/month/day structure).
    Returns list of paths like: ['2025/10/7', '2025/10/6', ...]
    """
    folders = rclone_list([target.location("records", SERVER_NAME), "--dirs-only", "--recursive", "--max-depth", "3"])

    # Filter year/month/day folders
    date_folders = []
    for folder in folders or []:
        folder = folder.rstrip("/")
        parts = folder.split("/")
        if len(parts) == 3:
            try:
                # Validate it's a valid date
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                datetime.date(year, month, day)
                date_folders.append(folder)
            except (ValueError, IndexError):
                continue

    # Sort by date (newest first)
    date_folders.sort(key=lambda x: [int(p) for p in x.split("/")], reverse=True)
    return date_folders


def list_dump_files(target: metadb.Target, dbname: str | None = None) -> dict[str, list[str]]:
    patterns = [f"{dbname}{suffix}" for suffix in DUMP_SUFFIXES] if dbname else [f"*{suffix}" for suffix in DUMP_SUFFIXES]
    args = [target.location("records", SERVER_NAME), "--recursive", "--files-only", "--max-depth", "4"]
    for pattern in patterns:
        args += ["--include", f"/*/*/*/{pattern}"]

    listing: dict[str, list[str]] = {}
    for line in rclone_list(args) or []:
        parts = line.split("/")
        if len(parts) != 4:
            continue
        try:
            folder_date("/".join(parts[:3]))
        except ValueError:
            continue
        listing.setdefault("/".join(parts[:3]), []).append(parts[3])
    return listing


def preferred_dump(files: list[str], dbname: str) -> str:
    for suffix in (DUMP_SUFFIX, ".sql"):
        if f"{dbname}{suffix}" in files:
            return f"{dbname}{suffix}"
    raise RestoreError(f"No dump for '{dbname}' among {', '.join(files)}")


def latest_cloud_backup(target: metadb.Target):
    """Get the most recent backup folder from cloud"""
    folders = list_cloud_backups(target)
    if not folders:
        raise RestoreError(f"No backups found in cloud storage: {target.location('records', SERVER_NAME)}")
    return folders[0]


def download_from_cloud(target: metadb.Target, date_folder: str, local_temp_dir: pathlib.Path, dbname: str | None = None):
    """
    Download backup from cloud to local temp directory.

    Args:
        date_folder: Date folder path (e.g. "2025/10/7")
        local_temp_dir: Local temporary directory to download to
        dbname: If specified, download only this database's .sql file
    """
    folder = target.location("records", SERVER_NAME, date_folder)
    if dbname:
        print(f"[DOWNLOAD] Fetching {dbname} dump from {target.name}: {date_folder}")
        includes = [f"{dbname}{suffix}" for suffix in DUMP_SUFFIXES] + [CHECKSUM_FILE, MANIFEST_FILE, ROLES_FILE]
        filters = [arg for name in includes for arg in ("--include", f"/{name}")]
        run(["rclone", "copy", folder, str(local_temp_dir), "--max-depth", "1", *filters])
    else:
        print(f"[DOWNLOAD] Fetching full backup from {target.name}: {date_folder}")
        run(["rclone", "copy", folder, str(local_temp_dir), "--max-depth", "1"])

    print(f"[OK] Download completed: {local_temp_dir}")


def download_selected(target: metadb.Target, date_folder: str, local_dir: pathlib.Path, filenames: list[str]):
    names = [*filenames, CHECKSUM_FILE, MANIFEST_FILE]
    filters = [arg for name in names for arg in ("--include", f"/{name}")]
    print(f"[DOWNLOAD] Fetching {len(filenames)} file(s) from {target.name}: {date_folder}")
    run(["rclone", "copy", target.location("records", SERVER_NAME, date_folder), str(local_dir), "--max-depth", "1", *filters])


def parse_date_arg(date_arg: str) -> str:
    """
    Parse date argument and convert to year/month/day format.
    Accepts: YYYY-MM-DD or YYYY/MM/DD
    Returns: year/month/day (e.g. "2025/10/7")
    """
    # Try YYYY-MM-DD format
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date_arg):
        parts = date_arg.split("-")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    # Try YYYY/MM/DD format
    elif re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", date_arg):
        parts = date_arg.split("/")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise RestoreError(f"Invalid date format: {date_arg} (expected YYYY-MM-DD or YYYY/MM/DD)")

    # Validate date
    try:
        datetime.date(year, month, day)
    except ValueError:
        raise RestoreError(f"Invalid date: {date_arg}")

    return f"{year}/{month}/{day}"


def find_cloud_backup(target: metadb.Target, date_arg: str | None) -> str:
    """
    Find backup folder in cloud by date or return latest.
    Returns the date folder path (e.g. "2025/10/7").
    """
    if date_arg:
        date_path = parse_date_arg(date_arg)

        # Check if folder exists in cloud
        cloud_folders = list_cloud_backups(target)
        if date_path not in cloud_folders:
            raise RestoreError(
                f"Backup not found in {target.name}: {date_path}\nAvailable: {', '.join(cloud_folders[:5]) or 'none'}"
            )
        return date_path

    # Auto-select latest backup
    return latest_cloud_backup(target)


def check_file_in_cloud(target: metadb.Target, date_folder: str, filename: str) -> bool:
    """
    Check if a specific file exists in a cloud backup folder using rclone lsf.
    No download needed — just lists remote files.
    """
    files = scan(target, lambda t: rclone_list([t.location("records", SERVER_NAME, date_folder), "--files-only"]))
    return filename in (files or [])


def guess_latest_cloud_backup_for_db(target: metadb.Target, db: str) -> str | None:
    """
    Find the most recent cloud backup containing SQL dump for specified database.
    Uses rclone lsf to check file existence (no download needed).
    """
    listing = list_dump_files(target, db)
    for date_folder in sorted(listing, key=folder_date, reverse=True):
        print(f"[FOUND] Database backup found in {target.name}: {date_folder}")
        return date_folder
    return None


def db_exists(db: str) -> bool:
    """Check if database exists"""
    sql = f"SELECT 1 FROM pg_database WHERE datname='{db}'"
    result = run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres", "-tAc", sql],
        capture=True,
        check=False
    )
    return result.stdout.strip() == b"1"


def terminate_connections(db: str):
    """Kill all active connections to database"""
    sql = (
        "SELECT pg_terminate_backend(pid) "
        f"FROM pg_stat_activity WHERE datname='{db}' AND pid<>pg_backend_pid();"
    )
    run(
        [
            "psql",
            "-h", PGHOST,
            "-p", PGPORT,
            "-U", PGUSER,
            "-d", "postgres",
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ],
        check=False,
    )


def safety_backup_before_restore(db: str, targets: list[metadb.Target], meta=None) -> str | None:
    """
    Create a timestamped safety backup before destructive restore operation.
    Uploads directly to cloud (no local storage).
    Returns cloud path for reference.
    """
    if not db_exists(db):
        print(f"[INFO] Database '{db}' does not exist yet, skipping safety backup")
        return None
    for target in targets:
        scan(target, lambda t: rclone_list([t.location(), "--dirs-only", "--max-depth", "1"]))
    targets = [target for target in targets if target.name not in UNREACHABLE]
    if not targets:
        raise RestoreError(
            f"No reachable backup target for '{db}', cannot store a safety backup "
            "(pass --target or --skip-safety-backup)"
        )

    today = datetime.date.today()
    timestamp = datetime.datetime.now().strftime("%H-%M-%S")
    filename = f"{db}_before_restore_{timestamp}{DUMP_SUFFIX}"
    cloud_path = f"manual_backups/{SERVER_NAME}/{today.year}/{today.month}/{today.day}"

    # Create temp file for safety backup
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="safety_backup_"))
    backup_file = temp_dir / filename

    try:
        print(f"[SAFETY] Creating backup before restore: {filename}")
        dump_single_db(db, backup_file)
        dumped = describe(backup_file)

        # Upload to cloud
        stored = []
        for target in targets:
            print(f"[UPLOAD] Uploading safety backup to {target.name}: {cloud_path}/")
            started = now_utc()
            error = upload_file(target, backup_file, cloud_path)
            if meta is not None:
                try:
                    with meta.cursor() as cur:
                        metadb.record_run(
                            cur,
                            datname=db,
                            target=target.name,
                            kind="safety",
                            started_at=started,
                            duration=now_utc() - started,
                            folder=target.location(cloud_path),
                            filename=filename,
                            size_bytes=dumped.size,
                            sha256=dumped.sha256,
                            status="failed" if error else "success",
                            error=error,
                        )
                except psycopg2.Error as e:
                    print(f"[WARN] Could not record safety backup run: {str(e).strip()}")
            if error:
                print(f"[ERROR] Safety backup upload to {target.name} failed: {error}")
                UNREACHABLE.setdefault(target.name, f"safety backup upload failed: {error}")
                continue
            stored.append(target.location(cloud_path, filename))
            print(f"[OK] Safety backup uploaded: {stored[-1]}")

        if not stored:
            raise RestoreError("Safety backup could not be stored on any target, restore aborted")
        return ", ".join(stored)

    except subprocess.CalledProcessError as e:
        raise RestoreError(f"Safety backup failed: {e}")

    finally:
        # Always cleanup temp
        shutil.rmtree(temp_dir, ignore_errors=True)


def replaced_leftover_error(db: str, replaced: str) -> RestoreError:
    return RestoreError(
        f"Database {replaced} exists, left over from an interrupted restore of '{db}'; "
        "check it and drop it before restoring again"
    )


def create_staging_db(db: str) -> str:
    """Drop and recreate database"""
    staging = transient_name(STAGING_PREFIX, db)
    replaced = transient_name(REPLACED_PREFIX, db)
    conn = connect()
    try:
        with conn.cursor() as cur:
            if database_exists(cur, replaced):
                raise replaced_leftover_error(db, replaced)
            if database_exists(cur, staging):
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (staging,))
                if cur.fetchone()[0]:
                    raise RestoreError(f"Another restore of '{db}' is running ({staging} is in use)")
                print(f"[DROP] Dropping leftover staging database: {staging}")
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(staging)))

            print(f"[CREATE] Creating staging database: {staging}")
            owner = db if db not in SYSTEM_DATABASES and role_exists(cur, db) else PGUSER
            create_database(cur, staging, owner)
    finally:
        conn.close()
    return staging


def drop_staging_db(db: str, staging: str):
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                if not database_exists(cur, staging):
                    return
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(staging)))
                kept = database_exists(cur, db)
        finally:
            conn.close()
    except psycopg2.Error as e:
        print(f"[WARN] Could not drop staging database {staging}: {str(e).strip()}")
        return
    print(f"[ROLLBACK] Staging database {staging} dropped{f', {db} is unchanged' if kept else ''}")


def rename_db(cur, old: str, new: str):
    cur.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(old), sql.Identifier(new)))


def allow_connections(cur, db: str, allowed: bool):
    cur.execute(
        sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS {}").format(
            sql.Identifier(db), sql.SQL("true" if allowed else "false")
        )
    )


def swap_database(db: str, staging: str):
    replaced = transient_name(REPLACED_PREFIX, db)
    conn = connect()
    try:
        with conn.cursor() as cur:
            if not database_exists(cur, db):
                rename_db(cur, staging, db)
                print(f"[CREATE] Restored copy is now database {db}")
                return
            if database_exists(cur, replaced):
                raise replaced_leftover_error(db, replaced)

            print(f"[SWAP] Replacing {db} with the restored copy")
            allow_connections(cur, db, False)
            try:
                terminate_connections(db)
                rename_db(cur, db, replaced)
            except psycopg2.Error:
                allow_connections(cur, db, True)
                raise
            try:
                rename_db(cur, staging, db)
            except psycopg2.Error:
                rename_db(cur, replaced, db)
                allow_connections(cur, db, True)
                raise

            try:
                cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(replaced)))
                print(f"[DROP] Previous copy of {db} dropped")
            except psycopg2.Error as e:
                print(f"[WARN] Previous copy kept as {replaced}, drop it manually: {str(e).strip()}")
    finally:
        conn.close()


def discard_role(db: str) -> bool:
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                if database_exists(cur, db) or not role_exists(cur, db):
                    return False
                cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(db)))
        finally:
            conn.close()
    except psycopg2.Error as e:
        print(f"[WARN] Could not remove role {db}: {str(e).strip()}")
        return False
    print(f"[ROLLBACK] Role '{db}' created by this restore removed")
    return True


def dump_format(path: pathlib.Path) -> str:
    with path.open("rb") as f:
        head = f.read(5)
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head == b"PGDMP":
        return "custom"
    return "plain"


def dump_lines(path: pathlib.Path, fmt: str):
    if fmt == "custom":
        result = subprocess.run(
            ["pg_restore", "--schema-only", "--no-owner", "--no-acl", "-f", "-", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RestoreError(f"Cannot read {path.name}: {result.stderr.strip()}")
        yield from result.stdout.splitlines()
        return
    opener = gzip.open if fmt == "gzip" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        yield from f


def parse_role_list(text: str) -> set[str]:
    names = set()
    for token in text.split(","):
        token = token.strip()
        if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
            token = token[1:-1].replace('""', '"')
        if not token or token.lower() in ROLE_KEYWORDS or token.startswith("pg_") or len(token) > 63:
            continue
        names.add(token)
    return names


def referenced_roles(path: pathlib.Path, fmt: str) -> set[str]:
    roles = set()
    in_copy = False
    try:
        for raw in dump_lines(path, fmt):
            line = raw.rstrip("\r\n")
            if in_copy:
                in_copy = line != "\\."
                continue
            if line.startswith("COPY ") and line.endswith("FROM stdin;"):
                in_copy = True
                continue
            for pattern in ROLE_REFERENCE_PATTERNS:
                match = pattern.match(line)
                if match:
                    for group in match.groups():
                        if group:
                            roles |= parse_role_list(group)
    except (OSError, EOFError) as e:
        raise RestoreError(f"Cannot read {path.name}: {e}")
    return roles


def create_placeholders(roles: set[str]) -> list[str]:
    created = []
    conn = connect()
    try:
        with conn.cursor() as cur:
            for name in sorted(roles):
                if not role_exists(cur, name):
                    cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
                    created.append(name)
    finally:
        conn.close()
    if created:
        print(f"[ROLE] Temporary placeholder role(s) for owners missing on this server: {', '.join(created)}")
    return created


def drop_placeholders(db: str, placeholders: list[str], owner: str):
    if not placeholders:
        return
    try:
        conn = connect(db)
        try:
            with conn.cursor() as cur:
                for name in placeholders:
                    retarget_policies(cur, name, owner)
                    cur.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(sql.Identifier(name), sql.Identifier(owner)))
                    cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        finally:
            conn.close()
        conn = connect()
        try:
            with conn.cursor() as cur:
                for name in placeholders:
                    cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
        finally:
            conn.close()
        print(f"[ROLE] Placeholder role(s) removed: {', '.join(placeholders)}")
    except psycopg2.Error as e:
        print(f"[WARN] Could not remove placeholder role(s) {', '.join(placeholders)}: {str(e).strip()}")


def load_dump(db: str, path: pathlib.Path, owner: str):
    fmt = dump_format(path)
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    placeholders = create_placeholders(referenced_roles(path, fmt))
    try:
        with tempfile.TemporaryFile() as errors:
            if fmt == "custom":
                cmd = [
                    "pg_restore",
                    "-h", PGHOST,
                    "-p", PGPORT,
                    "-U", PGUSER,
                    "-d", db,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    str(path),
                ]
                returncode = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=errors).returncode
            else:
                cmd = [
                    "psql",
                    "-h", PGHOST,
                    "-p", PGPORT,
                    "-U", PGUSER,
                    "-d", db,
                    "-X",
                    "-v", "ON_ERROR_STOP=1",
                    "-q",  # Quiet mode
                ]
                if fmt == "plain":
                    cmd += ["-f", str(path)]
                    returncode = subprocess.run(
                        cmd,
                        env=env,
                        stdout=subprocess.DEVNULL,  # Suppress output
                        stderr=errors,
                    ).returncode
                else:
                    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
                    try:
                        with gzip.open(path, "rb") as source:
                            shutil.copyfileobj(source, proc.stdin, 1024 * 1024)
                    except BrokenPipeError:
                        pass
                    except (OSError, EOFError) as e:
                        proc.kill()
                        proc.wait()
                        raise RestoreError(f"Cannot decompress {path.name}: {e}")
                    finally:
                        try:
                            proc.stdin.close()
                        except BrokenPipeError:
                            pass
                    returncode = proc.wait()

            if returncode != 0:
                errors.seek(0)
                detail = "\n".join(errors.read().decode(errors="replace").strip().splitlines()[-15:])
                raise RestoreError(f"Loading {path.name} into '{db}' failed (exit code {returncode}):\n{detail}")

        if owner != PGUSER:
            reassign_ownership(db, owner)
    finally:
        drop_placeholders(db, placeholders, owner)


def restore_from_folder(db: str, folder: pathlib.Path, cleanup_after: bool = False, into: str | None = None):
    """
    Restore database from SQL backup file.

    Args:
        db: Database name
        folder: Local folder containing backup files
        cleanup_after: If True, delete folder after successful restore
    """
    sql_path = next((folder / f"{db}{suffix}" for suffix in (DUMP_SUFFIX, ".sql") if (folder / f"{db}{suffix}").exists()), None)

    if sql_path is None:
        candidates = sorted([p.name for p in folder.iterdir() if p.name.endswith(DUMP_SUFFIXES)])
        hint = (
            f"Available SQL files: {', '.join(candidates)}"
            if candidates
            else "No SQL files found in folder"
        )
        raise RestoreError(f"No backup found for '{db}' in {folder.name}. {hint}")

    print(f"[RESTORE] Loading SQL: {sql_path.name} (this may take a while...)")
    try:
        load_dump(into or db, sql_path, PGUSER if db in SYSTEM_DATABASES else db)
        print("[OK] Restore completed successfully")
    except RestoreError:
        print("[ERROR] Restore failed!")
        raise

    if cleanup_after:
        print(f"[CLEANUP] Removing temporary backup: {folder}")
        shutil.rmtree(folder, ignore_errors=True)


def list_tables(db: str):
    """Display tables in database for verification"""
    print("\n[VERIFY] Listing tables in public schema:")
    run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", db, "-c", r"\dt"],
        check=False,
    )


def open_meta():
    try:
        meta = metadb.connect_meta()
        with meta.cursor() as cur:
            targets = metadb.load_targets(cur)
            policies = metadb.load_policies(cur)
        return meta, targets, policies
    except psycopg2.Error as e:
        print(f"[WARN] {META_DB} is unreachable: {str(e).strip()}")
        return None, None, None


def resolve_target_arg(value: str, targets: dict | None) -> metadb.Target:
    if ":" in value:
        remote, _, prefix = value.partition(":")
        if not remote:
            raise RestoreError(f"Invalid target: {value}")
        return metadb.Target(name=value.rstrip(":"), type="raw", remote=remote, prefix=prefix.strip("/"), retention_days=0)
    if targets is None:
        print(f"[WARN] {META_DB} unavailable, treating '{value}' as rclone remote '{value}:'")
        return metadb.Target(name=value, type="raw", remote=value, prefix="", retention_days=0)
    if value not in targets:
        raise RestoreError(f"Unknown target '{value}'. Known targets: {', '.join(targets) or 'none'}")
    return targets[value]


def policy_targets(db: str, targets: dict | None, policies: dict | None) -> list[metadb.Target]:
    if targets is None:
        return []
    chosen, _ = metadb.resolve_targets(db, targets, policies, metadb.default_target_names())
    return chosen


def locate_backup(db: str, candidates: list[metadb.Target], meta, date_arg: str | None):
    wanted = parse_date_arg(date_arg) if date_arg else None
    by_name = {target.name: target for target in candidates}

    if meta is not None:
        with meta.cursor() as cur:
            runs = metadb.successful_runs(cur, db, list(by_name))
        skipped = None
        for target_name, folder, filename, _, started_at in runs:
            target = by_name[target_name]
            base = target.location("records", SERVER_NAME) + "/"
            if not folder.startswith(base):
                continue
            date_folder = folder[len(base):]
            if wanted and date_folder != wanted:
                continue
            if check_file_in_cloud(target, date_folder, filename):
                print(f"[FOUND] Latest successful backup ({local_time(started_at)}): {target.name} {date_folder}/{filename}")
                if skipped and folder_date(skipped[1]) > folder_date(date_folder):
                    print(f"[WARN] A newer backup ({skipped[1]}) is on {skipped[0]}, which is unreachable")
                return target, date_folder, filename
            if skipped is None and target.name in UNREACHABLE:
                skipped = (target.name, date_folder)

    best = None
    for target in candidates:
        listing = scan(target, lambda t: list_dump_files(t, db))
        for date_folder, files in (listing or {}).items():
            if wanted and date_folder != wanted:
                continue
            if best is None or folder_date(date_folder) > folder_date(best[1]):
                best = (target, date_folder, preferred_dump(files, db))
    if best is None:
        where = ", ".join(target.name for target in candidates)
        missing = [target.name for target in candidates if target.name in UNREACHABLE]
        note = f" ({', '.join(missing)} unreachable)" if missing else ""
        raise RestoreError(f"No backup found for '{db}'{' on ' + wanted if wanted else ''} in: {where}{note}")
    print(f"[FOUND] Database backup found in {best[0].name}: {best[1]}/{best[2]}")
    return best


def known_checksums(folder: pathlib.Path, location: str, filename: str, meta) -> dict[str, str]:
    known = {}
    if meta is not None:
        try:
            with meta.cursor() as cur:
                for digest in metadb.file_hashes(cur, location, filename):
                    known.setdefault(digest, f"{META_DB}.runs")
        except psycopg2.Error:
            pass
    sums = folder / CHECKSUM_FILE
    if sums.exists():
        for line in sums.read_text().splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip().lstrip("*") == filename:
                known.setdefault(parts[0], CHECKSUM_FILE)
    manifest = folder / MANIFEST_FILE
    if manifest.exists():
        entry = json.loads(manifest.read_text()).get("files", {}).get(filename)
        if entry and entry.get("sha256"):
            known.setdefault(entry["sha256"], MANIFEST_FILE)
    return known


def verify_checksum(folder: pathlib.Path, location: str, filename: str, meta):
    known = known_checksums(folder, location, filename, meta)
    if not known:
        print(f"[WARN] No recorded checksum for {filename}, integrity not verified")
        return
    actual = file_sha256(folder / filename)
    if actual not in known:
        raise RestoreError(
            f"Checksum mismatch for {filename}: {actual} matches none of the recorded checksums "
            f"({', '.join(sorted(set(known.values())))})"
        )
    print(f"[OK] Checksum verified for {filename} ({known[actual]})")


def load_roles_backup(folder: pathlib.Path, location: str, meta) -> dict | None:
    path = folder / ROLES_FILE
    if not path.exists():
        return None
    verify_checksum(folder, location, ROLES_FILE, meta)
    return {role["name"]: role for role in json.loads(path.read_text()).get("roles", [])}


def create_role_from_backup(cur, spec: dict):
    flags = [
        "SUPERUSER" if spec["superuser"] else "NOSUPERUSER",
        "INHERIT" if spec["inherit"] else "NOINHERIT",
        "CREATEROLE" if spec["createrole"] else "NOCREATEROLE",
        "CREATEDB" if spec["createdb"] else "NOCREATEDB",
        "LOGIN" if spec["login"] else "NOLOGIN",
        "REPLICATION" if spec["replication"] else "NOREPLICATION",
        "BYPASSRLS" if spec["bypassrls"] else "NOBYPASSRLS",
    ]
    parts = [
        sql.SQL("CREATE ROLE {} WITH").format(sql.Identifier(spec["name"])),
        sql.SQL(" ".join(flags)),
        sql.SQL("CONNECTION LIMIT {}").format(sql.Literal(int(spec["connection_limit"]))),
    ]
    if spec.get("password"):
        parts.append(sql.SQL("PASSWORD {}").format(sql.Literal(spec["password"])))
    if spec.get("valid_until"):
        parts.append(sql.SQL("VALID UNTIL {}").format(sql.Literal(spec["valid_until"])))
    cur.execute(sql.SQL(" ").join(parts))
    for item in spec.get("config") or []:
        key, _, value = item.partition("=")
        cur.execute(
            sql.SQL("ALTER ROLE {} SET {} = {}").format(
                sql.Identifier(spec["name"]), sql.Identifier(key), sql.Literal(value)
            )
        )


def grant_memberships(cur, spec: dict):
    for membership in spec.get("member_of") or []:
        if not role_exists(cur, membership["role"]):
            print(f"[WARN] {spec['name']}: parent role {membership['role']} missing, membership skipped")
            continue
        cur.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN {}, INHERIT {}, SET {}").format(
                sql.Identifier(membership["role"]),
                sql.Identifier(spec["name"]),
                sql.SQL("TRUE" if membership.get("admin") else "FALSE"),
                sql.SQL("TRUE" if membership.get("inherit", True) else "FALSE"),
                sql.SQL("TRUE" if membership.get("set", True) else "FALSE"),
            )
        )


def ensure_project_role(db: str, roles_backup: dict | None, new_password: bool) -> tuple[str | None, bool]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if role_exists(cur, db):
                return None, False
            spec = None if new_password or roles_backup is None else roles_backup.get(db)
            if spec:
                create_role_from_backup(cur, spec)
                grant_memberships(cur, spec)
                print(f"[ROLE] Role '{db}' restored from {ROLES_FILE} (previous password kept)")
                return None, True
            password = create_role(cur, db)
            print(f"[ROLE] Role '{db}' created with a new password")
            return password, True
    finally:
        conn.close()


def restore_roles(roles_backup: dict):
    skip = {PGUSER, *SERVICE_ROLES}
    created = []
    conn = connect()
    try:
        with conn.cursor() as cur:
            for name, spec in sorted(roles_backup.items()):
                if name in skip or name.startswith("pg_"):
                    continue
                if role_exists(cur, name):
                    print(f"[ROLE] {name}: already exists, left unchanged")
                    continue
                create_role_from_backup(cur, spec)
                created.append(name)
                print(f"[ROLE] {name}: restored")
            for name in created:
                grant_memberships(cur, roles_backup[name])
    finally:
        conn.close()
    return created


def ensure_policies(meta, db: str):
    if meta is None or db == META_DB:
        return
    try:
        with meta.cursor() as cur:
            cur.execute("SELECT 1 FROM policies WHERE datname = %s", (db,))
            if cur.fetchone() is None:
                names = metadb.add_default_policies(cur, db)
                print(f"[POLICY] Backup targets for {db}: {', '.join(names)}")
    except (psycopg2.Error, metadb.MetaError) as e:
        print(f"[WARN] Could not add backup policy for {db}: {str(e).strip()}")


def restore_database(db: str, loader, *, roles_backup, safety_targets, meta, skip_safety: bool, new_password: bool):
    # Safety backup (before destructive operation)
    safety_cloud_path = None
    if not skip_safety:
        safety_cloud_path = safety_backup_before_restore(db, safety_targets, meta)
        if safety_cloud_path:
            print(f"[INFO] Safety backup stored in cloud: {safety_cloud_path}\n")
    else:
        print("[WARN] Safety backup skipped\n")

    password, created = (None, False) if db in SYSTEM_DATABASES else ensure_project_role(db, roles_backup, new_password)
    staging = None
    try:
        staging = create_staging_db(db)

        # Restore data
        loader(staging)

        # Terminate connections and recreate DB
        swap_database(db, staging)

        # Verify
        list_tables(db)
    except BaseException:
        if staging:
            drop_staging_db(db, staging)
        if created and discard_role(db):
            password = None
        if password:
            print(f"[WARN] Restore of '{db}' failed after its role was created; the new credentials follow")
            print_connection_info(db, password)
        raise
    return password, safety_cloud_path


def validate_restore_name(db: str):
    if db == META_DB:
        return
    error = validate_name(db)
    if error:
        raise RestoreError(f"Invalid database name '{db}': {error}")


def finish() -> int:
    if not UNREACHABLE:
        return 0
    print(f"[WARN] Unreachable target(s): {unreachable_report()}")
    return 1


def restore_single(args, meta, targets, policies) -> int:
    db = args.dbname
    validate_restore_name(db)
    if args.target:
        candidates = [resolve_target_arg(args.target, targets)]
    elif targets is None:
        raise RestoreError(f"{META_DB} is unreachable, pass --target <rclone-remote>:<prefix>")
    else:
        candidates = policy_targets(db, targets, policies)
        if not candidates:
            raise RestoreError(f"No enabled backup target for '{db}'")

    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="restore_"))
    try:
        while True:
            # Find appropriate cloud backup
            target, date_folder, filename = locate_backup(db, candidates, meta, args.date)
            location = target.location("records", SERVER_NAME, date_folder)

            print("="*60)
            print(f"[INFO] Source       : {location}/{filename}")
            print(f"[INFO] Target database: {db}")
            print(f"[INFO] Server        : {PGUSER}@{PGHOST}:{PGPORT}")
            print("="*60 + "\n")

            # Download only the single .sql file from cloud
            try:
                download_from_cloud(target, date_folder, temp_dir, dbname=db)
                break
            except subprocess.CalledProcessError as e:
                mark_unreachable(target, f"download failed (rclone exit code {e.returncode})")
                shutil.rmtree(temp_dir, ignore_errors=True)
                temp_dir.mkdir()

        # Verify backup was downloaded
        if not (temp_dir / filename).exists():
            raise RestoreError(f"No backup for '{db}' in {date_folder}.")
        verify_checksum(temp_dir, location, filename, meta)
        roles_backup = load_roles_backup(temp_dir, location, meta)

        existed = db_exists(db)
        safety_targets = policy_targets(db, targets, policies) or [target]
        password, safety_cloud_path = restore_database(
            db,
            lambda staging: restore_from_folder(db, temp_dir, into=staging),
            roles_backup=roles_backup,
            safety_targets=safety_targets,
            meta=meta,
            skip_safety=args.skip_safety_backup,
            new_password=args.new_password,
        )
        if not existed:
            ensure_policies(meta, db)

        print("\n" + "="*60)
        print("[SUCCESS] Restore completed")
        if safety_cloud_path:
            print(f"[INFO] Safety backup: {safety_cloud_path}")
        print("="*60 + "\n")
        if password:
            print_connection_info(db, password)

    finally:
        # Always cleanup temp directory
        print(f"[CLEANUP] Removing temporary files: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
    return finish()


def restore_file(args, meta, targets, policies) -> int:
    db = args.dbname
    validate_restore_name(db)
    path = pathlib.Path(args.from_file)
    if not path.is_file():
        raise RestoreError(f"File not found: {path}")

    print("="*60)
    print(f"[INFO] Source       : {path} ({dump_format(path)})")
    print(f"[INFO] Target database: {db}")
    print(f"[INFO] Server        : {PGUSER}@{PGHOST}:{PGPORT}")
    print("="*60 + "\n")

    safety_targets = policy_targets(db, targets, policies)
    if args.target:
        safety_targets = [resolve_target_arg(args.target, targets)]
    existed = db_exists(db)
    password, safety_cloud_path = restore_database(
        db,
        lambda staging: load_dump(staging, path, PGUSER if db in SYSTEM_DATABASES else db),
        roles_backup=None,
        safety_targets=safety_targets,
        meta=meta,
        skip_safety=args.skip_safety_backup,
        new_password=True,
    )
    if not existed:
        ensure_policies(meta, db)

    print("\n" + "="*60)
    print(f"[SUCCESS] Imported {path.name} into '{db}'")
    if safety_cloud_path:
        print(f"[INFO] Safety backup: {safety_cloud_path}")
    print("="*60 + "\n")
    if password:
        print_connection_info(db, password)
    return finish()


def confirm_all(date_folder: str, plan: dict, databases: list[str]) -> bool:
    print(f"[CONFIRM] This restores roles and {len(databases)} database(s) from {date_folder}:")
    for db in databases:
        print(f"          {db} <- {plan[db][0].name}")
    print("          Existing databases with the same names are replaced once their copy has loaded (after a safety backup).")
    if not sys.stdin.isatty():
        print("[ERROR] Confirmation required: run interactively or pass --yes")
        return False
    return input("Type 'restore all' to continue: ").strip() == "restore all"


def meta_is_empty(meta) -> bool:
    if meta is None:
        return True
    try:
        with meta.cursor() as cur:
            cur.execute("SELECT NOT EXISTS (SELECT 1 FROM runs)")
            return cur.fetchone()[0]
    except psycopg2.Error:
        return True


def dump_names(files: list[str]) -> set[str]:
    return {name.removesuffix(".gz").removesuffix(".sql") for name in files if name.endswith(DUMP_SUFFIXES)}


def collect_dumps(sources: list[metadb.Target], date_folder: str) -> list:
    folders = []
    for source in sources:
        files = scan(source, lambda t: rclone_list([t.location("records", SERVER_NAME, date_folder), "--files-only"]))
        if files:
            folders.append((source, files))
    folders.sort(key=lambda item: -len(dump_names(item[1])))
    return folders


def plan_dumps(folders: list) -> dict:
    plan = {}
    for source, files in folders:
        for db in sorted(dump_names(files)):
            plan.setdefault(db, (source, preferred_dump(files, db)))
    return plan


def fetch_dumps(folders: list, databases: list[str], date_folder: str, temp_dir: pathlib.Path, meta, with_roles: bool):
    fetched = {}
    errors = {}
    roles_backup = None
    for source, files in folders:
        if source.name in UNREACHABLE:
            continue
        available = dump_names(files)
        wanted = {db: preferred_dump(files, db) for db in databases if db in available and db not in fetched}
        fetch_roles = with_roles and roles_backup is None and ROLES_FILE in files
        if not wanted and not fetch_roles:
            continue
        folder = temp_dir / source.name
        location = source.location("records", SERVER_NAME, date_folder)
        try:
            download_selected(source, date_folder, folder, [*wanted.values(), *([ROLES_FILE] if fetch_roles else [])])
        except subprocess.CalledProcessError as e:
            mark_unreachable(source, f"download failed (rclone exit code {e.returncode})")
            continue
        if fetch_roles:
            try:
                roles_backup = load_roles_backup(folder, location, meta)
            except RestoreError as e:
                print(f"[ERROR] {ROLES_FILE} from {source.name}: {e}")
        for db, filename in wanted.items():
            try:
                verify_checksum(folder, location, filename, meta)
            except RestoreError as e:
                print(f"[ERROR] {db} from {source.name}: {e}")
                errors[db] = str(e)
                continue
            fetched[db] = (source, filename)
            errors.pop(db, None)
    for db in databases:
        if db not in fetched and db not in errors:
            errors[db] = "dump could not be downloaded from any reachable target"
            print(f"[ERROR] {db}: {errors[db]}")
    return fetched, roles_backup, errors


def restore_all(args, meta, targets, policies) -> int:
    date_folder = parse_date_arg(args.all)
    if args.target:
        sources = [resolve_target_arg(args.target, targets)]
    elif targets:
        sources = [t for t in targets.values() if t.enabled]
    else:
        raise RestoreError(f"No backup target known in {META_DB}, pass --target <rclone-remote>:<prefix>")

    folders = collect_dumps(sources, date_folder)
    plan = plan_dumps(folders)
    if not plan:
        note = f" (unreachable: {unreachable_report()})" if UNREACHABLE else ""
        raise RestoreError(f"No database dumps for {date_folder} in: {', '.join(s.name for s in sources)}{note}")
    databases = sorted(plan, key=lambda name: (name != META_DB, name))

    if not args.yes and not confirm_all(date_folder, plan, databases):
        print("[CANCEL] Nothing was restored")
        return 1

    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="restore_all_"))
    failures = {}
    restored = []
    created_passwords = {}
    fetched = {}
    scanned = {source.name for source in sources}
    try:
        fetched, roles_backup, errors = fetch_dumps(folders, databases, date_folder, temp_dir, meta, with_roles=True)
        failures.update(errors)
        if roles_backup:
            print(f"[OK] Roles restored: {len(restore_roles(roles_backup))}")
        else:
            print(f"[WARN] {ROLES_FILE} not found, missing roles get new passwords")

        index = 0
        while index < len(databases):
            db = databases[index]
            index += 1
            if db not in fetched:
                continue
            source, filename = fetched[db]
            print("\n" + "="*60)
            print(f"[RESTORE] {db} <- {source.location('records', SERVER_NAME, date_folder)}/{filename}")
            print("="*60)
            skip_safety = args.skip_safety_backup or (db == META_DB and meta_is_empty(meta))
            try:
                validate_restore_name(db)
                password, _ = restore_database(
                    db,
                    lambda staging, db=db, source=source: restore_from_folder(db, temp_dir / source.name, into=staging),
                    roles_backup=roles_backup,
                    safety_targets=policy_targets(db, targets, policies) or [source],
                    meta=meta,
                    skip_safety=skip_safety,
                    new_password=False,
                )
                if password:
                    created_passwords[db] = password
            except (RestoreError, psycopg2.Error, subprocess.CalledProcessError) as e:
                print(f"[ERROR] {db}: {str(e).strip()}")
                failures[db] = str(e).strip()
                continue
            restored.append(db)

            if db != META_DB:
                if META_DB not in plan:
                    ensure_policies(meta, db)
                continue
            if meta is not None:
                meta.close()
            meta, targets, policies = open_meta()
            if meta is None:
                continue
            with meta.cursor() as cur:
                metadb.ensure_schema(cur)
            if args.target:
                continue
            extra_sources = [t for t in targets.values() if t.enabled and t.name not in scanned]
            scanned |= {t.name for t in extra_sources}
            extra_folders = collect_dumps(extra_sources, date_folder)
            extra_plan = {name: entry for name, entry in plan_dumps(extra_folders).items() if name not in plan}
            if not extra_plan:
                continue
            print(f"[FOUND] Targets restored from {META_DB} hold more databases: {', '.join(sorted(extra_plan))}")
            plan.update(extra_plan)
            databases.extend(sorted(extra_plan))
            more, _, errors = fetch_dumps(extra_folders, sorted(extra_plan), date_folder, temp_dir, meta, with_roles=False)
            fetched.update(more)
            failures.update(errors)
    finally:
        if meta is not None:
            meta.close()
        print(f"[CLEANUP] Removing temporary files: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    for db, password in created_passwords.items():
        print_connection_info(db, password)
    used = sorted({fetched[db][0].name for db in restored})
    print("\n" + "="*60)
    print(
        f"[SUMMARY] Restored {len(restored)}/{len(databases)} database(s) of {date_folder} "
        f"from {', '.join(used) or 'no target'}"
    )
    if failures:
        print(f"[SUMMARY] Failed: {', '.join(sorted(failures))}")
    if UNREACHABLE:
        print(f"[SUMMARY] Unreachable target(s): {unreachable_report()}")
        hidden = sorted(
            name
            for name, entries in (policies or {}).items()
            if name not in plan and any(target in UNREACHABLE for target, enabled in entries if enabled)
        )
        if hidden:
            print(f"[SUMMARY] Not found, their policy includes an unreachable target: {', '.join(hidden)}")
    print("="*60 + "\n")
    return 1 if failures or UNREACHABLE else 0


def list_backups(args, targets) -> int:
    if args.target:
        selected = [resolve_target_arg(args.target, targets)]
    elif targets is None:
        raise RestoreError(f"{META_DB} is unreachable, pass --target <rclone-remote>:<prefix>")
    else:
        selected = list(targets.values())
    if not selected:
        raise RestoreError(f"No backup target defined in {META_DB}")

    for target in selected:
        state = "" if target.enabled else " (disabled)"
        print(f"\n[{target.name}]{state} {target.location('records', SERVER_NAME)}")
        listing = scan(target, lambda t: list_dump_files(t, args.dbname))
        if listing is None:
            print("  (unreachable)")
            continue
        if not listing:
            print("  (no backups)")
            continue
        for date_folder in sorted(listing, key=folder_date, reverse=True):
            names = sorted(name.removesuffix(".gz").removesuffix(".sql") for name in listing[date_folder])
            detail = ", ".join(sorted(listing[date_folder])) if args.dbname else ", ".join(names)
            print(f"  {folder_date(date_folder).isoformat()}  {detail}")
    return finish()


def main():
    ap = argparse.ArgumentParser(
        description="Restore database from cloud backup with safety features",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Restore from latest cloud backup
  python restore.py my_django_db

  # Restore from specific date
  python restore.py my_django_db 2025-10-06

  # Skip safety backup
  python restore.py my_django_db --skip-safety-backup

  python restore.py my_django_db 2025-10-06 --target hetzner
  python restore.py --list [my_django_db]
  python restore.py my_django_db --from-file /tmp/my_django_db.sql.gz
  python restore.py --all 2025-10-06 --target hetzner:my-bucket
        """
    )
    ap.add_argument("dbname", nargs="?", help="Database name to restore")
    ap.add_argument("date", nargs="?", help="Backup date (YYYY-MM-DD), auto-detects latest if omitted")
    ap.add_argument("--target", help="Target name, or <rclone-remote>:<prefix> when backup_meta is unavailable")
    ap.add_argument("--list", action="store_true", help="List available backups per target")
    ap.add_argument("--from-file", metavar="PATH", help="Import a local .sql, .sql.gz or pg_dump -Fc file")
    ap.add_argument("--all", metavar="DATE", help="Restore roles, backup_meta and every database of DATE")
    ap.add_argument("--new-password", action="store_true", help="Give a missing role a new password instead of restoring it")
    ap.add_argument("--skip-safety-backup", action="store_true", help="Don't create safety backup before restore")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt of --all")
    args = ap.parse_args()

    if args.list and (args.date or args.from_file or args.all):
        ap.error("--list only accepts an optional database name and --target")
    if args.all and (args.dbname or args.from_file):
        ap.error("--all restores every database, do not pass a database name")
    if args.from_file and args.date:
        ap.error("--from-file does not take a date")
    if not (args.list or args.all) and not args.dbname:
        ap.error("database name is required")

    meta, targets, policies = open_meta()
    try:
        if args.list:
            status = list_backups(args, targets)
        elif args.all:
            status = restore_all(args, meta, targets, policies)
            meta = None
        elif args.from_file:
            status = restore_file(args, meta, targets, policies)
        else:
            status = restore_single(args, meta, targets, policies)
    except (RestoreError, metadb.MetaError, psycopg2.Error) as e:
        print(f"[ERROR] {str(e).strip()}")
        sys.exit(1)
    finally:
        if meta is not None:
            meta.close()
    sys.exit(status)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.stderr.write((e.stdout or b"").decode())
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\n[CANCEL] Restore cancelled by user")
        sys.exit(130)
