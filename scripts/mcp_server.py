import os
import asyncio
import logging
import json
import contextlib
import io
import datetime
import tempfile
import shutil
from pathlib import Path
from typing import Optional, List
from mcp.server.fastmcp import FastMCP

# Import existing scripts
import backup
import mkdb
import restore
import clone
from mcp.server.transport_security import TransportSecuritySettings

sec = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
    allowed_hosts=["127.0.0.1:*", "*", "[::1]:*"],
    allowed_origins=["*", "http://localhost:*", "http://[::1]:*"],
)


# Initialize FastMCP server
mcp = FastMCP("Postgres Manager", host="0.0.0.0", port=8080, transport_security=sec)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")

def capture_output(func, *args, **kwargs):
    """Helper to capture stdout/stderr from existing script functions"""
    f = io.StringIO()
    with contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        try:
            func(*args, **kwargs)
            success = True
            error = None
        except Exception as e:
            success = False
            error = str(e)
            print(f"Error: {e}") # This goes to the captured buffer

    return success, f.getvalue(), error

@mcp.tool()
def list_databases() -> str:
    """List all non-template databases."""
    try:
        dbs = backup.list_databases()
        return json.dumps(dbs, indent=2)
    except Exception as e:
        return f"Error listing databases: {str(e)}"

@mcp.tool()
def create_database(dbname: str) -> str:
    """Create a new database."""
    success, output, error = capture_output(mkdb.create_database, dbname)
    if success:
        # Also capture the connection info output
        _, info_output, _ = capture_output(mkdb.print_connection_info, dbname)
        return f"{output}\n{info_output}"
    else:
        return f"Failed to create database: {output}"

@mcp.tool()
def delete_database(dbname: str, confirm: bool = False) -> str:
    """
    Delete a database.

    Args:
        dbname: The name of the database to delete.
        confirm: Confirmation flag. Must be set to True to proceed with deletion.
    """
    if not confirm:
        return f"WARNING: Are you sure you want to delete database '{dbname}'? This action cannot be undone. To proceed, call this tool again with 'confirm=True'."

    # Re-use restore.py functions to kill connections and drop db
    try:
        output_log = io.StringIO()
        with contextlib.redirect_stdout(output_log), contextlib.redirect_stderr(output_log):
             restore.terminate_connections(dbname)
             # restore.drop_create_db(dbname) # This recreates it. We just want drop.
             print(f"[DROP] Dropping database: {dbname}")
             restore.run(["dropdb", "-h", restore.PGHOST, "-p", restore.PGPORT, "-U", restore.PGUSER, dbname])

        return f"Database '{dbname}' successfully deleted.\n{output_log.getvalue()}"

    except Exception as e:
        return f"Failed to delete database: {str(e)}"

@mcp.tool()
def clone_database(source_db: str, target_db: str) -> str:
    """
    Clone a database to a new database.

    Args:
        source_db: The name of the source database.
        target_db: The name of the new target database (must not exist).
    """
    success, output, error = capture_output(clone.clone_database, source_db, target_db)
    if success:
        return f"Successfully cloned {source_db} to {target_db}.\n{output}"
    else:
        return f"Failed to clone database: {output}\nError: {error}"

@mcp.tool()
def backup_all_databases() -> str:
    """Trigger a full backup of all databases."""
    try:
        # backup_all_databases returns a result dict
        result = backup.backup_all_databases()
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Backup process failed: {str(e)}"

@mcp.tool()
def backup_database(dbname: str) -> str:
    """Create a manual backup of a single database."""

    today = datetime.date.today()
    timestamp = datetime.datetime.now().strftime("%H-%M-%S")
    remote_path = f"manual_backups/{today.year}/{today.month}/{today.day}"

    temp_dir = Path(tempfile.mkdtemp(prefix=f"manual_backup_{dbname}_"))
    try:
        logger.info(f"Starting manual backup for {dbname}")
        output_file = temp_dir / f"{dbname}.sql"

        # Dump
        backup.dump_single_db(dbname, output_file)

        # Upload
        backup.upload_to_cloud(temp_dir, remote_path)

        return f"Successfully backed up {dbname} to {backup.RCLONE_REMOTE}{remote_path}/{dbname}.sql"

    except Exception as e:
        return f"Manual backup failed: {str(e)}"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@mcp.tool()
def list_backups() -> str:
    """List available backup dates from cloud storage."""
    try:
        dates = restore.list_cloud_backups()
        return json.dumps(dates, indent=2)
    except Exception as e:
        return f"Error listing backups: {str(e)}"

@mcp.tool()
def restore_database(dbname: str, date: Optional[str] = None) -> str:
    """
    Restore a database from a backup.

    Args:
        dbname: The name of the database to restore.
        date: Optional date (YYYY-MM-DD) of the backup to restore. Defaults to latest.
    """
    # This is a complex operation. We'll try to replicate restore.py main logic carefully.

    output_log = io.StringIO()

    def log(msg):
        output_log.write(msg + "\n")
        logger.info(msg)

    try:
        # Find appropriate cloud backup
        if date:
            date_folder = restore.find_cloud_backup(date)
        else:
            date_folder = restore.guess_latest_cloud_backup_for_db(dbname)

        log(f"Restoring {dbname} from {date_folder}")

        # Download backup from cloud to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="restore_"))
        try:
            # We can't easily capture output of subprocesses called inside these functions
            # unless we modify them or monkeypatch 'run'.
            # For now, we rely on the functions running and raising exceptions on failure.

            log("Downloading backup...")
            restore.download_from_cloud(date_folder, temp_dir)

            if not (temp_dir / f"{dbname}.sql").exists():
                raise Exception(f"Backup file for {dbname} not found in {date_folder}")

            # Safety backup
            log("Creating safety backup...")
            safety_path = restore.safety_backup_before_restore(dbname)
            if safety_path:
                log(f"Safety backup created at {safety_path}")
            else:
                log("Safety backup skipped (DB might not exist)")

            # Terminate connections and recreate DB
            log("Recreating database...")
            restore.terminate_connections(dbname)
            restore.drop_create_db(dbname)

            # Restore data
            log("Restoring data...")
            restore.restore_from_folder(dbname, temp_dir)

            log("Restore completed successfully.")
            return output_log.getvalue()

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        return f"Restore failed: {str(e)}\nLog:\n{output_log.getvalue()}"

def main():
    # Initialize and run the server
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()