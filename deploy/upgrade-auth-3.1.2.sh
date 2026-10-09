#!/usr/bin/env bash
# Run after extracting the v3.1.2 release over /opt/instradar-py/current.
set -euo pipefail
test "$(id -u)" -eq 0 || { echo 'Run as root'; exit 1; }
test "$(pwd -P)" = /opt/instradar-py/current || { echo 'Run from /opt/instradar-py/current'; exit 1; }
test -s /opt/instradar-py/shared/app.env || { echo 'Missing existing app.env; refusing upgrade'; exit 1; }
test "$(systemctl show instradar-web -p WorkingDirectory --value)" = /opt/instradar-py/current || {
    echo 'Unexpected Django unit location'; exit 1;
}
grep -Fxq 'SESSION_COOKIE_NAME = "instradar_session"' config/settings.py || {
    echo 'v3.1.2 files were not extracted'; exit 1;
}
grep -Fq '"version": "3.1.2"' radar/views.py || {
    echo 'v3.1.2 health code was not extracted'; exit 1;
}
set -a
source /opt/instradar-py/shared/app.env
set +a
runuser -u instradar -- /opt/instradar-py/venv/bin/python manage.py check
systemctl restart instradar-web
systemctl is-active --quiet instradar-web || { echo 'Django web did not start'; exit 1; }
curl -fsS -H 'Host: srun.ir' http://127.0.0.1:18832/instradar/health/ |
    python3 -c 'import json,sys; assert json.load(sys.stdin)["version"] == "3.1.2"'
echo 'Django 3.1.2 active. app.env, PostgreSQL, browser, Celery and Nginx untouched.'
