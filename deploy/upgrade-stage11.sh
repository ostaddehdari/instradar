#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="/opt/instradar-py"
CURRENT="$APP_ROOT/current"
SHARED="$APP_ROOT/shared"
VENV="$APP_ROOT/venv"
STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP="$APP_ROOT/backups/stage11-$STAMP"
mkdir -p "$BACKUP"

if [[ ! -f "$SHARED/app.env" ]]; then echo "missing $SHARED/app.env" >&2; exit 1; fi
if [[ ! -x "$VENV/bin/python" ]]; then echo "missing $VENV/bin/python" >&2; exit 1; fi

set -a; source "$SHARED/app.env"; set +a
RELEASE_ROOT="$(pwd -P)"
test -f "$RELEASE_ROOT/VERSION" || { echo "ERROR: release VERSION is missing" >&2; exit 1; }
VERSION="$(tr -d '[:space:]' < "$RELEASE_ROOT/VERSION")"
test -n "$VERSION"

echo "[1/8] backup current source"
tar -czf "$BACKUP/current.tar.gz" -C "$APP_ROOT" current

echo "[2/8] backup database"
# Django uses PG* variables, not DATABASE_URL. Refuse deployment if a DB dump
# cannot be created; a release upgrade must never proceed without a DB backup.
for name in PGDATABASE PGUSER PGPASSWORD PGHOST PGPORT; do
  [[ -n "${!name:-}" ]] || { echo "ERROR: $name is required for PostgreSQL backup." >&2; exit 1; }
done
export PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD
pg_dump --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" --dbname="$PGDATABASE" \
  --no-owner --no-privileges > "$BACKUP/database.sql"
if ! test -s "$BACKUP/database.sql"; then
  echo "ERROR: PostgreSQL backup is empty; refusing deployment." >&2
  exit 1
fi
echo "Database backup: $BACKUP/database.sql"

echo "[3/8] stage source"
STAGE="$(mktemp -d "$APP_ROOT/stage11.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
cp -a . "$STAGE/"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +

# This script is copied into the extracted release; deploy it from the release directory.
if [[ "$STAGE" == *"/current" ]]; then :; fi

echo "[4/8] python compile"
"$VENV/bin/python" -m compileall -q "$STAGE"

# Atomic-ish release swap: keep current intact until checks pass.
NEW="$APP_ROOT/releases/stage11-$STAMP"
mkdir -p "$APP_ROOT/releases"
cp -a "$STAGE/." "$NEW/"
ln -sfn "$NEW" "$APP_ROOT/next"

echo "[5/8] django check + migrate"
cd "$NEW"
set -a; source "$SHARED/app.env"; set +a
"$VENV/bin/python" manage.py check
"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py collectstatic --noinput >/dev/null

echo "[6/8] smoke tests"
TEST_OUTPUT="$("$VENV/bin/python" -m unittest discover -s tests -p 'test_stage11*.py' -v 2>&1)" || {
  printf '%s\n' "$TEST_OUTPUT"
  exit 1
}
printf '%s\n' "$TEST_OUTPUT"
if grep -Eq 'Ran 0 tests in' <<<"$TEST_OUTPUT"; then
  echo "Stage 11 smoke test discovery found 0 tests; refusing deployment." >&2
  exit 1
fi

# Preserve the existing nginx-facing current path and services.
if [[ -L "$APP_ROOT/current" ]]; then
  ln -sfn "$NEW" "$APP_ROOT/current"
else
  # Existing installations may use a real directory. Keep the directory and
  # replace its contents only after migration/checks have passed.
  rsync -a --delete "$NEW/" "$APP_ROOT/current/"
fi

echo "[7/8] restart services"
systemctl restart instradar-web instradar-worker instradar-beat instradar-browser instradar-telegram
sleep 2
systemctl is-active --quiet instradar-web
systemctl is-active --quiet instradar-worker
systemctl is-active --quiet instradar-beat
systemctl is-active --quiet instradar-browser
systemctl is-active --quiet instradar-telegram

 echo "[8/8] health"
curl -fsS -H 'Host: srun.ir' http://127.0.0.1:18832/instradar/health/ | \
  "$VENV/bin/python" -c 'import json,sys; d=json.load(sys.stdin); assert d["version"] == sys.argv[1] and all(d[k] is True for k in ("database", "browser", "worker", "telegram"))' "$VERSION"
printf '\n%s installed successfully: %s\n' "$VERSION" "$NEW"
