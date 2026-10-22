"""An in-process stand-in for the bridge's message store reads, for tests.

`install(handlers)` replaces `message_store.query` so each named read is
answered by `handlers[<name>](args)` instead of messages.db. Each read is
recorded in the returned list as (name, args). The SQL behind the reads is
covered against a real SQLite file in tests/test_message_store.py.
"""

import message_store

def install(handlers):
    calls = []

    def query(name, args=None):
        args = dict(args or {})
        calls.append((name, args))
        handler = handlers.get(name)
        if handler is None:
            raise message_store.StoreError(f"Unknown WhatsApp store read: {name}")
        return handler(args)

    message_store.query = query
    return calls


def ms(iso):
    """An ISO-8601 time as epoch milliseconds."""
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
