#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys

from common import NAME_PATTERN
from garage_admin import (
    S3_INTERNAL_ENDPOINT,
    S3_REGION,
    GarageError,
    bucket_name,
    bucket_name_error,
    call,
    find_bucket,
    keys_named,
)


def confirm(name: str, bucket: str, info: dict | None) -> bool:
    if not sys.stdin.isatty():
        print("[ERROR] Confirmation required: run interactively or pass --yes")
        return False
    what = f"bucket {bucket} ({info['objects']} object(s)) and the key" if info else "the key"
    answer = input(f"Type '{name}' to permanently delete {what} of this project: ")
    return answer.strip() == name


def empty_bucket(bucket: str, bucket_id: str):
    key = call("POST", "CreateKey", {"name": f"rms3-{bucket}"})
    try:
        call(
            "POST",
            "AllowBucketKey",
            {
                "bucketId": bucket_id,
                "accessKeyId": key["accessKeyId"],
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )
        env = {
            **os.environ,
            "RCLONE_CONFIG_RMS3_TYPE": "s3",
            "RCLONE_CONFIG_RMS3_PROVIDER": "Other",
            "RCLONE_CONFIG_RMS3_ENDPOINT": S3_INTERNAL_ENDPOINT,
            "RCLONE_CONFIG_RMS3_REGION": S3_REGION,
            "RCLONE_CONFIG_RMS3_ACCESS_KEY_ID": key["accessKeyId"],
            "RCLONE_CONFIG_RMS3_SECRET_ACCESS_KEY": key["secretAccessKey"],
        }
        result = subprocess.run(["rclone", "delete", f"rms3:{bucket}"], env=env, capture_output=True, text=True)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            raise GarageError(f"Emptying {bucket} failed: {lines[-1] if lines else result.returncode}")
    finally:
        call("POST", "DeleteKey", query={"id": key["accessKeyId"]})
    uploads = call("POST", "CleanupIncompleteUploads", {"bucketId": bucket_id, "olderThanSecs": 0})
    if uploads and uploads.get("uploadsDeleted"):
        print(f"[DROP] Incomplete multipart uploads: {uploads['uploadsDeleted']}")


def main():
    ap = argparse.ArgumentParser(
        description="Permanently delete a project's Garage bucket, every object in it and its key",
        epilog="Example: docker-compose exec pgbackup python /app/rms3.py my_django_app",
    )
    ap.add_argument("name", help="Project name given to mks3.py")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args()

    if not NAME_PATTERN.fullmatch(args.name):
        print(f"[ERROR] Invalid project name: {args.name}")
        sys.exit(1)
    bucket = bucket_name(args.name)
    error = bucket_name_error(bucket)
    if error:
        print(f"[ERROR] {error}")
        sys.exit(1)

    try:
        info = find_bucket(bucket)
        keys = keys_named(args.name)
        if not info and not keys:
            print(f"[ERROR] Neither bucket '{bucket}' nor a key named '{args.name}' exists")
            sys.exit(1)
        if not args.yes and not confirm(args.name, bucket, info):
            print("[CANCEL] Nothing was deleted")
            sys.exit(1)
        for key in keys:
            call("POST", "DeleteKey", query={"id": key["id"]})
            print(f"[DROP] Key: {key['id']}")
        if info:
            empty_bucket(bucket, info["id"])
            call("POST", "DeleteBucket", query={"id": info["id"]})
            print(f"[DROP] Bucket: {bucket}")
    except GarageError as e:
        print(f"[ERROR] Failed to delete: {e}")
        sys.exit(1)

    print(f"[OK] S3 bucket and key of '{args.name}' deleted")


if __name__ == "__main__":
    main()
