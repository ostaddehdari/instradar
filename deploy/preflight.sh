#!/usr/bin/env bash
set -euo pipefail
printf 'Target host / Nginx:\n'; hostname -f || hostname
nginx -t
printf '\nExact old/new services:\n'
for svc in instradar-web instradar-worker instradar-beat instradar-browser instaradar instaradar instagramradar instradar aiograpi-web aiograpi-worker instaradar-display; do
  systemctl show "${svc}.service" -p FragmentPath -p WorkingDirectory -p ExecStart -p ActiveState --no-pager 2>/dev/null || true
done
printf '\nNginx routes (read-only; review owning server block before changing):\n'
nginx -T 2>&1 | grep -En 'server_name|location.*(instradar|instaradar|instagramradar)|include.*instradar' || true
printf '\nExisting paths (read-only):\n'
for p in /opt/instagram-radar/app /opt/instagram-radar/shared /opt/instagramradar /opt/instradar /opt/instradar-py; do
  if [ -e "$p" ]; then ls -ld "$p"; fi
done
printf '\nnoVNC listener and display:\n'
ss -ltnp | grep -E ':6080|:18832' || true
ls -ld /tmp/.X11-unix/X99 2>/dev/null || true
