"""A local, authenticated browser RPC. Raw cookies never enter the HTTP response."""
import os
from multiprocessing.connection import Client
from django.conf import settings


class BrowserCallError(RuntimeError):
    """A controlled browser-daemon error code safe for structured job logs."""


def call(action, account_id=None, **payload):
    auth = os.environ.get("BROWSER_AUTH_KEY", "").encode()
    if len(auth) < 32:
        raise RuntimeError("BROWSER_AUTH_KEY must have at least 32 characters")
    with Client(settings.INSTRADAR_BROWSER_SOCKET, family="AF_UNIX", authkey=auth) as conn:
        conn.send({"action": action, "account_id": account_id, "payload": payload})
        response = conn.recv()
    if not response.get("ok"):
        raise BrowserCallError(response.get("error", "BrowserUnavailable"))
    return response
