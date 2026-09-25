"""An in-process stand-in for Convex's HTTP query API, for tests.

`install(handlers)` patches urllib so POST {base}/api/query is answered by
`handlers[<function name>](args)` instead of a live deployment. Each request
is recorded in the returned list as (function name, args without the token).
"""

import io
import json
import os
import urllib.error
import urllib.request
from unittest import mock

TEST_TOKEN = "test-read-token"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def install(handlers):
    os.environ["WHATSAPP_CONVEX_URL"] = "http://convex.test"
    os.environ["WHATSAPP_CONVEX_READ_TOKEN"] = TEST_TOKEN
    calls = []

    def urlopen(request, timeout=None):
        assert request.full_url == "http://convex.test/api/query", request.full_url
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["format"] == "json"
        args = dict(payload["args"])
        if args.pop("token", None) != TEST_TOKEN:
            body = {"status": "error", "errorMessage": "Unauthorized"}
        else:
            module, _, name = payload["path"].partition(":")
            assert module == "whatsapp", payload["path"]
            calls.append((name, args))
            handler = handlers.get(name)
            if handler is None:
                body = {"status": "error", "errorMessage": f"Could not find function for '{payload['path']}'"}
            else:
                body = {"status": "success", "value": handler(args)}
        return _Response(json.dumps(body).encode("utf-8"))

    patcher = mock.patch.object(urllib.request, "urlopen", urlopen)
    patcher.start()
    return calls


def ms(iso):
    """An ISO-8601 time as Convex epoch milliseconds."""
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp() * 1000


def message_row(**fields):
    row = {
        "timestamp": None,
        "sender": "",
        "chatName": None,
        "content": "",
        "isFromMe": False,
        "chatJid": "",
        "id": "",
        "mediaType": None,
        "replyToMessageId": None,
        "replySender": None,
        "replyContent": None,
        "replyMediaType": None,
        "editedAt": None,
        "reactions": [],
        "receipts": [],
    }
    row.update(fields)
    return row
