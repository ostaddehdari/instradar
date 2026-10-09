"""Python Playwright daemon attached to the existing DISPLAY=:99 noVNC desktop."""
import os
from multiprocessing.connection import Listener
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from playwright.sync_api import sync_playwright
from radar.browser_relationships import BrowserRelationshipCollector


class Command(BaseCommand):
    help = "Run isolated noVNC browser controller"

    def handle(self, *args, **kwargs):
        key = os.environ.get("BROWSER_AUTH_KEY", "")
        if len(key) < 32:
            raise RuntimeError("BROWSER_AUTH_KEY must have at least 32 characters")
        socket = Path(settings.INSTRADAR_BROWSER_SOCKET)
        socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if socket.exists():
            socket.unlink()  # Only our own exact Unix socket on startup.
        browser_dir = settings.INSTRADAR_SHARED / "browser-profiles"
        browser_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        contexts = {}
        listener = Listener(str(socket), family="AF_UNIX", authkey=key.encode())
        os.chmod(socket, 0o600)
        try:
            with sync_playwright() as playwright:
                relationship_collector = BrowserRelationshipCollector()

                def context_for(account_id):
                    if account_id in contexts:
                        return contexts[account_id]
                    # Playwright's sync API owns an asyncio loop in this thread;
                    # Django ORM access is isolated in a short worker thread.
                    from concurrent.futures import ThreadPoolExecutor
                    from django.db import connections
                    from radar.models import InstagramAccount

                    def load_profile_id():
                        try:
                            return str(InstagramAccount.objects.values_list(
                                "profile_id", flat=True).get(pk=account_id))
                        finally:
                            connections.close_all()

                    with ThreadPoolExecutor(max_workers=1) as db_pool:
                        profile_id = db_pool.submit(load_profile_id).result()
                    profile = browser_dir / profile_id
                    contexts[account_id] = playwright.chromium.launch_persistent_context(
                        str(profile), headless=False,
                        executable_path=os.environ.get("CHROME_PATH", "/usr/bin/chromium"),
                        args=["--no-first-run", "--no-default-browser-check"], timeout=30000)
                    return contexts[account_id]

                while True:
                    with listener.accept() as conn:
                        try:
                            request = conn.recv()
                            action = request.get("action")
                            account_id = request.get("account_id")
                            payload = request.get("payload") or {}
                            if action == "health":
                                result = {"ok": True, "display": os.environ.get("DISPLAY", ""),
                                          "x_socket": Path("/tmp/.X11-unix/X99").exists(),
                                          "active": len(contexts),
                                          "relationship_sessions": len(relationship_collector.sessions)}
                            elif not isinstance(account_id, int) or account_id < 1:
                                result = {"ok": False, "error": "Invalid account"}
                            elif action == "launch":
                                context = context_for(account_id)
                                page = context.pages[0] if context.pages else context.new_page()
                                page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=30000)
                                result = {"ok": True}
                            elif action == "capture":
                                context = contexts.get(account_id)
                                if context is None:
                                    result = {"ok": False, "error": "Open Chrome first"}
                                else:
                                    cookies = context.cookies("https://www.instagram.com/")
                                    page = context.pages[0] if context.pages else context.new_page()
                                    user_agent = page.evaluate("navigator.userAgent")
                                    result = {"ok": bool(next((c for c in cookies if c["name"] == "sessionid"), None)),
                                              "cookies": cookies, "user_agent": user_agent,
                                              "error": "Instagram login incomplete"}
                            elif action == "relationships":
                                context = context_for(account_id)
                                result = {"ok": True, **relationship_collector.fetch(
                                    context, account_id,
                                    target=payload.get("target"),
                                    stream=payload.get("stream"),
                                    cursor=payload.get("cursor", ""),
                                    limit=payload.get("limit", 50),
                                    target_pk=payload.get("target_pk", ""),
                                    expected_total=payload.get("expected_total"),
                                )}
                            elif action == "profile":
                                context = context_for(account_id)
                                target = str(payload.get("target") or "").strip().lstrip("@").lower()
                                if not target:
                                    raise ValueError("Invalid profile target")
                                page = context.pages[0] if context.pages else context.new_page()
                                page.goto(f"https://www.instagram.com/{target}/",
                                          wait_until="domcontentloaded", timeout=45000)
                                if "/accounts/login" in page.url or page.locator(
                                        'input[name="username"]').count():
                                    raise RuntimeError("BrowserInstagramLoginRequired")
                                profile = page.evaluate("""async ({target, targetPk}) => {
                                    if (String(targetPk || '').match(/^\\d+$/)) {
                                        try {
                                            const csrf = document.cookie.split('; ').find(v => v.startsWith('csrftoken='));
                                            const response = await fetch(`/api/v1/users/${targetPk}/info/`, {
                                                credentials: 'include',
                                                headers: {
                                                    'X-IG-App-ID': '936619743392459',
                                                    'X-Requested-With': 'XMLHttpRequest',
                                                    ...(csrf ? {'X-CSRFToken': decodeURIComponent(csrf.split('=').slice(1).join('='))} : {})
                                                }
                                            });
                                            const payload = await response.json();
                                            const user = payload.user || payload.data?.user;
                                            if (user) return user;
                                        } catch (_) {}
                                    }
                                    const scripts = [...document.scripts].map(item => item.textContent || '');
                                    const stack = [];
                                    for (const text of scripts) {
                                        try { stack.push(JSON.parse(text)); } catch (_) {}
                                    }
                                    const visit = value => {
                                        if (!value || typeof value !== 'object') return null;
                                        if ((value.username || '').toLowerCase() === target &&
                                            String(value.pk || value.pk_id || '').match(/^\\d+$/)) return value;
                                        for (const child of Object.values(value)) {
                                            const found = visit(child);
                                            if (found) return found;
                                        }
                                        return null;
                                    };
                                    const value = stack.map(visit).find(Boolean);
                                    if (!value) return null;
                                    return {
                                        pk: String(value.pk || value.pk_id),
                                        username: String(value.username || target).toLowerCase(),
                                        full_name: String(value.full_name || ''),
                                        follower_count: value.follower_count ?? value.edge_followed_by?.count ?? null,
                                        following_count: value.following_count ?? value.edge_follow?.count ?? null,
                                        media_count: value.media_count ?? value.edge_owner_to_timeline_media?.count ?? null,
                                        is_private: Boolean(value.is_private),
                                        is_verified: Boolean(value.is_verified),
                                        profile_pic_url: String(value.profile_pic_url || '')
                                    };
                                }""", {"target": target, "targetPk": str(payload.get("target_pk") or "")})
                                if not profile and str(payload.get("target_pk") or "").isdecimal():
                                    profile = {"pk": str(payload["target_pk"]), "username": target,
                                               "full_name": "", "follower_count": None,
                                               "following_count": None, "media_count": None,
                                               "is_private": False, "is_verified": False,
                                               "profile_pic_url": ""}
                                if not profile:
                                    raise RuntimeError("BrowserProfilePayloadMissing")
                                result = {"ok": True, "profile": profile}
                            elif action == "close":
                                relationship_collector.close_account(account_id)
                                context = contexts.pop(account_id, None)
                                if context:
                                    context.close()
                                result = {"ok": True}
                            else:
                                result = {"ok": False, "error": "Unknown action"}
                        except Exception as exc:
                            # Keep failure details in the private systemd journal.
                            import traceback
                            traceback.print_exc()
                            allowed = {
                                "BrowserInstagramLoginRequired",
                                "BrowserRelationshipResponseEmpty",
                                "RelationshipListNotFound",
                                "Invalid target username",
                                "Invalid relationship stream",
                                "Invalid browser cursor",
                                "Invalid target id",
                            }
                            message = str(exc)
                            safe_diagnostic = message.startswith(
                                "BrowserRelationshipResponseEmpty:"
                            )
                            result = {"ok": False,
                                      "error": message if message in allowed or safe_diagnostic
                                      else type(exc).__name__}
                        conn.send(result)
        finally:
            listener.close()
            socket.unlink(missing_ok=True)
