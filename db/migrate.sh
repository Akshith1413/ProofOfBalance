#!/bin/sh
# Apply every migration in order, in ONE transaction, holding an advisory lock
# so two concurrent runs (e.g. two Helm upgrades) cannot interleave.
# Used by docker compose and by the Helm migration Job (both run postgres:16).
#
# Env: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE, MIGRATIONS_DIR (default /migrations)
set -eu

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
WAIT_SECONDS="${WAIT_SECONDS:-180}"

ls "$MIGRATIONS_DIR"/*.sql >/dev/null 2>&1 || {
  echo "migrate: no .sql files in $MIGRATIONS_DIR" >&2
  exit 1
}

echo "migrate: waiting up to ${WAIT_SECONDS}s for postgres at ${PGHOST:-localhost}:${PGPORT:-5432}"
i=0
until pg_isready -q; do
  i=$((i + 2))
  if [ "$i" -ge "$WAIT_SECONDS" ]; then
    echo "migrate: postgres not ready after ${WAIT_SECONDS}s" >&2
    exit 1
  fi
  sleep 2
done

{
  printf '%s\n' 'DO $$ BEGIN PERFORM pg_advisory_xact_lock(4242); END $$;'
  for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort); do
    printf '\\echo migrate: applying %s\n' "$(basename "$f")"
    cat "$f"
    printf '\n'
  done
} | psql -X -v ON_ERROR_STOP=1 --single-transaction -q

echo "migrate: done"
