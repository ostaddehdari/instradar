#!/usr/bin/env bash
# Upgrade an existing Stage 1 installation; leave PostgreSQL, env, noVNC, Nginx and other sites intact.
set -euo pipefail
test "$(id -u)" -eq 0 || { echo 'Run as root'; exit 1; }
test "$(pwd -P)" = /opt/instradar-py/current || { echo 'Wrong application directory'; exit 1; }
test -s /opt/instradar-py/shared/app.env || { echo 'Existing app.env is required'; exit 1; }
test -x /opt/instradar-py/venv/bin/python || { echo 'Existing Python venv not found'; exit 1; }
test "$(systemctl show instradar-web -p WorkingDirectory --value)" = /opt/instradar-py/current || {
  echo 'Unexpected Django service location'; exit 1;
}
grep -Fq '"version": "3.2.0"' radar/views.py || { echo 'Stage 2 files not extracted'; exit 1; }
set -a
source /opt/instradar-py/shared/app.env
set +a
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py check
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py migrate --noinput
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py collectstatic --noinput
systemctl restart instradar-web instradar-worker instradar-beat
systemctl is-active --quiet instradar-web
systemctl is-active --quiet instradar-worker
systemctl is-active --quiet instradar-beat
curl -fsS -H 'Host: srun.ir' http://127.0.0.1:18832/instradar/health/ |
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["version"]=="3.2.0" and d["database"]'
echo 'Stage 2 version 3.2.0 is running. No existing accounts or sessions were reset.'
