#!/usr/bin/env bash
# Safe v3.1.0 -> v3.1.1 repair: retain existing PostgreSQL data, encryption keys, noVNC, and Nginx.
set -euo pipefail
test "$(id -u)" -eq 0 || { echo 'Run as root'; exit 1; }
test "$(pwd -P)" = /opt/instradar-py/current || { echo 'Run from /opt/instradar-py/current'; exit 1; }
test -s /opt/instradar-py/shared/app.env || { echo 'Existing app.env not found; use fresh install instructions instead'; exit 1; }
test -x /opt/instradar-py/venv/bin/gunicorn && test -x /opt/instradar-py/venv/bin/celery
for service in instradar-web instradar-worker instradar-beat; do
    unit="/etc/systemd/system/${service}.service"
    test -f "$unit" || { echo "Missing unit: $unit"; exit 1; }
    grep -Fxq 'WorkingDirectory=/opt/instradar-py/current' "$unit" || {
        echo "Unexpected unit working directory: $unit"; exit 1;
    }
    grep -Fxq 'PrivateTmp=yes' "deploy/${service}.service" || {
        echo "Corrected service file missing PrivateTmp=yes: $service"; exit 1;
    }
done
for service in instradar-web instradar-worker instradar-beat; do
    install -m 644 "deploy/${service}.service" "/etc/systemd/system/${service}.service"
done
systemctl daemon-reload
systemctl restart instradar-web instradar-worker instradar-beat
for service in instradar-web instradar-worker instradar-beat; do
    systemctl show "$service" -p PrivateTmp -p ActiveState --no-pager
done
echo 'Only three Python units repaired. Database, app.env, browser, old sites and Nginx untouched.'
