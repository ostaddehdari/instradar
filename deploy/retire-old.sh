#!/usr/bin/env bash
# Only run AFTER manually removing the old two Nginx location blocks.
# Defaults to inspection; permanently deletes exact old service working directories when confirmed.
set -euo pipefail
test "$(id -u)" -eq 0
node_dir=/opt/instagram-radar/app
node_service=instaradar.service
printf 'Old Node service: '; systemctl show "$node_service" -p WorkingDirectory -p ExecStart --no-pager
printf 'Optional old Django service: %s\n' "${OLD_DJANGO_SERVICE:-not identified}"
printf 'Optional old Django directory: %s\n' "${OLD_DJANGO_DIR:-not identified}"
printf 'Display/noVNC services will NOT be removed.\n'
if [ "${CONFIRM_OLD_SITES_PURGE:-}" != "PURGE_EXACT_TWO_SITES" ]; then
  echo 'Dry-run only. Set CONFIRM_OLD_SITES_PURGE=PURGE_EXACT_TWO_SITES after verifying preflight.'
  exit 0
fi
# Never take old systems down unless the new public route is healthy.
curl -fsS --resolve srun.ir:443:127.0.0.1 https://srun.ir/instradar/health/ |
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["version"]=="3.2.0" and all(d[k] for k in ("database","browser","worker"))'
test "$(systemctl show "$node_service" -p WorkingDirectory --value)" = "$node_dir" || {
  echo 'Unexpected Node working directory; refusing purge'; exit 1;
}
systemctl show "$node_service" -p ExecStart --value | grep -Fq '/usr/bin/node server.js' || {
  echo 'Unexpected Node ExecStart; refusing purge'; exit 1;
}
test -d "$node_dir"
if [ -z "${OLD_DJANGO_SERVICE:-}" ] || [ -z "${OLD_DJANGO_DIR:-}" ]; then
  echo 'Identify exact second old service and directory from preflight before deleting either one'; exit 1
fi
case "$OLD_DJANGO_SERVICE" in
  instagramradar.service|instagramradar-web.service) ;;
  *) echo 'Unexpected second service name; inspect server manually'; exit 1;;
esac
case "$OLD_DJANGO_DIR" in
  /opt/instagramradar|/opt/instagram-radar-django) ;;
  *) echo 'Unexpected second directory; inspect server manually'; exit 1;;
esac
test -d "$OLD_DJANGO_DIR" && test "$OLD_DJANGO_DIR" != "$node_dir"
test "$(systemctl show "$OLD_DJANGO_SERVICE" -p WorkingDirectory --value)" = "$OLD_DJANGO_DIR" || {
  echo 'Second service directory mismatch'; exit 1;
}
# Check that existing display/noVNC service does not still execute files from either old tree.
if systemctl cat instaradar-display.service 2>/dev/null | grep -E "${node_dir}|${OLD_DJANGO_DIR}"; then
  echo 'Existing noVNC/display depends on an old directory; migrate it first'; exit 1
fi
# Nginx -T includes source configs; ensure the old routes have already been removed from srun.ir.
if nginx -T 2>&1 | grep -Eq 'location[^#]*(/instagramradar/|/instaradar/)'; then
  echo 'Old Nginx locations still present. Remove exactly those two routes first'; exit 1
fi
systemctl disable --now "$node_service" "$OLD_DJANGO_SERVICE"
# Preserve the shared display / noVNC services and their dependencies.
rm -r -- "$node_dir" "$OLD_DJANGO_DIR"
echo 'Two exact old application directories were permanently deleted; noVNC/display were retained.'
