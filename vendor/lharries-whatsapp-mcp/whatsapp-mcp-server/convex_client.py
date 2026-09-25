"""Read WhatsApp data from Near's Convex deployment over its HTTP query API.

The bridge writes chats, messages, reactions, receipts, and the account's
LID/phone identity map into Convex; the CLI and MCP read them back through the
`whatsapp:*` query functions. Times cross the wire as epoch milliseconds.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

DEFAULT_CONVEX_URL = "http://127.0.0.1:3210"
KEYCHAIN_ACCOUNT = "near"
KEYCHAIN_SERVICE = "n4.convex-read"

_keychain_token: Optional[str] = None


class ConvexError(RuntimeError):
    """A Convex read failed: unreachable, unauthorized, or the query errored."""


def convex_url() -> str:
    return os.environ.get("WHATSAPP_CONVEX_URL", DEFAULT_CONVEX_URL).rstrip("/")


def read_token() -> str:
    global _keychain_token
    token = os.environ.get("WHATSAPP_CONVEX_READ_TOKEN")
    if token:
        return token
    if _keychain_token is None:
        try:
            process = subprocess.run(
                ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConvexError(f"Could not read the Convex read token from the macOS Keychain: {exc}") from exc
        if process.returncode != 0 or not process.stdout.strip():
            raise ConvexError(
                "No Convex read token: set WHATSAPP_CONVEX_READ_TOKEN or store it in the macOS Keychain "
                f"(account {KEYCHAIN_ACCOUNT}, service {KEYCHAIN_SERVICE})."
            )
        _keychain_token = process.stdout.strip()
    return _keychain_token


def query(name: str, args: Optional[dict] = None, timeout: float = 60) -> Any:
    """Run the Convex query `whatsapp:<name>` and return its value."""
    path = f"whatsapp:{name}"
    payload = {"path": path, "args": {**(args or {}), "token": read_token()}, "format": "json"}
    request = urllib.request.Request(
        f"{convex_url()}/api/query",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            detail = json.loads(body)
        except ValueError:
            detail = None
        if not (isinstance(detail, dict) and detail.get("status") == "error"):
            message = detail.get("message") if isinstance(detail, dict) else None
            raise ConvexError(f"Convex query {path} failed with HTTP {exc.code}: {message or exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConvexError(f"Convex at {convex_url()} is not reachable: {reason}") from exc

    try:
        result = json.loads(body)
    except ValueError as exc:
        raise ConvexError(f"Convex query {path} returned a non-JSON response.") from exc
    if not isinstance(result, dict) or result.get("status") != "success":
        message = result.get("errorMessage") if isinstance(result, dict) else None
        raise ConvexError(f"Convex query {path} failed: {message or result}")
    return result.get("value")


def from_ms(value: Optional[float]) -> Optional[datetime]:
    """Epoch milliseconds to a timezone-aware local datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000).astimezone()


def to_ms(value: datetime) -> int:
    """A datetime (naive means local time) to epoch milliseconds."""
    return int(value.astimezone().timestamp() * 1000)
