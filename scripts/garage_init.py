#!/usr/bin/env python3
import os
import re
import sys
import time

import psycopg2

import metadb
from garage_admin import ADMIN_TOKEN, ADMIN_URL, BACKUP_BUCKET, GarageError, call, find_bucket

BUCKET = BACKUP_BUCKET
CAPACITY_GB = int(os.getenv("GARAGE_CAPACITY_GB", "100"))
ACCESS_KEY_ID = os.getenv("RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID", "")
SECRET_ACCESS_KEY = os.getenv("RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY", "")
TARGET_NAME = "garage"
TARGET_REMOTE = "garage"


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
    bucket = find_bucket(BUCKET)
    if bucket:
        print(f"[OK] Bucket exists: {BUCKET}")
    else:
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
            else:
                cur.execute(
                    "INSERT INTO targets (name, type, remote, prefix, retention_days) VALUES (%s, 's3', %s, %s, %s)",
                    (TARGET_NAME, TARGET_REMOTE, BUCKET, int(os.getenv("BACKUP_RETENTION_DAYS", "15"))),
                )
                print(f"[OK] Backup target registered: {TARGET_NAME} -> {TARGET_REMOTE}:{BUCKET}")
    finally:
        conn.close()
    if TARGET_NAME in metadb.default_target_names():
        print(
            f"[WARN] BACKUP_DEFAULT_TARGETS includes {TARGET_NAME}, which lives on this server's disk; "
            "it is not an off-site backup, keep an off-site target as the default"
        )


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
