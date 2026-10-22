#!/bin/sh
# Sauvegarde Postgres : un dump SQL compressé horodaté dans BACKUP_DIR, puis purge de la rétention.
# Configuration par variables d'environnement (PGHOST, PGUSER, PGPASSWORD, PGDATABASE) ; aucun secret ici.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

case "$RETENTION_DAYS" in
  ''|*[!0-9]*) echo "BACKUP_RETENTION_DAYS invalide : $RETENTION_DAYS" >&2; exit 1 ;;
esac

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/${PGDATABASE}_${stamp}.sql.gz"
tmp="$final.part"

trap 'rm -f "$tmp" "$tmp.sql"' EXIT
# Pas de pipe : sans pipefail, un échec de pg_dump serait masqué par gzip.
pg_dump --clean --if-exists --no-owner > "$tmp.sql"
[ "$(wc -c < "$tmp.sql")" -gt 200 ] || { echo "dump vide ou tronqué" >&2; exit 1; }
gzip -9 -c "$tmp.sql" > "$tmp"
rm -f "$tmp.sql"
gzip -t "$tmp"
mv "$tmp" "$final"
trap - EXIT
echo "backup ok : $final ($(du -h "$final" | cut -f1))"

if [ "$RETENTION_DAYS" -gt 0 ]; then
  find "$BACKUP_DIR" -maxdepth 1 -name "${PGDATABASE}_*.sql.gz" -type f -mtime "+$RETENTION_DAYS" -print -delete
fi
