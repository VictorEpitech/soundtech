#!/bin/sh
# Restauration d'un dump créé par backup.sh. DESTRUCTIF : remplace le contenu de la base PGDATABASE.
# Usage : restore.sh <fichier.sql.gz>   (sans argument : liste les sauvegardes)
# Confirmation : taper exactement le nom de la base, ou RESTORE_CONFIRM=<nom de la base>.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"

if [ "$#" -ne 1 ]; then
  echo "Usage : $0 <fichier.sql.gz>" >&2
  echo "Sauvegardes disponibles dans $BACKUP_DIR :" >&2
  ls -1t "$BACKUP_DIR" 2>/dev/null >&2 || true
  exit 2
fi

file="$1"
[ -f "$file" ] || file="$BACKUP_DIR/$1"
[ -f "$file" ] || { echo "Fichier introuvable : $1" >&2; exit 1; }
gzip -t "$file" || { echo "Archive corrompue : $file" >&2; exit 1; }

echo "ATTENTION : la base '$PGDATABASE' sur '$PGHOST' va être écrasée par $file."
echo "Arrêter api et worker avant de continuer."
answer="${RESTORE_CONFIRM:-}"
if [ -z "$answer" ]; then
  printf "Pour confirmer, taper le nom de la base (%s) : " "$PGDATABASE"
  read -r answer || answer=""
fi
if [ "$answer" != "$PGDATABASE" ]; then
  echo "Confirmation invalide, abandon." >&2
  exit 1
fi

gunzip -c "$file" | psql --set ON_ERROR_STOP=1 --single-transaction --quiet
echo "restauration ok : $file"
