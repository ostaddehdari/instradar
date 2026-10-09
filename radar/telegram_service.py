"""Encrypted Telethon account lifecycle and native Instagram media delivery."""
from __future__ import annotations

import asyncio
from pathlib import Path

from django.utils import timezone

from .crypto import seal, unseal
from .models import TelegramAccount, TelegramDestination, TelegramSettings


class TelegramServiceError(RuntimeError):
    pass


def _credentials(owner):
    try:
        config = TelegramSettings.objects.get(owner=owner)
        return int(config.api_id), str(unseal(config.encrypted_api_hash))
    except TelegramSettings.DoesNotExist as exc:
        raise TelegramServiceError("ابتدا API ID و API Hash تلگرام را ثبت کنید") from exc


async def _send_code(api_id, api_hash, phone, session):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session or ""), api_id, api_hash,
                            connection_retries=10, retry_delay=3)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client.session.save(), sent.phone_code_hash
    finally:
        await client.disconnect()


def request_login_code(account):
    api_id, api_hash = _credentials(account.owner)
    pending = str(unseal(account.encrypted_pending_session)) if account.encrypted_pending_session else ""
    try:
        session, code_hash = asyncio.run(_send_code(api_id, api_hash, account.phone, pending))
    except Exception as exc:
        raise TelegramServiceError(f"ارسال کد تلگرام ناموفق بود ({type(exc).__name__})") from exc
    account.encrypted_pending_session = seal(session)
    account.encrypted_phone_code_hash = seal(code_hash)
    account.status = TelegramAccount.Status.CODE_SENT
    account.last_error = ""
    account.save(update_fields=["encrypted_pending_session", "encrypted_phone_code_hash",
                                "status", "last_error", "updated_at"])


async def _sign_in(api_id, api_hash, phone, session, code_hash, code=None, password=None):
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), api_id, api_hash,
                            connection_retries=10, retry_delay=3)
    await client.connect()
    try:
        try:
            if password is not None:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            return {"password": True, "session": client.session.save()}
        me = await client.get_me()
        if not me:
            raise TelegramServiceError("هویت حساب تلگرام دریافت نشد")
        return {"password": False, "session": client.session.save(), "id": me.id,
                "username": me.username or "", "first_name": me.first_name or ""}
    finally:
        await client.disconnect()


def verify_login(account, *, code=None, password=None):
    if not account.encrypted_pending_session:
        raise TelegramServiceError("نشست موقت ورود وجود ندارد؛ دوباره کد بخواهید")
    api_id, api_hash = _credentials(account.owner)
    session = str(unseal(account.encrypted_pending_session))
    code_hash = (str(unseal(account.encrypted_phone_code_hash))
                 if account.encrypted_phone_code_hash else "")
    try:
        result = asyncio.run(_sign_in(api_id, api_hash, account.phone, session,
                                     code_hash, code=code, password=password))
    except Exception as exc:
        raise TelegramServiceError(f"تأیید ورود تلگرام ناموفق بود ({type(exc).__name__})") from exc
    if result["password"]:
        account.encrypted_pending_session = seal(result["session"])
        account.status = TelegramAccount.Status.NEEDS_PASSWORD
        account.save(update_fields=["encrypted_pending_session", "status", "updated_at"])
        return False
    account.encrypted_session = seal(result["session"])
    account.encrypted_pending_session = None
    account.encrypted_phone_code_hash = None
    account.telegram_user_id = result["id"]
    account.username = result["username"]
    account.first_name = result["first_name"]
    account.status = TelegramAccount.Status.ACTIVE
    account.last_error = ""
    account.connected_at = timezone.now()
    account.save()
    return True


async def _dialogs(api_id, api_hash, session):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import Channel, Chat

    client = TelegramClient(StringSession(session), api_id, api_hash,
                            connection_retries=10, retry_delay=3)
    await client.connect()
    rows = []
    try:
        if not await client.is_user_authorized():
            raise TelegramServiceError("نشست تلگرام منقضی شده است")
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            admin = bool(getattr(entity, "creator", False) or
                         getattr(entity, "admin_rights", None))
            if not admin or not isinstance(entity, (Channel, Chat)):
                continue
            if isinstance(entity, Channel):
                kind = (TelegramDestination.Kind.SUPERGROUP if entity.megagroup
                        else TelegramDestination.Kind.CHANNEL)
                rights = getattr(entity, "admin_rights", None)
                can_post = bool(entity.megagroup or getattr(entity, "creator", False) or
                                (rights and getattr(rights, "post_messages", False)))
            else:
                kind, can_post = TelegramDestination.Kind.GROUP, True
            rows.append({"peer_id": entity.id,
                         "access_hash": getattr(entity, "access_hash", None),
                         "title": dialog.name or getattr(entity, "title", ""),
                         "username": getattr(entity, "username", "") or "",
                         "kind": kind, "is_admin": True, "can_post": can_post})
        return rows
    finally:
        await client.disconnect()


def sync_destinations(account):
    if account.status != TelegramAccount.Status.ACTIVE or not account.encrypted_session:
        raise TelegramServiceError("حساب تلگرام هنوز متصل نیست")
    api_id, api_hash = _credentials(account.owner)
    try:
        rows = asyncio.run(_dialogs(api_id, api_hash, str(unseal(account.encrypted_session))))
    except Exception as exc:
        if isinstance(exc, TelegramServiceError):
            raise
        raise TelegramServiceError(f"خواندن کانال‌ها ناموفق بود ({type(exc).__name__})") from exc
    seen = []
    for row in rows:
        destination, _ = TelegramDestination.objects.update_or_create(
            account=account, peer_id=row["peer_id"],
            defaults={**row, "is_active": True, "last_synced_at": timezone.now()})
        seen.append(destination.pk)
    account.destinations.exclude(pk__in=seen).update(is_active=False)
    return len(seen)


def build_caption(post, template=""):
    caption = post.caption or ""
    tags = " ".join(f"#{tag}" for tag in post.hashtags.values_list("tag", flat=True)
                    if f"#{tag}".casefold() not in caption.casefold())
    base = template or "{caption}\n\n{hashtags}\n\nمنبع: {url}"
    try:
        text = base.format(caption=caption, hashtags=tags,
                           url=post.url, username=post.owner_username,
                           shortcode=post.shortcode)
    except (KeyError, ValueError) as exc:
        raise TelegramServiceError("قالب کپشن معتبر نیست") from exc
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _peer(destination):
    from telethon.tl.types import InputPeerChannel, InputPeerChat

    if destination.kind == TelegramDestination.Kind.GROUP and not destination.access_hash:
        return InputPeerChat(destination.peer_id)
    if destination.access_hash is None:
        raise TelegramServiceError("Access hash مقصد موجود نیست؛ فهرست کانال‌ها را تازه کنید")
    return InputPeerChannel(destination.peer_id, destination.access_hash)


async def _send_media(api_id, api_hash, session, destination, remote_urls, local_paths, caption):
    """Use the provided direct-URL-first strategy, then upload gallery-dl files."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), api_id, api_hash,
                            connection_retries=10, retry_delay=3)
    await client.connect()
    sent = []
    try:
        if not await client.is_user_authorized():
            raise TelegramServiceError("نشست تلگرام منقضی شده است")
        entity = _peer(destination)
        media_caption, remainder = caption[:1024], caption[1024:]

        # First attempt from the user's sample: Telegram fetches a single URL.
        # If it cannot recognise the native media, delete that message and use
        # the authenticated gallery-dl output instead.
        if len(remote_urls) == 1:
            url = remote_urls[0]
            try:
                direct = await client.send_file(entity, url, caption=media_caption,
                                                force_document=False,
                                                parse_mode=None,
                                                supports_streaming=Path(url.split("?", 1)[0]).suffix.lower()
                                                in (".mp4", ".mov", ".m4v"))
                expected_video = Path(url.split("?", 1)[0]).suffix.lower() in (".mp4", ".mov", ".m4v")
                native = bool(getattr(direct, "video", None) if expected_video
                              else getattr(direct, "photo", None))
                if native:
                    sent.append(direct)
                else:
                    await direct.delete()
            except Exception:
                sent = []

        if not sent:
            if not local_paths:
                raise TelegramServiceError("فایل محلی برای fallback موجود نیست")
            for offset in range(0, len(local_paths), 10):
                batch = local_paths[offset:offset + 10]
                if len(batch) == 1 and Path(batch[0]).suffix.lower() in (
                        ".jpg", ".jpeg", ".png", ".webp"):
                    # The user's reference implementation deliberately wraps a
                    # normalized JPEG in InputMediaUploadedPhoto so Telegram
                    # cannot downgrade a photo to a generic document.
                    import io
                    from PIL import Image
                    from telethon.tl.types import InputMediaUploadedPhoto

                    with Image.open(batch[0]) as image:
                        image.load()
                        image = image.convert("RGB")
                        memory = io.BytesIO()
                        image.save(memory, format="JPEG", quality=95, optimize=True)
                    memory.name = "instagram_photo.jpg"
                    memory.seek(0)
                    uploaded = await client.upload_file(memory, file_name=memory.name)
                    try:
                        result = await client.send_file(
                            entity, InputMediaUploadedPhoto(file=uploaded),
                            caption=media_caption if offset == 0 else "", parse_mode=None)
                    finally:
                        memory.close()
                else:
                    result = await client.send_file(
                        entity, batch if len(batch) > 1 else batch[0],
                        caption=media_caption if offset == 0 else "", force_document=False,
                        supports_streaming=True, parse_mode=None)
                sent.extend(result if isinstance(result, list) else [result])
        if remainder:
            for offset in range(0, len(remainder), 4096):
                sent.append(await client.send_message(
                    entity, remainder[offset:offset + 4096], parse_mode=None))
        return [message.id for message in sent]
    finally:
        await client.disconnect()


def send_media(account, destination, post, remote_urls, local_paths, template=""):
    api_id, api_hash = _credentials(account.owner)
    if not account.encrypted_session:
        raise TelegramServiceError("نشست تلگرام ذخیره نشده است")
    return asyncio.run(_send_media(
        api_id, api_hash, str(unseal(account.encrypted_session)), destination,
        remote_urls, [str(path) for path in local_paths], build_caption(post, template)))
