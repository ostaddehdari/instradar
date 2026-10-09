#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

APP_ROOT="/opt/instradar-py"
CURRENT="$APP_ROOT/current"
SHARED="$APP_ROOT/shared"
VENV="$APP_ROOT/venv"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP="$APP_ROOT/backups/stage13-$STAMP"
NEW="$APP_ROOT/releases/stage13-$STAMP-$$"
EXPECTED_VERSION="3.13.0-account-pool-safety"
SERVICES=(instradar-web instradar-worker instradar-beat instradar-browser instradar-telegram)
SWITCHED=0
OLD_TARGET=""
OLD_DIRECTORY=""

rollback_release() {
  local code=$?
  trap - ERR
  set +e
  if [[ "$SWITCHED" -eq 1 ]]; then
    echo "Upgrade failed after release switch; restoring previous code." >&2
    systemctl stop "${SERVICES[@]}" >/dev/null 2>&1
    if [[ -n "$OLD_TARGET" && -d "$OLD_TARGET" ]]; then
      local rollback_link="$APP_ROOT/.current-rollback-$$"
      ln -s "$OLD_TARGET" "$rollback_link"
      mv -Tf "$rollback_link" "$CURRENT"
    elif [[ -n "$OLD_DIRECTORY" && -d "$OLD_DIRECTORY" ]]; then
      if [[ -L "$CURRENT" ]]; then unlink "$CURRENT"; fi
      mv "$OLD_DIRECTORY" "$CURRENT"
    fi
    systemctl start "${SERVICES[@]}" >/dev/null 2>&1
  fi
  echo "Upgrade stopped. Backups are available in: $BACKUP" >&2
  exit "$code"
}
trap rollback_release ERR

[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: run this script as root." >&2; exit 1; }
[[ -f "$SHARED/app.env" ]] || { echo "ERROR: missing $SHARED/app.env" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || { echo "ERROR: missing $VENV/bin/python" >&2; exit 1; }
[[ -e "$CURRENT" ]] || { echo "ERROR: existing installation not found at $CURRENT" >&2; exit 1; }
for command_name in pg_dump rsync tar curl systemctl runuser; do
  command -v "$command_name" >/dev/null || {
    echo "ERROR: required command is missing: $command_name" >&2
    exit 1
  }
done

VERSION="$(tr -d '[:space:]' < "$SOURCE/VERSION")"
[[ "$VERSION" == "$EXPECTED_VERSION" ]] || {
  echo "ERROR: expected $EXPECTED_VERSION, found $VERSION" >&2
  exit 1
}

set -a
# This administrator-owned file is the canonical deployment environment.
source "$SHARED/app.env"
set +a
# Never let a shell inherited from a developer test session select SQLite for
# production checks or migrations. The smoke-test command sets it explicitly.
unset INSTRADAR_TEST
for name in PGDATABASE PGUSER PGPASSWORD PGHOST PGPORT DJANGO_SECRET_KEY; do
  [[ -n "${!name:-}" ]] || { echo "ERROR: $name is missing from app.env" >&2; exit 1; }
done

mkdir -p "$BACKUP" "$APP_ROOT/releases"

echo "[1/9] Backing up active source"
tar --dereference -czf "$BACKUP/current.tar.gz" -C "$APP_ROOT" current
test -s "$BACKUP/current.tar.gz"

echo "[2/9] Backing up PostgreSQL"
export PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD
pg_dump --format=custom --no-owner --no-privileges --file="$BACKUP/database.dump"
test -s "$BACKUP/database.dump"

echo "[3/9] Preparing immutable release"
mkdir "$NEW"
rsync -a --delete \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='*.py[co]' --exclude='staticfiles/' \
  "$SOURCE/" "$NEW/"
chown -R instradar:instradar "$NEW"

echo "[4/9] Installing, compiling and validating dependencies"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  -r "$NEW/requirements.txt"
"$VENV/bin/python" -c '
from importlib.metadata import version
assert version("aiograpi") == "2.0.5", version("aiograpi")
import socksio
from aiograpi import Client
client = Client(private_transport="requests")
assert client.private_transport == "requests", client.private_transport
'
runuser -u instradar -- "$VENV/bin/python" -m compileall -q "$NEW"
"$VENV/bin/python" -m pip check

echo "[5/9] Running isolated release tests"
(
  cd "$NEW"
  runuser -u instradar -- env INSTRADAR_TEST=1 \
    "$VENV/bin/python" manage.py test \
      tests.test_stage1 tests.test_stage3 tests.test_stage4 \
      tests.test_stage8 tests.test_stage12 tests.test_stage12_static \
      tests.test_stage13 \
      --verbosity 1
)

echo "[6/9] Checking Django, migrating and collecting static files"
(
  cd "$NEW"
  runuser -u instradar -- "$VENV/bin/python" manage.py check --deploy
  runuser -u instradar -- "$VENV/bin/python" manage.py migrate --noinput
  runuser -u instradar -- "$VENV/bin/python" manage.py collectstatic --noinput >/dev/null
)

echo "[7/9] Installing service definitions and switching release"
for service in "${SERVICES[@]}"; do
  install -m 644 "$NEW/deploy/$service.service" "/etc/systemd/system/$service.service"
done
systemctl daemon-reload
systemctl stop "${SERVICES[@]}"
if [[ -L "$CURRENT" ]]; then
  OLD_TARGET="$(readlink -f "$CURRENT")"
  next_link="$APP_ROOT/.current-stage13-$$"
  ln -s "$NEW" "$next_link"
  mv -Tf "$next_link" "$CURRENT"
elif [[ -d "$CURRENT" ]]; then
  OLD_DIRECTORY="$APP_ROOT/releases/pre-stage13-$STAMP-$$"
  mv "$CURRENT" "$OLD_DIRECTORY"
  ln -s "$NEW" "$CURRENT"
else
  echo "ERROR: current path is neither a directory nor a symlink." >&2
  exit 1
fi
SWITCHED=1

echo "[8/9] Starting and checking services"
systemctl start "${SERVICES[@]}"
for service in "${SERVICES[@]}"; do
  systemctl is-active --quiet "$service"
done

echo "[9/9] Verifying application health"
health_json="$(curl --retry 8 --retry-delay 2 --retry-connrefused -fsS \
  -H 'Host: srun.ir' http://127.0.0.1:18832/instradar/health/)"
printf '%s' "$health_json" | "$VENV/bin/python" -c '
import json, sys
data = json.load(sys.stdin)
expected = sys.argv[1]
assert data.get("version") == expected, data
assert all(data.get(key) is True for key in ("database", "browser", "worker", "telegram")), data
' "$EXPECTED_VERSION"

SWITCHED=0
trap - ERR
printf '\n%s installed successfully.\nRelease: %s\nBackup: %s\n' \
  "$EXPECTED_VERSION" "$NEW" "$BACKUP"
