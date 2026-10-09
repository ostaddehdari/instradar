from pathlib import Path
from django.test import SimpleTestCase


class ServiceIsolationTests(SimpleTestCase):
    def test_readonly_services_have_private_writable_tmp(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy"
        for name in ("instradar-web", "instradar-worker", "instradar-beat",
                     "instradar-telegram"):
            content = (deploy / f"{name}.service").read_text()
            self.assertIn("ProtectSystem=strict", content)
            self.assertIn("PrivateTmp=yes", content)

    def test_browser_service_has_writable_home_for_chrome(self):
        unit = (Path(__file__).resolve().parents[1] / "deploy" / "instradar-browser.service").read_text()
        self.assertIn("Environment=HOME=/opt/instradar-py/shared/browser-home", unit)
        self.assertIn("Environment=XDG_CONFIG_HOME=/opt/instradar-py/shared/browser-home/.config", unit)
        self.assertIn("ReadWritePaths=/opt/instradar-py/shared", unit)

    def test_stage8_upgrade_preserves_shared_state_and_checks_health(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy"
        script = (deploy / "upgrade-stage9.sh").read_text()
        self.assertNotIn("--delete $shared", script)
        self.assertNotIn("dropdb", script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('d["version"]=="3.9.0"', script)

    def test_stage3_upgrade_preserves_shared_state_and_checks_health(self):
        script = (Path(__file__).resolve().parents[1] / "deploy" / "upgrade-stage3.sh").read_text()
        self.assertNotIn("--delete $shared", script)
        self.assertNotIn("dropdb", script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('d["version"]=="3.3.0"', script)

    def test_stage4_upgrade_preserves_shared_state_and_checks_health(self):
        script = (Path(__file__).resolve().parents[1] / "deploy" / "upgrade-stage4.sh").read_text()
        self.assertNotIn("--delete $shared", script)
        self.assertNotIn("dropdb", script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('d["version"]=="3.4.0"', script)

    def test_stage5_upgrade_preserves_shared_state_and_checks_health(self):
        script = (Path(__file__).resolve().parents[1] / "deploy" / "upgrade-stage5.sh").read_text()
        self.assertNotIn("--delete $shared", script)
        self.assertNotIn("dropdb", script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('d["version"]=="3.5.5"', script)

    def test_stage9_upgrade_retires_download_worker(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy"
        script = (deploy / "upgrade-stage9.sh").read_text()
        self.assertIn("disable instradar-download", script)
        self.assertFalse((deploy / "instradar-download.service").exists())

    def test_stage7_upgrade_and_telethon_worker_are_isolated_and_non_destructive(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy"
        script = (deploy / "upgrade-stage7.sh").read_text()
        unit = (deploy / "instradar-telegram.service").read_text()
        self.assertNotIn("--delete $shared", script)
        self.assertNotIn("dropdb", script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('d["version"]=="3.7.0"', script)
        self.assertIn("--queues=telegram", unit)
        self.assertIn("--concurrency=1", unit)
        self.assertIn("ReadWritePaths=/opt/instradar-py/shared", unit)
