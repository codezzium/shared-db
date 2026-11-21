#!/bin/sh
set -eu

: "${HBA_TEMPLATE:?HBA_TEMPLATE is required}"
: "${HBA_FILE:?HBA_FILE is required}"
: "${INTERNAL_SUBNET:?INTERNAL_SUBNET is required}"
: "${PGBOUNCER_IP:?PGBOUNCER_IP is required}"

tmp="$HBA_FILE.tmp"
sed -e "s|@INTERNAL_SUBNET@|$INTERNAL_SUBNET|g" -e "s|@PGBOUNCER_IP@|$PGBOUNCER_IP|g" "$HBA_TEMPLATE" > "$tmp"
if grep -q '@[A-Z_]*@' "$tmp"; then
    echo "render-hba: unresolved placeholder in $HBA_TEMPLATE" >&2
    rm -f "$tmp"
    exit 1
fi
chmod 644 "$tmp"
mv "$tmp" "$HBA_FILE"

[ "$#" -eq 0 ] || exec "$@"
