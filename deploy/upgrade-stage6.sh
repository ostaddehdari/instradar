#!/usr/bin/env bash
# Safe upgrade of an existing InstaRadar Python installation to Stage 6 / 3.6.0.
set -Eeuo pipefail

test "$(id -u)" -eq 0 || { echo 'ERROR: run this installer as root'; exit 1; }
source_root="$(cd "$(dirname "$0")/.." && pwd -P)"
current=/opt/instradar-py/current
shared=/opt/instradar-py/shared
venv=/opt/instradar-py/venv
test "$source_root" != "$current" || { echo 'ERROR: extract the ZIP under /root and run it from there'; exit 1; }
test "$(tr -d '[:space:]' < "$source_root/VERSION")" = 3.6.0 || { echo 'ERROR: this is not the 3.6.0 package'; exit 1; }
test -s "$shared/app.env" || { echo 'ERROR: existing app.env was not found'; exit 1; }
test -x "$venv/bin/python" || { echo 'ERROR: existing Python virtualenv was not found'; exit 1; }
test -d "$current" || { echo 'ERROR: current application directory was not found'; exit 1; }
test "$(systemctl show instradar-web.service -p WorkingDirectory --value)" = "$current" || {
  echo 'ERROR: instradar-web points to an unexpected directory'; exit 1;
}
for command in rsync pg_dump nginx curl; do
  command -v "$command" >/dev/null || { echo "ERROR: missing command: $command"; exit 1; }
done
free_kb="$(df -Pk /opt | awk 'NR==2 {print $4}')"
test "${free_kb:-0}" -ge 2097152 || { echo 'ERROR: at least 2 GiB free disk space is required'; exit 1; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="/opt/instradar-py/backups/$stamp-before-v3.6.0"
install -d -m 0750 -o instradar -g instradar "$backup_root"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='staticfiles' \
  -C "$current" -czf "$backup_root/application.tar.gz" .

set -a
source "$shared/app.env"
set +a
runuser -u instradar -- env \
  PGPASSWORD="${PGPASSWORD:-}" PGHOST="${PGHOST:-127.0.0.1}" PGPORT="${PGPORT:-5432}" \
  PGUSER="${PGUSER:-instradar_py}" PGDATABASE="${PGDATABASE:-instradar_py}" \
  pg_dump --format=custom --file="$backup_root/database.dump"
chmod 0600 "$backup_root/database.dump"

"$venv/bin/pip" install --disable-pip-version-check -r "$source_root/requirements.txt"
"$venv/bin/python" -m gallery_dl --version >/dev/null
restart_on_exit=1
cleanup() {
  if [ "$restart_on_exit" -eq 1 ]; then
    systemctl restart instradar-web instradar-worker instradar-beat instradar-browser >/dev/null 2>&1 || true
    systemctl restart instradar-download >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
systemctl stop instradar-web instradar-worker instradar-beat instradar-browser
systemctl stop instradar-download >/dev/null 2>&1 || true
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.before-*' \
  "$source_root/" "$current/"
chown -R root:instradar "$current"
find "$current" -type d -exec chmod 0755 {} +
find "$current" -type f -exec chmod 0644 {} +
chmod 0755 "$current/manage.py" "$current/deploy/"*.sh

install -d -m 0700 -o instradar -g instradar \
  "$shared/browser-home" "$shared/browser-home/.config" "$shared/browser-home/.cache" \
  "$shared/browser-home/.local" "$shared/browser-home/.local/share" \
  "$shared/browser-profiles" "$shared/runtime"
install -d -m 0750 -o instradar -g instradar "$shared/storage"
grep -q '^INSTRADAR_STORAGE_ROOT=' "$shared/app.env" || \
  printf 'INSTRADAR_STORAGE_ROOT=/opt/instradar-py/shared/storage\n' >> "$shared/app.env"
grep -q '^INSTRADAR_DOWNLOAD_RUNTIME=' "$shared/app.env" || \
  printf 'INSTRADAR_DOWNLOAD_RUNTIME=/opt/instradar-py/shared/runtime\n' >> "$shared/app.env"
for service in instradar-web instradar-worker instradar-beat instradar-browser instradar-download; do
  install -m 0644 "$current/deploy/${service}.service" "/etc/systemd/system/${service}.service"
done
if getent group systemd-journal >/dev/null; then usermod -aG systemd-journal instradar; fi

set -a
source "$shared/app.env"
set +a
runuser -u instradar -- "$venv/bin/python" "$current/manage.py" check
runuser -u instradar -- "$venv/bin/python" "$current/manage.py" migrate --noinput
runuser -u instradar -- "$venv/bin/python" "$current/manage.py" collectstatic --noinput
systemctl daemon-reload
systemctl restart instradar-web instradar-worker instradar-beat instradar-browser instradar-download
systemctl enable instradar-download >/dev/null
for service in instradar-web instradar-worker instradar-beat instradar-browser instradar-download instaradar-display; do
  systemctl is-active --quiet "$service" || { echo "ERROR: $service is not active"; exit 1; }
done
nginx -t
systemctl reload nginx

health_ok=0
for _ in $(seq 1 45); do
  if curl -fsS -H 'Host: srun.ir' http://127.0.0.1:18832/instradar/health/ 2>/dev/null | \
      "$venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); assert d["version"]=="3.6.0" and all(d[k] for k in ("database","browser","worker","downloader"))' 2>/dev/null; then
    health_ok=1
    break
  fi
  sleep 1
done
test "$health_ok" -eq 1 || { echo 'ERROR: health check did not become green; inspect service journals'; exit 1; }
restart_on_exit=0
trap - EXIT
echo "OK: InstaRadar 3.6.0 is active. Backup: $backup_root"
