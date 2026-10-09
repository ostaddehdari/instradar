"""Owner-scoped, bounded Stage 5 graph construction without external graph services."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from django.db.models import Count, Exists, F, OuterRef, Q

from .models import (AccountRelationship, Comment, InstagramIdentity, Post,
                     RelationshipSnapshot)


@dataclass(frozen=True)
class GraphOptions:
    depth: int = 1
    node_limit: int = 150
    followers: bool = True
    following: bool = True
    mutual: bool = True
    comments: bool = True

    def normalized(self):
        return GraphOptions(
            depth=min(3, max(1, int(self.depth))),
            node_limit=min(2000, max(25, int(self.node_limit))),
            followers=bool(self.followers), following=bool(self.following),
            mutual=bool(self.mutual), comments=bool(self.comments),
        )


def visible_relationship_ids(owner):
    return RelationshipSnapshot.objects.filter(job__owner=owner).values_list(
        "relationship_id", flat=True)


def visible_identity_ids(owner):
    return RelationshipSnapshot.objects.filter(job__owner=owner).values_list(
        "source_id", flat=True).union(
        RelationshipSnapshot.objects.filter(job__owner=owner).values_list("target_id", flat=True),
        Comment.objects.filter(post__postsnapshot__job__owner=owner,
                               author_identity__isnull=False).values_list("author_identity_id", flat=True),
        Comment.objects.filter(post__postsnapshot__job__owner=owner,
                               post__owner_identity__isnull=False).values_list("post__owner_identity_id", flat=True),
        Post.objects.filter(postsnapshot__job__owner=owner,
                            owner_identity__isnull=False).values_list("owner_identity_id", flat=True),
    )


def _identity_payload(identity, depth):
    return {
        "id": str(identity.pk), "instagram_pk": identity.instagram_pk,
        "username": identity.current_username, "full_name": identity.full_name,
        "verified": identity.is_verified, "private": identity.is_private,
        "profile_pic_url": identity.profile_pic_url, "depth": depth,
        "community": 0, "influence": 0.0, "shared_neighbors": 0, "path": [],
    }


def _follow_candidates(owner, frontier, scan_limit):
    visible = visible_relationship_ids(owner)
    reverse = AccountRelationship.objects.filter(
        source_id=OuterRef("target_id"), target_id=OuterRef("source_id"),
        relation_type=AccountRelationship.Type.FOLLOWS, is_active=True,
        pk__in=visible,
    )
    return list(AccountRelationship.objects.filter(
        pk__in=visible, relation_type=AccountRelationship.Type.FOLLOWS,
        is_active=True,
    ).filter(Q(source_id__in=frontier) | Q(target_id__in=frontier)).annotate(
        is_mutual=Exists(reverse)).select_related("source", "target").order_by(
        "-last_seen_at", "pk")[:scan_limit])


def _comment_candidates(owner, frontier, scan_limit):
    return list(Comment.objects.filter(
        post__postsnapshot__job__owner=owner,
        author_identity__isnull=False, post__owner_identity__isnull=False,
    ).filter(Q(author_identity_id__in=frontier) |
             Q(post__owner_identity_id__in=frontier)).exclude(
        author_identity_id=F("post__owner_identity_id")
    ).values("author_identity_id", "post__owner_identity_id").annotate(
        weight=Count("id", distinct=True)).order_by("-weight")[:scan_limit])


def _communities(seed_id, node_ids, edges):
    """Connected components without the seed; singleton branches group by edge kind."""
    adjacency = defaultdict(set)
    seed_kinds = defaultdict(set)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source == seed_id:
            seed_kinds[target].add(edge["type"])
        elif target == seed_id:
            seed_kinds[source].add(edge["type"])
        else:
            adjacency[source].add(target)
            adjacency[target].add(source)
    remaining = set(node_ids) - {seed_id}
    groups = []
    while remaining:
        root = min(remaining, key=int)
        component, queue = set(), [root]
        while queue:
            current = queue.pop()
            if current in component:
                continue
            component.add(current)
            queue.extend(adjacency[current] - component)
        remaining -= component
        groups.append(component)
    singleton_buckets = defaultdict(set)
    final_groups = []
    for group in groups:
        if len(group) > 1:
            final_groups.append(group)
        else:
            node = next(iter(group))
            kind = sorted(seed_kinds[node])[0] if seed_kinds[node] else "isolated"
            singleton_buckets[kind].add(node)
    final_groups.extend(singleton_buckets.values())
    final_groups.sort(key=lambda group: (-len(group), min(map(int, group))))
    mapping = {seed_id: 0}
    for number, group in enumerate(final_groups, start=1):
        for node in group:
            mapping[node] = number
    return mapping, len(final_groups)


def build_graph(owner, seed, options):
    options = options.normalized()
    if not any((options.followers, options.following, options.mutual, options.comments)):
        raise ValueError("AtLeastOneGraphRelationRequired")
    seed_id = str(seed.pk)
    identities = {seed.pk: seed}
    depths = {seed.pk: 0}
    parents = {seed.pk: None}
    edges = {}
    truncated = False
    scan_limit = min(50000, max(2000, options.node_limit * 30))

    for level in range(1, options.depth + 1):
        frontier = {pk for pk, depth in depths.items() if depth == level - 1}
        if not frontier:
            break
        candidates = []
        if options.followers or options.following or options.mutual:
            rows = _follow_candidates(owner, frontier, scan_limit)
            if len(rows) == scan_limit:
                truncated = True
            mutual_pairs = set()
            for relation in rows:
                source, target = relation.source_id, relation.target_id
                mutual_key = tuple(sorted((source, target)))
                if relation.is_mutual:
                    if not options.mutual or mutual_key in mutual_pairs:
                        continue
                    mutual_pairs.add(mutual_key)
                    kind, priority, directed = "mutual", 4, False
                    edge_id = f"m-{mutual_key[0]}-{mutual_key[1]}"
                elif source in frontier and options.following:
                    kind, priority, directed = "following", 2, True
                    edge_id = f"f-{relation.pk}"
                elif target in frontier and options.followers:
                    kind, priority, directed = "follower", 3, True
                    edge_id = f"f-{relation.pk}"
                else:
                    continue
                candidates.append((priority, 1, edge_id, source, target, kind, directed,
                                   relation.source, relation.target))
        if options.comments:
            comments = _comment_candidates(owner, frontier, scan_limit)
            if len(comments) == scan_limit:
                truncated = True
            identity_ids = {row["author_identity_id"] for row in comments} | {
                row["post__owner_identity_id"] for row in comments}
            comment_identities = InstagramIdentity.objects.in_bulk(identity_ids)
            for row in comments:
                source, target = row["author_identity_id"], row["post__owner_identity_id"]
                if source == target or source not in comment_identities or target not in comment_identities:
                    continue
                candidates.append((1, row["weight"], f"c-{source}-{target}", source, target,
                                   "commenter", True, comment_identities[source],
                                   comment_identities[target]))

        candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
        for _, weight, edge_id, source, target, kind, directed, source_obj, target_obj in candidates:
            if source not in frontier and target not in frontier and source not in identities and target not in identities:
                continue
            new_ids = [pk for pk in (source, target) if pk not in identities]
            if new_ids and len(identities) + len(set(new_ids)) > options.node_limit:
                truncated = True
                continue
            for pk, identity in ((source, source_obj), (target, target_obj)):
                if pk not in identities:
                    identities[pk] = identity
                    depths[pk] = level
                    parents[pk] = target if pk == source and target in frontier else source
            edges.setdefault(edge_id, {
                "id": edge_id, "source": str(source), "target": str(target),
                "type": kind, "directed": directed, "weight": int(weight),
                "label": {"follower": "Follower", "following": "Following",
                          "mutual": "Mutual", "commenter": "Commenter"}[kind],
            })

    node_payload = {str(pk): _identity_payload(identity, depths[pk])
                    for pk, identity in identities.items()}
    edge_payload = list(edges.values())
    adjacency = defaultdict(set)
    weighted_degree = defaultdict(float)
    for edge in edge_payload:
        source, target = edge["source"], edge["target"]
        adjacency[source].add(target)
        adjacency[target].add(source)
        multiplier = 2.0 if edge["type"] == "mutual" else (1.0 + min(edge["weight"], 20) / 10
                                                            if edge["type"] == "commenter" else 1.0)
        weighted_degree[source] += multiplier
        weighted_degree[target] += multiplier
    community_map, community_count = _communities(seed_id, set(node_payload), edge_payload)
    max_degree = max(weighted_degree.values(), default=1)
    seed_neighbors = adjacency[seed_id]
    for node_id, node in node_payload.items():
        node["community"] = community_map.get(node_id, 0)
        node["influence"] = round(100 * weighted_degree[node_id] / max_degree, 1)
        node["shared_neighbors"] = len(adjacency[node_id] & seed_neighbors)
        path_ids, current, guard = [], int(node_id), set()
        while current is not None and current not in guard:
            guard.add(current)
            path_ids.append(current)
            current = parents.get(current)
        path_ids.reverse()
        node["path"] = [node_payload[str(pk)]["username"] for pk in path_ids
                        if str(pk) in node_payload]
    nodes = sorted(node_payload.values(), key=lambda row: (row["depth"], -row["influence"], row["username"]))
    influencers = sorted((node for node in nodes if node["id"] != seed_id),
                         key=lambda row: (-row["influence"], -row["shared_neighbors"], row["username"]))[:10]
    possible_edges = len(nodes) * (len(nodes) - 1) / 2
    return {
        "seed": seed_id, "nodes": nodes, "edges": edge_payload,
        "influencers": influencers,
        "stats": {
            "nodes": len(nodes), "edges": len(edge_payload),
            "communities": community_count, "max_depth": max(depths.values(), default=0),
            "density": round(100 * len(edge_payload) / possible_edges, 2) if possible_edges else 0,
            "truncated": truncated,
            "next_limit": min(2000, max(options.node_limit + 100, options.node_limit * 2)),
        },
        "filters": {
            "depth": options.depth, "node_limit": options.node_limit,
            "followers": options.followers, "following": options.following,
            "mutual": options.mutual, "comments": options.comments,
        },
    }
