"""Capture the live relationship responses used by Instagram's own web UI."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from urllib.parse import unquote_plus, urlsplit


USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,100}$")
RELATION_PATH_MARKERS = ("friendship", "follower", "following", "edge_follow")
RELATION_COLLECTION_KEYS = {"users", "edges", "nodes", "items", "connections"}
RELATION_NODE_KEYS = {"node", "user", "profile"}


def extract_relationship_users(payload, stream, *, trusted_direct=False):
    """Return normalized users from legacy or current relationship payloads.

    The traversal deliberately accepts identities only below relationship-like
    keys unless the response URL is the direct friendships endpoint. It never
    returns raw response data to Django or stores it in logs.
    """
    found = OrderedDict()

    visited = 0
    decoded_strings = 0

    def direct_collection_context(lowered_path):
        """Accept a user only when it is a direct list member or node wrapper."""
        for index, part in enumerate(lowered_path):
            if part not in RELATION_COLLECTION_KEYS:
                continue
            remainder = lowered_path[index + 1:]
            if len(remainder) == 1 and remainder[0].isdecimal():
                return True
            if (len(remainder) == 2 and remainder[0].isdecimal()
                    and remainder[1] in RELATION_NODE_KEYS):
                return True
        return False

    def walk(value, path=()):
        nonlocal visited, decoded_strings
        visited += 1
        if visited > 250000 or len(path) > 80:
            return
        if isinstance(value, dict):
            username = str(value.get("username") or "").strip().lstrip("@").lower()
            # Current xdt friendship payloads are not consistent: depending on
            # the rollout the stable id is exposed as pk, pk_id or
            # strong_id__.  Accept only decimal identifiers and never invent
            # an id from a username.
            instagram_pk = str(
                value.get("pk") or value.get("pk_id") or value.get("strong_id__")
                or value.get("rest_id") or value.get("user_id")
                or value.get("instagram_id") or value.get("id") or ""
            )
            lowered_path = tuple(part.lower() for part in path)
            path_text = ".".join(lowered_path)
            # A classified relationship request can contain unrelated viewer
            # or owner objects too.  Trust identities only inside a list-like
            # collection, while explicit friendship/follower roots remain
            # self-authenticating.
            relationship_context = any(
                marker in path_text for marker in RELATION_PATH_MARKERS
            ) or (trusted_direct and direct_collection_context(lowered_path))
            if (relationship_context and instagram_pk.isdecimal()
                    and USERNAME_RE.fullmatch(username)):
                picture = value.get("profile_pic_url") or value.get("profile_pic_url_hd") or ""
                if isinstance(picture, dict):
                    picture = picture.get("url") or ""
                found.setdefault(instagram_pk, {
                    "pk": instagram_pk,
                    "username": username,
                    "full_name": str(value.get("full_name") or "")[:250],
                    "is_private": bool(value.get("is_private", False)),
                    "is_verified": bool(value.get("is_verified", False)),
                    "profile_pic_url": str(picture or ""),
                })
            for key, child in value.items():
                walk(child, path + (str(key),))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (str(index),))
        elif (isinstance(value, (str, bytes)) and decoded_strings < 100
              and 2 <= len(value) <= 4 * 1024 * 1024):
            # Relay occasionally places an incremental GraphQL patch inside a
            # JSON string.  Decode only strings that can contain a user object;
            # this keeps traversal bounded and avoids inspecting arbitrary text.
            marker = value if isinstance(value, bytes) else value.encode("utf-8", errors="ignore")
            if b"username" in marker and (b"{" in marker or b"[" in marker):
                decoded_strings += 1
                for decoded in _json_values_from_text(value):
                    walk(decoded, path + ("$json",))

    walk(payload)
    return list(found.values())


def extract_relationship_cursor(payload, stream, *, trusted_direct=False):
    """Find the current cursor in direct, xdt or legacy edge payloads."""
    found = ""

    visited = 0
    decoded_strings = 0

    def walk(value, path=()):
        nonlocal found, visited, decoded_strings
        visited += 1
        if found or visited > 250000 or len(path) > 80:
            return
        if isinstance(value, (str, bytes)):
            if decoded_strings >= 100 or not (2 <= len(value) <= 4 * 1024 * 1024):
                return
            marker = value if isinstance(value, bytes) else value.encode("utf-8", errors="ignore")
            if not any(key in marker for key in (b"next_max_id", b"next_cursor", b"end_cursor")):
                return
            decoded_strings += 1
            for decoded in _json_values_from_text(value):
                walk(decoded, path + ("$json",))
            return
        if not isinstance(value, (dict, list)):
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (str(index),))
            return

        path_text = ".".join(path).lower()
        relationship_context = trusted_direct or any(
            marker in path_text for marker in RELATION_PATH_MARKERS
        )
        if relationship_context:
            for key in ("next_max_id", "next_cursor"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    found = str(candidate)
                    return
            page_info = value.get("page_info")
            if isinstance(page_info, dict) and page_info.get("has_next_page", True):
                candidate = page_info.get("end_cursor")
                if candidate not in (None, ""):
                    found = str(candidate)
                    return
        for key, child in value.items():
            walk(child, path + (str(key),))

    walk(payload)
    return found


def _json_values_from_text(value):
    """Decode JSON, anti-XSSI JSON and multipart/streamed JSON envelopes."""
    if isinstance(value, bytes):
        value = value[:16 * 1024 * 1024].decode("utf-8", errors="replace")
    text = str(value or "")[:16 * 1024 * 1024]
    decoder = json.JSONDecoder()
    values = []
    index = 0
    while index < len(text):
        brace = text.find("{", index)
        bracket = text.find("[", index)
        starts = [item for item in (brace, bracket) if item >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            decoded, end = decoder.raw_decode(text, start)
        except (TypeError, ValueError, json.JSONDecodeError):
            index = start + 1
            continue
        if isinstance(decoded, (dict, list)):
            values.append(decoded)
        index = max(end, start + 1)
    return values


def response_json_payloads(response):
    """Read a Playwright response, including Relay multipart/stream bodies."""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, (dict, list)):
        return [payload]
    try:
        return _json_values_from_text(response.body())
    except Exception:
        return []


def relationship_response_kind(response, stream):
    """Classify a response from its URL, operation body and safe name headers."""
    url = str(getattr(response, "url", "") or "").lower()
    request = getattr(response, "request", None)
    post_data = unquote_plus(str(getattr(request, "post_data", "") or "")).lower()
    headers = getattr(request, "headers", {}) or {}
    if callable(headers):
        try:
            headers = headers()
        except Exception:
            headers = {}
    if not isinstance(headers, dict):
        headers = {}
    headers = {str(key).lower(): value for key, value in headers.items()}
    named_headers = " ".join(str(headers.get(key, "")) for key in (
        "x-fb-friendly-name",
        "x-root-field-name",
        "x-fb-request-analytics-tags",
    )).lower()
    marker_text = unquote_plus(" ".join((url, post_data, named_headers)))
    endpoint = "followers" if stream == "followers" else "following"
    if "/friendships/" in url and f"/{endpoint}/" in url:
        return "direct"
    if "graphql" not in url:
        return ""
    expected_markers = (
        "followerslist" if stream == "followers" else "followinglist",
        "followersdialog" if stream == "followers" else "followingdialog",
        f"friendships__{endpoint}",
        "edge_followed_by" if stream == "followers" else "edge_follow",
        "followersquery" if stream == "followers" else "followingquery",
        "polarisfollowers" if stream == "followers" else "polarisfollowing",
    )
    return "graphql" if any(marker in marker_text for marker in expected_markers) else "possible_graphql"


class BrowserRelationshipCollector:
    """Stateful modal scroller. Cursor is a durable numeric result offset."""

    def __init__(self):
        self.sessions = {}

    def close_account(self, account_id):
        for key in [item for item in self.sessions if item[0] == account_id]:
            state = self.sessions.pop(key)
            try:
                state["page"].close()
            except Exception:
                pass

    def fetch(self, context, account_id, target, stream, cursor="", limit=50,
              target_pk="", expected_total=None):
        target = str(target or "").strip().lstrip("@").lower()
        if not USERNAME_RE.fullmatch(target):
            raise ValueError("Invalid target username")
        if stream not in ("followers", "following"):
            raise ValueError("Invalid relationship stream")
        raw_cursor = str(cursor or "")
        direct_cursor = ""
        cursor_mode = "initial"
        if raw_cursor.startswith("direct:"):
            direct_cursor = raw_cursor.removeprefix("direct:")
            if not direct_cursor:
                raise ValueError("Invalid browser cursor")
            cursor_mode = "direct"
            offset = 0
        elif raw_cursor.startswith("offset:"):
            cursor_mode = "offset"
            offset_value = raw_cursor.removeprefix("offset:")
            if not offset_value.isdecimal():
                raise ValueError("Invalid browser cursor")
            offset = int(offset_value)
        elif raw_cursor.isdecimal() or not raw_cursor:
            # Numeric cursors were emitted by versions up to 3.12.0.
            cursor_mode = "offset" if raw_cursor else "initial"
            offset = int(raw_cursor or 0)
        else:
            # Resume a native direct cursor written by an older hotfix build.
            if raw_cursor.startswith("-"):
                raise ValueError("Invalid browser cursor")
            direct_cursor = raw_cursor
            cursor_mode = "direct"
            offset = 0
        if offset < 0:
            raise ValueError("Invalid browser cursor")
        limit = max(1, min(int(limit or 50), 100))
        if expected_total in (None, ""):
            expected_total = None
        else:
            expected_total = max(0, int(expected_total))
            # Never request or accept more identities than Instagram's own
            # profile counter.  The web dialog appends Suggested accounts
            # after the actual relationship list, especially for small lists.
            remaining = max(0, expected_total - offset)
            if remaining == 0:
                return {"users": [], "cursor": "", "captured": offset,
                        "exhausted": True, "source": "expected-total",
                        "diagnostic": {"status": 200}}
            limit = min(limit, remaining)
        target_pk = str(target_pk or "")
        if target_pk and not target_pk.isdecimal():
            raise ValueError("Invalid target id")

        # Ask from inside the authenticated Instagram tab first.  This uses
        # the exact web cookies, CSRF state and browser fingerprint that make
        # the visible Followers/Following dialog work.  It fixes the common
        # case where aiograpi receives HTTP 200 with an intentionally empty
        # users array for the same endpoint.
        direct = (
            self._browser_fetch(
                context, target, target_pk, stream, direct_cursor, limit
            )
            if cursor_mode != "offset"
            else {
                "users": [], "cursor": "", "captured": 0,
                "exhausted": False, "source": "instagram-browser-fetch",
                "diagnostic": {"status": 0},
            }
        )
        if direct["users"]:
            return direct
        direct_diagnostic = direct.get("diagnostic") or {}
        key = (account_id, target, stream)
        state = self.sessions.get(key)
        if state is None or state["page"].is_closed() or offset == 0:
            if state is not None:
                try:
                    state["page"].close()
                except Exception:
                    pass
            state = self._open(context, target, stream)
            self.sessions[key] = state
        state["diagnostics"]["direct_status"] = direct_diagnostic.get("status", 0)
        self._harvest_dom(state, stream, expected_total=expected_total)

        required = offset + limit
        stalls = 0
        for _ in range(60):
            if len(state["users"]) >= required or state["exhausted"]:
                break
            before = len(state["users"])
            scroll = state["dialog"].evaluate("""root => {
                const nodes = [root, ...root.querySelectorAll('div')];
                const candidates = nodes.filter(n => n.scrollHeight > n.clientHeight + 8);
                let moved = false;
                let result = {oldTop: 0, top: 0, height: root.scrollHeight, client: root.clientHeight};
                for (const box of candidates.sort((a,b) => b.scrollHeight - a.scrollHeight)) {
                    const oldTop = box.scrollTop;
                    const step = Math.max(box.clientHeight * 0.85, 480);
                    box.scrollTop = Math.min(box.scrollHeight, oldTop + step);
                    box.dispatchEvent(new Event('scroll', {bubbles: true}));
                    box.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles: true}));
                    if (box.scrollTop > oldTop) {
                        moved = true;
                        result = {oldTop, top: box.scrollTop, height: box.scrollHeight, client: box.clientHeight};
                    }
                }
                const anchors = root.querySelectorAll('a[href]');
                if (anchors.length) anchors[anchors.length - 1].scrollIntoView({block: 'end'});
                return {...result, moved};
            }""")
            state["page"].wait_for_timeout(1400)
            self._harvest_dom(state, stream, expected_total=expected_total)
            if len(state["users"]) == before:
                stalls += 1
            else:
                stalls = 0
            at_end = (scroll.get("top", 0) + scroll.get("client", 0)
                      >= scroll.get("height", 0) - 4)
            if stalls >= 7 and at_end and not scroll.get("moved", False):
                state["exhausted"] = True
                break

        values = list(state["users"].values())
        if expected_total is not None:
            values = values[:expected_total]
        batch = values[offset:required]
        if not batch:
            diagnostic = state["diagnostics"]
            raise RuntimeError(
                "BrowserRelationshipResponseEmpty:"
                f"responses={diagnostic['responses']},"
                f"json={diagnostic['json_payloads']},"
                f"errors={diagnostic['capture_errors']},"
                f"active={diagnostic['active_responses']},"
                f"possible={diagnostic['possible_graphql']},"
                f"dom={diagnostic['dom_matched']}/{diagnostic['dom_handles']},"
                f"kind={diagnostic['last_kind'] or 'none'},"
                f"status={diagnostic['last_status'] or diagnostic['direct_status']}"
            )
        end = offset + len(batch)
        next_cursor = (
            f"offset:{end}"
            if (expected_total is None or end < expected_total) and (
                end < len(values) or not state["exhausted"])
            else ""
        )
        return {
            "users": batch,
            "cursor": next_cursor,
            "captured": len(values),
            "exhausted": state["exhausted"],
            "source": "instagram-browser-network",
            "diagnostic": dict(state["diagnostics"]),
        }

    def _harvest_dom(self, state, stream, *, expected_total=None):
        """Read stable user ids from React props attached to visible list rows.

        Instagram sometimes renders a relationship response through Relay but
        exposes only a generic streamed envelope to Playwright.  The visible
        row still carries the same user object in React props.  This fallback
        never invents an id from a username and ignores plain DOM handles that
        do not include Instagram's numeric user id.
        """
        diagnostics = state["diagnostics"]
        diagnostics["dom_scans"] += 1
        try:
            result = state["dialog"].evaluate("""root => {
                const usernamePattern = /^[A-Za-z0-9._]{1,100}$/;
                const excluded = new Set([
                    'accounts', 'direct', 'explore', 'p', 'reel', 'reels',
                    'stories', 'about', 'legal', 'web', 'challenge'
                ]);
                const idKeys = ['pk', 'pk_id', 'strong_id__', 'rest_id',
                    'user_id', 'instagram_id', 'id'];
                const output = new Map();
                let handles = 0;
                let inspected = 0;
                let budget = 140000;

                const textValue = value => {
                    if (typeof value === 'string' || typeof value === 'bigint') return String(value);
                    if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
                    return '';
                };
                const add = (value, expected) => {
                    if (!value || typeof value !== 'object') return;
                    const username = String(value.username || '').replace(/^@/, '').trim().toLowerCase();
                    if (!usernamePattern.test(username) || username !== expected) return;
                    let pk = '';
                    for (const key of idKeys) {
                        pk = textValue(value[key]);
                        if (/^[0-9]+$/.test(pk)) break;
                        pk = '';
                    }
                    if (!pk) return;
                    let picture = value.profile_pic_url || value.profile_pic_url_hd || '';
                    if (picture && typeof picture === 'object') picture = picture.url || '';
                    if (!output.has(pk)) output.set(pk, {
                        pk,
                        username,
                        full_name: String(value.full_name || '').slice(0, 250),
                        is_private: Boolean(value.is_private),
                        is_verified: Boolean(value.is_verified),
                        profile_pic_url: String(picture || '')
                    });
                };
                const walk = (value, expected, depth, seen) => {
                    if (budget-- <= 0 || depth > 6 || value === null || typeof value !== 'object') return;
                    if (seen.has(value)) return;
                    seen.add(value);
                    inspected += 1;
                    add(value, expected);
                    if (value instanceof Node) return;
                    let entries;
                    try { entries = Object.entries(value).slice(0, 60); }
                    catch (_) { return; }
                    for (const [key, child] of entries) {
                        if (['return', 'child', 'sibling', 'stateNode', '_owner'].includes(key)) continue;
                        walk(child, expected, depth + 1, seen);
                    }
                };

                for (const anchor of root.querySelectorAll('a[href]')) {
                    let parts = [];
                    try { parts = new URL(anchor.href, location.origin).pathname.split('/').filter(Boolean); }
                    catch (_) { continue; }
                    if (parts.length !== 1) continue;
                    const expected = String(parts[0] || '').toLowerCase();
                    if (!usernamePattern.test(expected) || excluded.has(expected)) continue;
                    handles += 1;
                    let element = anchor;
                    for (let parentDepth = 0; element && parentDepth < 7; parentDepth++, element = element.parentElement) {
                        let keys = [];
                        try { keys = Object.getOwnPropertyNames(element); } catch (_) {}
                        for (const key of keys) {
                            if (key.startsWith('__reactProps$')) {
                                walk(element[key], expected, 0, new WeakSet());
                            } else if (key.startsWith('__reactFiber$')) {
                                let fiber = element[key];
                                for (let level = 0; fiber && level < 10; level++, fiber = fiber.return) {
                                    walk(fiber.memoizedProps, expected, 0, new WeakSet());
                                    walk(fiber.pendingProps, expected, 0, new WeakSet());
                                }
                            }
                        }
                    }
                    if (budget <= 0) break;
                }
                return {users: Array.from(output.values()), handles, inspected};
            }""")
            if not isinstance(result, dict):
                return
            diagnostics["dom_handles"] = max(
                diagnostics["dom_handles"], int(result.get("handles") or 0))
            diagnostics["dom_inspected"] += int(result.get("inspected") or 0)
            normalized = extract_relationship_users(
                {"users": result.get("users") or []}, stream, trusted_direct=True)
            for user in normalized:
                if user["username"] == state.get("target"):
                    continue
                if expected_total is not None and len(state["users"]) >= expected_total:
                    break
                state["users"].setdefault(user["pk"], user)
            diagnostics["dom_matched"] = len(state["users"])
        except Exception:
            diagnostics["dom_errors"] += 1

    def _browser_fetch(self, context, target, target_pk, stream, cursor, limit):
        if not target_pk:
            return {"users": [], "cursor": "", "captured": 0,
                    "exhausted": False, "source": "instagram-browser-fetch",
                    "diagnostic": {"status": 0}}
        pages = list(context.pages)
        page = pages[0] if pages else context.new_page()
        page.goto(f"https://www.instagram.com/{target}/", wait_until="domcontentloaded",
                  timeout=45000)
        if ("/accounts/login" in page.url or
                page.locator('input[name="username"]').count()):
            raise RuntimeError("BrowserInstagramLoginRequired")
        endpoint = "followers" if stream == "followers" else "following"
        result = page.evaluate("""async ({targetPk, endpoint, cursor, limit}) => {
            const params = new URLSearchParams({
                count: String(limit),
                search_surface: 'follow_list_page',
                query: '',
                enable_groups: 'true'
            });
            if (cursor) params.set('max_id', cursor);
            const csrf = document.cookie.split('; ').find(v => v.startsWith('csrftoken='));
            const response = await fetch(
                `/api/v1/friendships/${targetPk}/${endpoint}/?${params.toString()}`,
                {
                    credentials: 'include',
                    headers: {
                        'X-IG-App-ID': '936619743392459',
                        'X-ASBD-ID': '129477',
                        'X-Requested-With': 'XMLHttpRequest',
                        ...(csrf ? {'X-CSRFToken': decodeURIComponent(csrf.split('=').slice(1).join('='))} : {})
                    }
                }
            );
            let payload = null;
            try { payload = await response.json(); } catch (_) {}
            return {status: response.status, payload};
        }""", {"targetPk": target_pk, "endpoint": endpoint,
                "cursor": str(cursor or ""), "limit": limit})
        payload = result.get("payload") if isinstance(result, dict) else None
        status = int(result.get("status") or 0) if isinstance(result, dict) else 0
        if not isinstance(payload, (dict, list)):
            return {"users": [], "cursor": "", "captured": 0,
                    "exhausted": False, "source": "instagram-browser-fetch",
                    "diagnostic": {"status": status}}
        users = extract_relationship_users(payload, stream, trusted_direct=True)
        next_cursor = extract_relationship_cursor(payload, stream, trusted_direct=True)
        durable_cursor = f"direct:{next_cursor}" if next_cursor else ""
        return {"users": users, "cursor": durable_cursor, "captured": len(users),
                "exhausted": not bool(next_cursor), "source": "instagram-browser-fetch",
                "diagnostic": {"status": status}}

    def _open(self, context, target, stream):
        page = context.new_page()
        state = {
            "page": page,
            "target": target,
            "dialog": None,
            "users": OrderedDict(),
            "exhausted": False,
            "capture_active": False,
            "diagnostics": {
                "responses": 0,
                "json_payloads": 0,
                "capture_errors": 0,
                "active_responses": 0,
                "possible_graphql": 0,
                "direct_status": 0,
                "last_status": 0,
                "last_kind": "",
                "dom_scans": 0,
                "dom_handles": 0,
                "dom_inspected": 0,
                "dom_matched": 0,
                "dom_errors": 0,
            },
        }

        def capture(response):
            try:
                kind = relationship_response_kind(response, stream)
                if not kind:
                    return
                status = int(getattr(response, "status", 0) or 0)
                payloads = response_json_payloads(response)
                diagnostics = state["diagnostics"]
                diagnostics["responses"] += 1
                diagnostics["json_payloads"] += len(payloads)
                diagnostics["last_status"] = status
                diagnostics["last_kind"] = kind
                if state["capture_active"]:
                    diagnostics["active_responses"] += 1
                if kind == "possible_graphql":
                    diagnostics["possible_graphql"] += 1

                matched = 0
                for payload in payloads:
                    users = extract_relationship_users(
                        payload,
                        stream,
                        trusted_direct=(
                            kind in ("direct", "graphql")
                            or (state["capture_active"] and kind == "possible_graphql")
                        ),
                    )
                    matched += len(users)
                    for user in users:
                        state["users"].setdefault(user["pk"], user)

                # Only structural counters and the URL path enter the private
                # journal.  Response bodies, query variables and headers never
                # do, so the diagnostic is safe to surface in a job event.
                url = str(getattr(response, "url", "") or "")
                print(
                    "REL_RESPONSE",
                    "stream=", stream,
                    "status=", status,
                    "kind=", kind,
                    "path=", urlsplit(url).path[:240],
                    "payloads=", len(payloads),
                    "matched=", matched,
                    "captured=", len(state["users"]),
                    flush=True,
                )

            except Exception as exc:
                state["diagnostics"]["capture_errors"] += 1
                print(
                    "REL_CAPTURE_ERROR",
                    "stream=", stream,
                    "type=", type(exc).__name__,
                    flush=True,
                )

        page.on("response", capture)
        page.goto(f"https://www.instagram.com/{target}/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        if ("/accounts/login" in page.url or
                page.locator('input[name="username"]').count()):
            page.close()
            raise RuntimeError("BrowserInstagramLoginRequired")
        suffix = "followers" if stream == "followers" else "following"

        # Instagram changes trailing slashes and may add query parameters or
        # client-side routing attributes. Match the relationship path rather
        # than one exact href. If the profile link is not rendered, the direct
        # relationship route remains the same route used by Instagram Web.
        link = page.locator(f'a[href*="/{suffix}"]').first
        try:
            link.wait_for(state="visible", timeout=8000)
            state["capture_active"] = True
            link.click(timeout=15000)
        except Exception:
            state["capture_active"] = True
            page.goto(
                f"https://www.instagram.com/{target}/{suffix}/",
                wait_until="domcontentloaded", timeout=45000)

        page.wait_for_timeout(1800)
        if ("/accounts/login" in page.url or
                page.locator('input[name="username"]').count()):
            page.close()
            raise RuntimeError("BrowserInstagramLoginRequired")

        dialog = page.locator('div[role="dialog"]').last
        try:
            dialog.wait_for(state="visible", timeout=12000)
            state["dialog"] = dialog
        except Exception:
            # Some Instagram builds render /followers and /following as a full
            # page instead of role=dialog. Accept it only while the route still
            # identifies the requested list, otherwise scrolling <main> could
            # accidentally capture the profile timeline.
            current = page.url.lower().split("?", 1)[0].rstrip("/")
            if not current.endswith(f"/{suffix}"):
                page.close()
                raise RuntimeError("RelationshipListNotFound")
            main = page.locator("main").first
            try:
                main.wait_for(state="visible", timeout=8000)
            except Exception:
                page.close()
                raise RuntimeError("RelationshipListNotFound")
            state["dialog"] = main

        page.wait_for_timeout(1500)
        return state
