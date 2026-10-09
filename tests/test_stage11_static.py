import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Stage11StaticTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(
            (ROOT / "VERSION").read_text().strip(),
            "3.13.0-account-pool-safety",
        )

    def test_required_stage11_models(self):
        source = (ROOT / "radar/models.py").read_text()
        tree = ast.parse(source)
        classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        required = {
            "CrawlSession",
            "CrawlCursor",
            "ExtractionProgress",
            "Contact",
            "FollowEdge",
            "FollowerSnapshot",
        }
        self.assertTrue(required <= classes, sorted(required - classes))

    def test_forward_model_references_are_lazy(self):
        source = (ROOT / "radar/models.py").read_text()
        self.assertIn('models.OneToOneField("ObservationJob"', source)
        self.assertIn('models.ForeignKey("CrawlSession"', source)
        self.assertIn('models.ForeignKey("FollowEdge"', source)

    def test_migration_is_after_stage10(self):
        source = (ROOT / "radar/migrations/0015_stage11_unified_resume.py").read_text()
        self.assertIn(
            '("radar", "0014_incremental_crawl_state")',
            source,
        )

    def test_shared_session_bridge_and_browser_fallback_present(self):
        session = (ROOT / "radar/session.py").read_text()
        rel = (ROOT / "radar/relationship_tasks.py").read_text()
        self.assertIn("login_by_sessionid", session)
        self.assertIn('browser.call, "relationships"', rel)

    def test_incremental_anchor_present(self):
        media = (ROOT / "radar/media_tasks.py").read_text()
        self.assertIn("previous_top_pk", media)
        self.assertIn("reached_anchor", media)

    def test_telegram_force_resend_present(self):
        models = (ROOT / "radar/models.py").read_text()
        tasks = (ROOT / "radar/publish_tasks.py").read_text()
        self.assertIn("force_resend", models)
        self.assertIn("not job.force_resend", tasks)


if __name__ == "__main__":
    unittest.main()
