"""Service layer wrapping existing backup scripts."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from django.conf import settings

SCRIPTS_DIR = Path(settings.SCRIPTS_PATH)
PYTHON_BIN = sys.executable
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "grdive:")
CRON_CONTAINER = os.getenv("BACKUP_CONTAINER_NAME", "shared-pgbackup")

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import backup as backup_module  # type: ignore
except Exception:
    backup_module = None


@dataclass
class CommandResult:
    ok: bool
    stdout: list[str]
    stderr: list[str]


def _run_command(cmd: list[str], cwd: Path | None = None) -> CommandResult:
    """Execute command and capture stdout/stderr."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return CommandResult(False, [], [f"Komut bulunamadı: {cmd[0]}"])

    stdout_lines = proc.stdout.splitlines() if proc.stdout else []
    stderr_lines = proc.stderr.splitlines() if proc.stderr else []
    return CommandResult(proc.returncode == 0, stdout_lines, stderr_lines)


def _extract_json(lines: list[str]) -> tuple[list[str], dict[str, Any] | None]:
    """Split log lines and optional trailing JSON blob."""
    for idx, line in enumerate(lines):
        striped = line.strip()
        if striped.startswith("{") and striped.endswith("}"):
            try:
                payload = json.loads("\n".join(lines[idx:]))
                return lines[:idx], payload
            except json.JSONDecodeError:
                continue
    return lines, None


def _list_cloud_folders() -> CommandResult:
    """List date-based folders under records/."""
    return _run_command(
        ["rclone", "lsf", f"{RCLONE_REMOTE}records/", "--dirs-only", "--recursive"]
    )


def _list_backup_files(date_folder: str) -> list[str]:
    """List SQL filenames under a specific backup folder."""
    result = _run_command(
        ["rclone", "lsf", f"{RCLONE_REMOTE}records/{date_folder}", "--files-only"]
    )
    return [line.strip() for line in result.stdout if line.strip() and line.endswith(".sql")]


def _available_databases() -> list[str]:
    """Return list of databases using backup module helper."""
    if not backup_module:
        return []
    try:
        return backup_module.list_databases()
    except Exception:
        return []


def _parse_backup_folders(limit: int = 7) -> tuple[list[dict[str, Any]], list[str]]:
    """Return parsed backup metadata and potential warnings."""
    warnings: list[str] = []
    result = _list_cloud_folders()
    if not result.ok:
        warnings.extend(result.stderr or ["Bulut klasörleri listelenemedi."])
        return [], warnings

    rows: list[dict[str, Any]] = []
    for raw in result.stdout:
        folder = raw.rstrip("/")
        parts = folder.split("/")
        if len(parts) != 3:
            continue
        try:
            year, month, day = map(int, parts)
            date_obj = dt.date(year, month, day)
        except ValueError:
            continue

        files = _list_backup_files(folder)
        rows.append(
            {
                "date_obj": date_obj,
                "date": date_obj.strftime("%Y-%m-%d"),
                "files": files,
                "raw": folder,
            }
        )

    rows.sort(key=lambda item: item["date_obj"], reverse=True)
    return rows[:limit], warnings


def cron_health(latest_backup: dt.date | None) -> dict[str, Any]:
    """Inspect docker health information for cron container."""
    freshness: str
    if latest_backup:
        delta = (dt.date.today() - latest_backup).days
        if delta == 0:
            freshness = "Son yedek bugün alındı."
        elif delta == 1:
            freshness = "Son yedek dün alındı."
        else:
            freshness = f"Son yedek {delta} gün önce alındı."
    else:
        freshness = "Bulutta hiç tarihli yedek bulunamadı."

    cmd = [
        "docker",
        "inspect",
        CRON_CONTAINER,
        "--format",
        "{{json .State}}",
    ]
    result = _run_command(cmd)
    status_message = "Docker durumu okunamadı."

    if result.ok and result.stdout:
        try:
            state = json.loads(result.stdout[0])
            health = state.get("Health", {}).get("Status", "unknown")
            status = state.get("Status", "unknown")
            started = state.get("StartedAt", "")
            status_message = f"Container {status} (health: {health})"
            if started:
                status_message += f" • Başlangıç: {started}"
        except (json.JSONDecodeError, IndexError):
            status_message = "Docker inspect çıktısı çözümlenemedi."
    elif result.stderr:
        status_message = result.stderr[0]

    return {
        "status": status_message,
        "freshness": freshness,
        "container": CRON_CONTAINER,
    }


def build_dashboard_context() -> dict[str, Any]:
    """Collect dashboard data from cloud storage and cron inspection."""
    backups, warnings = _parse_backup_folders()
    latest = backups[0] if backups else None
    latest_info = {
        "date": latest["date"] if latest else None,
        "count": len(latest["files"]) if latest else 0,
        "warnings": warnings,
    }
    cron_info = cron_health(latest["date_obj"] if latest else None)

    return {
        "latest_backup": latest_info,
        "cron": cron_info,
        "backups": backups,
        "warnings": warnings,
        "databases": _available_databases(),
    }


def run_backup() -> Tuple[bool, List[str]]:
    """Trigger backup script and return success flag with details."""
    cmd = [PYTHON_BIN, str(SCRIPTS_DIR / "backup.py"), "--json"]
    result = _run_command(cmd, cwd=SCRIPTS_DIR)
    log_lines, payload = _extract_json(result.stdout)
    details: list[str] = log_lines[:]

    if payload:
        status = payload.get("status")
        databases = payload.get("databases", [])
        details.append(f"Durum: {status} | Veritabanları: {', '.join(databases) or 'yok'}")
        cloud_path = payload.get("cloud_path")
        if cloud_path:
            details.append(f"Bulut dizini: {cloud_path}")

    details.extend(f"Hata: {line}" for line in result.stderr)
    success = payload is not None and payload.get("status") in {"success", "partial"}
    if not payload:
        success = result.ok
    return success, details or ["Çıktı alınamadı."]


def run_restore(db: str, date: str | None, skip_safety: bool) -> Tuple[bool, List[str]]:
    """Trigger restore script for given database."""
    cmd: list[str] = [PYTHON_BIN, str(SCRIPTS_DIR / "restore.py"), db]
    clean_date = date.strip() if date else ""
    if clean_date:
        cmd.append(clean_date)
    if skip_safety:
        cmd.append("--skip-safety-backup")

    result = _run_command(cmd, cwd=SCRIPTS_DIR)
    details = result.stdout + [f"Hata: {line}" for line in result.stderr]
    return result.ok, details or ["Restore betiğinden çıktı alınamadı."]


def get_cron_status() -> Iterable[str]:
    """Return human-readable cron status messages for the UI."""
    ctx = build_dashboard_context()
    cron = ctx.get("cron", {})
    messages = [
        f"Cron container: {cron.get('container', CRON_CONTAINER)}",
        cron.get("status", "Durum alınamadı."),
        cron.get("freshness", ""),
    ]
    return [m for m in messages if m]
