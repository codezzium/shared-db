# 🐘 Shared PostgreSQL Backup System

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
- **☁️ Cloud-First Architecture**: All backups stored in Google Drive (S3 ready)
- **🗄️ Multiple Databases**: Each database backed up to separate SQL files
- **🔒 Safety Backups**: Automatic safety dump before restore operations
- **🧹 Auto Cleanup**: 15-day retention policy on cloud storage
- **📅 Hierarchical Storage**: Year/Month/Day folder structure
- **♻️ Smart Archiving**: Multiple same-day backups archived in `olds/HH_MM/` subfolders
- **🚀 Zero Local Storage**: Temp files only, auto-cleaned after operations
- **📊 pgAdmin Integration**: Web UI for database management

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Postgres   │  │   pgAdmin    │  │   pgBackup   │    │
│  │   (Port 5432)│◄─┤  (Port 9090) │  │   (Cron)     │    │
│  │              │  │              │  │              │    │
│  └──────┬───────┘  └──────────────┘  └──────┬───────┘    │
│         │                                     │            │
│         │                                     │            │
└─────────┼─────────────────────────────────────┼────────────┘
          │                                     │
          │                                     │
    ┌─────▼──────┐                        ┌────▼─────┐
    │   Volume   │                        │  Rclone  │
    │ postgres_  │                        │  Config  │
    │   data     │                        └────┬─────┘
    └────────────┘                             │
                                               │
                                         ┌─────▼──────┐
                                         │   Google   │
                                         │   Drive    │
                                         │            │
                                         │  records/  │
                                         │  manual_   │
                                         │  backups/  │
                                         └────────────┘
```

### Backup Flow

```
1. Cron triggers backup.py (02:00 AM daily)
2. Create temp directory
3. Dump each database → /tmp/backup_TIMESTAMP/
4. Generate SHA256 checksums
5. Check if backup already exists for today
   - If exists: Move to records/YYYY/MM/DD/olds/HH_MM/
6. Upload to Google Drive → records/YYYY/MM/DD/
7. Clean temp directory
8. Prune old cloud backups (15+ days)
```

### Restore Flow

```
1. User runs restore.py
2. List cloud backups, find latest with target DB
3. Download to temp → /tmp/restore_TIMESTAMP/
4. Safety backup current DB → manual_backups/YYYY/MM/DD/
5. Drop and recreate database
6. Restore from SQL file (quiet mode)
7. Verify tables
8. Clean temp directory
```

---

## 📦 Prerequisites

- **Docker & Docker Compose** (v2.0+)
- **Google Cloud Project** with Drive API enabled
- **OAuth 2.0 Credentials** (Client ID & Secret)

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

# pgAdmin Configuration
PGADMIN_EMAIL=admin@example.com
PGADMIN_PASSWORD=admin_secure_password

# Backup Configuration
BACKUP_RETENTION_DAYS=15
RCLONE_REMOTE=grdive:  # Default remote name
```

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
| `db` | 5432 (localhost only) | PostgreSQL 17 database |
| `pgadmin` | 9090 (localhost only) | Web-based DB management UI |
| `pgbackup` | - | Backup/restore automation container |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | `shared_db` | Default database name |
| `POSTGRES_USER` | `postgres` | Database superuser |
| `POSTGRES_PASSWORD` | - | **Required**: Database password |
| `POSTGRES_HOST` | `shared-db` | Docker service name (hardcoded in scripts) |
| `POSTGRES_PORT` | `5432` | Database port |
| `BACKUP_RETENTION_DAYS` | `15` | Days to keep backups in cloud |
| `RCLONE_REMOTE` | `grdive:` | rclone remote name |

---

## 📖 Usage

### Create New Database

```bash
# Interactive mode
docker-compose exec pgbackup python /app/mkdb.py

# Non-interactive mode
docker-compose exec pgbackup python /app/mkdb.py my_django_db
```

### Manual Backup

```bash
# Trigger backup manually
docker-compose exec pgbackup python /app/backup.py

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
```

---

## 📁 File Structure

```
shared-db/
├── docker-compose.yml          # Orchestration configuration
├── .env                        # Environment variables (NEVER commit!)
├── .gitignore                  # Git ignore rules
├── README.md                   # This file
│
├── rclone/
│   └── rclone.conf             # rclone OAuth tokens (NEVER commit!)
│
└── scripts/
    ├── Dockerfile              # Backup container image
    ├── requirements.txt        # Python dependencies
    ├── backup.py               # Automated daily backup script
    ├── restore.py              # Database restore script
    └── mkdb.py                 # Quick database creation utility
```

### Cloud Storage Structure

```
Google Drive (or S3):
├── records/                    # Daily automated backups
│   └── 2025/
│       └── 10/
│           └── 7/
│               ├── shared_db.sql              ← Latest backup (restore uses this)
│               ├── my_django_db.sql
│               ├── SHA256SUMS
│               └── olds/                      ← Previous same-day backups
│                   ├── 02_00/                 ← 02:00 backup
│                   │   ├── shared_db.sql
│                   │   ├── my_django_db.sql
│                   │   └── SHA256SUMS
│                   └── 14_30/                 ← 14:30 backup
│                       ├── shared_db.sql
│                       ├── my_django_db.sql
│                       └── SHA256SUMS
│
└── manual_backups/             # Safety backups before restore
    └── 2025/
        └── 10/
            └── 7/
                ├── shared_db_before_restore_05-19-21.sql
                └── my_django_db_before_restore_12-30-45.sql
```

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
      DATABASE_URL: postgresql://postgres:password@shared-db:5432/my_django_db
    networks:
      - shared-db-net

networks:
  shared-db-net:
    external: true
    name: shared-db-net
```

### Migrate to S3

Update `.env`:
```bash
RCLONE_REMOTE=s3backup:
```

Configure rclone:
```bash
docker-compose exec pgbackup rclone config
# Select: s3 (Amazon S3)
# Provider: AWS
# Enter credentials
```

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

Update `.env`:
```bash
BACKUP_RETENTION_DAYS=30  # Keep for 30 days
```

Restart container:
```bash
docker-compose restart pgbackup
```

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

### Update PostgreSQL Version

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:18  # Update version
```

**Warning:** Always backup before major version upgrades!

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
- ✅ Use localhost binding for exposed ports (already configured)
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
