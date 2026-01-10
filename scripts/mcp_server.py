import os
import asyncio
import logging
import json
import contextlib
import io
import datetime
import tempfile
import shutil
import subprocess
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

@mcp.tool()
def clone_database(source_db: str, target_db: str) -> str:
    """
    Clone a database to a new database.

    Args:
        source_db: Name of the existing database to clone from.
        target_db: Name of the new database to create.
    """
    if clone.db_exists(target_db):
         return f"Error: Target database '{target_db}' already exists. Clone aborted."

    if not clone.db_exists(source_db):
        return f"Error: Source database '{source_db}' does not exist."

    try:
        clone.create_db(target_db)
        clone.clone_db(source_db, target_db)
        return f"Successfully cloned '{source_db}' to '{target_db}'"
    except Exception as e:
        return f"Clone failed: {str(e)}"

@mcp.tool()
def delete_database(dbname: str, confirm: bool = False) -> str:
    """
    Delete a database. Requires confirmation.

    Args:
        dbname: Name of the database to delete.
        confirm: Set to True to confirm deletion.
    """
    if not confirm:
        return f"WARNING: Database '{dbname}' will be PERMANENTLY DELETED. This cannot be undone.\nTo proceed, please call this function again with confirm=True."

    if not restore.db_exists(dbname):
        return f"Database '{dbname}' does not exist."

    try:
        # Terminate connections first
        restore.terminate_connections(dbname)

        # Drop database using subprocess directly or via a helper
        # restore.drop_create_db drops and creates, we just want drop.
        # We can use subprocess here directly or define a helper.
        # Let's use subprocess directly for simplicity as we have access to env vars via os.environ if needed,
        # but better to use the pattern in restore.py/mkdb.py if possible.
        # restore.run is not easily importable as a standalone without 'restore.' prefix.

        pg_host = os.getenv("POSTGRES_HOST")
        pg_port = os.getenv("POSTGRES_PORT")
        pg_user = os.getenv("POSTGRES_USER")
        pg_password = os.getenv("POSTGRES_PASSWORD")

        env = os.environ.copy()
        if pg_password:
            env["PGPASSWORD"] = pg_password

        cmd = ["dropdb", "-h", pg_host, "-p", pg_port, "-U", pg_user, dbname]

        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)

        return f"Database '{dbname}' successfully deleted."

    except subprocess.CalledProcessError as e:
        return f"Failed to delete database: {e.stderr}"
    except Exception as e:
        return f"Error deleting database: {str(e)}"

@mcp.tool()
def run_sql(dbname: str, query: str, confirm: bool = False) -> str:
    """
    Run a raw SQL query on a database.
    WARNING: This can be dangerous. Use with caution.

    Args:
        dbname: Name of the database to run the query on.
        query: The SQL query to execute.
        confirm: Set to True to confirm execution.
    """
    if not confirm:
        return f"WARNING: You are about to execute the following SQL on database '{dbname}':\n\n{query}\n\nThis operation requires confirmation. Call again with confirm=True."

    pg_host = os.getenv("POSTGRES_HOST")
    pg_port = os.getenv("POSTGRES_PORT")
    pg_user = os.getenv("POSTGRES_USER")
    pg_password = os.getenv("POSTGRES_PASSWORD")

    env = os.environ.copy()
    if pg_password:
        env["PGPASSWORD"] = pg_password

    cmd = ["psql", "-h", pg_host, "-p", pg_port, "-U", pg_user, "-d", dbname, "-c", query]

    try:
        result = subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)

        output = []
        if result.stdout:
            output.append("STDOUT:\n" + result.stdout)
        if result.stderr:
            output.append("STDERR:\n" + result.stderr)

        return "\n".join(output) if output else "Query executed successfully (no output)."

    except subprocess.CalledProcessError as e:
        return f"SQL execution failed:\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}"
    except Exception as e:
        return f"Error executing SQL: {str(e)}"

def main():
    # Initialize and run the server
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()