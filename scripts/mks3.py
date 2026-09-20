#!/usr/bin/env python3
import argparse
import sys

from common import NAME_PATTERN
from garage_admin import (
    S3_ENDPOINT,
    S3_REGION,
    GarageError,
    bucket_name,
    bucket_name_error,
    call,
    find_bucket,
    keys_named,
)


def create_bucket_and_key(project: str, bucket: str) -> dict:
    if find_bucket(bucket):
        raise GarageError(f"Bucket '{bucket}' already exists")
    if keys_named(project):
        raise GarageError(f"A key named '{project}' already exists; remove it with rms3.py first")

    created = call("POST", "CreateBucket", {"globalAlias": bucket})
    key = None
    completed = False
    try:
        key = call("POST", "CreateKey", {"name": project})
        call(
            "POST",
            "AllowBucketKey",
            {
                "bucketId": created["id"],
                "accessKeyId": key["accessKeyId"],
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )
        completed = True
    finally:
        if not completed:
            print(f"[ROLLBACK] Removing partially created bucket and key of {project}")
            try:
                if key:
                    call("POST", "DeleteKey", query={"id": key["accessKeyId"]})
                call("POST", "DeleteBucket", query={"id": created["id"]})
            except GarageError as e:
                print(f"[WARN] Rollback incomplete: {e}")
    return key


def print_connection_info(bucket: str, key: dict):
    print("\n" + "="*60)
    print("S3 CONNECTION INFO")
    print("="*60)
    print(f"\n🪣 Bucket: {bucket}")
    print(f"🔗 Endpoint: {S3_ENDPOINT} (region {S3_REGION}, path-style addressing)")
    print("\n🔧 Environment Variables (.env):")
    print(f"   AWS_S3_ENDPOINT_URL={S3_ENDPOINT}")
    print(f"   AWS_S3_REGION_NAME={S3_REGION}")
    print("   AWS_S3_ADDRESSING_STYLE=path")
    print(f"   AWS_STORAGE_BUCKET_NAME={bucket}")
    print(f"   AWS_ACCESS_KEY_ID={key['accessKeyId']}")
    print(f"   AWS_SECRET_ACCESS_KEY={key['secretAccessKey']}")
    print("   AWS_REQUEST_CHECKSUM_CALCULATION=when_required")
    print("   AWS_RESPONSE_CHECKSUM_VALIDATION=when_required")
    print("\nℹ️  The two checksum settings are needed: boto3 rejects Garage's checksum of multipart uploads.")
    print("⚠️  This secret is shown only once. Store it now.")
    print("\n" + "="*60 + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Create a Garage S3 bucket and a key that can only read and write that bucket",
        epilog="Example: docker-compose exec pgbackup python /app/mks3.py my_django_app",
    )
    ap.add_argument("name", help="Project name; the bucket is the name with '_' replaced by '-'")
    args = ap.parse_args()

    if not NAME_PATTERN.fullmatch(args.name):
        print(f"[ERROR] Invalid project name: {args.name}")
        print("[ERROR] Use lowercase letters, digits and underscores, starting with a letter")
        sys.exit(1)
    bucket = bucket_name(args.name)
    error = bucket_name_error(bucket)
    if error:
        print(f"[ERROR] {error}")
        sys.exit(1)

    try:
        key = create_bucket_and_key(args.name, bucket)
    except GarageError as e:
        print(f"[ERROR] Failed to create S3 bucket: {e}")
        sys.exit(1)

    print(f"[CREATE] Bucket: {bucket}, key {key['accessKeyId']} (read/write on this bucket only)")
    print_connection_info(bucket, key)


if __name__ == "__main__":
    main()
