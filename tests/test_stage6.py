from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import NoReverseMatch, reverse


class RetiredDownloaderTests(TestCase):
    """The former user-facing downloader is gone; Telegram keeps its private cache."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "stage9-ui", password="safe-password")
        self.client.force_login(self.user)

    def test_downloader_routes_and_worker_are_removed(self):
        for route in ("download_jobs", "create_download_job", "instant_download_post"):
            with self.assertRaises(NoReverseMatch):
                reverse("radar:" + route)
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "radar" / "download_tasks.py").exists())
        self.assertFalse((root / "deploy" / "instradar-download.service").exists())

    def test_responsive_shell_contains_right_sidebar_controls_and_ui_libraries(self):
        response = self.client.get(reverse("radar:dashboard"))
        self.assertContains(response, 'id="menu-toggle"')
        self.assertContains(response, 'id="sidebar-overlay"')
        self.assertContains(response, "bootstrap@5.3.3")
        self.assertContains(response, "font-awesome")
        self.assertContains(response, "animate.css")
        self.assertContains(response, "Vazirmatn")
        self.assertNotContains(response, "دانلودر")
