import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from radar.crypto import seal
from radar.models import CrawlJob, CrawlMetric, InstagramAccount, JobLogCapture, SystemMetric
from radar.monitoring import finish_capture, metric_payload
from radar.session import gallery_cookiefile, instagram_cookies


FERNET_KEY = "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="


class MonitoringAndSessionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_superuser("monitor-admin", "admin@example.test", "test-password")
        self.operator = users.objects.create_user("monitor-operator", password="test-password")
        self.account = InstagramAccount.objects.create(owner=self.admin, username="session.account")
        self.job = CrawlJob.objects.create(owner=self.admin, account=self.account, target="target.account")

    def test_monitoring_and_journal_are_superuser_only(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("radar:monitoring")).status_code, 302)
        self.client.force_login(self.admin)
        with patch("radar.views.metric_payload") as payload:
            metric = SystemMetric.objects.create(cpu_percent=12, memory_percent=23)
            payload.return_value = (metric, 10, 20, [metric])
            self.assertEqual(self.client.get(reverse("radar:monitoring")).status_code, 200)

    def test_configurable_job_log_capture_is_owned_and_enqueued(self):
        self.client.force_login(self.admin)
        with patch("radar.views.finish_job_log_capture.delay") as delay:
            response = self.client.post(
                reverse("radar:capture_job_log", args=["profile", self.job.pk]),
                {"window_seconds": "75"},
            )
        self.assertEqual(response.status_code, 302)
        capture = JobLogCapture.objects.get()
        self.assertEqual(capture.window_seconds, 75)
        self.assertAlmostEqual((capture.ends_at - capture.started_at).total_seconds(), 75, delta=0.1)
        delay.assert_called_once_with(str(capture.pk))

    @override_settings(INSTRADAR_LOG_MAX_LINES=1000, INSTRADAR_LOG_MAX_BYTES=100000)
    def test_project_journal_is_redacted_and_structured_events_are_kept(self):
        end = timezone.now()
        capture = JobLogCapture.objects.create(
            owner=self.admin, job_kind="profile", job_id=str(self.job.pk),
            window_seconds=60, started_at=end - timedelta(seconds=60), ends_at=end,
        )
        CrawlMetric.objects.create(job=self.job, stage="profile", status="failed", message="safe event")
        output = "password=plain-secret\nCookie: sessionid=instagram-secret\nAuthorization=Bearer abc.def\n"
        with patch("radar.monitoring.subprocess.run", return_value=SimpleNamespace(
                stdout=output, stderr="", returncode=0)) as run:
            finish_capture(capture.pk)
        capture.refresh_from_db()
        self.assertEqual(capture.status, "ready")
        self.assertNotIn("plain-secret", capture.journal_text)
        self.assertNotIn("instagram-secret", capture.journal_text)
        self.assertNotIn("abc.def", capture.journal_text)
        self.assertIn("[REDACTED]", capture.journal_text)
        self.assertEqual(capture.job_events[0]["message"], "safe event")
        command = run.call_args.args[0]
        self.assertIn("instradar-worker.service", command)
        self.assertNotIn("--system", command)

    def test_metric_rates_use_two_latest_samples(self):
        then = timezone.now() - timedelta(seconds=10)
        now = timezone.now()
        SystemMetric.objects.create(captured_at=then, network_rx_bytes=1000, network_tx_bytes=2000)
        latest = SystemMetric.objects.create(captured_at=now, network_rx_bytes=2000, network_tx_bytes=5000)
        current, rx, tx, points = metric_payload()
        self.assertEqual(current.pk, latest.pk)
        self.assertAlmostEqual(rx, 100, delta=2)
        self.assertAlmostEqual(tx, 300, delta=2)
        self.assertEqual(len(points), 2)

    def test_one_browser_cookie_bundle_feeds_gallery_dl_and_rejects_fake_domains(self):
        cookies = [
            {"name": "sessionid", "value": "shared-secret", "domain": ".instagram.com",
             "path": "/", "secure": True, "expires": 1893456000},
            {"name": "csrftoken", "value": "csrf-value", "domain": "www.instagram.com",
             "path": "/", "secure": True, "expires": 1893456000},
            {"name": "sessionid", "value": "evil-secret", "domain": "evilinstagram.com",
             "path": "/", "secure": True, "expires": 1893456000},
        ]
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": FERNET_KEY}):
            self.account.encrypted_cookies = seal(cookies)
            self.assertEqual(len(instagram_cookies(self.account)), 2)
            with gallery_cookiefile(self.account) as cookie_path:
                content = cookie_path.read_text()
                self.assertEqual(cookie_path.stat().st_mode & 0o777, 0o600)
                self.assertIn("shared-secret", content)
                self.assertIn("csrf-value", content)
                self.assertNotIn("evil-secret", content)
            self.assertFalse(cookie_path.exists())

    def test_persian_account_status(self):
        self.account.status = "captured"
        self.assertEqual(self.account.status_fa, "نشست مرورگر دریافت شد")
        self.assertEqual(self.account.status_css, "info")
