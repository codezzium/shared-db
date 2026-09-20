#!/usr/bin/env python3
import argparse
import datetime
import os
import subprocess
import sys

import psycopg2
from psycopg2 import sql

import metadb
from common import META_DB, SERVER_NAME, connect, db_exists, local_time


def human_size(size: int | None) -> str:
    if size is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def print_table(headers: tuple, rows: list):
    rows = [tuple("-" if value is None else str(value) for value in row) for row in rows]
    widths = [max([len(str(header)), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)]
    print("  ".join(str(header).ljust(widths[i]) for i, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip())


def databases() -> list[str]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname")
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def target_list(cur, args):
    rows = [
        (t.name, t.type, t.location(), t.retention_days, "yes" if t.enabled else "no")
        for t in metadb.load_targets(cur).values()
    ]
    if not rows:
        print("No backup targets defined.")
        return
    print_table(("TARGET", "TYPE", "LOCATION", "RETENTION_DAYS", "ENABLED"), rows)


def target_add(cur, args):
    if args.type == "s3" and not args.prefix.strip("/"):
        raise metadb.MetaError("s3 targets need --prefix <bucket>")
    cur.execute(
        "INSERT INTO targets (name, type, remote, prefix, retention_days, enabled) VALUES (%s, %s, %s, %s, %s, %s)",
        (args.name, args.type, args.remote.rstrip(":"), args.prefix.strip("/"), args.retention, not args.disabled),
    )
    print(f"[OK] Target added: {args.name}")


def target_edit(cur, args):
    fields = {}
    if args.type:
        fields["type"] = args.type
    if args.remote:
        fields["remote"] = args.remote.rstrip(":")
    if args.prefix is not None:
        fields["prefix"] = args.prefix.strip("/")
    if args.retention:
        fields["retention_days"] = args.retention
    if args.enable:
        fields["enabled"] = True
    if args.disable:
        fields["enabled"] = False
    if not fields:
        raise metadb.MetaError("Nothing to change")
    assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(key)) for key in fields)
    cur.execute(sql.SQL("UPDATE targets SET {} WHERE name = %s").format(assignments), [*fields.values(), args.name])
    if cur.rowcount == 0:
        raise metadb.MetaError(f"Unknown target: {args.name}")
    print(f"[OK] Target updated: {args.name}")


def target_test(cur, args):
    target = metadb.require_targets(cur, [args.name])[args.name]
    probe = target.location("records", SERVER_NAME, f".backupctl-probe-{datetime.datetime.now():%Y%m%d%H%M%S}")
    print(f"[TEST] Writing and deleting {probe}")
    for command in (["rclone", "touch", probe], ["rclone", "deletefile", probe]):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            raise metadb.MetaError(f"{' '.join(command[:2])} failed: {lines[-1] if lines else result.returncode}")
    print(f"[OK] Target {args.name} is writable")


def set_policy(cur, args):
    if args.db == META_DB:
        raise metadb.MetaError(f"{META_DB} is always backed up to every enabled target")
    conn = connect()
    try:
        with conn.cursor() as pg:
            if not db_exists(pg, args.db):
                raise metadb.MetaError(f"Database does not exist: {args.db}")
    finally:
        conn.close()
    names = list(dict.fromkeys(args.targets))
    metadb.set_policies(cur, args.db, names)
    print(f"[OK] {args.db} -> {', '.join(names)}")


def show(cur, args):
    targets = metadb.load_targets(cur)
    policies = metadb.load_policies(cur)
    effective, source = metadb.resolve_targets(args.db, targets, policies, metadb.default_target_names())

    print(f"Database: {args.db}")
    entries = policies.get(args.db, [])
    if entries:
        print_table(
            ("TARGET", "POLICY", "TARGET_ENABLED"),
            [
                (name, "enabled" if enabled else "disabled", "yes" if name in targets and targets[name].enabled else "no")
                for name, enabled in entries
            ],
        )
    else:
        print("No explicit policy.")
    print(f"Effective targets ({source}): {', '.join(t.name for t in effective) or 'NONE'}")

    cur.execute(
        """
        SELECT DISTINCT ON (target) target, started_at, status, size_bytes, folder, filename, error
        FROM runs
        WHERE datname = %s AND kind IN ('scheduled', 'manual')
        ORDER BY target, started_at DESC
        """,
        (args.db,),
    )
    rows = cur.fetchall()
    if rows:
        print("\nLast backup per target:")
        print_table(
            ("TARGET", "STARTED (TRT)", "STATUS", "SIZE", "LOCATION", "ERROR"),
            [
                (target, local_time(started), status, human_size(size), f"{folder}/{filename}", error)
                for target, started, status, size, folder, filename, error in rows
            ],
        )


def list_policies(cur, args):
    targets = metadb.load_targets(cur)
    policies = metadb.load_policies(cur)
    defaults = metadb.default_target_names()
    rows = []
    for db in databases():
        effective, source = metadb.resolve_targets(db, targets, policies, defaults)
        rows.append((db, ", ".join(t.name for t in effective) or "NONE", source if effective else "missing"))
    print_table(("DATABASE", "TARGETS", "SOURCE"), rows)


def list_runs(cur, args):
    query = """
        SELECT started_at, coalesce(datname, '(roles)'), target, kind, status, size_bytes,
               date_trunc('second', duration), error
        FROM runs
        {where}
        ORDER BY started_at DESC
        LIMIT %s
    """
    if args.db:
        cur.execute(query.format(where="WHERE datname = %s"), (args.db, args.limit))
    else:
        cur.execute(query.format(where=""), (args.limit,))
    rows = cur.fetchall()
    if not rows:
        print("No backup runs recorded.")
        return
    print_table(
        ("STARTED (TRT)", "DATABASE", "TARGET", "KIND", "STATUS", "SIZE", "DURATION", "ERROR"),
        [
            (local_time(started), db, target, kind, status, human_size(size), duration, error)
            for started, db, target, kind, status, size, duration, error in rows
        ],
    )


def main():
    ap = argparse.ArgumentParser(
        description="Manage backup targets and per-database backup policies",
        epilog="Example: docker-compose exec pgbackup python /app/backupctl.py set my_django_db gdrive hetzner",
    )
    commands = ap.add_subparsers(dest="command", required=True)

    target = commands.add_parser("target", help="List, add, edit or test backup targets")
    actions = target.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="List backup targets").set_defaults(handler=target_list)

    add = actions.add_parser("add", help="Add a backup target")
    add.add_argument("name")
    add.add_argument("--type", choices=metadb.TARGET_TYPES, required=True)
    add.add_argument("--remote", required=True, help="rclone remote name, e.g. grdive or hetzner")
    add.add_argument("--prefix", default="", help="Path inside the remote; the bucket name for s3")
    add.add_argument("--retention", type=int, default=int(os.getenv("BACKUP_RETENTION_DAYS", "15")), help="Days to keep")
    add.add_argument("--disabled", action="store_true", help="Create the target disabled")
    add.set_defaults(handler=target_add)

    edit = actions.add_parser("edit", help="Change a backup target")
    edit.add_argument("name")
    edit.add_argument("--type", choices=metadb.TARGET_TYPES)
    edit.add_argument("--remote")
    edit.add_argument("--prefix")
    edit.add_argument("--retention", type=int)
    state = edit.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    edit.set_defaults(handler=target_edit)

    test = actions.add_parser("test", help="Write and delete a probe file on a target")
    test.add_argument("name")
    test.set_defaults(handler=target_test)

    set_cmd = commands.add_parser("set", help="Replace the backup targets of a database")
    set_cmd.add_argument("db")
    set_cmd.add_argument("targets", nargs="+")
    set_cmd.set_defaults(handler=set_policy)

    show_cmd = commands.add_parser("show", help="Show the policy and last backups of a database")
    show_cmd.add_argument("db")
    show_cmd.set_defaults(handler=show)

    commands.add_parser("list", help="Show where every database is backed up").set_defaults(handler=list_policies)

    runs = commands.add_parser("runs", help="Show recent backup runs")
    runs.add_argument("db", nargs="?")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(handler=list_runs)

    args = ap.parse_args()

    try:
        meta = metadb.connect_meta()
        try:
            with meta.cursor() as cur:
                args.handler(cur, args)
        finally:
            meta.close()
    except (metadb.MetaError, psycopg2.Error) as e:
        print(f"[ERROR] {str(e).strip()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
