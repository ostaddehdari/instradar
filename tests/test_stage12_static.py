import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StageTwelveStaticTests(unittest.TestCase):
    def test_stage12_migration_follows_profile_fields(self):
        source = (ROOT / "radar/migrations/0019_stage12_progress_and_indexes.py").read_text()
        self.assertIn('("radar", "0018_profile_extraction_fields")', source)

    def test_progress_module_has_every_reorderable_stream(self):
        source = (ROOT / "radar/observation_progress.py").read_text()
        tree = ast.parse(source)
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TASK_META"
                    for target in node.targets)
        )
        keys = set(ast.literal_eval(assignment.value))
        self.assertEqual(
            keys,
            {"posts", "reels", "followers", "following", "comments", "telegram"},
        )

    def test_release_installer_targets_current_version(self):
        script = (ROOT / "deploy/upgrade-stage13.sh").read_text()
        self.assertIn('EXPECTED_VERSION="3.13.0-account-pool-safety"', script)
        self.assertIn("pg_dump --format=custom", script)
        self.assertIn('pip install --disable-pip-version-check', script)
        self.assertIn("tests.test_stage12", script)
        self.assertIn("tests.test_stage13", script)
        self.assertIn("tests.test_stage4", script)

    def test_relationship_client_contains_upstream_follow_list_fix(self):
        requirements = (ROOT / "requirements.txt").read_text()
        relationship_tasks = (ROOT / "radar/relationship_tasks.py").read_text()
        self.assertIn("aiograpi==2.0.5", requirements)
        self.assertIn("socksio>=1.0,<2", requirements)
        self.assertIn("284797047911918316998205836755", relationship_tasks)
        self.assertIn("16104639286363954576550227636", relationship_tasks)

    def test_standalone_log_detail_is_absent(self):
        urls = (ROOT / "radar/urls.py").read_text()
        self.assertNotIn('path("logs/<uuid:pk>/"', urls)
        self.assertNotIn("job_log_detail", urls)
        self.assertFalse((ROOT / "templates/radar/job_log_detail.html").exists())


if __name__ == "__main__":
    unittest.main()
