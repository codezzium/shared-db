#!/bin/sh
set -eu

: "${REDIS_ADMIN_PASSWORD:?REDIS_ADMIN_PASSWORD is required}"

acl_dir=/etc/redis/acl
acl_file="$acl_dir/users.acl"
admin_hash=$(printf '%s' "$REDIS_ADMIN_PASSWORD" | sha256sum | cut -d ' ' -f 1)

mkdir -p "$acl_dir" /run/redis
touch "$acl_file"
grep -Ev '^user (default|admin) ' "$acl_file" > "$acl_file.tmp" || true
{
    echo "user default off resetkeys resetchannels -@all"
    echo "user admin on #$admin_hash ~* &* +@all"
    cat "$acl_file.tmp"
} > "$acl_file"
rm -f "$acl_file.tmp"
chown -R redis:redis "$acl_dir" /run/redis
chmod 700 "$acl_dir"
chmod 600 "$acl_file"

exec docker-entrypoint.sh redis-server /usr/local/etc/redis/redis.conf --maxmemory "${REDIS_MAXMEMORY:-512mb}"
