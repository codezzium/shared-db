import json
import os
import urllib.error
import urllib.parse
import urllib.request

ADMIN_URL = os.getenv("GARAGE_ADMIN_URL", "http://garage:3903")
ADMIN_TOKEN = os.getenv("GARAGE_ADMIN_TOKEN")
BACKUP_BUCKET = os.getenv("GARAGE_BUCKET", "shared-db-backups")
S3_HOST = "shared-garage"
S3_ENDPOINT = f"http://{S3_HOST}:3900"
S3_INTERNAL_ENDPOINT = "http://garage:3900"
S3_REGION = "garage"


class GarageError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
        raise GarageError(f"{endpoint}: HTTP {e.code} {e.read().decode(errors='replace').strip()}", e.code) from None
    except urllib.error.URLError as e:
        raise GarageError(f"Garage admin API unreachable at {ADMIN_URL} ({e.reason}); is COMPOSE_PROFILES=garage set?") from None
    return json.loads(payload) if payload else None


def find_bucket(alias: str) -> dict | None:
    try:
        return call("GET", "GetBucketInfo", query={"globalAlias": alias})
    except GarageError as e:
        if e.status in (400, 404):
            return None
        raise


def keys_named(name: str) -> list[dict]:
    return [key for key in call("GET", "ListKeys") if key["name"] == name]


def bucket_name(project: str) -> str:
    return project.replace("_", "-")


def bucket_name_error(bucket: str) -> str | None:
    if not 3 <= len(bucket) <= 63:
        return "S3 bucket names need 3 to 63 characters"
    if bucket.endswith("-") or bucket.startswith("xn--") or bucket.endswith("-s3alias"):
        return f"'{bucket}' is not a valid S3 bucket name"
    if bucket == BACKUP_BUCKET:
        return f"'{bucket}' is the backup bucket"
    return None
