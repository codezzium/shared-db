#!/usr/bin/env python3
import os
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

from backup import BACKUP_ROLE
from mkdb import PGBOUNCER_ADMIN_ROLE, PGBOUNCER_AUTH_ROLE, PGBOUNCER_STATS_ROLE, connect, role_exists

UNPRIVILEGED = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
BACKUP_ATTRIBUTES = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS"

AUTH_FUNCTION = """
CREATE OR REPLACE FUNCTION pgbouncer.user_lookup(i_username text)
RETURNS TABLE (uname text, phash text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT rolname::text, rolpassword
    FROM pg_authid
    WHERE rolname = i_username
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolreplication
      AND NOT rolbypassrls
      AND rolpassword IS NOT NULL
      AND (rolvaliduntil IS NULL OR rolvaliduntil > now())
$$
"""


def ensure_role(cur, name: str, password: str | None, attributes: str):
    action = "ALTER" if role_exists(cur, name) else "CREATE"
    if password:
        verifier = encrypt_password(password, name, cur, "scram-sha-256")
        login = sql.SQL("LOGIN PASSWORD {}").format(sql.Literal(verifier))
    else:
        login = sql.SQL("NOLOGIN PASSWORD NULL")
    cur.execute(
        sql.SQL("{} ROLE {} WITH {} {}").format(
            sql.SQL(action), sql.Identifier(name), sql.SQL(attributes), login
        )
    )
    print(f"[OK] Role {name}: {'login enabled' if password else 'login disabled'}")


def ensure_auth_function(cur):
    auth_role = sql.Identifier(PGBOUNCER_AUTH_ROLE)
    cur.execute("CREATE SCHEMA IF NOT EXISTS pgbouncer")
    cur.execute("REVOKE ALL ON SCHEMA pgbouncer FROM PUBLIC")
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA pgbouncer TO {}").format(auth_role))
    cur.execute(AUTH_FUNCTION)
    cur.execute("REVOKE ALL ON FUNCTION pgbouncer.user_lookup(text) FROM PUBLIC")
    cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION pgbouncer.user_lookup(text) TO {}").format(auth_role))
    print("[OK] Auth function: pgbouncer.user_lookup")


def main():
    backup_password = os.getenv("BACKUP_PASSWORD")
    auth_password = os.getenv("PGBOUNCER_AUTH_PASSWORD")
    missing = [
        name
        for name, value in (("BACKUP_PASSWORD", backup_password), ("PGBOUNCER_AUTH_PASSWORD", auth_password))
        if not value
    ]
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    try:
        conn = connect()
        conn.autocommit = False
        try:
            with conn, conn.cursor() as cur:
                ensure_role(cur, BACKUP_ROLE, backup_password, BACKUP_ATTRIBUTES)
                cur.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(BACKUP_ROLE)))
                ensure_role(cur, PGBOUNCER_AUTH_ROLE, auth_password, UNPRIVILEGED)
                ensure_role(cur, PGBOUNCER_ADMIN_ROLE, os.getenv("PGBOUNCER_ADMIN_PASSWORD"), UNPRIVILEGED)
                ensure_role(cur, PGBOUNCER_STATS_ROLE, os.getenv("PGBOUNCER_STATS_PASSWORD"), UNPRIVILEGED)
                ensure_auth_function(cur)
        finally:
            conn.close()
    except psycopg2.Error as e:
        print(f"[ERROR] Bootstrap failed: {str(e).strip()}")
        sys.exit(1)

    print("[OK] Bootstrap completed")


if __name__ == "__main__":
    main()
