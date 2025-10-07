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

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "15"))
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "grdive:")


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
        ["psql", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-tA", "-c", sql],
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
    
    run(cmd, env=env)
    print(f"[OK] Database dumped: {db} -> {output_path.name}")


def sha256sums(directory: pathlib.Path):
    """Generate SHA256 checksums for all SQL files in directory"""
    try:
        files = [p.name for p in directory.glob("*.sql")]
        if not files:
            return
        
        checksum_file = directory / "SHA256SUMS"
        with checksum_file.open("w") as f:
            for fname in sorted(files):
                res = run(["sha256sum", fname], cwd=str(directory), capture=True)
                f.write(res.stdout.decode())
        
        print(f"[OK] Checksums generated: {checksum_file.name}")
    except Exception as e:
        print(f"[WARN] Checksum generation failed: {e}")


def move_existing_to_olds(remote_path: str):
    """
    Check if backup already exists in cloud, if so move to olds/HH_MM/ subfolder.
    This prevents overwriting same-day backups.
    
    Args:
        remote_path: Remote path to check (e.g. records/2025/10/7)
    """
    # Check if any .sql files exist in the target path
    result = run(
        ["rclone", "lsf", f"{RCLONE_REMOTE}{remote_path}", "--files-only"],
        capture=True,
        check=False,
    )
    
    if result.returncode != 0 or not result.stdout.strip():
        # No existing backup, safe to proceed
        return
    
    files = result.stdout.decode().strip().split("\n")
    sql_files = [f for f in files if f.endswith(".sql")]
    
    if not sql_files:
        # No SQL files, safe to proceed
        return
    
    # Existing backup found, move to olds/HH_MM/
    now = datetime.datetime.now()
    olds_path = f"{remote_path}/olds/{now.strftime('%H_%M')}"
    
    print(f"[ARCHIVE] Existing backup found, moving to: {olds_path}")
    
    # Move all files from current path to olds subfolder
    run(
        [
            "rclone",
            "move",
            f"{RCLONE_REMOTE}{remote_path}",
            f"{RCLONE_REMOTE}{olds_path}",
            "--exclude", "olds/**",  # Don't move existing olds folder
        ],
        check=False,
    )
    
    print(f"[OK] Previous backup archived to: {olds_path}")


def upload_to_cloud(local_dir: pathlib.Path, remote_path: str):
    """
    Upload backup directory to cloud storage using rclone.
    
    Args:
        local_dir: Local backup directory (e.g. /backups/records/2025/10/7)
        remote_path: Remote path relative to RCLONE_REMOTE (e.g. records/2025/10/7)
    """
    print(f"[UPLOAD] Uploading to {RCLONE_REMOTE}{remote_path}")
    
    run(
        [
            "rclone",
            "copy",
            str(local_dir),
            f"{RCLONE_REMOTE}{remote_path}",
            "--progress",
        ]
    )
    
    print(f"[OK] Upload completed: {remote_path}")


def prune_cloud_backups(days: int):
    """
    Delete cloud backup folders older than specified days.
    Searches recursively through year/month/day structure.
    """
    print(f"[CLOUD-CLEANUP] Checking for backups older than {days} days...")
    
    # List all day-level directories recursively
    result = run(
        ["rclone", "lsf", f"{RCLONE_REMOTE}records/", "--dirs-only", "--recursive"],
        capture=True,
        check=False,
    )
    
    if result.returncode != 0:
        print("[WARN] Could not list cloud backups for cleanup")
        return
    
    cutoff_date = datetime.date.today() - datetime.timedelta(days=days)
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
                    print(f"[CLOUD-DELETE] Removing old backup: {folder}")
                    run(
                        ["rclone", "purge", f"{RCLONE_REMOTE}records/{folder}"],
                        check=False,
                    )
            except (ValueError, IndexError):
                continue
    
    print("[OK] Cloud cleanup completed")


def backup_all_databases():
    """
    Main backup function: dump all databases to temp, upload to cloud, cleanup temp.
    Returns dict with backup status for API/logging.
    """
    import tempfile
    import shutil
    
    today = datetime.date.today()
    today_path = f"{today.year}/{today.month}/{today.day}"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    result = {
        "status": "success",
        "timestamp": datetime.datetime.now().isoformat(),
        "cloud_path": f"records/{today_path}",
        "databases": [],
        "errors": []
    }
    
    # Create temporary directory for backup
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"backup_{timestamp}_"))
    
    try:
        print(f"[{result['timestamp']}] Backup started")
        print(f"[TEMP] Using temporary directory: {temp_dir}")
        
        # Dump each database to temp
        databases = list_databases()
        for db in databases:
            try:
                print(f"[DUMP] Database: {db}")
                output_file = temp_dir / f"{db}.sql"
                dump_single_db(db, output_file)
                result["databases"].append(db)
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to dump {db}: {e}"
                print(f"[ERROR] {error_msg}")
                result["errors"].append(error_msg)
        
        # Generate checksums
        sha256sums(temp_dir)
        
        # Upload to cloud
        try:
            # Archive existing backup if any
            move_existing_to_olds(f"records/{today_path}")
            
            # Upload new backup
            upload_to_cloud(temp_dir, f"records/{today_path}")
            result["cloud_uploaded"] = True
            print(f"[OK] Backup uploaded to cloud: records/{today_path}")
            
        except subprocess.CalledProcessError as e:
            error_msg = f"Cloud upload failed: {e}"
            print(f"[ERROR] {error_msg}")
            result["errors"].append(error_msg)
            result["cloud_uploaded"] = False
        
        # Cleanup old cloud backups
        prune_cloud_backups(RETENTION_DAYS)
        
        if result["errors"]:
            result["status"] = "partial"
        
        print(f"[{datetime.datetime.now().isoformat()}] Backup completed")
        
    except Exception as e:
        result["status"] = "failed"
        result["errors"].append(str(e))
        print(f"[ERROR] Backup failed: {e}")
        raise
    
    finally:
        # Always cleanup temp directory
        if temp_dir.exists():
            print(f"[CLEANUP] Removing temporary files: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return result


def main():
    """CLI entry point for automated cron jobs"""
    try:
        result = backup_all_databases()
        
        # Optional: output JSON for parsing by external tools
        if "--json" in sys.argv:
            print(json.dumps(result, indent=2))
        
        sys.exit(0 if result["status"] in ["success", "partial"] else 1)
        
    except subprocess.CalledProcessError as e:
        sys.stderr.write((e.stdout or b"").decode())
        sys.exit(e.returncode)
    except Exception as e:
        sys.stderr.write(f"Fatal error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
