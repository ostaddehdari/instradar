"""Safe gallery-dl adapter and canonical on-disk storage for Stage 6."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import DownloadItem, MediaAsset
from .session import gallery_cookiefile


class DownloadFailure(RuntimeError):
    pass


def safe_component(value, fallback="unknown"):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return value[:120] or fallback


def storage_root(job):
    configured = settings.INSTRADAR_STORAGE_ROOT.resolve()
    selected = Path(job.volume.root_path).resolve() if job.volume_id else configured
    if selected != configured and configured not in selected.parents:
        raise DownloadFailure("مسیر Storage Volume خارج از فضای مجاز است")
    selected.mkdir(parents=True, exist_ok=True, mode=0o750)
    usage = shutil.disk_usage(selected)
    reserve = max(settings.INSTRADAR_DOWNLOAD_MIN_FREE_BYTES,
                  job.volume.reserved_bytes if job.volume_id else 0)
    if usage.free < reserve:
        raise DownloadFailure("فضای آزاد دیسک از حد امن کمتر است")
    return selected


def post_directory(job, post):
    owner = safe_component(post.owner_username or post.discovered_from, "instagram")
    shortcode = safe_component(post.shortcode, str(post.pk))
    target = (storage_root(job) / "instagram" / owner / shortcode).resolve()
    root = storage_root(job).resolve()
    if root not in target.parents:
        raise DownloadFailure("مسیر خروجی معتبر نیست")
    target.mkdir(parents=True, exist_ok=True, mode=0o750)
    return target


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(stderr, returncode):
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    tail = lines[-1] if lines else "بدون جزئیات"
    tail = re.sub(r"(?i)(sessionid|csrftoken|password)(\s*[=:]\s*)\S+",
                  r"\1\2[REDACTED]", tail)
    return f"gallery-dl با کد {returncode} متوقف شد: {tail[:260]}"


def _thumbnail(source, destination, mime_type):
    try:
        if mime_type.startswith("image/"):
            from PIL import Image
            with Image.open(source) as image:
                image.thumbnail((640, 640))
                image.convert("RGB").save(destination, "JPEG", quality=82, optimize=True)
            return True
        if mime_type.startswith("video/") and shutil.which("ffmpeg"):
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "00:00:01",
                 "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", str(destination)],
                capture_output=True, timeout=45, check=False,
            )
            return result.returncode == 0 and destination.is_file()
    except Exception:
        pass
    return False


def download_post(item: DownloadItem):
    """Download one archived post with the account's captured noVNC cookies."""
    job, post, account = item.job, item.post, item.job.account
    runtime_root = settings.INSTRADAR_DOWNLOAD_RUNTIME
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = post_directory(job, post)
    with tempfile.TemporaryDirectory(prefix=f"job-{job.pk}-", dir=runtime_root) as raw_temp:
        temp = Path(raw_temp)
        with gallery_cookiefile(account) as cookie_path:
            command = [
                sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
                "--no-colors", "--no-mtime", "--write-metadata", "--retries", "1",
                "--http-timeout", "45", "--cookies", str(cookie_path),
                "--directory", str(temp), post.url,
            ]
            if settings.INSTRADAR_DOWNLOAD_RATE_LIMIT:
                command[command.index("--directory"):command.index("--directory")] = [
                    "--limit-rate", settings.INSTRADAR_DOWNLOAD_RATE_LIMIT]
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                                        timeout=settings.INSTRADAR_DOWNLOAD_TIMEOUT,
                                        check=False, env={**os.environ, "PYTHONUNBUFFERED": "1"})
            except subprocess.TimeoutExpired as exc:
                raise DownloadFailure("مهلت دانلود gallery-dl تمام شد") from exc
        if result.returncode:
            raise DownloadFailure(_safe_error(result.stderr, result.returncode))

        files = sorted(path for path in temp.rglob("*") if path.is_file() and
                       path.suffix.lower() not in {".json", ".txt", ".part"})
        if not files:
            raise DownloadFailure("gallery-dl هیچ فایل رسانه‌ای برنگرداند")

        assets, total_bytes = [], 0
        for position, source in enumerate(files, 1):
            digest = _sha256(source)
            extension = source.suffix.lower()[:12] or ".bin"
            final_name = f"{position:02d}{extension}"
            final_path = destination / final_name
            existing = MediaAsset.objects.filter(owner=job.owner, post=post, position=position,
                                                  sha256=digest).first()
            if not (existing and final_path.is_file() and _sha256(final_path) == digest):
                partial = destination / f".{final_name}.part"
                shutil.move(str(source), partial)
                os.replace(partial, final_path)
            size = final_path.stat().st_size
            total_bytes += size
            mime_type = mimetypes.guess_type(final_name)[0] or "application/octet-stream"
            kind = (MediaAsset.Kind.IMAGE if mime_type.startswith("image/") else
                    MediaAsset.Kind.VIDEO if mime_type.startswith("video/") else
                    MediaAsset.Kind.OTHER)
            thumb = destination / f"{position:02d}.thumb.jpg"
            thumbnail_path = ""
            if _thumbnail(final_path, thumb, mime_type):
                thumbnail_path = str(thumb.relative_to(storage_root(job)))
            remote = post.assets.filter(position=position).first()
            asset, _ = MediaAsset.objects.update_or_create(
                owner=job.owner, post=post, position=position,
                defaults={"item": item, "kind": kind,
                          "source_url": (remote.video_url or remote.preview_url) if remote else "",
                          "local_path": str(final_path.relative_to(storage_root(job))),
                          "file_name": final_name, "mime_type": mime_type,
                          "size_bytes": size, "sha256": digest,
                          "thumbnail_path": thumbnail_path, "downloaded_at": timezone.now()},
            )
            assets.append(asset)

        metadata = {
            "instagram_media_id": post.media_pk, "shortcode": post.shortcode,
            "url": post.url, "owner": post.owner_username,
            "published_at": post.published_at.isoformat() if post.published_at else None,
            "is_reel": post.is_reel, "like_count": post.like_count,
            "view_count": post.view_count, "play_count": post.play_count,
            "comment_count": post.comment_count,
            "files": [{"position": asset.position, "name": asset.file_name,
                       "sha256": asset.sha256, "size": asset.size_bytes}
                      for asset in assets],
        }
        (destination / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (destination / "caption.txt").write_text(post.caption or "", encoding="utf-8")
        return assets, total_bytes
