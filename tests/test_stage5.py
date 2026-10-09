from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from radar.graph_engine import GraphOptions, build_graph
from radar.models import (AccountRelationship, Comment, GraphWorkspace,
                          InstagramAccount, InstagramIdentity, MediaSyncJob,
                          Post, PostSnapshot, RelationshipScanJob,
                          RelationshipSnapshot)


class GraphStageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("stage5", password="safe-stage5-password")
        self.other = get_user_model().objects.create_user("stage5-other", password="safe-stage5-password")
        self.account = InstagramAccount.objects.create(owner=self.user, username="runner")
        self.seed = self.identity(100, "seed")
        self.a = self.identity(200, "alpha")
        self.b = self.identity(300, "beta")
        self.c = self.identity(400, "gamma")
        self.commenter = self.identity(500, "commenter")
        self.scan = RelationshipScanJob.objects.create(
            owner=self.user, account=self.account, target="seed", target_pk="100",
            seed_identity=self.seed, status="done")
        self.relation(self.a, self.seed)
        self.relation(self.seed, self.a)
        self.relation(self.seed, self.b)
        self.relation(self.b, self.c, depth=2)
        self.post = Post.objects.create(
            media_pk="900", shortcode="GRAPH900", owner_pk="100", owner_username="seed",
            owner_identity=self.seed, discovered_from="seed")
        media_job = MediaSyncJob.objects.create(owner=self.user, account=self.account, target="seed")
        PostSnapshot.objects.create(job=media_job, post=self.post, caption_hash="")
        Comment.objects.create(
            post=self.post, instagram_pk="comment-1", author_pk="500",
            author_username="commenter", author_identity=self.commenter, text="first")
        Comment.objects.create(
            post=self.post, instagram_pk="comment-2", author_pk="500",
            author_username="commenter", author_identity=self.commenter, text="second")

    def identity(self, instagram_pk, username):
        return InstagramIdentity.objects.create(
            instagram_pk=str(instagram_pk), current_username=username)

    def relation(self, source, target, depth=1):
        relation = AccountRelationship.objects.create(
            source=source, target=target, depth=depth, discovered_from=self.seed,
            last_seen_job=self.scan)
        RelationshipSnapshot.objects.create(
            job=self.scan, relationship=relation, source=source, target=target)
        return relation

    def test_three_level_graph_direction_mutual_comment_path_and_rank(self):
        graph = build_graph(self.user, self.seed, GraphOptions(depth=3, node_limit=100))
        nodes = {node["username"]: node for node in graph["nodes"]}
        self.assertEqual(set(nodes), {"seed", "alpha", "beta", "gamma", "commenter"})
        self.assertEqual(nodes["gamma"]["depth"], 2)
        self.assertEqual(nodes["gamma"]["path"], ["seed", "beta", "gamma"])
        self.assertEqual(nodes["commenter"]["depth"], 1)
        self.assertEqual({edge["type"] for edge in graph["edges"]},
                         {"mutual", "following", "commenter"})
        comment_edge = next(edge for edge in graph["edges"] if edge["type"] == "commenter")
        self.assertEqual((comment_edge["source"], comment_edge["target"], comment_edge["weight"]),
                         (str(self.commenter.pk), str(self.seed.pk), 2))
        self.assertTrue(graph["influencers"])
        self.assertGreaterEqual(graph["stats"]["communities"], 1)

    def test_depth_and_relation_filters_are_enforced(self):
        graph = build_graph(self.user, self.seed, GraphOptions(
            depth=1, node_limit=100, followers=False, following=True,
            mutual=False, comments=False))
        self.assertEqual({node["username"] for node in graph["nodes"]}, {"seed", "beta"})
        self.assertEqual([edge["type"] for edge in graph["edges"]], ["following"])
        with self.assertRaises(ValueError):
            build_graph(self.user, self.seed, GraphOptions(
                followers=False, following=False, mutual=False, comments=False))

    def test_node_cap_is_safe_and_reports_progressive_limit(self):
        for number in range(1000, 1040):
            neighbor = self.identity(number, f"person{number}")
            self.relation(neighbor, self.seed)
        graph = build_graph(self.user, self.seed, GraphOptions(depth=1, node_limit=25))
        self.assertEqual(graph["stats"]["nodes"], 25)
        self.assertTrue(graph["stats"]["truncated"])
        self.assertEqual(graph["stats"]["next_limit"], 125)

    def test_workspace_pages_api_exports_and_owner_isolation(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("radar:graph_builder"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "گراف‌ساز سه‌سطحی")
        response = self.client.post(reverse("radar:save_graph_workspace"), {
            "name": "شبکه آزمایشی", "seed": self.seed.pk, "depth": "2", "limit": "75",
            "followers": "on", "following": "on", "mutual": "on", "comments": "on",
        })
        self.assertEqual(response.status_code, 302)
        workspace = GraphWorkspace.objects.get(owner=self.user)
        self.assertEqual((workspace.depth, workspace.node_limit), (2, 75))

        query = f"?seed={self.seed.pk}&depth=2&limit=75&followers=1&following=1&mutual=1&comments=1"
        data = self.client.get(reverse("radar:graph_data") + query)
        self.assertEqual(data.status_code, 200)
        self.assertEqual(data.json()["seed"], str(self.seed.pk))
        for file_format, content_type in (("json", "application/json"),
                                           ("csv", "text/csv"),
                                           ("graphml", "application/graphml+xml")):
            export = self.client.get(reverse("radar:graph_export", args=[file_format]) + query)
            self.assertEqual(export.status_code, 200)
            self.assertTrue(export["Content-Type"].startswith(content_type))
            self.assertIn("attachment", export["Content-Disposition"])

        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("radar:graph_data") + query).status_code, 404)
        self.assertEqual(self.client.get(
            reverse("radar:graph_builder") + f"?workspace={workspace.pk}").status_code, 404)
        self.assertEqual(self.client.post(
            reverse("radar:delete_graph_workspace", args=[workspace.pk])).status_code, 404)

    def test_workspace_rejects_missing_seed(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("radar:save_graph_workspace"), {
            "seed": "", "depth": "1", "limit": "100", "followers": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GraphWorkspace.objects.exists())
