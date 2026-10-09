#!/usr/bin/env bash
# Run only on an inspected server, from /opt/instradar-py/current.
set -euo pipefail
test "$(id -u)" -eq 0 || { echo 'Run as root'; exit 1; }
test "$(pwd -P)" = /opt/instradar-py/current || { echo 'Expected /opt/instradar-py/current'; exit 1; }
test -f manage.py && test -f deploy/instradar-web.service
test ! -e /opt/instradar-py/shared/app.env || { echo 'Fresh install only: app.env already exists'; exit 1; }
command -v psql >/dev/null
command -v redis-server >/dev/null
command -v nginx >/dev/null
command -v python3 >/dev/null
if [ -x /usr/bin/google-chrome ]; then chrome=/usr/bin/google-chrome
elif [ -x /usr/bin/chromium ]; then chrome=/usr/bin/chromium
elif [ -x /snap/bin/chromium ]; then chrome=/snap/bin/chromium
else echo 'Install a supported Chrome/Chromium first'; exit 1; fi
test -S /tmp/.X11-unix/X99 || { echo 'Existing DISPLAY=:99 is not running'; exit 1; }
id instradar >/dev/null 2>&1 || useradd --system --home /opt/instradar-py --shell /usr/sbin/nologin instradar
install -d -o instradar -g instradar -m 700 /opt/instradar-py/shared
install -d -o instradar -g instradar -m 700 /opt/instradar-py/shared/runtime
install -d -o instradar -g instradar -m 750 /opt/instradar-py/shared/storage
install -d -o instradar -g instradar -m 755 /opt/instradar-py/public/static
python3 -m venv /opt/instradar-py/venv
/opt/instradar-py/venv/bin/pip install -r requirements.txt
pgpassword="$(openssl rand -hex 32)"
# First installation of a dedicated, fresh database; never drop an existing database.
if runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='instradar_py'" | grep -q 1; then
  echo 'Database role already exists; refusing to overwrite existing credentials'; exit 1
fi
if runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='instradar_py'" | grep -q 1; then
  echo 'Database already exists; refusing destructive installation'; exit 1
fi
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE instradar_py LOGIN PASSWORD '${pgpassword}'"
runuser -u postgres -- createdb -O instradar_py instradar_py
django_secret="$(openssl rand -hex 48)"
browser_secret="$(openssl rand -hex 48)"
fernet_key="$(/opt/instradar-py/venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
install -m 600 -o instradar -g instradar /dev/null /opt/instradar-py/shared/app.env
{
  printf 'DJANGO_SECRET_KEY=%s\n' "$django_secret"
  printf 'PGDATABASE=instradar_py\nPGUSER=instradar_py\nPGPASSWORD=%s\nPGHOST=127.0.0.1\n' "$pgpassword"
  printf 'INSTRADAR_FERNET_KEY=%s\nBROWSER_AUTH_KEY=%s\n' "$fernet_key" "$browser_secret"
  printf 'INSTRADAR_SHARED=/opt/instradar-py/shared\nINSTRADAR_BROWSER_SOCKET=/opt/instradar-py/shared/browser.sock\n'
  printf 'INSTRADAR_STORAGE_ROOT=/opt/instradar-py/shared/storage\nINSTRADAR_DOWNLOAD_RUNTIME=/opt/instradar-py/shared/runtime\n'
  printf 'STATIC_ROOT=/opt/instradar-py/public/static\nCHROME_PATH=%s\n' "$chrome"
  printf 'ALLOWED_HOSTS=srun.ir,127.0.0.1,localhost\nREDIS_URL=redis://127.0.0.1:6379/6\n'
} >> /opt/instradar-py/shared/app.env
set -a
# Generated file contains only fixed names and hex / URL-safe generated values.
source /opt/instradar-py/shared/app.env
set +a
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py migrate --noinput
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py collectstatic --noinput >/dev/null
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py check --deploy
for service in instradar-web instradar-worker instradar-beat instradar-browser instradar-telegram; do
  install -m 644 "deploy/${service}.service" "/etc/systemd/system/${service}.service"
done
systemctl daemon-reload
systemctl enable --now redis-server instradar-web instradar-worker instradar-beat instradar-browser instradar-telegram
echo 'App started on 127.0.0.1:18832. Create admin next; configure Nginx ONLY after nginx -t.'
