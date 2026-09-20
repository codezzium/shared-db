# 🐘 Shared PostgreSQL Backup System [BU BİR HAKAN KARATOPAK PROJESİDİR. TÜM SORUMLULUK BANA AİTTİR.]

Cloud-first PostgreSQL backup and restore system with automated daily backups, Google Drive integration, and zero local storage.

# TODO:
  - [ ] Docker içinde fazlalık olan cron işini django ya bırakılacak. 
  - [ ] Django da çok daha iyi arayüz hazırlanacak, mümkünse api sağlanacak sadece. Ouath2 filan
  - [ ] Yedekleme, restore etme filan bunları adım adım görmek istiyorum ön yüzde.

## 📋 Table of Contents

- [Features](#-features)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [File Structure](#-file-structure)
- [Troubleshooting](#-troubleshooting)
- [Advanced](#-advanced)

---

## ✨ Features

- **🔄 Automated Daily Backups**: Scheduled at 02:00 AM TRT
- **☁️ Multiple Backup Targets**: Google Drive, S3 (Hetzner Object Storage) and an optional local Garage S3, chosen per database
- **🧾 Backup Metadata**: Targets, per-database policies and every run are kept in the `backup_meta` database
- **🗄️ Multiple Databases**: Each database backed up to separate SQL files
- **🔒 Safety Backups**: Automatic safety dump before restore operations
- **🔐 Per-Project Isolation**: Every project gets its own role and database; apps reach Postgres only through PgBouncer
- **🧱 Shared Redis**: Redis 8 with one ACL user per project, limited to its own key and channel prefix
- **🪣 Local S3 for Projects**: Optional Garage bucket and key per project (`mks3.py`), reachable only from `shared-db-net`
- **🛟 Replace-on-Success Restore**: Dumps load into a staging database first; a failed restore leaves the current one untouched
- **🆘 Disaster Recovery**: `roles.json` and `manifest.json` in every backup folder, `restore.py --all` rebuilds the whole server
- **🧹 Auto Cleanup**: Retention per target (`retention_days`)
- **📅 Hierarchical Storage**: Year/Month/Day folder structure
- **♻️ Smart Archiving**: Multiple same-day backups archived in `olds/HH_MM/` subfolders
- **🚀 Zero Local Storage**: Temp files only, auto-cleaned after operations
- **📊 pgAdmin Integration**: Web UI for database management

---

## 🏗️ Architecture

```
Applications ── shared-db-net (external) ──┬───────────────┬───────────────┐
                                           ▼               ▼               ▼
                                    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
                                    │  PgBouncer  │ │   Redis 8   │ │  Garage S3  │
                                    │    :5432    │ │    :6379    │ │    :3900    │
                                    └──────┬──────┘ └─────────────┘ └──────┬──────┘
                                           │ 10.250.0.10                   │
┌─ shared-db-internal (internal) ──────────┼───────────────────────────────┼──────────┐
│                                   ┌──────▼───────┐                       ▼          │
│  ┌──────────────┐  postgres /     │   Postgres   │                   admin API      │
│  │   pgBackup   ├─── backup ─────►│  (shared-db) │               10.250.0.11:3903   │
│  │ cron + admin │                 └──────┬───────┘                                  │
│  └──────┬───────┘                        │                                          │
└─────────┼────────────────────────────────┼──────────────────────────────────────────┘
          ▼                                ▼
  rclone → Drive / S3 / Garage   postgres_data volume
```

- `shared-db-net` is the only network applications join. It holds PgBouncer, Redis and the S3 API of Garage, never Postgres.
- `shared-db-internal` (`internal: true`, `10.250.0.0/24`) holds Postgres, PgBouncer (`10.250.0.10`), pgBackup, the one-shot `db-init` and, when enabled, Garage (`10.250.0.11`). pgBackup also joins the compose `default` network for rclone's internet access.
- Garage's admin API listens on `10.250.0.11:3903` only and its RPC port on `127.0.0.1` inside the container, so applications can use S3 but cannot manage buckets or keys.
- `pg_hba.conf` accepts only `sameuser` project logins from PgBouncer's address and rejects `postgres`/`backup` there; `postgres` and `backup` may connect only from the rest of the internal network.
- `db-init` runs `bootstrap.py` on every `docker compose up`; PgBouncer and pgBackup start only after it succeeds.

### Backup Flow

```
1. Cron triggers backup.py (02:00 Europe/Istanbul)
2. Read targets and policies from backup_meta
3. Dump each database as the read-only `backup` role → <db>.sql.gz
   - database without an enabled policy → BACKUP_DEFAULT_TARGETS, with a warning in the log
   - backup_meta → every enabled target
4. Dump project roles as superuser → roles.json (no superuser, no service roles)
5. For every enabled target:
   - move an existing same-day folder to records/<SERVER_NAME>/YYYY/M/D/olds/HH_MM/
   - upload each dump separately and record it in backup_meta.runs
   - upload SHA256SUMS and manifest.json
   - delete folders older than the target's retention_days (only when every upload succeeded)
6. Report to BACKUP_HEALTHCHECK_URL; exit code 1 if any dump or upload failed
```

### Restore Flow

```
1. Pick the source: --target, or the database's policy targets (latest successful run, then folder listings)
   - a target that does not answer is reported and skipped, the other targets are still used
2. Download <db>.sql.gz (or a legacy <db>.sql) and verify its SHA-256
3. Safety backup of the current database → manual_backups/<SERVER_NAME>/YYYY/M/D/ on its targets
4. Missing role: restored from roles.json with its old password, or created with a new one
5. Create the staging database _restore_<db> (owned by the project role, ICU tr-TR, CONNECT only for the project and `backup`)
6. Load with psql -v ON_ERROR_STOP=1 (pg_restore --exit-on-error for -Fc dumps), then hand every object to the project role
7. Replace the old database with the staging one by renaming both, then drop the old one
8. Verify tables and clean the temp directory
9. Exit code 1 if anything failed or a target was unreachable, even when the restore itself succeeded
```

---

## 📦 Prerequisites

- **Docker & Docker Compose** (v2.0+)
- **Google Cloud Project** with Drive API enabled
- **OAuth 2.0 Credentials** (Client ID & Secret)
- **Hetzner Object Storage** bucket and access keys (optional, for the S3 target)

---

## 🚀 Quick Start

### 1. Clone & Configure

```bash
# Clone repository
git clone <your-repo-url>
cd shared-db

# Create environment file
cp .env.example .env
nano .env
```

### 2. Configure Environment Variables

Create `.env` file:

```bash
# PostgreSQL Configuration
POSTGRES_DB=shared_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password
POSTGRES_HOST=shared-db  # Docker service name (DO NOT CHANGE)
POSTGRES_PORT=5432
BACKUP_PASSWORD=backup_secure_password
PGBOUNCER_AUTH_PASSWORD=pgbouncer_auth_secure_password
PGBOUNCER_ADMIN_PASSWORD=
PGBOUNCER_STATS_PASSWORD=
REDIS_ADMIN_PASSWORD=redis_admin_secure_password
SERVER_NAME=apps

# pgAdmin Configuration
PGADMIN_EMAIL=admin@example.com
PGADMIN_PASSWORD=admin_secure_password

# Backup Configuration
BACKUP_RETENTION_DAYS=15
BACKUP_DEFAULT_TARGETS=gdrive,hetzner
RCLONE_REMOTE=grdive:  # Default remote name
```

`SERVER_NAME` separates the backups of every server (`apps`, `asel`, `aparthouse`) in shared Drive folders and buckets.

### 3. Setup Google Drive (rclone)

```bash
# Start container
docker-compose up -d pgbackup

# Configure rclone interactively
docker-compose exec pgbackup rclone config

# Follow prompts:
# - Name: grdive
# - Storage: drive
# - Client ID: <your-client-id>
# - Client Secret: <your-client-secret>
# - Scope: drive
# - Root folder ID: <your-folder-id>  (optional, recommended)
# - Use headless mode if no browser available
```

**Get Google Drive Folder ID:**
1. Create folder in Google Drive: "PostgreSQL Backups"
2. Open folder, copy ID from URL: `https://drive.google.com/drive/folders/1qCTRvCtm...`
3. Use this ID in rclone config as `root_folder_id`

On the first start `db-init` registers this remote as the backup target `gdrive` (remote from `RCLONE_REMOTE`, retention from `BACKUP_RETENTION_DAYS`).

### 3b. Setup Hetzner Object Storage (S3)

The S3 remote is defined through rclone environment variables, no `rclone config` needed. The values follow Hetzner's recommended rclone settings (`provider = Other`, location endpoint, `acl = private`, `region`):

```bash
RCLONE_CONFIG_HETZNER_TYPE=s3
RCLONE_CONFIG_HETZNER_PROVIDER=Other
RCLONE_CONFIG_HETZNER_ACCESS_KEY_ID=<access key>
RCLONE_CONFIG_HETZNER_SECRET_ACCESS_KEY=<secret key>
RCLONE_CONFIG_HETZNER_ENDPOINT=fsn1.your-objectstorage.com
RCLONE_CONFIG_HETZNER_REGION=fsn1
RCLONE_CONFIG_HETZNER_ACL=private
```

Use the endpoint and region of the bucket's location (`fsn1`, `nbg1` or `hel1`). After starting the stack, register the target and check that it is writable:

```bash
docker compose exec pgbackup python /app/backupctl.py target add hetzner --type s3 --remote hetzner --prefix <bucket> --retention 30
docker compose exec pgbackup python /app/backupctl.py target test hetzner
```

### 4. Start Services

```bash
# Build and start all services
docker-compose up -d

# Check logs
docker-compose logs -f pgbackup

# Verify services
docker-compose ps
```

---

## ⚙️ Configuration

### Docker Compose Services

| Service | Port | Description |
|---------|------|-------------|
| `db` | - (internal network only) | PostgreSQL 18 + pgvector, `pg_hba.conf` mounted from the repo |
| `db-init` | - | One-shot: service roles, PgBouncer auth function and `backup_meta` schema |
| `pgbouncer` | - (never published to the host) | Connection pooler, the only entry point for applications; `pg_isready` healthcheck |
| `redis` | - (never published to the host) | Shared Redis 8 (`shared-redis`) on `shared-db-net`, AOF on, `noeviction` |
| `pgadmin` | 9090 (localhost only) | Web-based DB management UI |
| `pgbackup` | - | Backup cron and admin scripts |
| `garage` / `garage-init` | - (never published to the host) | Optional local S3 (profile `garage`): S3 API on both networks, admin API and RPC internal only |

Every image is pinned to an exact version and every service has a memory limit, because the VM is shared with the projects.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | `shared_db` | Default database name |
| `POSTGRES_USER` | `postgres` | Database superuser; keep it `postgres`, `pg_hba.conf` refers to it |
| `POSTGRES_PASSWORD` | - | **Required**: Database password |
| `POSTGRES_HOST` | `shared-db` | Docker service name (hardcoded in scripts) |
| `POSTGRES_PORT` | `5432` | Database port |
| `BACKUP_PASSWORD` | - | **Required**: Password of the `backup` role (`pg_read_all_data` + `BYPASSRLS`) used for dumps |
| `PGBOUNCER_AUTH_PASSWORD` | - | **Required**: Password of the `pgbouncer_auth` role that runs PgBouncer's `auth_query` |
| `PGBOUNCER_ADMIN_PASSWORD` | - | Enables the `pgbouncer_admin` console user; empty disables its login |
| `PGBOUNCER_STATS_PASSWORD` | - | Enables the `pgbouncer_stats` console user, which PgBouncer's healthcheck also uses; empty disables its login and makes every check log a warning |
| `SERVER_NAME` | - | **Required**: Server name used in backup paths (`records/<SERVER_NAME>/...`) |
| `BACKUP_RETENTION_DAYS` | `15` | Retention of the seeded `gdrive` target and default of `backupctl target add` |
| `RCLONE_REMOTE` | `grdive:` | rclone remote of the seeded `gdrive` target |
| `BACKUP_DEFAULT_TARGETS` | `gdrive` | Comma-separated targets for new databases and for databases without a policy |
| `BACKUP_HEALTHCHECK_URL` | - | Optional; pinged after every nightly run, `/fail` is appended on failure (healthchecks.io style) |
| `RCLONE_CONFIG_<REMOTE>_*` | - | rclone remotes defined by environment, e.g. `RCLONE_CONFIG_HETZNER_*`, `RCLONE_CONFIG_GARAGE_*` |
| `REDIS_ADMIN_PASSWORD` | - | **Required**: Password of the Redis `admin` user used by `mkredis.py` |
| `REDIS_MAXMEMORY` | `512mb` | Redis memory limit; with `noeviction`, writes fail instead of evicting when it is full |
| `COMPOSE_PROFILES` | - | `garage` enables the optional local S3 |
| `GARAGE_RPC_SECRET` / `GARAGE_ADMIN_TOKEN` | - | Garage secrets (`openssl rand -hex 32`) |
| `GARAGE_BUCKET` / `GARAGE_CAPACITY_GB` / `GARAGE_DATA_DIR` | `shared-db-backups` / `100` / volume | Garage backup bucket, layout capacity and data location (a host path such as an external disk) |
| `DB_MEM_LIMIT` | `2g` | Memory limit of Postgres; a container that exceeds its limit is killed, so leave room for the projects |
| `PGBOUNCER_MEM_LIMIT` | `128m` | Memory limit of PgBouncer |
| `REDIS_MEM_LIMIT` | `1g` | Memory limit of the Redis container; keep it well above `REDIS_MAXMEMORY` (AOF rewrites fork the process) |
| `GARAGE_MEM_LIMIT` | `512m` | Memory limit of Garage |
| `PGBACKUP_MEM_LIMIT` | `512m` | Memory limit of the backup container (pg_dump, psql and rclone run here) |

`db-init` runs `bootstrap.py` on every `docker compose up`, before PgBouncer and pgBackup start. It creates or updates the `backup`, `pgbouncer_auth`, `pgbouncer_admin` and `pgbouncer_stats` roles from these passwords, installs `pgbouncer.user_lookup()` (the `SECURITY DEFINER` function behind PgBouncer's `auth_query`; it never returns superusers or `BYPASSRLS`/replication roles) and creates the `backup_meta` database.

---

## 📖 Usage

### Create New Database

```bash
docker-compose exec pgbackup python /app/mkdb.py my_django_db
docker-compose exec pgbackup python /app/mkdb.py my_ai_app --extension vector
```

- Creates a role and a database with the same name. Names must match `^[a-z][a-z0-9_]*$` (max 63 characters); `postgres`, `backup`, `pgbouncer*`, `pg_*` and other reserved names are refused.
- The role is `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE` with a random password stored as SCRAM-SHA-256.
- The database is owned by that role, uses ICU `tr-TR` from `template0`, and only the role itself and `backup` have `CONNECT`.
- `--extension NAME` (repeatable) installs the extension as superuser inside the new database.
- The database gets a backup policy for the targets in `BACKUP_DEFAULT_TARGETS`.
- The output contains only the project's connection info (`shared-pgbouncer:5432`). The password is shown once and cannot be retrieved later.

### Delete Database

```bash
docker-compose exec pgbackup python /app/rmdb.py my_django_db
docker-compose exec pgbackup python /app/rmdb.py my_django_db --yes
```

Drops the database (terminating its connections), the role with the same name and its backup policies. Without `--yes` it asks you to type the name. Its run history stays in `backup_meta.runs`.

A project may also own a Redis user and an S3 bucket; those are removed separately with `rmredis.py` and `rms3.py`. Backups already in the cloud are kept until their retention expires.

### Backup Targets and Policies

Targets and policies live in the `backup_meta` database; credentials never do (they stay in `.env` and `rclone.conf`).

```bash
docker compose exec pgbackup python /app/backupctl.py target list
docker compose exec pgbackup python /app/backupctl.py target add hetzner --type s3 --remote hetzner --prefix my-bucket --retention 30
docker compose exec pgbackup python /app/backupctl.py target edit gdrive --retention 30
docker compose exec pgbackup python /app/backupctl.py target edit gdrive --disable
docker compose exec pgbackup python /app/backupctl.py target test hetzner
docker compose exec pgbackup python /app/backupctl.py set my_django_db gdrive hetzner
docker compose exec pgbackup python /app/backupctl.py show my_django_db
docker compose exec pgbackup python /app/backupctl.py list
docker compose exec pgbackup python /app/backupctl.py runs my_django_db --limit 10
```

- `set` replaces the database's policy; every database goes to each of its targets separately.
- A database without an enabled policy is still backed up, to `BACKUP_DEFAULT_TARGETS`, and the nightly log warns about it. `backup_meta` itself and `roles.json` go to every enabled target.
- A failing target does not stop the others, but the nightly job exits with code 1 and skips retention on that target.
- The `backup` role can only read `targets`/`policies` and insert into `runs`.

### Manual Backup

```bash
# Trigger backup manually
docker-compose exec pgbackup python /app/backup.py

# Back up one database to its policy targets
docker-compose exec pgbackup python /app/backup.py my_django_db

# Check cloud storage
docker-compose exec pgbackup rclone lsf grdive:records/ --recursive
```

### Restore Database

```bash
# Restore from latest backup
docker-compose exec pgbackup python /app/restore.py my_django_db

# Restore from specific date (accepts YYYY-MM-DD or YYYY/MM/DD)
docker-compose exec pgbackup python /app/restore.py my_django_db 2025-10-07
docker-compose exec pgbackup python /app/restore.py my_django_db 2025/10/7

# Skip safety backup (not recommended)
docker-compose exec pgbackup python /app/restore.py my_django_db --skip-safety-backup
```

- Without `--target`, the latest successful backup among the database's policy targets is used (`backup_meta.runs`, then the folder listings). `--target hetzner` picks one explicitly.
- A target that does not answer is reported once and skipped; the restore continues from the remaining targets and ends with exit code 1, so a silent fallback to an older copy cannot go unnoticed. Listings use short timeouts, so a target whose packets are dropped costs about a minute instead of blocking the run.
- `restore.py --list [db]` shows which dates each target holds and marks unreachable ones.
- Old `.sql` backups are restored as well as the new `.sql.gz` ones. The file's SHA-256 must match `backup_meta.runs`, `SHA256SUMS` or `manifest.json`.
- SQL runs with `ON_ERROR_STOP=1`; any error stops the restore with the failing statement and exit code 1.
- The dump is loaded into the staging database `_restore_<db>` and the old database is replaced only after it succeeded. A failed restore leaves the current database untouched, and a role created for it is removed again.
- A missing role is restored from the folder's `roles.json` with its old password, or created with a new password that is shown once (`--new-password` forces this).
- Afterwards every object belongs to the project role and the database ACL matches `mkdb.py`. Objects owned by roles missing on this server are handed over through temporary placeholder roles; RLS policies of those roles move to the project role.
- The server needs room for both copies while a database is being restored, and the nightly job skips (and warns about) a `_restore_*` database left behind by an interrupted restore.

Import a dump from another server (e.g. Netcup): `.sql`, `.sql.gz` and `pg_dump -Fc` files are accepted.

```bash
docker compose cp ./my_django_db.sql.gz pgbackup:/tmp/my_django_db.sql.gz
docker compose exec pgbackup python /app/restore.py my_django_db --from-file /tmp/my_django_db.sql.gz
```

Disaster recovery restores the project roles, `backup_meta` and every database of a day. Targets that are only defined in the restored `backup_meta` are searched afterwards, so databases kept on a single target are found too. Unreachable targets are skipped with a warning and listed in the closing summary together with the failed databases:

```bash
docker compose exec pgbackup python /app/restore.py --all 2025-10-07
docker compose exec pgbackup python /app/restore.py --all 2025-10-07 --target hetzner:my-bucket --yes
```

When `backup_meta` is unavailable, `--target` also accepts a raw rclone location, `<remote>:<prefix>` (for example `grdive:` or `hetzner:my-bucket`).

### Clone Database

```bash
docker compose exec pgbackup python /app/clone.py my_django_db my_django_db_staging
```

The clone gets its own role (password shown once), the ACL of `mkdb.py` and a default backup policy. Every object belongs to the new role, and RLS policies of the source role move to it.

### Per-Project Connection Limits

Global defaults in `pgbouncer.ini` are `max_user_connections = 20` and `max_db_connections = 20`. Override a project under `[users]`:

```ini
[users]
my_django_db = max_user_connections=10
```

Reload without dropping clients:

```bash
docker-compose kill -s HUP pgbouncer
```

If the change is not picked up (some editors replace the file, and a single-file bind mount keeps pointing at the old one), run `docker-compose restart pgbouncer`. Keep the sum of project limits below Postgres `max_connections` (100 by default).

### PgBouncer Console

`admin_users` and `stats_users` are the non-superuser roles `pgbouncer_admin` and `pgbouncer_stats`, enabled by their passwords in `.env`. `pgbouncer_hba.conf` lets them in only from the internal network (and only to the `pgbouncer` console); `pgbouncer_auth` cannot log in through PgBouncer at all.

```bash
docker compose exec pgbackup psql -h shared-pgbouncer -U pgbouncer_stats pgbouncer -c "SHOW POOLS"
```

### psql in the db Container

Local connections inside the `db` container need a password too:

```bash
docker exec -e PGPASSWORD=<POSTGRES_PASSWORD> -it shared-db psql -U postgres
```

### Shared Redis

Create one ACL user per project:

```bash
docker compose exec pgbackup python /app/mkredis.py my_django_app
```

- The user can only use keys `my_django_app:*` and channels `my_django_app:*` (plus the literal Celery fan-out patterns `my_django_app:/0.celery.pidbox`, `my_django_app:/0.celeryev/*`, `my_django_app:/0.celeryev/worker.*`, because Redis matches `PSUBSCRIBE` patterns literally).
- `@dangerous` commands are blocked, including `FLUSHALL`, `FLUSHDB`, `KEYS`, `CONFIG` and `DEBUG`. The `default` user is disabled.
- The password is shown once. Users are stored in `redis/acl/users.acl`, which is git-ignored.
- `SCAN` cannot be filtered by ACL, so a project can see other projects' key names (not their values).

Remove a project from Redis, or rotate its password:

```bash
docker compose exec pgbackup python /app/rmredis.py my_django_app
docker compose exec pgbackup python /app/rmredis.py my_django_app --keep-keys && \
  docker compose exec pgbackup python /app/mkredis.py my_django_app
```

Deleting the user closes its open connections; without `--keep-keys` every `my_django_app:*` key is deleted too. Both need `--yes` when they cannot ask for confirmation.

Celery (verified with Celery 5.6 / Kombu 5.6: tasks, groups, chords, countdowns and `inspect ping`):

```python
CELERY_BROKER_URL = "redis://my_django_app:<password>@shared-redis:6379/0"
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_BROKER_TRANSPORT_OPTIONS = {"global_keyprefix": "my_django_app:"}
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {"global_keyprefix": "my_django_app:"}
```

Keep database `/0` in the URL; the fan-out patterns above are for db 0.

Django cache:

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://my_django_app:<password>@shared-redis:6379/0",
        "KEY_PREFIX": "my_django_app",
        "TIMEOUT": 300,
    }
}
```

`KEY_PREFIX` makes the keys `my_django_app:1:<key>`. Always set a `TIMEOUT`: with `noeviction`, keys without a TTL are never evicted and writes fail once `REDIS_MAXMEMORY` is reached. `cache.clear()` uses `FLUSHDB` and is refused.

### Local S3 with Garage (optional)

The stack can run [Garage](https://garagehq.deuxfleurs.fr/) (`dxflrs/garage:v2.4.1`) as a local S3: a bucket per project for file storage, and optionally a backup target. Add to `.env`:

```bash
COMPOSE_PROFILES=garage
GARAGE_RPC_SECRET=<openssl rand -hex 32>
GARAGE_ADMIN_TOKEN=<openssl rand -hex 32>
GARAGE_DATA_DIR=/mnt/backup-disk/garage
RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID=GK<openssl rand -hex 12>
RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY=<openssl rand -hex 32>
```

`garage-init` applies the single-node layout, creates the backup bucket, imports the access key and registers the backup target `garage`; it is safe to run again.

⚠️ Garage runs on this server and writes to this server's disk, so it is **not an off-site backup**: it does not survive a disk or server failure and must not be the only or default backup target. Keep Drive or Hetzner S3 in `BACKUP_DEFAULT_TARGETS` and put `GARAGE_DATA_DIR` on another disk. An existing Garage elsewhere (for example on another server reachable over Tailscale) is a real off-site target: point `RCLONE_CONFIG_GARAGE_ENDPOINT` at it, keep `COMPOSE_PROFILES` empty and add the target with `backupctl.py target add garage --type s3 --remote garage --prefix <bucket>`.

#### A Bucket per Project

```bash
docker compose exec pgbackup python /app/mks3.py my_django_app
docker compose exec pgbackup python /app/rms3.py my_django_app        # bucket, its objects and the key
```

- The bucket is the project name with `_` replaced by `-` (S3 names cannot contain underscores), so `my_django_app` gets `my-django-app`.
- The key may read and write only that bucket. It cannot create buckets, delete its own bucket or touch the backup bucket, and the secret is shown once.
- Applications reach the S3 API at `http://shared-garage:3900` from `shared-db-net`. The admin API (`10.250.0.11:3903`) and the RPC port stay on the internal network, so `mks3.py`/`rms3.py` only work from `pgbackup`.
- Browsers cannot resolve `shared-garage`; serve files through the application (or its own reverse proxy) instead of handing out presigned URLs directly.

Django with `django-storages[s3]`:

```python
AWS_S3_ENDPOINT_URL = "http://shared-garage:3900"
AWS_S3_REGION_NAME = "garage"
AWS_S3_ADDRESSING_STYLE = "path"
AWS_STORAGE_BUCKET_NAME = "my-django-app"
AWS_ACCESS_KEY_ID = "GK..."
AWS_SECRET_ACCESS_KEY = "..."
STORAGES = {"default": {"BACKEND": "storages.backends.s3.S3Storage"}, ...}
```

Also set these two environment variables for the application (boto3 reads them):

```bash
AWS_REQUEST_CHECKSUM_CALCULATION=when_required
AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
```

Without them, downloading a file that boto3 uploaded in parts (over 8 MB by default) fails with `FlexibleChecksumError`, because Garage returns a checksum that boto3 verifies as if it covered the whole object. Tested with boto3 1.43 against Garage v2.4.1 and v2.3.0.

### Access pgAdmin

1. Open browser: `http://localhost:9090`
2. Login with credentials from `.env`
3. Add server:
   - Host: `shared-db`
   - Port: `5432`
   - User: `POSTGRES_USER`
   - Password: `POSTGRES_PASSWORD`

### Check Backup Status

```bash
# View backup logs (via docker logs)
docker-compose logs pgbackup

# Follow logs in real-time
docker-compose logs -f pgbackup

# Check cloud backups
docker-compose exec pgbackup rclone ls grdive:records/2025/10/7/

# Check manual backups
docker-compose exec pgbackup rclone ls grdive:manual_backups/

# Check cron status
docker-compose exec pgbackup pgrep crond

# Recent runs per database and target
docker compose exec pgbackup python /app/backupctl.py runs
```

---

## 📁 File Structure

```
shared-db/
├── docker-compose.yml          # Orchestration configuration
├── pgbouncer.ini               # PgBouncer pooling, auth_query and per-project limits
├── pgbouncer_hba.conf          # PgBouncer client rules (service users only from the internal network)
├── pg_hba.conf                 # Postgres client authentication rules
├── .env                        # Environment variables (NEVER commit!)
├── .gitattributes              # LF line endings for scripts and configs
├── .gitignore                  # Git ignore rules
├── README.md                   # This file
│
├── garage/
│   └── garage.toml             # Optional local S3 (profile garage)
│
├── rclone/
│   └── rclone.conf             # rclone OAuth tokens (NEVER commit!)
│
├── redis/
│   ├── redis.conf              # AOF, noeviction, ACL file, unix socket
│   ├── entrypoint.sh           # Keeps the default user disabled and the admin user in sync
│   └── acl/users.acl           # Redis users (NEVER commit!)
│
└── scripts/
    ├── Dockerfile              # Backup container image
    ├── requirements.txt        # Python dependencies
    ├── common.py               # Shared settings, names and connections
    ├── metadb.py               # backup_meta schema, targets, policies and runs
    ├── backup.py               # Automated daily backup script
    ├── backupctl.py            # Backup targets and policies CLI
    ├── bootstrap.py            # Service roles, PgBouncer auth function, backup_meta (db-init)
    ├── restore.py              # Database restore script
    ├── clone.py                # Clone a database into a new isolated project
    ├── mkdb.py                 # Isolated project role + database creation
    ├── rmdb.py                 # Project database + role removal
    ├── mkredis.py              # Per-project Redis ACL user
    ├── rmredis.py              # Removes a project's Redis user and keys
    ├── mks3.py                 # Per-project Garage bucket and key
    ├── rms3.py                 # Removes a project's bucket, its objects and key
    ├── garage_admin.py         # Garage admin API client and bucket naming
    └── garage_init.py          # Garage layout, backup bucket, key and target (garage-init)
```

### Cloud Storage Structure

```
<remote>:<prefix>/             (Drive root, or the bucket for S3/Garage)
├── records/<SERVER_NAME>/      # Daily automated backups, one folder per server
│   └── 2025/
│       └── 10/
│           └── 7/
│               ├── shared_db.sql.gz           ← Latest backup (restore uses this)
│               ├── my_django_db.sql.gz
│               ├── backup_meta.sql.gz         ← Targets, policies and run history
│               ├── roles.json                 ← Roles with password hashes
│               ├── SHA256SUMS
│               ├── manifest.json              ← Policies, targets and checksums of this folder
│               └── olds/                      ← Previous same-day backups
│                   └── 02_00/
│
└── manual_backups/<SERVER_NAME>/   # Safety backups before restore
    └── 2025/
        └── 10/
            └── 7/
                └── my_django_db_before_restore_12-30-45.sql.gz
```

Backups taken before the multi-target version are plain `<db>.sql` files; `restore.py` still reads them.

---

## 🔧 Troubleshooting

### Issue: "Google verification process not completed"

**Solution:**
1. Go to Google Cloud Console → OAuth consent screen
2. Add your email as "Test user"
3. Or publish the app (for production)

### Issue: "File not found: ." after setting root_folder_id

**Solution:**
- Ensure you're using only the folder ID, not the full URL
- Correct: `1qCTRvCtmfOM9h6851P8aDzb-TrQYUwf0`
- Wrong: `https://drive.google.com/drive/folders/1qCTRvCtm...`

### Issue: "ERROR: Failed to save config after 10 tries"

**Solution:**
- Ensure rclone folder is mounted as directory, not single file
- Check `docker-compose.yml`: `- ./rclone:/root/.config/rclone`

### Issue: Backup container not starting

**Solution:**
```bash
# Check logs
docker-compose logs pgbackup

# Rebuild container
docker-compose up -d --build pgbackup

# Verify cron is running
docker-compose exec pgbackup pgrep crond
```

### Issue: Database connection refused

**Solution:**
```bash
# Check if DB is healthy
docker-compose ps

# Check DB logs
docker-compose logs db

# Verify network connectivity
docker-compose exec pgbackup ping shared-db
```

### Issue: "fe_sendauth: no password supplied" in the db container

**Solution:**
- `pg_hba.conf` requires SCRAM for local connections too; pass `-e PGPASSWORD=...` (see [psql in the db Container](#psql-in-the-db-container))

### Issue: Celery worker stops with "No permissions to access a channel"

**Solution:**
- The Redis user needs the literal Celery fan-out patterns; users created by `mkredis.py` have them for db 0
- Keep `/0` in the broker URL and set `global_keyprefix` for both the broker and the result backend

### Issue: A `_restore_*` database is left over

**Solution:**
- An interrupted restore leaves its staging database behind; the nightly job warns about it and does not back it up
- Check it, then drop it: `docker exec -e PGPASSWORD=<POSTGRES_PASSWORD> -it shared-db psql -U postgres -c 'DROP DATABASE "_restore_my_django_db"'`
- A `_replaced_*` database holds the **previous** content of a database whose restore was interrupted during the swap; check it before dropping it

### Issue: `FlexibleChecksumError` when downloading from Garage

**Solution:**
- Set `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` for the application (see [A Bucket per Project](#a-bucket-per-project))

### Issue: Restore ends with exit code 1 although it succeeded

**Solution:**
- One of the backup targets did not answer; the summary names it. The data came from another target, but the copy on the unreachable one could be newer — check that target before treating the restore as complete

### Issue: Restore shows too many system tables

**Solution:**
- This is normal! The `\dt` command only shows public schema tables
- System tables are in other schemas and won't be shown

---

## 🚀 Advanced

### Multiple Same-Day Backups

The system automatically handles multiple backups on the same day:

**Scenario:**
1. **02:00 AM** - Cron runs, backup created at `records/2025/10/7/`
2. **02:30 PM** - Manual backup triggered

**Result:**
```
records/2025/10/7/
├── shared_db.sql           ← 14:30 backup (latest)
├── test_db.sql
└── olds/
    └── 02_00/              ← 02:00 backup (archived)
        ├── shared_db.sql
        └── test_db.sql
```

**Behavior:**
- Latest backup always at root: `records/YYYY/MM/DD/`
- Previous backups moved to: `records/YYYY/MM/DD/olds/HH_MM/`
- Restore always uses latest (root level)
- Old backups preserved for manual recovery if needed

### Connect Django Project

```yaml
# In your Django project's docker-compose.yml
services:
  web:
    # ... your config
    depends_on:
      - db
    environment:
      DATABASE_URL: postgresql://my_django_db:<password from mkdb>@shared-pgbouncer:5432/my_django_db
    networks:
      - shared-db-net

networks:
  shared-db-net:
    external: true
    name: shared-db-net
```

Postgres is not on `shared-db-net`; always connect through `shared-pgbouncer` with the project's own role. PgBouncer runs in transaction mode, so set `"DISABLE_SERVER_SIDE_CURSORS": True` in the Django `DATABASES` entry.

### Migrate to S3

S3 is a backup target like Drive: define the remote with `RCLONE_CONFIG_<REMOTE>_*` variables and register it with `backupctl.py target add` (see [Setup Hetzner Object Storage](#3b-setup-hetzner-object-storage-s3)).

### Custom Backup Schedule

Edit `scripts/Dockerfile`:
```dockerfile
# Change cron schedule (current: 02:00 daily)
RUN echo '0 2 * * * python /app/backup.py >> /backups/backup.log 2>&1' > /etc/crontabs/root

# Examples:
# Every 6 hours: '0 */6 * * *'
# Every Sunday 03:00: '0 3 * * 0'
# Twice daily: '0 2,14 * * *'
```

Rebuild container:
```bash
docker-compose up -d --build pgbackup
```

### Change Retention Period

Retention is set per target:
```bash
docker compose exec pgbackup python /app/backupctl.py target edit gdrive --retention 30
```

Old folders under `records/<SERVER_NAME>/` and `manual_backups/<SERVER_NAME>/` are deleted after the next successful nightly upload to that target.

### Monitor Backup Size

```bash
# Check total cloud storage
docker-compose exec pgbackup rclone size grdive:

# Check specific backup
docker-compose exec pgbackup rclone size grdive:records/2025/10/7/

# Check archived backups
docker-compose exec pgbackup rclone lsf grdive:records/2025/10/7/olds/ --recursive

# List largest backups
docker-compose exec pgbackup rclone ls grdive:records/ --recursive | sort -k1 -n -r | head -10
```

### Backup Single Database Manually

```bash
# Create temp directory
docker-compose exec pgbackup mkdir -p /tmp/manual_dump

# Dump specific database
docker-compose exec pgbackup pg_dump \
  -h shared-db \
  -p 5432 \
  -U postgres \
  -d my_django_db \
  -f /tmp/manual_dump/my_django_db.sql

# Upload to custom path
docker-compose exec pgbackup rclone copy \
  /tmp/manual_dump/my_django_db.sql \
  grdive:custom_backups/

# Cleanup
docker-compose exec pgbackup rm -rf /tmp/manual_dump
```

---

## 📊 Maintenance

### Update Images

Every image is pinned to an exact version (`pgvector/pgvector:0.8.6-pg18-trixie`, `edoburu/pgbouncer:v1.25.2-p0`, `redis:8.8.2-alpine3.23`, `dxflrs/garage:v2.4.1`, `python:3.14.7-alpine3.24`), so an update is a deliberate edit of `docker-compose.yml` or `scripts/Dockerfile`:

```bash
docker compose pull && docker compose up -d
```

**Warning:** Always backup before major version upgrades! After a pgvector update run `ALTER EXTENSION vector UPDATE;` in every database that uses it, and keep the PostgreSQL major version at 18 unless you plan a `pg_upgrade`.

### Update Python Dependencies

```bash
# Update requirements.txt
docker-compose exec pgbackup pip list --outdated

# Rebuild container
docker-compose up -d --build pgbackup
```

### Rotate rclone Token

```bash
# Reconfigure rclone
docker-compose exec pgbackup rclone config reconnect grdive:

# Or delete and recreate
docker-compose exec pgbackup rclone config delete grdive
docker-compose exec pgbackup rclone config
```

---

## 🔐 Security Notes

- ⚠️ **NEVER commit** `.env` or `rclone/rclone.conf` to Git
- ✅ Use strong passwords for `POSTGRES_PASSWORD` and `PGADMIN_PASSWORD`
- ✅ Keep OAuth tokens secure in `rclone.conf`
- ✅ Limit Google Drive access using `root_folder_id`
- ✅ No host ports: Postgres, PgBouncer, Redis and Garage are reachable only from Docker networks
- ✅ Give each application only its own project role, never `POSTGRES_PASSWORD`
- ✅ Nightly dumps run as the `backup` role (`pg_read_all_data` + `BYPASSRLS`). The superuser is used in exactly one step: dumping roles to `roles.json`, because password hashes live in `pg_authid`, which only superusers can read. Without them a disaster restore could not give applications their old passwords back.
- ⚠️ `roles.json` holds the SCRAM verifiers of the project roles; keep backup buckets and Drive folders private. `postgres` and the service roles are not in it — they are recreated from `.env` by `db-init`.
- ✅ Redis project users are limited to their own prefix; the Redis `admin` user is only used through the unix socket shared with `pgbackup`
- ✅ Garage keys are limited to one bucket, and its admin API is only reachable from the internal network, so no application can create, read or delete another project's bucket
- ✅ Regularly rotate database passwords
- ✅ Monitor cloud storage quotas

---

## 📝 License

This project is provided as-is for educational and production use.

---

## 🤝 Contributing

Contributions welcome! Please follow these guidelines:

1. Test changes locally first
2. Update README if adding features
3. Follow existing code style
4. Document environment variables

---

## 📞 Support

For issues or questions:
1. Check [Troubleshooting](#-troubleshooting) section
2. Review Docker logs: `docker-compose logs`
3. Verify environment configuration
4. Test rclone connectivity: `rclone lsd grdive:`

---

**Built with ❤️ for reliable PostgreSQL backups**
