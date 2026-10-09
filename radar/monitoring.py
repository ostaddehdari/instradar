"""Linux host metrics and project-scoped journal capture for InstaRadar."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (CommentSyncEvent, CrawlMetric, JobLogCapture,
                     MediaSyncEvent, PublishAttempt, RelationshipScanEvent, SystemMetric)

PROJECT_UNITS = (
    "instradar-web.service",
    "instradar-worker.service",
    "instradar-telegram.service",
    "instradar-beat.service",
    "instradar-browser.service",
    "instaradar-display.service",
)
SECRET_PATTERNS = (
    re.compile(r"(?im)^(\s*(?:authorization|cookie)\s*[:=]\s*).*$"),
    re.compile(r"(?i)((?:sessionid|csrftoken|password|passwd|api[_-]?hash)\s*[=:]\s*)[^\s;,]+"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
)


def _cpu_sample():
    def read():
        with open("/proc/stat", encoding="ascii") as source:
            fields = [int(x) for x in source.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    total1, idle1 = read()
    time.sleep(0.10)
    total2, idle2 = read()
    delta = max(1, total2 - total1)
    return round(max(0, min(100, 100 * (1 - (idle2 - idle1) / delta))), 2)


def _memory():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return used, total, round(100 * used / total, 2) if total else 0


def _network():
    rx = tx = 0
    with open("/proc/net/dev", encoding="ascii") as source:
        for line in list(source)[2:]:
            interface, values = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            parts = values.split()
            rx += int(parts[0])
            tx += int(parts[8])
    return rx, tx


def collect_metric():
    used, total, memory_percent = _memory()
    rx, tx = _network()
    disk = shutil.disk_usage(settings.INSTRADAR_SHARED)
    return SystemMetric.objects.create(
        cpu_percent=_cpu_sample(), memory_percent=memory_percent,
        memory_used_bytes=used, memory_total_bytes=total,
        disk_percent=round(100 * disk.used / disk.total, 2),
        network_rx_bytes=rx, network_tx_bytes=tx,
    )


def metric_payload(limit=80):
    points = list(SystemMetric.objects.order_by("-captured_at")[:limit])
    if not points:
        points = [collect_metric()]
    current = points[0]
    rx_rate = tx_rate = 0
    if len(points) > 1:
        elapsed = max(1, (current.captured_at - points[1].captured_at).total_seconds())
        rx_rate = max(0, (current.network_rx_bytes - points[1].network_rx_bytes) / elapsed)
        tx_rate = max(0, (current.network_tx_bytes - points[1].network_tx_bytes) / elapsed)
    return current, rx_rate, tx_rate, list(reversed(points))


def _sanitize(text):
    safe = text
    for pattern in SECRET_PATTERNS:
        safe = pattern.sub(lambda match: match.group(1) + "[REDACTED]", safe)
    max_bytes = settings.INSTRADAR_LOG_MAX_BYTES
    encoded = safe.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        safe = "[ابتدای لاگ به علت سقف نگهداری حذف شد]\n" + encoded[-max_bytes:].decode("utf-8", errors="replace")
    return safe


def _events(capture):
    start = capture.started_at - timedelta(seconds=5)
    end = capture.ends_at + timedelta(seconds=5)
    if capture.job_kind == JobLogCapture.Kind.OBSERVATION:
        rows = []
        for event in MediaSyncEvent.objects.filter(
                job__observation_id=capture.job_id, created_at__range=(start, end)):
            rows.append({"time": event.created_at.isoformat(), "stage": f"media/{event.stream}",
                         "records": event.records, "duration_ms": event.duration_ms,
                         "message": event.message})
        for event in RelationshipScanEvent.objects.filter(
                job__observation_id=capture.job_id, created_at__range=(start, end)):
            rows.append({"time": event.created_at.isoformat(), "stage": f"relations/{event.stream}",
                         "records": event.records, "duration_ms": event.duration_ms,
                         "message": event.message})
        for event in CommentSyncEvent.objects.filter(
                job__observation_id=capture.job_id, created_at__range=(start, end)):
            rows.append({"time": event.created_at.isoformat(), "stage": f"comments/{event.stage}",
                         "records": event.records, "duration_ms": event.duration_ms,
                         "message": event.message})
        return sorted(rows, key=lambda row: row["time"])
    if capture.job_kind == JobLogCapture.Kind.PROFILE:
        query = CrawlMetric.objects.filter(job_id=capture.job_id, created_at__range=(start, end))
        return [{"time": row.created_at.isoformat(), "stage": row.stage, "status": row.status,
                 "records": row.records, "duration_ms": row.duration_ms, "message": row.message}
                for row in query.order_by("created_at")]
    if capture.job_kind == JobLogCapture.Kind.MEDIA:
        query = MediaSyncEvent.objects.filter(job_id=capture.job_id, created_at__range=(start, end))
        return [{"time": row.created_at.isoformat(), "stage": row.stream, "records": row.records,
                 "duration_ms": row.duration_ms, "message": row.message} for row in query.order_by("created_at")]
    if capture.job_kind == JobLogCapture.Kind.COMMENT:
        query = CommentSyncEvent.objects.filter(job_id=capture.job_id, created_at__range=(start, end))
        return [{"time": row.created_at.isoformat(), "stage": row.stage, "records": row.records,
                "duration_ms": row.duration_ms, "message": row.message} for row in query.order_by("created_at")]
    if capture.job_kind == JobLogCapture.Kind.RELATIONSHIP:
        query = RelationshipScanEvent.objects.filter(job_id=capture.job_id, created_at__range=(start, end))
        return [{"time": row.created_at.isoformat(), "stage": row.stream, "records": row.records,
                 "duration_ms": row.duration_ms, "message": row.message} for row in query.order_by("created_at")]
    if capture.job_kind == JobLogCapture.Kind.TRANSFER:
        query = PublishAttempt.objects.filter(item__job_id=capture.job_id,
                                              created_at__range=(start, end)).select_related("item__post")
        return [{"time": row.created_at.isoformat(), "stage": "telegram",
                 "records": 1 if row.status in ("done", "skipped") else 0,
                 "duration_ms": row.duration_ms,
                 "message": f"{row.item.post.shortcode}: {row.message}"}
                for row in query.order_by("created_at")]
    return []


@transaction.atomic
def finish_capture(capture_id):
    capture = JobLogCapture.objects.select_for_update().get(pk=capture_id)
    if capture.status != JobLogCapture.Status.COLLECTING:
        return capture
    command = ["journalctl", "--no-pager", "--output=short-iso", "--since", capture.started_at.isoformat(),
               "--until", capture.ends_at.isoformat(), "--lines", str(settings.INSTRADAR_LOG_MAX_LINES)]
    for unit in PROJECT_UNITS:
        command.extend(("--unit", unit))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=25,
                                env={**os.environ, "SYSTEMD_COLORS": "0"}, check=False)
        text = _sanitize(result.stdout)
        error = _sanitize(result.stderr.strip())[:300]
        capture.journal_text = text
        capture.job_events = _events(capture)
        capture.line_count = len(text.splitlines())
        capture.status = JobLogCapture.Status.READY if result.returncode == 0 else JobLogCapture.Status.PARTIAL
        capture.error = error
    except Exception as exc:
        capture.status = JobLogCapture.Status.PARTIAL
        capture.error = f"{type(exc).__name__}: خواندن journal ممکن نبود"[:300]
        capture.job_events = _events(capture)
    capture.collected_at = timezone.now()
    capture.save()
    return capture
