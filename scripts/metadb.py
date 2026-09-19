import os
from dataclasses import dataclass

from psycopg2 import sql

from common import BACKUP_ROLE, META_DB, connect, connect_as_backup

TARGET_TYPES = ("gdrive", "s3")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS targets (
        name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{0,62}$'),
        type text NOT NULL CHECK (type IN ('gdrive', 's3')),
        remote text NOT NULL CHECK (remote ~ '^[A-Za-z0-9_-]+$'),
        prefix text NOT NULL DEFAULT '' CHECK (prefix = btrim(prefix, '/')),
        retention_days integer NOT NULL CHECK (retention_days > 0),
        enabled boolean NOT NULL DEFAULT true,
        CHECK (type <> 's3' OR prefix <> '')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policies (
        datname text NOT NULL,
        target text NOT NULL REFERENCES targets (name) ON UPDATE CASCADE,
        enabled boolean NOT NULL DEFAULT true,
        PRIMARY KEY (datname, target)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        datname text,
        target text NOT NULL,
        kind text NOT NULL CHECK (kind IN ('scheduled', 'manual', 'safety', 'roles')),
        started_at timestamptz NOT NULL,
        duration interval NOT NULL,
        folder text NOT NULL,
        filename text NOT NULL,
        size_bytes bigint,
        sha256 text,
        status text NOT NULL CHECK (status IN ('success', 'failed')),
        error text,
        CHECK (datname IS NOT NULL OR kind = 'roles')
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_lookup ON runs (datname, target, started_at DESC)",
)


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    remote: str
    prefix: str
    retention_days: int
    enabled: bool = True

    def location(self, *parts: str) -> str:
        segments = [segment.strip("/") for segment in (self.prefix, *parts) if segment]
        return f"{self.remote}:" + "/".join(segment for segment in segments if segment)


class MetaError(Exception):
    pass


def connect_meta(as_backup: bool = False):
    return connect_as_backup(META_DB) if as_backup else connect(META_DB)


def default_target_names() -> list[str]:
    raw = os.getenv("BACKUP_DEFAULT_TARGETS", "gdrive")
    return [name.strip() for name in raw.split(",") if name.strip()]


def load_targets(cur) -> dict[str, Target]:
    cur.execute("SELECT name, type, remote, prefix, retention_days, enabled FROM targets ORDER BY name")
    return {row[0]: Target(*row) for row in cur.fetchall()}


def load_policies(cur) -> dict[str, list[tuple[str, bool]]]:
    cur.execute("SELECT datname, target, enabled FROM policies ORDER BY datname, target")
    policies: dict[str, list[tuple[str, bool]]] = {}
    for datname, target, enabled in cur.fetchall():
        policies.setdefault(datname, []).append((target, enabled))
    return policies


def resolve_targets(
    datname: str,
    targets: dict[str, Target],
    policies: dict[str, list[tuple[str, bool]]],
    defaults: list[str],
) -> tuple[list[Target], str]:
    enabled = {name: target for name, target in targets.items() if target.enabled}
    if datname == META_DB:
        return list(enabled.values()), "all"
    chosen = [enabled[name] for name, active in policies.get(datname, []) if active and name in enabled]
    if chosen:
        return chosen, "policy"
    return [enabled[name] for name in defaults if name in enabled], "default"


def require_targets(cur, names: list[str]) -> dict[str, Target]:
    targets = load_targets(cur)
    missing = [name for name in names if name not in targets]
    if missing:
        raise MetaError(f"Unknown backup target: {', '.join(missing)}")
    return targets


def set_policies(cur, datname: str, names: list[str]):
    require_targets(cur, names)
    cur.execute("DELETE FROM policies WHERE datname = %s", (datname,))
    for name in names:
        cur.execute("INSERT INTO policies (datname, target) VALUES (%s, %s)", (datname, name))


def validate_default_targets(cur) -> list[str]:
    names = default_target_names()
    if not names:
        raise MetaError("BACKUP_DEFAULT_TARGETS is empty")
    require_targets(cur, names)
    return names


def add_default_policies(cur, datname: str) -> list[str]:
    names = validate_default_targets(cur)
    for name in names:
        cur.execute(
            "INSERT INTO policies (datname, target) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (datname, name),
        )
    return names


def delete_policies(cur, datname: str) -> int:
    cur.execute("DELETE FROM policies WHERE datname = %s", (datname,))
    return cur.rowcount


def record_run(cur, **run):
    cur.execute(
        """
        INSERT INTO runs (datname, target, kind, started_at, duration, folder, filename,
                          size_bytes, sha256, status, error)
        VALUES (%(datname)s, %(target)s, %(kind)s, %(started_at)s, %(duration)s, %(folder)s, %(filename)s,
                %(size_bytes)s, %(sha256)s, %(status)s, %(error)s)
        """,
        run,
    )


def successful_runs(cur, datname: str, target_names: list[str]) -> list[tuple]:
    cur.execute(
        """
        SELECT target, folder, filename, sha256, started_at
        FROM runs
        WHERE datname = %s
          AND target = ANY(%s)
          AND status = 'success'
          AND kind IN ('scheduled', 'manual')
        ORDER BY started_at DESC
        """,
        (datname, target_names),
    )
    return cur.fetchall()


def file_hashes(cur, folder: str, filename: str) -> set[str]:
    cur.execute(
        """
        SELECT DISTINCT sha256
        FROM runs
        WHERE folder = %s AND filename = %s AND status = 'success' AND sha256 IS NOT NULL
        """,
        (folder, filename),
    )
    return {row[0] for row in cur.fetchall()}


def ensure_schema(cur):
    for statement in SCHEMA:
        cur.execute(statement)
    role = sql.Identifier(BACKUP_ROLE)
    cur.execute(sql.SQL("GRANT SELECT ON targets, policies TO {}").format(role))
    cur.execute(sql.SQL("GRANT INSERT ON runs TO {}").format(role))


def seed_targets(cur) -> Target | None:
    cur.execute("SELECT count(*) FROM targets")
    if cur.fetchone()[0]:
        return None
    target = Target(
        name="gdrive",
        type="gdrive",
        remote=os.getenv("RCLONE_REMOTE", "grdive:").rstrip(":"),
        prefix="",
        retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "15")),
    )
    cur.execute(
        "INSERT INTO targets (name, type, remote, prefix, retention_days) VALUES (%s, %s, %s, %s, %s)",
        (target.name, target.type, target.remote, target.prefix, target.retention_days),
    )
    return target
