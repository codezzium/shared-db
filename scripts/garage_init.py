#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2

import metadb

ADMIN_URL = os.getenv("GARAGE_ADMIN_URL", "http://garage:3903")
ADMIN_TOKEN = os.getenv("GARAGE_ADMIN_TOKEN")
BUCKET = os.getenv("GARAGE_BUCKET", "shared-db-backups")
CAPACITY_GB = int(os.getenv("GARAGE_CAPACITY_GB", "100"))
ACCESS_KEY_ID = os.getenv("RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID", "")
SECRET_ACCESS_KEY = os.getenv("RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY", "")
TARGET_NAME = "garage"
TARGET_REMOTE = "garage"


class GarageError(Exception):
    pass


def call(method: str, endpoint: str, body: dict | None = None, query: dict | None = None):
    url = f"{ADMIN_URL}/v2/{endpoint}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
    except urllib.error.HTTPError as e:
        raise GarageError(f"{endpoint}: HTTP {e.code} {e.read().decode(errors='replace').strip()}") from None
    return json.loads(payload) if payload else None


def wait_for_garage() -> dict:
    last_error = None
    for _ in range(30):
        try:
            return call("GET", "GetClusterStatus")
        except (GarageError, OSError) as e:
            last_error = e
            time.sleep(2)
    raise GarageError(f"Admin API not reachable at {ADMIN_URL}: {last_error}")


def ensure_layout(status: dict):
    layout = call("GET", "GetClusterLayout")
    if layout["roles"]:
        print(f"[OK] Garage layout v{layout['version']} already applied")
        return
    node_id = status["nodes"][0]["id"]
    call(
        "POST",
        "UpdateClusterLayout",
        {"roles": [{"id": node_id, "zone": "dc1", "capacity": CAPACITY_GB * 10**9, "tags": ["shared-db"]}]},
    )
    call("POST", "ApplyClusterLayout", {"version": layout["version"] + 1})
    print(f"[OK] Garage layout applied: node {node_id[:16]}, capacity {CAPACITY_GB} GB")


def ensure_bucket() -> str:
    try:
        bucket = call("GET", "GetBucketInfo", query={"globalAlias": BUCKET})
        print(f"[OK] Bucket exists: {BUCKET}")
    except GarageError:
        bucket = call("POST", "CreateBucket", {"globalAlias": BUCKET})
        print(f"[OK] Bucket created: {BUCKET}")
    return bucket["id"]


def ensure_key(bucket_id: str):
    try:
        call("GET", "GetKeyInfo", query={"id": ACCESS_KEY_ID})
        print(f"[OK] Access key exists: {ACCESS_KEY_ID}")
    except GarageError:
        call(
            "POST",
            "ImportKey",
            {"accessKeyId": ACCESS_KEY_ID, "secretAccessKey": SECRET_ACCESS_KEY, "name": "shared-db-backup"},
        )
        print(f"[OK] Access key imported: {ACCESS_KEY_ID}")
    call(
        "POST",
        "AllowBucketKey",
        {
            "bucketId": bucket_id,
            "accessKeyId": ACCESS_KEY_ID,
            "permissions": {"read": True, "write": True, "owner": True},
        },
    )


def register_target():
    conn = metadb.connect_meta()
    try:
        with conn.cursor() as cur:
            if TARGET_NAME in metadb.load_targets(cur):
                print(f"[OK] Backup target already registered: {TARGET_NAME}")
                return
            cur.execute(
                "INSERT INTO targets (name, type, remote, prefix, retention_days) VALUES (%s, 's3', %s, %s, %s)",
                (TARGET_NAME, TARGET_REMOTE, BUCKET, int(os.getenv("BACKUP_RETENTION_DAYS", "15"))),
            )
            print(f"[OK] Backup target registered: {TARGET_NAME} -> {TARGET_REMOTE}:{BUCKET}")
    finally:
        conn.close()


def main():
    problems = []
    if not ADMIN_TOKEN:
        problems.append("GARAGE_ADMIN_TOKEN is empty")
    if not re.fullmatch(r"GK[0-9a-f]{24}", ACCESS_KEY_ID):
        problems.append("RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID must be GK followed by 24 hex characters")
    if not re.fullmatch(r"[0-9a-f]{64}", SECRET_ACCESS_KEY):
        problems.append("RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY must be 64 hex characters")
    if problems:
        for problem in problems:
            print(f"[ERROR] {problem}")
        sys.exit(1)

    try:
        status = wait_for_garage()
        ensure_layout(status)
        ensure_key(ensure_bucket())
        register_target()
    except (GarageError, OSError, psycopg2.Error) as e:
        print(f"[ERROR] Garage init failed: {str(e).strip()}")
        sys.exit(1)

    print("[OK] Garage ready")


if __name__ == "__main__":
    main()
