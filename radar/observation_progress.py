"""Real progress and queue ordering for unified observations.

The first unified-job implementation ordered broad stages (media, relations,
comments, Telegram).  That was not granular enough to pause posts after a page
and run followers next.  This module keeps backwards compatibility with those
legacy values while exposing six independently ordered work streams.
"""
from __future__ import annotations

from dataclasses import dataclass


TASK_META = {
    "posts": ("همه پست‌ها", "fa-regular fa-images"),
    "reels": ("همه ریلزها", "fa-solid fa-film"),
    "followers": ("همه فالورها", "fa-solid fa-users"),
    "following": ("همه فالوینگ‌ها", "fa-solid fa-user-plus"),
    "comments": ("همه کامنت‌ها", "fa-regular fa-comments"),
    "telegram": ("انتقال تلگرام", "fa-brands fa-telegram"),
}

LEGACY_TASKS = {
    "media": ("posts", "reels"),
    "relationships": ("followers", "following"),
    "comments": ("comments",),
    "telegram": ("telegram",),
}

TERMINAL_STATUSES = {"done", "partial", "failed", "skipped"}


@dataclass
class ObservationSubtask:
    key: str
    label: str
    icon: str
    extracted: int = 0
    expected: int | None = None
    percent: int = 0
    status: str = "waiting"
    status_label: str = "منتظر"
    issue: str = ""
    posts_done: int | None = None
    posts_total: int | None = None
    comments_extracted: int | None = None
    comments_expected: int | None = None

    @property
    def counter(self):
        total = "؟" if self.expected is None else self.expected
        return f"{self.extracted}/{total}"

    @property
    def terminal(self):
        return self.status in TERMINAL_STATUSES


def enabled_task_keys(job):
    keys = []
    if job.include_posts:
        keys.append("posts")
    if job.include_reels:
        keys.append("reels")
    if job.include_followers:
        keys.append("followers")
    if job.include_following:
        keys.append("following")
    if job.include_comments:
        keys.append("comments")
    if job.publish_to_telegram:
        keys.append("telegram")
    return keys


def normalized_task_order(job):
    """Expand 3.11 stage names and return every enabled stream exactly once."""
    enabled = enabled_task_keys(job)
    ordered = []
    for raw in job.stage_order or []:
        expanded = LEGACY_TASKS.get(raw, (raw,))
        for key in expanded:
            if key in enabled and key not in ordered:
                ordered.append(key)
    ordered.extend(key for key in enabled if key not in ordered)
    return ordered


def _percent(extracted, expected, status):
    if expected is None:
        return 100 if status == "done" else 0
    if expected == 0:
        return 100 if status in TERMINAL_STATUSES else 0
    return max(0, min(100, round(100 * extracted / expected)))


def _status_label(value):
    return {
        "waiting": "منتظر",
        "queued": "در صف",
        "running": "در حال استخراج",
        "paused": "متوقف",
        "done": "کامل",
        "partial": "ناقص",
        "failed": "ناموفق",
        "skipped": "ردشده",
    }.get(value, value)


def _stream_status(parent, stream, current, key):
    if stream is not None and stream.finished:
        return "partial" if stream.issue else "done"
    if parent is not None and parent.status == "failed":
        return "failed"
    if parent is not None and parent.status == "partial" and stream is not None and stream.issue:
        return "partial"
    if current == key and parent is not None and parent.status == "running":
        return "running"
    if parent is not None and parent.status == "paused":
        return "paused"
    if parent is not None:
        return "queued"
    return "waiting"


def build_observation_subtasks(job):
    """Build counters from durable cursors; no weighted/fake progress values."""
    media = getattr(job, "media_job", None)
    relation = getattr(job, "relationship_job", None)
    publish = getattr(job, "publish_job", None)
    session = getattr(job, "crawl_session", None)
    media_streams = ({row.stream: row for row in media.streams.all()} if media else {})
    relation_streams = ({row.stream: row for row in relation.streams.all()} if relation else {})
    comment_jobs = list(job.comment_jobs.all())

    rows = {}
    for key in enabled_task_keys(job):
        label, icon = TASK_META[key]
        rows[key] = ObservationSubtask(key=key, label=label, icon=icon)

    if "posts" in rows:
        stream = media_streams.get("timeline")
        bounded_media = (job.mode != "full" or job.date_from is not None or
                         job.date_to is not None)
        expected = None if bounded_media else getattr(session, "profile_posts_count", None)
        if expected is None and media is not None and not bounded_media:
            expected = media.expected_media_count
        status = _stream_status(media, stream, job.current_stage, "posts")
        extracted = stream.seen if stream else 0
        rows["posts"].extracted = extracted
        rows["posts"].expected = expected
        rows["posts"].status = status
        rows["posts"].issue = stream.issue if stream else ""

    if "reels" in rows:
        stream = media_streams.get("reels")
        bounded_media = (job.mode != "full" or job.date_from is not None or
                         job.date_to is not None)
        expected = None if bounded_media else getattr(session, "profile_reels_count", None)
        status = _stream_status(media, stream, job.current_stage, "reels")
        extracted = stream.seen if stream else 0
        rows["reels"].extracted = extracted
        rows["reels"].expected = expected
        rows["reels"].status = status
        rows["reels"].issue = stream.issue if stream else ""

    for key in ("followers", "following"):
        if key not in rows:
            continue
        stream = relation_streams.get(key)
        expected = (relation.expected_followers if relation and key == "followers"
                    else relation.expected_following if relation else None)
        status = _stream_status(relation, stream, job.current_stage, key)
        rows[key].extracted = stream.seen if stream else 0
        rows[key].expected = expected
        rows[key].status = status
        rows[key].issue = stream.issue if stream else ""

    if "comments" in rows:
        skipped_posts = int(getattr(job, "comments_skipped_count", 0) or 0)
        posts_total = len(comment_jobs) + skipped_posts
        posts_done = (sum(item.status in TERMINAL_STATUSES for item in comment_jobs) +
                      skipped_posts)
        comments_extracted = sum(int(getattr(item, "extracted_count", 0) or 0)
                                 for item in comment_jobs)
        known_expected = [int(item.expected_count) for item in comment_jobs
                          if item.expected_count is not None]
        comments_expected = (sum(known_expected)
                             if posts_total and len(known_expected) == posts_total else None)
        if not comment_jobs:
            status = "skipped" if skipped_posts else "waiting"
        elif any(item.status == "running" for item in comment_jobs):
            status = "running"
        elif any(item.status == "queued" for item in comment_jobs):
            status = "queued"
        elif all(item.status == "done" for item in comment_jobs):
            status = "done"
        elif all(item.status == "failed" for item in comment_jobs):
            status = "failed"
        else:
            status = "partial"
        rows["comments"].extracted = comments_extracted
        rows["comments"].expected = comments_expected
        rows["comments"].status = status
        rows["comments"].posts_done = posts_done
        rows["comments"].posts_total = posts_total
        rows["comments"].comments_extracted = comments_extracted
        rows["comments"].comments_expected = comments_expected
        rows["comments"].issue = next(
            (item.error for item in comment_jobs if item.error),
            f"{skipped_posts} پست بدون تغییر بود و دوباره خوانده نشد" if skipped_posts else "")

    if "telegram" in rows:
        if publish is None:
            status, extracted, expected, issue = "waiting", 0, None, ""
        else:
            status = {"waiting": "waiting", "cancelled": "failed"}.get(
                publish.status, publish.status)
            extracted = publish.completed_items + publish.failed_items
            expected = publish.total_items
            issue = publish.error
        rows["telegram"].extracted = extracted
        rows["telegram"].expected = expected
        rows["telegram"].status = status
        rows["telegram"].issue = issue

    ordered = [rows[key] for key in normalized_task_order(job) if key in rows]
    for row in ordered:
        row.percent = _percent(row.extracted, row.expected, row.status)
        if row.key == "comments" and row.expected is None and row.posts_total:
            row.percent = _percent(row.posts_done or 0, row.posts_total, row.status)
        row.status_label = _status_label(row.status)

    # Use real item weighting where totals are known.  This prevents a tiny
    # 1-item following stream from making a 30/5895 follower scan look 50%
    # complete. Unknown-size streams retain equal fallback weighting.
    known = [row for row in ordered if row.expected is not None]
    if known and sum(max(0, row.expected or 0) for row in known):
        total_expected = sum(max(0, row.expected or 0) for row in known)
        total_extracted = sum(min(max(0, row.extracted), max(0, row.expected or 0))
                              for row in known)
        overall = round(100 * total_extracted / total_expected)
    else:
        overall = round(sum(row.percent for row in ordered) / len(ordered)) if ordered else 100
    if job.status == "done":
        overall = 100
    return ordered, max(0, min(100, overall))


def decorate_observation(job):
    rows, overall = build_observation_subtasks(job)
    job.subtasks = rows
    job.stage_items = [(row.key, row.label) for row in rows]
    job.progress_percent = overall
    job.relationship_provider = ""
    job.relationship_issue = ""
    relation = getattr(job, "relationship_job", None)
    if relation:
        states = list(relation.streams.all())
        display = next((item for item in states if item.issue), states[0] if states else None)
        if display:
            job.relationship_provider = display.provider
            job.relationship_issue = display.issue
    return job
