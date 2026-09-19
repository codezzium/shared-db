#!/usr/bin/env python3
"""
Quick database creator for new projects.
Usage: docker exec -it shared-pgbackup python /app/mkdb.py <database name>
"""
import argparse
import secrets
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

import metadb
from common import APP_HOST, APP_PORT, BACKUP_ROLE, connect, db_exists, role_exists, validate_name

OWNERSHIP_QUERY = """
WITH owner AS (SELECT %(owner)s::regrole::oid AS oid),
members AS (SELECT classid, objid FROM pg_depend WHERE deptype = 'e')
SELECT 1, format('ALTER SCHEMA %%I OWNER TO %%I', n.nspname, %(owner)s)
FROM pg_namespace n, owner
WHERE n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND n.nspowner NOT IN (owner.oid, 'pg_database_owner'::regrole)
  AND ('pg_namespace'::regclass, n.oid) NOT IN (SELECT classid, objid FROM members)
UNION ALL
SELECT 2, format(
    'ALTER %%s %%s OWNER TO %%I',
    CASE c.relkind
        WHEN 'v' THEN 'VIEW'
        WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'f' THEN 'FOREIGN TABLE'
        WHEN 'S' THEN 'SEQUENCE'
        ELSE 'TABLE'
    END,
    c.oid::regclass,
    %(owner)s
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace, owner
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND c.relowner <> owner.oid
  AND ('pg_class'::regclass, c.oid) NOT IN (SELECT classid, objid FROM members)
  AND NOT (
      c.relkind = 'S'
      AND EXISTS (
          SELECT 1
          FROM pg_depend d
          WHERE d.classid = 'pg_class'::regclass
            AND d.objid = c.oid
            AND d.refclassid = 'pg_class'::regclass
            AND d.deptype IN ('a', 'i')
      )
  )
UNION ALL
SELECT 3, format('ALTER ROUTINE %%s OWNER TO %%I', p.oid::regprocedure, %(owner)s)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace, owner
WHERE n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND p.proowner <> owner.oid
  AND ('pg_proc'::regclass, p.oid) NOT IN (SELECT classid, objid FROM members)
UNION ALL
SELECT 4, format(
    'ALTER %%s %%s OWNER TO %%I',
    CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
    t.oid::regtype,
    %(owner)s
)
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace, owner
WHERE t.typtype IN ('c', 'd', 'e', 'r')
  AND (t.typtype <> 'c' OR EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind = 'c'))
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND t.typowner <> owner.oid
  AND ('pg_type'::regclass, t.oid) NOT IN (SELECT classid, objid FROM members)
UNION ALL
SELECT 5, format('ALTER LARGE OBJECT %%s OWNER TO %%I', l.oid, %(owner)s)
FROM pg_largeobject_metadata l, owner
WHERE l.lomowner <> owner.oid
ORDER BY 1
"""


class ProjectError(Exception):
    pass


def unavailable_extensions(cur, extensions: list[str]) -> list[str]:
    cur.execute("SELECT name FROM pg_available_extensions WHERE name = ANY(%s)", (extensions,))
    available = {row[0] for row in cur.fetchall()}
    return [ext for ext in extensions if ext not in available]


def create_role(cur, name: str) -> str:
    password = secrets.token_urlsafe(32)
    verifier = encrypt_password(password, name, cur, "scram-sha-256")
    cur.execute(
        sql.SQL(
            "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(name), sql.Literal(verifier))
    )
    return password


def create_database(cur, dbname: str, owner: str):
    """Create new database with Turkish ICU collation settings"""
    db = sql.Identifier(dbname)
    cur.execute(
        sql.SQL(
            "CREATE DATABASE {} WITH OWNER {} LOCALE_PROVIDER icu ICU_LOCALE 'tr-TR' TEMPLATE template0"
        ).format(db, sql.Identifier(owner))
    )
    cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
    cur.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
            db, sql.Identifier(owner), sql.Identifier(BACKUP_ROLE)
        )
    )


def install_extensions(dbname: str, extensions: list[str]):
    conn = connect(dbname)
    try:
        with conn.cursor() as cur:
            for ext in extensions:
                print(f"[EXTENSION] {ext}")
                cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
    finally:
        conn.close()


def reassign_ownership(dbname: str, owner: str) -> int:
    conn = connect(dbname)
    try:
        with conn.cursor() as cur:
            cur.execute(OWNERSHIP_QUERY, {"owner": owner})
            statements = [row[1] for row in cur.fetchall()]
            for statement in statements:
                cur.execute(statement)
    finally:
        conn.close()
    if statements:
        print(f"[OWNER] {len(statements)} object(s) in {dbname} now owned by {owner}")
    return len(statements)


def retarget_policies(cur, old_role: str, new_role: str):
    cur.execute("SELECT %s::regrole::oid", (old_role,))
    old_oid = cur.fetchone()[0]
    cur.execute(
        """
        SELECT p.polname, p.polrelid::regclass::text,
               array(SELECT CASE WHEN r = 0 THEN NULL ELSE pg_get_userbyid(r) END FROM unnest(p.polroles) AS r),
               array(SELECT r FROM unnest(p.polroles) AS r)
        FROM pg_policy p
        WHERE %s = ANY(p.polroles)
        """,
        (old_oid,),
    )
    for name, table, role_names, role_oids in cur.fetchall():
        targets = []
        for role, oid in zip(role_names, role_oids):
            if role is None:
                target = sql.SQL("PUBLIC")
            else:
                target = sql.Identifier(new_role if oid == old_oid else role)
            if target not in targets:
                targets.append(target)
        cur.execute(
            sql.SQL("ALTER POLICY {} ON {} TO {}").format(
                sql.Identifier(name), sql.SQL(table), sql.SQL(", ").join(targets)
            )
        )
        print(f"[POLICY] {table}.{name}: role {old_role} replaced by {new_role}")


def drop_project(cur, name: str):
    if db_exists(cur, name):
        print(f"[DROP] Database: {name}")
        cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    if role_exists(cur, name):
        print(f"[DROP] Role: {name}")
        cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def create_project(name: str, extensions: list[str]) -> str:
    conn = connect()
    meta = metadb.connect_meta()
    try:
        with conn.cursor() as cur, meta.cursor() as meta_cur:
            if role_exists(cur, name):
                raise ProjectError(f"Role '{name}' already exists")
            if db_exists(cur, name):
                raise ProjectError(f"Database '{name}' already exists")
            missing = unavailable_extensions(cur, extensions)
            if missing:
                raise ProjectError(f"Extension not available: {', '.join(missing)}")
            metadb.validate_default_targets(meta_cur)

            print(f"[CREATE] Role: {name}")
            password = create_role(cur, name)
            completed = False
            try:
                print(f"[CREATE] Database: {name}")
                create_database(cur, name, name)
                install_extensions(name, extensions)
                targets = metadb.add_default_policies(meta_cur, name)
                print(f"[POLICY] Backup targets: {', '.join(targets)}")
                completed = True
            finally:
                if not completed:
                    print(f"[ROLLBACK] Removing partially created project: {name}")
                    metadb.delete_policies(meta_cur, name)
                    drop_project(cur, name)
    finally:
        meta.close()
        conn.close()
    return password


def print_connection_info(dbname: str, password: str):
    """Print connection string and environment variables"""
    conn_string = f"postgresql://{dbname}:{password}@{APP_HOST}:{APP_PORT}/{dbname}"

    print("\n" + "="*60)
    print("DATABASE CONNECTION INFO")
    print("="*60)
    print(f"\n📦 Database: {dbname}")
    print(f"🔗 Connection String:\n   {conn_string}")
    print("\n🔧 Environment Variables (.env):")
    print(f"   PGHOST={APP_HOST}")
    print(f"   PGPORT={APP_PORT}")
    print(f"   PGDATABASE={dbname}")
    print(f"   PGUSER={dbname}")
    print(f"   PGPASSWORD={password}")
    print("\n⚠️  This password is shown only once. Store it now.")
    print("\n" + "="*60 + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Create an isolated role and database for a project",
        epilog="Example: docker-compose exec pgbackup python /app/mkdb.py my_django_db --extension vector",
    )
    ap.add_argument("dbname", help="Project name, used for both the database and its role")
    ap.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar="NAME",
        help="Extension to install into the new database as superuser (repeatable)",
    )
    args = ap.parse_args()

    dbname = args.dbname

    # Validate database name (basic check)
    error = validate_name(dbname)
    if error:
        print(f"[ERROR] Invalid database name: {dbname}")
        print(f"[ERROR] {error}")
        sys.exit(1)

    try:
        password = create_project(dbname, args.extension)
    except (ProjectError, metadb.MetaError, psycopg2.Error) as e:
        print(f"[ERROR] Failed to create database: {str(e).strip()}")
        sys.exit(1)

    print_connection_info(dbname, password)


if __name__ == "__main__":
    main()
