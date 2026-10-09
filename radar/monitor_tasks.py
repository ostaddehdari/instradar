"""Periodic monitoring and delayed per-job log capture tasks."""
from datetime import timedelta
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from .models import JobLogCapture, SystemMetric
from .monitoring import collect_metric, finish_capture


@shared_task(name="radar.monitor_tasks.collect_system_metric")
def collect_system_metric():
    collect_metric()


@shared_task(name="radar.monitor_tasks.finish_job_log_capture")
def finish_job_log_capture(capture_id):
    finish_capture(capture_id)


@shared_task(name="radar.monitor_tasks.cleanup_monitoring")
def cleanup_monitoring():
    cutoff = timezone.now() - timedelta(days=settings.INSTRADAR_MONITOR_RETENTION_DAYS)
    SystemMetric.objects.filter(captured_at__lt=cutoff).delete()
    JobLogCapture.objects.filter(started_at__lt=cutoff).delete()
