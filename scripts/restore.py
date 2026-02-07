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

# Import reusable dump function from backup.py
from backup import dump_single_db

PGHOST = os.getenv("POSTGRES_HOST")
PGPORT = os.getenv("POSTGRES_PORT")
PGUSER = os.getenv("POSTGRES_USER")
PGPASSWORD = os.getenv("POSTGRES_PASSWORD")
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "grdive:")


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


def list_cloud_backups():
    """
    List all backup folders in cloud storage (year/month/day structure).
    Returns list of paths like: ['2025/10/7', '2025/10/6', ...]
    """
    result = run(
        ["rclone", "lsf", f"{RCLONE_REMOTE}records/", "--dirs-only", "--recursive"],
        capture=True,
        check=True,
    )
    
    folders = result.stdout.decode().strip().split("\n")
    # Filter year/month/day folders
    date_folders = []
    for folder in folders:
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


def latest_cloud_backup():
    """Get the most recent backup folder from cloud"""
    folders = list_cloud_backups()
    if not folders:
        sys.exit(f"ERROR: No backups found in cloud storage: {RCLONE_REMOTE}records/")
    return folders[0]


def download_from_cloud(date_folder: str, local_temp_dir: pathlib.Path, dbname: str | None = None):
    """
    Download backup from cloud to local temp directory.

    Args:
        date_folder: Date folder path (e.g. "2025/10/7")
        local_temp_dir: Local temporary directory to download to
        dbname: If specified, download only this database's .sql file
    """
    if dbname:
        print(f"[DOWNLOAD] Fetching {dbname}.sql from cloud: {date_folder}")
        run(
            [
                "rclone",
                "copy",
                f"{RCLONE_REMOTE}records/{date_folder}/{dbname}.sql",
                str(local_temp_dir),
                "--progress",
            ]
        )
    else:
        print(f"[DOWNLOAD] Fetching full backup from cloud: {date_folder}")
        run(
            [
                "rclone",
                "copy",
                f"{RCLONE_REMOTE}records/{date_folder}",
                str(local_temp_dir),
                "--progress",
            ]
        )

    print(f"[OK] Download completed: {local_temp_dir}")


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
        sys.exit(f"ERROR: Invalid date format: {date_arg} (expected YYYY-MM-DD or YYYY/MM/DD)")
    
    # Validate date
    try:
        datetime.date(year, month, day)
    except ValueError:
        sys.exit(f"ERROR: Invalid date: {date_arg}")
    
    return f"{year}/{month}/{day}"


def find_cloud_backup(date_arg: str | None) -> str:
    """
    Find backup folder in cloud by date or return latest.
    Returns the date folder path (e.g. "2025/10/7").
    """
    if date_arg:
        date_path = parse_date_arg(date_arg)
        
        # Check if folder exists in cloud
        cloud_folders = list_cloud_backups()
        if date_path not in cloud_folders:
            sys.exit(f"ERROR: Backup not found in cloud: {date_path}\nAvailable: {', '.join(cloud_folders[:5])}")
        return date_path
    
    # Auto-select latest backup
    return latest_cloud_backup()


def check_file_in_cloud(date_folder: str, filename: str) -> bool:
    """
    Check if a specific file exists in a cloud backup folder using rclone lsf.
    No download needed — just lists remote files.
    """
    result = run(
        ["rclone", "lsf", f"{RCLONE_REMOTE}records/{date_folder}", "--files-only"],
        capture=True,
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        return False
    files = result.stdout.decode().strip().split("\n")
    return filename in files


def guess_latest_cloud_backup_for_db(db: str) -> str:
    """
    Find the most recent cloud backup containing SQL dump for specified database.
    Uses rclone lsf to check file existence (no download needed).
    """
    cloud_folders = list_cloud_backups()

    for date_folder in cloud_folders:
        print(f"[SEARCH] Checking {date_folder} for {db}.sql...")

        if check_file_in_cloud(date_folder, f"{db}.sql"):
            print(f"[FOUND] Database backup found in: {date_folder}")
            return date_folder

    sys.exit(f"ERROR: No backup found for database '{db}' in cloud storage")


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


def safety_backup_before_restore(db: str) -> str | None:
    """
    Create a timestamped safety backup before destructive restore operation.
    Uploads directly to cloud (no local storage).
    Returns cloud path for reference.
    """
    if not db_exists(db):
        print(f"[INFO] Database '{db}' does not exist yet, skipping safety backup")
        return None
    
    today = datetime.date.today()
    timestamp = datetime.datetime.now().strftime("%H-%M-%S")
    filename = f"{db}_before_restore_{timestamp}.sql"
    cloud_path = f"manual_backups/{today.year}/{today.month}/{today.day}"
    
    # Create temp file for safety backup
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="safety_backup_"))
    backup_file = temp_dir / filename
    
    try:
        print(f"[SAFETY] Creating backup before restore: {filename}")
        dump_single_db(db, backup_file)
        
        # Upload to cloud
        print(f"[UPLOAD] Uploading safety backup to cloud: {cloud_path}/")
        run(
            [
                "rclone",
                "copy",
                str(backup_file),
                f"{RCLONE_REMOTE}{cloud_path}/",
            ]
        )
        print(f"[OK] Safety backup uploaded: {cloud_path}/{filename}")
        
        return f"{cloud_path}/{filename}"
        
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Safety backup failed: {e}")
        return None
    
    finally:
        # Always cleanup temp
        shutil.rmtree(temp_dir, ignore_errors=True)


def drop_create_db(db: str):
    """Drop and recreate database"""
    print(f"[DROP] Dropping database: {db}")
    run(["dropdb", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, db], check=False)
    
    print(f"[CREATE] Creating fresh database: {db}")
    run(["createdb", "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, db])


def restore_from_folder(db: str, folder: pathlib.Path, cleanup_after: bool = False):
    """
    Restore database from SQL backup file.
    
    Args:
        db: Database name
        folder: Local folder containing backup files
        cleanup_after: If True, delete folder after successful restore
    """
    sql_path = folder / f"{db}.sql"

    if not sql_path.exists():
        candidates = sorted([p.name for p in folder.glob("*.sql")])
        hint = (
            f"Available SQL files: {', '.join(candidates)}"
            if candidates
            else "No SQL files found in folder"
        )
        sys.exit(f"ERROR: No backup found for '{db}' in {folder.name}. {hint}")
    
    print(f"[RESTORE] Loading SQL: {sql_path.name} (this may take a while...)")
    try:
        run(
            [
                "psql",
                "-h", PGHOST,
                "-p", PGPORT,
                "-U", PGUSER,
                "-d", db,
                "-q",  # Quiet mode
                "-f", str(sql_path),
            ],
            quiet=True  # Suppress output
        )
        print("[OK] Restore completed successfully")
    except subprocess.CalledProcessError:
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


def cleanup_old_manual_backups(days: int = 15):
    """Clean up old manual/safety backups from cloud (year/month/day structure)"""
    print(f"[CLOUD-CLEANUP] Checking cloud manual backups older than {days} days...")
    try:
        result = run(
            ["rclone", "lsf", f"{RCLONE_REMOTE}manual_backups/", "--dirs-only", "--recursive"],
            capture=True,
            check=False,
        )
        
        if result.returncode == 0:
            cutoff_date = datetime.date.today() - datetime.timedelta(days=days)
            folders = result.stdout.decode().strip().split("\n")
            
            for folder in folders:
                folder = folder.rstrip("/")
                parts = folder.split("/")
                if len(parts) == 3:
                    try:
                        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                        folder_date = datetime.date(year, month, day)
                        
                        if folder_date < cutoff_date:
                            print(f"[CLOUD-DELETE] {folder}")
                            run(
                                ["rclone", "purge", f"{RCLONE_REMOTE}manual_backups/{folder}"],
                                check=False,
                            )
                    except (ValueError, IndexError):
                        continue
        
        print("[OK] Cloud cleanup completed")
    except Exception as e:
        print(f"[WARN] Cloud cleanup failed: {e}")


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
        """
    )
    ap.add_argument("dbname", help="Database name to restore")
    ap.add_argument("date", nargs="?", help="Backup date (YYYY-MM-DD), auto-detects latest if omitted")
    ap.add_argument("--skip-safety-backup", action="store_true", help="Don't create safety backup before restore")
    args = ap.parse_args()

    db = args.dbname

    # Find appropriate cloud backup
    if args.date:
        date_folder = find_cloud_backup(args.date)
    else:
        date_folder = guess_latest_cloud_backup_for_db(db)

    print("="*60)
    print(f"[INFO] Source       : {RCLONE_REMOTE}records/{date_folder}")
    print(f"[INFO] Target database: {db}")
    print(f"[INFO] Server        : {PGUSER}@{PGHOST}:{PGPORT}")
    print("="*60 + "\n")

    # Download only the single .sql file from cloud
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="restore_"))
    try:
        download_from_cloud(date_folder, temp_dir, dbname=db)

        # Verify backup was downloaded
        if not (temp_dir / f"{db}.sql").exists():
            sys.exit(f"ERROR: No backup for '{db}' in {date_folder}.")

        # Safety backup (before destructive operation)
        safety_cloud_path = None
        if not args.skip_safety_backup:
            safety_cloud_path = safety_backup_before_restore(db)
            if safety_cloud_path:
                print(f"[INFO] Safety backup stored in cloud: {RCLONE_REMOTE}{safety_cloud_path}\n")
        else:
            print("[WARN] Safety backup skipped\n")

        # Terminate connections and recreate DB
        terminate_connections(db)
        drop_create_db(db)

        # Restore data
        restore_from_folder(db, temp_dir)

        # Verify
        list_tables(db)

        # Cleanup old manual backups
        cleanup_old_manual_backups(15)

        print("\n" + "="*60)
        print("[SUCCESS] Restore completed")
        if safety_cloud_path:
            print(f"[INFO] Safety backup: {RCLONE_REMOTE}{safety_cloud_path}")
        print("="*60 + "\n")
        
    finally:
        # Always cleanup temp directory
        print(f"[CLEANUP] Removing temporary files: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.stderr.write((e.stdout or b"").decode())
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\n[CANCEL] Restore cancelled by user")
        sys.exit(130)

