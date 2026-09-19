#!/usr/bin/env python3
"""
Automated PostgreSQL backup system with cloud-ready architecture.
Runs daily via cron, dumps all databases to separate SQL files.
"""
import os
import subprocess
import pathlib
import datetime
import sys
import json
import hashlib
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass

import psycopg2

import metadb
from common import BACKUP_PASSWORD, BACKUP_ROLE, META_DB, PGHOST, PGPORT, SERVER_NAME, connect

PGUSER = BACKUP_ROLE
PGPASSWORD = BACKUP_PASSWORD
HEALTHCHECK_URL = os.getenv("BACKUP_HEALTHCHECK_URL")

DUMP_SUFFIX = ".sql.gz"
DUMP_SUFFIXES = (".sql", ".sql.gz")
ROLES_FILE = "roles.json"
MANIFEST_FILE = "manifest.json"
CHECKSUM_FILE = "SHA256SUMS"

ROLES_QUERY = """
SELECT r.rolname, r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
       r.rolreplication, r.rolbypassrls, r.rolconnlimit, r.rolvaliduntil, r.rolpassword,
       coalesce(
           (SELECT json_agg(json_build_object('role', g.rolname, 'admin', m.admin_option,
                                              'inherit', m.inherit_option, 'set', m.set_option)
                            ORDER BY g.rolname)
            FROM pg_auth_members m
            JOIN pg_roles g ON g.oid = m.roleid
            WHERE m.member = r.oid),
           '[]'::json
       ),
       coalesce((SELECT s.setconfig FROM pg_db_role_setting s WHERE s.setrole = r.oid AND s.setdatabase = 0), '{}')
FROM pg_authid r
WHERE r.rolname !~ '^pg_'
ORDER BY r.rolname
"""


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class DumpFile:
    path: pathlib.Path
    sha256: str
    size: int


def run(cmd, env=None, check=True, capture=False, cwd=None):
    """Execute shell command with optional environment"""
    print(f"[RUN] {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        env=env,
        check=check,
        cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def list_databases():
    """Get list of all non-template databases"""
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    sql = "SELECT datname FROM pg_database WHERE datistemplate=false AND datname<>'postgres';"
    res = run(
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", "postgres", "-tA", "-c", sql],
        env=env,
        capture=True,
    )
    return [x.strip() for x in (res.stdout or b"").decode().splitlines() if x.strip()]


def dump_single_db(db: str, output_path: pathlib.Path):
    """
    Dump a single database to SQL file.
    Reusable function for both automated and manual backups.

    Args:
        db: Database name
        output_path: Full path to output .sql file
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pg_dump",
        "-h", PGHOST,
        "-p", PGPORT,
        "-U", PGUSER,
        "-d", db,
        "-f", str(output_path),
    ]
    if output_path.name.endswith(".gz"):
        cmd.append("--compress=gzip:6")

    run(cmd, env=env)
    print(f"[OK] Database dumped: {db} -> {output_path.name}")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(path: pathlib.Path) -> DumpFile:
    return DumpFile(path=path, sha256=file_sha256(path), size=path.stat().st_size)


def sha256sums(directory: pathlib.Path):
    """Generate SHA256 checksums for all SQL files in directory"""
    try:
        files = [p.name for p in directory.iterdir() if p.name.endswith(DUMP_SUFFIXES)]
        if not files:
            return

        checksum_file = directory / CHECKSUM_FILE
        with checksum_file.open("w") as f:
            for fname in sorted(files):
                f.write(f"{file_sha256(directory / fname)}  {fname}\n")

        print(f"[OK] Checksums generated: {checksum_file.name}")
    except Exception as e:
        print(f"[WARN] Checksum generation failed: {e}")


def move_existing_to_olds(target: metadb.Target, remote_path: str):
    """
    Check if backup already exists in cloud, if so move to olds/HH_MM/ subfolder.
    This prevents overwriting same-day backups.

    Args:
        remote_path: Remote path to check (e.g. records/2025/10/7)
    """
    # Check if any .sql files exist in the target path
    result = run(
        ["rclone", "lsf", target.location(remote_path), "--files-only"],
        capture=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        # No existing backup, safe to proceed
        return

    files = result.stdout.decode().strip().split("\n")
    sql_files = [f for f in files if f.endswith(DUMP_SUFFIXES)]

    if not sql_files:
        # No SQL files, safe to proceed
        return

    # Existing backup found, move to olds/HH_MM/
    now = datetime.datetime.now()
    olds_path = f"{remote_path}/olds/{now.strftime('%H_%M')}"

    print(f"[ARCHIVE] Existing backup found in {target.name}, moving to: {olds_path}")

    # Move all files from current path to olds subfolder
    run(
        [
            "rclone",
            "move",
            target.location(remote_path),
            target.location(olds_path),
            "--exclude", "olds/**",  # Don't move existing olds folder
        ],
        check=False,
    )

    print(f"[OK] Previous backup archived to: {olds_path}")


def upload_to_cloud(target: metadb.Target, local_dir: pathlib.Path, remote_path: str):
    """
    Upload backup directory to cloud storage using rclone.

    Args:
        local_dir: Local backup directory (e.g. /backups/records/2025/10/7)
        remote_path: Remote path relative to RCLONE_REMOTE (e.g. records/2025/10/7)
    """
    location = target.location(remote_path)
    print(f"[UPLOAD] Uploading {local_dir.name} to {location}")

    result = run(["rclone", "copy", str(local_dir), location], capture=True, check=False)
    output = (result.stdout or b"").decode(errors="replace").strip()
    if output:
        print(output)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout)

    print(f"[OK] Upload completed: {location}/{local_dir.name}")


def upload_file(target: metadb.Target, path: pathlib.Path, remote_path: str) -> str | None:
    try:
        upload_to_cloud(target, path, remote_path)
    except subprocess.CalledProcessError as e:
        lines = (e.output or b"").decode(errors="replace").strip().splitlines()
        return lines[-1] if lines else str(e)
    return None


def prune_cloud_backups(target: metadb.Target):
    """
    Delete cloud backup folders older than specified days.
    Searches recursively through year/month/day structure.
    """
    days = target.retention_days
    print(f"[CLOUD-CLEANUP] {target.name}: checking for backups older than {days} days...")

    cutoff_date = datetime.date.today() - datetime.timedelta(days=days)
    for base in (f"records/{SERVER_NAME}/", f"manual_backups/{SERVER_NAME}/"):
        # List all day-level directories recursively
        result = run(
            ["rclone", "lsf", target.location(base), "--dirs-only", "--recursive", "--max-depth", "3"],
            capture=True,
            check=False,
        )

        if result.returncode == 3:
            continue
        if result.returncode != 0:
            print(f"[WARN] Could not list cloud backups for cleanup: {target.location(base)}")
            continue

        folders = result.stdout.decode().strip().split("\n")

        for folder in folders:
            folder = folder.rstrip("/")
            # Parse folder path: year/month/day
            parts = folder.split("/")
            if len(parts) == 3:
                try:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    folder_date = datetime.date(year, month, day)

                    if folder_date < cutoff_date:
                        print(f"[CLOUD-DELETE] Removing old backup: {target.name}:{base}{folder}")
                        run(
                            ["rclone", "purge", target.location(base, folder)],
                            check=False,
                        )
                except (ValueError, IndexError):
                    continue

    print("[OK] Cloud cleanup completed")


def dump_roles(output_path: pathlib.Path):
    print("[ROLES] Connecting as superuser for this step only: password hashes in pg_authid are superuser-only")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(ROLES_QUERY)
            roles = [
                {
                    "name": row[0],
                    "superuser": row[1],
                    "inherit": row[2],
                    "createrole": row[3],
                    "createdb": row[4],
                    "login": row[5],
                    "replication": row[6],
                    "bypassrls": row[7],
                    "connection_limit": row[8],
                    "valid_until": row[9].isoformat() if row[9] else None,
                    "password": row[10],
                    "member_of": row[11],
                    "config": row[12],
                }
                for row in cur.fetchall()
            ]
            cur.execute("SHOW server_version")
            version = cur.fetchone()[0]
    finally:
        conn.close()

    payload = {
        "server": SERVER_NAME,
        "created_at": datetime.datetime.now().astimezone().isoformat(),
        "postgres_version": version,
        "roles": roles,
    }
    output_path.write_text(json.dumps(payload, indent=2))
    output_path.chmod(0o600)
    print(f"[OK] Roles dumped: {len(roles)} role(s) -> {output_path.name}")


def record(meta, result: dict, **run_fields):
    try:
        with meta.cursor() as cur:
            metadb.record_run(cur, **run_fields)
    except psycopg2.Error as e:
        error_msg = f"Could not record run in {META_DB}: {str(e).strip()}"
        print(f"[ERROR] {error_msg}")
        result["errors"].append(error_msg)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def backup_single_database(dbname: str):
    """
    Backup a single database to cloud storage (records/YYYY/MM/DD/).
    Unlike backup_all_databases, this only uploads the single .sql file
    without archiving or touching other files in the same date folder.

    Returns dict with backup status.
    """
    today = datetime.date.today()
    today_path = f"{today.year}/{today.month}/{today.day}"
    remote_path = f"records/{SERVER_NAME}/{today_path}"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "status": "success",
        "timestamp": datetime.datetime.now().isoformat(),
        "cloud_path": remote_path,
        "database": dbname,
        "targets": {},
        "errors": []
    }

    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"backup_{dbname}_{timestamp}_"))
    meta = None

    try:
        print(f"[{result['timestamp']}] Single database backup started: {dbname}")
        print(f"[TEMP] Using temporary directory: {temp_dir}")

        meta = metadb.connect_meta(as_backup=True)
        with meta.cursor() as cur:
            targets = metadb.load_targets(cur)
            policies = metadb.load_policies(cur)
        chosen, source = metadb.resolve_targets(dbname, targets, policies, metadb.default_target_names())
        if source == "default":
            print(f"[WARN] {dbname} has no enabled backup policy, using default targets")
        if not chosen:
            raise BackupError(f"No available backup target for {dbname}")

        # Dump the single database
        output_file = temp_dir / f"{dbname}{DUMP_SUFFIX}"
        dump_single_db(dbname, output_file)
        dumped = describe(output_file)

        # Upload only this file to cloud (rclone copy merges, won't touch other files)
        for target in chosen:
            started = now_utc()
            error = upload_file(target, output_file, remote_path)
            record(
                meta,
                result,
                datname=dbname,
                target=target.name,
                kind="manual",
                started_at=started,
                duration=now_utc() - started,
                folder=target.location(remote_path),
                filename=output_file.name,
                size_bytes=dumped.size,
                sha256=dumped.sha256,
                status="failed" if error else "success",
                error=error,
            )
            result["targets"][target.name] = "failed" if error else "success"
            if error:
                error_msg = f"Upload to {target.name} failed: {error}"
                print(f"[ERROR] {error_msg}")
                result["errors"].append(error_msg)

        if result["errors"]:
            result["status"] = "partial"

        print(f"[{datetime.datetime.now().isoformat()}] Single database backup completed: {dbname}")

    except Exception as e:
        result["status"] = "failed"
        result["errors"].append(str(e))
        print(f"[ERROR] Backup failed: {e}")
        raise

    finally:
        if meta is not None:
            meta.close()
        if temp_dir.exists():
            print(f"[CLEANUP] Removing temporary files: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)

    return result


def backup_all_databases():
    """
    Main backup function: dump all databases to temp, upload to cloud, cleanup temp.
    Returns dict with backup status for API/logging.
    """
    today = datetime.date.today()
    today_path = f"{today.year}/{today.month}/{today.day}"
    remote_path = f"records/{SERVER_NAME}/{today_path}"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "status": "success",
        "timestamp": datetime.datetime.now().isoformat(),
        "cloud_path": remote_path,
        "databases": [],
        "targets": {},
        "warnings": [],
        "errors": []
    }

    # Create temporary directory for backup
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"backup_{timestamp}_"))
    meta = None

    try:
        print(f"[{result['timestamp']}] Backup started")
        print(f"[TEMP] Using temporary directory: {temp_dir}")

        meta = metadb.connect_meta(as_backup=True)
        with meta.cursor() as cur:
            targets = metadb.load_targets(cur)
            policies = metadb.load_policies(cur)
            cur.execute("SHOW server_version")
            server_version = cur.fetchone()[0]
        active = [target for target in targets.values() if target.enabled]
        if not active:
            raise BackupError(f"No enabled backup target in {META_DB}.targets")
        defaults = metadb.default_target_names()

        plan = {}
        for db in list_databases():
            chosen, source = metadb.resolve_targets(db, targets, policies, defaults)
            if source == "default":
                warning = (
                    f"{db} has no enabled backup policy, using default targets: "
                    f"{', '.join(target.name for target in chosen) or 'none'}"
                )
                print(f"[WARN] {warning}")
                result["warnings"].append(warning)
            if not chosen:
                error_msg = f"{db} has no available backup target (BACKUP_DEFAULT_TARGETS: {', '.join(defaults) or 'empty'})"
                print(f"[ERROR] {error_msg}")
                result["errors"].append(error_msg)
                continue
            plan[db] = (chosen, source)

        # Dump each database to temp
        dumps = {}
        for db, (chosen, source) in plan.items():
            output_file = temp_dir / "dumps" / f"{db}{DUMP_SUFFIX}"
            started = now_utc()
            try:
                print(f"[DUMP] Database: {db}")
                dump_single_db(db, output_file)
                dumps[db] = describe(output_file)
                result["databases"].append(db)
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to dump {db}: {e}"
                print(f"[ERROR] {error_msg}")
                result["errors"].append(error_msg)
                for target in chosen:
                    record(
                        meta,
                        result,
                        datname=db,
                        target=target.name,
                        kind="scheduled",
                        started_at=started,
                        duration=now_utc() - started,
                        folder=target.location(remote_path),
                        filename=output_file.name,
                        size_bytes=None,
                        sha256=None,
                        status="failed",
                        error=error_msg,
                    )

        roles_file = temp_dir / "dumps" / ROLES_FILE
        roles_dump = None
        try:
            roles_file.parent.mkdir(parents=True, exist_ok=True)
            dump_roles(roles_file)
            roles_dump = describe(roles_file)
        except (psycopg2.Error, OSError) as e:
            error_msg = f"Failed to dump roles: {str(e).strip()}"
            print(f"[ERROR] {error_msg}")
            result["errors"].append(error_msg)

        # Upload to cloud
        for target in active:
            folder = target.location(remote_path)
            stage = temp_dir / "stage" / target.name
            stage.mkdir(parents=True)
            failures = 0
            files = {}

            # Archive existing backup if any
            move_existing_to_olds(target, remote_path)

            # Upload new backup
            uploads = [
                (db, chosen, source, dumps.get(db), f"{db}{DUMP_SUFFIX}", "scheduled")
                for db, (chosen, source) in plan.items()
                if target in chosen
            ]
            uploads.append((None, active, "all", roles_dump, ROLES_FILE, "roles"))

            for db, chosen, source, dumped, filename, kind in uploads:
                entry = {
                    "database": db,
                    "targets": [t.name for t in chosen],
                    "source": source,
                    "sha256": dumped.sha256 if dumped else None,
                    "size": dumped.size if dumped else None,
                }
                if dumped is None:
                    files[filename] = {**entry, "status": "failed", "error": "dump failed"}
                    failures += 1
                    if kind == "roles":
                        record(
                            meta,
                            result,
                            datname=None,
                            target=target.name,
                            kind=kind,
                            started_at=now_utc(),
                            duration=datetime.timedelta(0),
                            folder=folder,
                            filename=filename,
                            size_bytes=None,
                            sha256=None,
                            status="failed",
                            error="roles dump failed",
                        )
                    continue

                started = now_utc()
                error = upload_file(target, dumped.path, remote_path)
                record(
                    meta,
                    result,
                    datname=db,
                    target=target.name,
                    kind=kind,
                    started_at=started,
                    duration=now_utc() - started,
                    folder=folder,
                    filename=filename,
                    size_bytes=dumped.size,
                    sha256=dumped.sha256,
                    status="failed" if error else "success",
                    error=error,
                )
                if error:
                    error_msg = f"Upload of {filename} to {target.name} failed: {error}"
                    print(f"[ERROR] {error_msg}")
                    result["errors"].append(error_msg)
                    files[filename] = {**entry, "status": "failed", "error": error}
                    failures += 1
                    continue
                files[filename] = {**entry, "status": "success"}
                os.link(dumped.path, stage / filename)

            # Generate checksums
            sha256sums(stage)

            manifest = {
                "server": SERVER_NAME,
                "date": today.isoformat(),
                "created_at": datetime.datetime.now().astimezone().isoformat(),
                "target": target.name,
                "folder": folder,
                "postgres_version": server_version,
                "targets": {
                    t.name: {
                        "type": t.type,
                        "remote": t.remote,
                        "prefix": t.prefix,
                        "retention_days": t.retention_days,
                        "enabled": t.enabled,
                    }
                    for t in targets.values()
                },
                "policies": {
                    db: [name for name, enabled in entries if enabled]
                    for db, entries in policies.items()
                },
                "default_targets": defaults,
                "files": files,
            }
            (stage / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))

            for index_file in (CHECKSUM_FILE, MANIFEST_FILE):
                if not (stage / index_file).exists():
                    continue
                error = upload_file(target, stage / index_file, remote_path)
                if error:
                    error_msg = f"Upload of {index_file} to {target.name} failed: {error}"
                    print(f"[ERROR] {error_msg}")
                    result["errors"].append(error_msg)
                    failures += 1

            result["targets"][target.name] = {"folder": folder, "failures": failures}
            if failures:
                print(f"[WARN] {target.name}: {failures} failure(s), skipping retention cleanup")
            else:
                print(f"[OK] Backup uploaded to {target.name}: {folder}")
                prune_cloud_backups(target)

        if result["errors"]:
            result["status"] = "partial"

        print(f"[{datetime.datetime.now().isoformat()}] Backup completed")

    except Exception as e:
        result["status"] = "failed"
        result["errors"].append(str(e))
        print(f"[ERROR] Backup failed: {e}")
        raise

    finally:
        if meta is not None:
            meta.close()
        # Always cleanup temp directory
        if temp_dir.exists():
            print(f"[CLEANUP] Removing temporary files: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)

    return result


def ping_healthcheck(success: bool, message: str):
    if not HEALTHCHECK_URL:
        return
    url = HEALTHCHECK_URL.rstrip("/") + ("" if success else "/fail")
    try:
        request = urllib.request.Request(url, data=message.encode()[:10000], method="POST")
        urllib.request.urlopen(request, timeout=10).close()
        print(f"[HEALTHCHECK] Reported {'success' if success else 'failure'}")
    except OSError as e:
        print(f"[WARN] Healthcheck ping failed: {e}")


def main():
    """
    CLI entry point.

    Usage:
      python backup.py              # Backup all databases (cron mode)
      python backup.py <dbname>     # Backup a single database
      python backup.py --json       # Output JSON result
    """
    try:
        # Check if a database name was given (first non-flag argument)
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        json_output = "--json" in sys.argv

        if args:
            dbname = args[0]
            result = backup_single_database(dbname)
        else:
            try:
                result = backup_all_databases()
            except Exception as e:
                ping_healthcheck(False, f"Backup failed: {e}")
                raise
            summary = "\n".join(
                [f"status: {result['status']}", *result["warnings"], *result["errors"]]
            )
            ping_healthcheck(result["status"] == "success", summary)

        if json_output:
            print(json.dumps(result, indent=2, default=str))

        sys.exit(0 if result["status"] == "success" else 1)

    except subprocess.CalledProcessError as e:
        sys.stderr.write((e.stdout or b"").decode())
        sys.exit(e.returncode)
    except Exception as e:
        sys.stderr.write(f"Fatal error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
