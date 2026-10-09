import os
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from radar.crypto import seal, unseal
from radar.models import CrawlJob, InstagramAccount, ProfileSnapshot
from radar.parsers import parse_profile, completeness_score
from radar.tasks import recover_jobs, _record_worker_heartbeat


class StageOneTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin-test", password="strong-admin-password")
        self.account = InstagramAccount.objects.create(owner=self.user, username="test.account")

    def test_session_encryption_roundtrip(self):
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.assertEqual(unseal(seal([{"name": "sessionid", "value": "secret"}])),
                             [{"name": "sessionid", "value": "secret"}])

    def test_parser_and_scoring(self):
        from types import SimpleNamespace
        profile = parse_profile(SimpleNamespace(pk=123, username="TEST", full_name="Test User",
                            is_private=False, is_verified=True, follower_count=10,
                            following_count=4, media_count=0, clips_count=0))
        self.assertEqual(profile["username"], "test")
        self.assertEqual(completeness_score(profile), 1)

    def test_old_running_job_requeued_from_checkpoint(self):
        job = CrawlJob.objects.create(owner=self.user, account=self.account, target="target",
            status=CrawlJob.Status.RUNNING, attempts=1, cursor={"last_pk": "42"},
            lease_until=timezone.now() - timedelta(seconds=5))
        with patch("radar.tasks.enqueue") as enqueue:
            recover_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, CrawlJob.Status.QUEUED)
        self.assertEqual(job.cursor["last_pk"], "42")
        enqueue.assert_called_once()

    def test_duplicate_message_cannot_claim_running_job(self):
        from radar.tasks import crawl_profile
        job = CrawlJob.objects.create(owner=self.user, account=self.account, target="target",
                                      status=CrawlJob.Status.RUNNING)
        self.assertIsNone(crawl_profile(str(job.pk)))

    def test_login_required_and_ownership(self):
        response = self.client.get(reverse("radar:accounts"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.user)
        for endpoint in ("radar:dashboard", "radar:accounts", "radar:jobs"):
            self.assertEqual(self.client.get(reverse(endpoint)).status_code, 200)
        other = get_user_model().objects.create_user("other", password="other-secret-password")
        other_account = InstagramAccount.objects.create(owner=other, username="private")
        response = self.client.post(reverse("radar:capture", args=[other_account.pk]))
        self.assertEqual(response.status_code, 404)

    def test_health_reports_unavailable_dependencies(self):
        with patch("radar.views.browser.call", side_effect=ConnectionRefusedError):
            response = self.client.get(reverse("radar:health"))
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["database"])
        self.assertFalse(response.json()["browser"])

    @patch("redis.Redis.from_url")
    def test_worker_process_heartbeat_refreshes_liveness_key(self, from_url):
        _record_worker_heartbeat()
        from_url.return_value.set.assert_called_once_with(
            "instradar:worker:heartbeat", "1", ex=90)

    def test_login_ignores_another_sites_sessionid(self):
        self.assertEqual(settings.SESSION_COOKIE_NAME, "instradar_session")
        self.assertEqual(settings.CSRF_COOKIE_NAME, "instradar_csrf")
        response = self.client.post(reverse("login"), {
            "username": "admin-test", "password": "strong-admin-password"}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertIn("instradar_session", self.client.cookies)
        # Legacy Django sites may set a conflicting default cookie on /.
        self.client.cookies["sessionid"] = "older-application-session"
        response = self.client.get(reverse("radar:dashboard"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_async_aiograpi_login_and_profile(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from radar.tasks import _fetch_profile
        client = MagicMock()
        client.username = "test.account"
        client.login_by_sessionid = AsyncMock(return_value=True)
        client.user_info_by_username = AsyncMock(return_value=SimpleNamespace(
            pk="123", username="target", follower_count=7, following_count=5, media_count=2))
        client.get_settings.return_value = {"device_settings": {"a": "b"}}
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.account.encrypted_cookies = seal([{
                "name": "sessionid", "value": "123_abc", "domain": ".instagram.com",
            }])
            with patch("aiograpi.Client", return_value=client), \
                    patch("radar.tasks.asyncio.sleep", new_callable=AsyncMock):
                profile, config = asyncio.run(_fetch_profile(self.account, "target"))
        self.assertEqual(profile["followers_count"], 7)
        self.assertEqual(profile["posts_count"], 2)
        self.assertIsNone(profile["reels_count"])
        self.assertIn("device_settings", config)
        client.login_by_sessionid.assert_awaited_once()
        client.user_info_by_username.assert_awaited_once_with("target")

    def test_profile_crawl_persists_complete_profile_snapshot(self):
        import asyncio
        from unittest.mock import AsyncMock
        job = CrawlJob.objects.create(owner=self.user, account=self.account,
                                      target="target", status=CrawlJob.Status.QUEUED)
        profile = {
            "instagram_pk": "123", "username": "target", "full_name": "Target User",
            "is_private": True, "is_verified": True, "followers_count": 1038589,
            "following_count": 93, "posts_count": 1245, "reels_count": 312,
            "media_count": 1245,
        }
        with patch("radar.tasks._fetch_profile", new=AsyncMock(return_value=(profile, {"device_settings": {}}))), \
                patch("radar.tasks.seal", return_value=b"settings"):
            from radar.tasks import crawl_profile
            crawl_profile(str(job.pk))
        snapshot = ProfileSnapshot.objects.get(job=job)
        self.assertEqual(snapshot.username, "target")
        self.assertEqual(snapshot.instagram_pk, "123")
        self.assertEqual(snapshot.full_name, "Target User")
        self.assertTrue(snapshot.is_private)
        self.assertTrue(snapshot.is_verified)
        self.assertEqual((snapshot.followers_count, snapshot.following_count), (1038589, 93))
        self.assertEqual((snapshot.posts_count, snapshot.reels_count), (1245, 312))

    def test_aiograpi_bridge_strips_old_authorization_and_reuses_device(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from radar.session import aiograpi_client
        client = MagicMock()
        client.username = "test.account"
        client.login_by_sessionid = AsyncMock(return_value=True)
        old = {
            "authorization_data": {"sessionid": "999_old", "ds_user_id": "999"},
            "cookies": {"sessionid": "999_old"},
            "ig_u_rur": "old-session-claim",
            "device_settings": {"model": "stable-device"},
            "uuids": {"uuid": "stable-uuid"},
            "locale": "en_US",
        }
        browser_session = "1234567890_" + "x" * 40
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.account.instagram_pk = "1234567890"
            self.account.encrypted_aiograpi_settings = seal(old)
            self.account.encrypted_cookies = seal([
                {"name": "sessionid", "value": browser_session, "domain": ".instagram.com"},
                {"name": "ds_user_id", "value": "1234567890", "domain": ".instagram.com"},
                {"name": "csrftoken", "value": "csrf", "domain": ".instagram.com"},
            ])
            with patch("aiograpi.Client", return_value=client) as client_factory:
                result = asyncio.run(aiograpi_client(self.account))
        self.assertIs(result, client)
        client_factory.assert_called_once_with(private_transport="requests")
        loaded = client.set_settings.call_args.args[0]
        self.assertEqual(loaded["device_settings"], {"model": "stable-device"})
        self.assertEqual(loaded["private_transport"], "requests")
        self.assertNotIn("authorization_data", loaded)
        self.assertNotIn("cookies", loaded)
        self.assertNotIn("ig_u_rur", loaded)
        client.login_by_sessionid.assert_awaited_once_with(browser_session)
        client.public.set_cookies.assert_called_once()
        client.graphql.set_cookies.assert_called_once()
        self.assertTrue(client._instradar_private_ready)

    def test_mobile_import_error_keeps_authenticated_web_session(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from radar.session import aiograpi_client
        client = MagicMock(username="")
        client.login_by_sessionid = AsyncMock(side_effect=RuntimeError("secret upstream body"))
        browser_session = "1234567890_" + "x" * 40
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.account.instagram_pk = "1234567890"
            self.account.encrypted_cookies = seal([
                {"name": "sessionid", "value": browser_session, "domain": ".instagram.com"},
                {"name": "ds_user_id", "value": "1234567890", "domain": ".instagram.com"},
                {"name": "csrftoken", "value": "csrf", "domain": ".instagram.com"},
            ])
            with patch("aiograpi.Client", return_value=client):
                result = asyncio.run(aiograpi_client(self.account))
        self.assertIs(result, client)
        self.assertFalse(result._instradar_private_ready)
        self.assertEqual(result._instradar_session_mode, "browser-web-only")
        self.assertEqual(result._instradar_session_error, "RuntimeError")
        self.assertNotIn("secret", result._instradar_session_error)
        self.assertEqual(result.username, "test.account")
        client.public.set_cookies.assert_called_once()
        client.graphql.set_cookies.assert_called_once()

    def test_rejected_session_is_permanent(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from radar.session import InstagramSessionError, aiograpi_client
        client = MagicMock(username="test.account")
        client.login_by_sessionid = AsyncMock(return_value=False)
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.account.instagram_pk = "1234567890"
            self.account.encrypted_cookies = seal([
                {"name": "sessionid", "value": "1234567890_expired", "domain": ".instagram.com"},
                {"name": "ds_user_id", "value": "1234567890", "domain": ".instagram.com"},
            ])
            with patch("aiograpi.Client", return_value=client), self.assertRaises(InstagramSessionError) as caught:
                asyncio.run(aiograpi_client(self.account))
        self.assertTrue(caught.exception.permanent)

    def test_recapture_discards_cached_aiograpi_session(self):
        self.client.force_login(self.user)
        self.account.encrypted_aiograpi_settings = b"old-private-session"
        self.account.verified_at = timezone.now()
        self.account.save(update_fields=["encrypted_aiograpi_settings", "verified_at"])
        browser_session = "1234567890_" + "x" * 40
        cookies = [
            {"name": "sessionid", "value": browser_session, "domain": ".instagram.com"},
            {"name": "ds_user_id", "value": "1234567890", "domain": ".instagram.com"},
            {"name": "csrftoken", "value": "csrf", "domain": ".instagram.com"},
        ]
        from unittest.mock import AsyncMock, MagicMock
        client = MagicMock(username="test.account")
        client.get_settings.return_value = {"user_agent": "Chrome"}
        with patch("radar.views.browser.call", return_value={"ok": True, "cookies": cookies,
                                                               "user_agent": "Chrome"}), \
                patch("radar.views.seal", return_value=b"new-browser-session"), \
                patch("radar.views.aiograpi_client", new=AsyncMock(return_value=client)):
            response = self.client.post(reverse("radar:capture", args=[self.account.pk]))
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.encrypted_aiograpi_settings)
        self.assertIsNotNone(self.account.verified_at)
        self.assertEqual(self.account.status, "active")
        self.assertEqual(self.account.browser_user_agent, "Chrome")

    def test_gallery_cookiefile_uses_same_encrypted_session(self):
        from radar.session import gallery_cookiefile
        with patch.dict(os.environ, {"INSTRADAR_FERNET_KEY": "dPuJ3zYpVzrfHiPOFU-QDpQQ0MfKnDvjeZeY3WGy50M="}):
            self.account.encrypted_cookies = seal([{"name": "sessionid", "value": "123_secret", "domain": ".instagram.com",
                                                     "path": "/", "secure": True, "expires": 1893456000}])
            with gallery_cookiefile(self.account) as path:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn("123_secret", path.read_text())
            self.assertFalse(path.exists())
