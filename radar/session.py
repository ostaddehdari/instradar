"""Single encrypted Instagram browser session shared by every Instagram adapter."""
import os
import re
from urllib.parse import unquote
import tempfile
from contextlib import contextmanager
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from .crypto import unseal


class InstagramSessionError(RuntimeError):
    """Safe session-bridge failure; never includes cookies or upstream bodies."""

    def __init__(self, reason, *, permanent=False):
        self.permanent = permanent
        super().__init__(reason)


# Only stable device and retry settings survive between captures. Cookies,
# Authorization and claims belong to the old session and must not be mixed with
# a new browser sessionid.
_STABLE_AIOGRAPI_KEYS = {
    "uuids", "device_settings", "user_agent", "country", "country_code",
    "locale", "timezone_offset", "timezone_name", "push_disabled", "usdid",
    "request_timeout", "public_request_retries_count",
    "public_request_retries_timeout", "session_retry_total",
    "session_retry_backoff_factor", "session_retry_statuses",
    "public_transport", "public_transport_impersonate", "private_transport",
    "tls_verify",
}


def _is_instagram_domain(value):
    domain = str(value or "").lower().lstrip(".")
    return domain == "instagram.com" or domain.endswith(".instagram.com")


def session_user_id(value):
    """Return the numeric owner encoded at the start of an Instagram sessionid."""
    match = re.match(r"^(\d+)", unquote(str(value or "")))
    return match.group(1) if match else ""


def normalize_browser_cookies(cookies):
    """Discard stale duplicate identity cookies before persisting a capture."""
    valid = [dict(item) for item in (cookies or [])
             if isinstance(item, dict) and _is_instagram_domain(item.get("domain"))
             and item.get("name") and item.get("value") is not None]
    sessions = [item for item in valid if item["name"] == "sessionid"]
    if not sessions:
        return valid
    session = sessions[-1]
    uid = session_user_id(session.get("value"))
    if not uid:
        return valid
    # ds_user_id left by an older profile is more harmful than its absence.
    valid = [item for item in valid
             if item["name"] != "ds_user_id" or str(item["value"]) == uid]
    if not any(item["name"] == "ds_user_id" for item in valid):
        identity = dict(session)
        identity.update(name="ds_user_id", value=uid)
        valid.append(identity)
    return valid


def session_id(account):
    cookies = instagram_cookies(account)
    value = next((c["value"] for c in cookies if c.get("name") == "sessionid"), "")
    if not value:
        raise ValueError("Instagram sessionid missing")
    return value


def instagram_cookies(account):
    if not account.encrypted_cookies:
        raise ValueError("Instagram cookies missing")
    cookies = unseal(account.encrypted_cookies)
    if not isinstance(cookies, list):
        raise ValueError("Invalid Instagram cookie bundle")
    return normalize_browser_cookies(cookies)


async def aiograpi_client(account):
    """Create an authenticated aiograpi client from the captured browser session."""
    from aiograpi import Client

    cookies = instagram_cookies(account)
    captured = {item.get("name"): item.get("value") for item in cookies
                if item.get("name") and item.get("value") is not None}
    current_sessionid = captured.get("sessionid", "")
    current_user_id = session_user_id(current_sessionid)
    if not current_user_id:
        raise InstagramSessionError("sessionid مرورگر ساختار معتبر ندارد", permanent=True)
    authenticated_user_id = current_user_id
    captured_user_id = str(captured.get("ds_user_id") or "")
    stored_user_id = str(account.instagram_pk or "")
    if captured_user_id and captured_user_id != authenticated_user_id:
        raise InstagramSessionError("Cookieهای مرورگر متعلق به یک هویت واحد نیستند", permanent=True)
    if stored_user_id and stored_user_id != authenticated_user_id:
        raise InstagramSessionError("نشست مرورگر با Instagram ID ذخیره‌شده هم‌خوان نیست", permanent=True)

    # aiograpi 2.x changed its default private transport to native curl.  That
    # transport is useful for password login, but importing an already-valid
    # browser session does not need it and some production hosts cannot build
    # the curl HTTP/2 client.  Construct the known-compatible transport first
    # so a transport bootstrap failure cannot silently disable every direct
    # relationship route before a request is made.
    client = Client(private_transport="requests")
    if account.encrypted_aiograpi_settings:
        saved = unseal(account.encrypted_aiograpi_settings)
        if isinstance(saved, dict):
            stable = {key: saved[key] for key in _STABLE_AIOGRAPI_KEYS
                      if key in saved and saved[key] is not None}
            # A setting saved by aiograpi 2.x must not switch the next worker
            # back to the failing curl bootstrap path.
            stable["private_transport"] = "requests"
            if stable:
                client.set_settings(stable)

    def attach_browser_web_session():
        # Web and GraphQL transports can keep using the browser identity even
        # when Instagram declines to import that cookie into its Mobile API.
        client.public.set_cookies(captured)
        client.graphql.set_cookies(captured)

    try:
        accepted = await client.login_by_sessionid(current_sessionid)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(exc, "code", None) or getattr(response, "status_code", None)
        suffix = f"/HTTP {status}" if status else ""
        # login_by_sessionid validates through the Mobile API.  A rejection
        # there does not invalidate a browser cookie that is still visibly
        # authenticated in Chrome.  Preserve a safe reason for job events and
        # continue with cookie-authenticated Web/GraphQL routes.
        attach_browser_web_session()
        client.username = (account.username or "").strip().lstrip("@").lower()
        client._instradar_private_ready = False
        client._instradar_session_mode = "browser-web-only"
        client._instradar_session_error = f"{type(exc).__name__}{suffix}"
        return client
    if not accepted:
        raise InstagramSessionError("Instagram نشست مشترک را رد کرد", permanent=True)

    authenticated_username = (client.username or "").strip().lstrip("@").lower()
    if not authenticated_username:
        raise InstagramSessionError("نام کاربری نشست از Instagram دریافت نشد")
    # The connection row may have been created with a placeholder/old username.
    # The identity verified by the captured cookie is the source of truth.
    if authenticated_username.casefold() != (account.username or "").casefold():
        from asgiref.sync import sync_to_async
        from .models import InstagramAccount
        await sync_to_async(
            InstagramAccount.objects.filter(pk=account.pk).update,
            thread_sensitive=True,
        )(username=authenticated_username, instagram_pk=authenticated_user_id)
        account.username = authenticated_username
        account.instagram_pk = authenticated_user_id

    # aiograpi's Web and GraphQL transports receive the complete captured
    # browser cookies. Mobile auth above was derived from the same sessionid.
    attach_browser_web_session()
    client._instradar_private_ready = True
    client._instradar_session_mode = "browser-shared"
    client._instradar_session_error = ""
    return client


@contextmanager
def gallery_cookiefile(account):
    """Netscape cookie file; deleted after use. Never hand the path to a browser client."""
    jar = MozillaCookieJar()
    for item in instagram_cookies(account):
        domain = item.get("domain", "")
        if not _is_instagram_domain(domain) or not item.get("name"):
            continue
        jar.set_cookie(Cookie(version=0, name=item["name"], value=item["value"],
            port=None, port_specified=False, domain=domain, domain_specified=True,
            domain_initial_dot=domain.startswith("."), path=item.get("path", "/"),
            path_specified=True, secure=item.get("secure", True), expires=item.get("expires", -1),
            discard=False, comment=None, comment_url=None, rest={"HttpOnly": item.get("httpOnly", False)}, rfc2109=False))
    fd, path = tempfile.mkstemp(prefix="instradar-ig-", suffix=".cookies")
    try:
        os.fchmod(fd, 0o600)
        os.close(fd)
        jar.save(path, ignore_discard=True, ignore_expires=True)
        yield Path(path)
    finally:
        Path(path).unlink(missing_ok=True)
