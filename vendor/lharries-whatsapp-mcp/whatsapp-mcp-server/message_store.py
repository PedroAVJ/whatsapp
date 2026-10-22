"""Read WhatsApp data from the bridge's local SQLite store (messages.db).

The bridge writes chats, messages, reactions, receipts, media metadata, and
the account's LID/phone identity map into messages.db in its store directory
(WHATSAPP_MCP_STORE_DIR, by default
~/.local/share/codex-whatsapp/upstream-store). The CLI and MCP backend read it
through `query(name, args)`, one named read per question they ask, each
returning plain JSON-shaped rows. Times are epoch milliseconds.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = 2


class StoreError(RuntimeError):
    """A store read failed: messages.db is missing, unreadable, or the read errored."""


def store_dir() -> Path:
    configured = os.environ.get("WHATSAPP_MCP_STORE_DIR")
    if configured:
        return Path(configured).expanduser()
    state_root = os.environ.get("WHATSAPP_PLUGIN_STATE_ROOT", "~/.local/share/codex-whatsapp")
    return Path(state_root).expanduser() / "upstream-store"


def messages_db_path() -> Path:
    return store_dir() / "messages.db"


def _contains(text: Optional[str], needle: Optional[str]) -> int:
    if not needle:
        return 1
    return int(needle.lower() in (text or "").lower())


def connect() -> sqlite3.Connection:
    """A read-only connection; the bridge is the only writer."""
    path = messages_db_path()
    if not path.is_file():
        raise StoreError(f"WhatsApp message store not found at {path}; start the bridge first.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.create_function("wa_contains", 2, _contains, deterministic=True)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        raise StoreError(f"Could not open the WhatsApp message store at {path}: {exc}") from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(
            f"The WhatsApp message store at {path} has schema version {version}, not {SCHEMA_VERSION}; "
            "convert it with scripts/migrate_store.py."
        )
    return conn


# ---- row shapes (the same JSON the CLI and MCP backend have always consumed) ----

def _chat_name(conn, jid, names):
    if jid not in names:
        row = conn.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
        names[jid] = row["name"] if row else None
    return names[jid]


def _some(value):
    return None if value in (None, "") else value


def _message_row(conn, m, names) -> dict:
    target = None
    if m["reply_to_message_id"]:
        target = conn.execute(
            "SELECT sender, content, media_type FROM messages WHERE chat_jid = ? AND id = ?",
            (m["chat_jid"], m["reply_to_message_id"]),
        ).fetchone()
    reactions = conn.execute(
        """SELECT * FROM message_reactions WHERE chat_jid = ? AND target_message_id = ? AND emoji != ''
        ORDER BY coalesce(timestamp, 0), reaction_sender""",
        (m["chat_jid"], m["id"]),
    ).fetchall()
    receipts = conn.execute(
        """SELECT * FROM message_receipts WHERE chat_jid = ? AND message_id = ?
        ORDER BY coalesce(timestamp, 0), receipt_sender""",
        (m["chat_jid"], m["id"]),
    ).fetchall()
    return {
        "timestamp": m["timestamp"],
        "sender": m["sender"],
        "chatName": _chat_name(conn, m["chat_jid"], names),
        "content": m["content"],
        "isFromMe": bool(m["is_from_me"]),
        "chatJid": m["chat_jid"],
        "id": m["id"],
        "mediaType": m["media_type"],
        "replyToMessageId": m["reply_to_message_id"],
        "replySender": _some(target["sender"] if target else None) or m["reply_to_sender"],
        "replyContent": _some(target["content"] if target else None) or m["reply_to_content"],
        "replyMediaType": _some(target["media_type"] if target else None) or m["reply_to_media_type"],
        "editedAt": m["edited_at"],
        "reactions": [
            {
                "sender": r["reaction_sender"],
                "emoji": r["emoji"],
                "timestamp": r["timestamp"],
                "reactionMessageId": r["reaction_message_id"],
                "targetMessageId": r["target_message_id"],
                "targetSender": r["target_sender"] or None,
                "groupingKey": r["grouping_key"],
                "senderTimestampMs": r["sender_timestamp_ms"],
                "isFromMe": bool(r["is_from_me"]),
            }
            for r in reactions
        ],
        "receipts": [
            {
                "sender": r["receipt_sender"],
                "type": r["receipt_type"],
                "timestamp": r["timestamp"],
                "messageId": r["message_id"],
                "chatJid": r["chat_jid"],
                "messageSender": r["message_sender"] or None,
            }
            for r in receipts
        ],
    }


def _message_rows(conn, rows) -> list:
    names: dict = {}
    return [_message_row(conn, m, names) for m in rows]


def _chat_row(conn, c, with_last: bool) -> dict:
    last = None
    if with_last:
        last = conn.execute(
            "SELECT content, sender, is_from_me FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 1",
            (c["jid"],),
        ).fetchone()
    return {
        "jid": c["jid"],
        "name": c["name"],
        "lastMessageTime": c["last_message_time"],
        "lastMessage": last["content"] if last else None,
        "lastSender": last["sender"] if last else None,
        "lastIsFromMe": bool(last["is_from_me"]) if last else None,
    }


# ---- reads ----

def _identity(conn) -> dict:
    self_row = conn.execute("SELECT jid FROM self WHERE id = 1").fetchone()
    return {
        "chats": [
            {"jid": c["jid"], "name": c["name"]}
            for c in conn.execute("SELECT jid, name FROM chats WHERE name IS NOT NULL AND name != ''")
        ],
        "lids": [{"lid": r["lid"], "pn": r["pn"]} for r in conn.execute("SELECT lid, pn FROM lid_map")],
        "self": self_row["jid"] if self_row else None,
    }


def _messages(conn, limit, offset, after=None, before=None, sender=None, chatJids=None, query=None) -> list:
    """Messages newest first, filtered by time range (exclusive), sender, chats, and text, paged by offset."""
    clauses, params = [], []
    if after is not None:
        clauses.append("timestamp > ?")
        params.append(after)
    if before is not None:
        clauses.append("timestamp < ?")
        params.append(before)
    if sender:
        clauses.append("sender = ?")
        params.append(sender)
    if chatJids is not None:
        if not chatJids:
            return []
        clauses.append(f"chat_jid IN ({','.join('?' * len(chatJids))})")
        params.extend(chatJids)
    if query:
        clauses.append("wa_contains(content, ?)")
        params.append(query)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()
    return _message_rows(conn, rows)


def _context(conn, id, before, after):
    m = conn.execute("SELECT * FROM messages WHERE id = ? ORDER BY timestamp DESC LIMIT 1", (id,)).fetchone()
    if not m:
        return None
    earlier = conn.execute(
        "SELECT * FROM messages WHERE chat_jid = ? AND timestamp < ? ORDER BY timestamp DESC LIMIT ?",
        (m["chat_jid"], m["timestamp"], int(before)),
    ).fetchall()
    later = conn.execute(
        "SELECT * FROM messages WHERE chat_jid = ? AND timestamp > ? ORDER BY timestamp ASC LIMIT ?",
        (m["chat_jid"], m["timestamp"], int(after)),
    ).fetchall()
    names: dict = {}
    return {
        "message": _message_row(conn, m, names),
        "before": [_message_row(conn, x, names) for x in earlier],
        "after": [_message_row(conn, x, names) for x in later],
    }


def _chats(conn, includeLast) -> list:
    """Every chat, newest activity first (chats without a time last)."""
    rows = conn.execute(
        "SELECT * FROM chats ORDER BY last_message_time IS NULL, last_message_time DESC"
    ).fetchall()
    return [_chat_row(conn, c, includeLast) for c in rows]


def _chat(conn, jid, includeLast):
    c = conn.execute("SELECT * FROM chats WHERE jid = ?", (jid,)).fetchone()
    return _chat_row(conn, c, includeLast) if c else None


def _contacts(conn, query) -> list:
    """Direct chats (not groups) whose name or JID contains the text."""
    rows = conn.execute(
        """SELECT jid, name FROM chats
        WHERE jid NOT LIKE '%@g.us' AND (wa_contains(name, ?) OR wa_contains(jid, ?))
        ORDER BY coalesce(name, ''), jid LIMIT 50""",
        (query, query),
    ).fetchall()
    return [{"jid": r["jid"], "name": r["name"]} for r in rows]


def _direct_chat(conn, phone):
    c = conn.execute(
        "SELECT * FROM chats WHERE instr(jid, ?) > 0 AND jid NOT LIKE '%@g.us' LIMIT 1", (phone,)
    ).fetchone()
    return _chat_row(conn, c, True) if c else None


def _contact_chats(conn, jid, limit, offset) -> list:
    """Chats involving a contact: their own chat and every chat where they sent a message."""
    rows = conn.execute(
        """SELECT * FROM chats WHERE jid = ? OR jid IN (SELECT DISTINCT chat_jid FROM messages WHERE sender = ?)
        ORDER BY coalesce(last_message_time, 0) DESC LIMIT ? OFFSET ?""",
        (jid, jid, int(limit), int(offset)),
    ).fetchall()
    return [_chat_row(conn, c, True) for c in rows]


def _last_interaction(conn, jid):
    """The contact's latest message, sent by them or in their chat."""
    m = conn.execute(
        "SELECT * FROM messages WHERE sender = ? OR chat_jid = ? ORDER BY timestamp DESC LIMIT 1", (jid, jid)
    ).fetchone()
    return _message_row(conn, m, {}) if m else None


def _sender_name(conn, jid):
    """A sender's display name: their chat's name, or a chat whose JID contains their number."""
    exact = conn.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
    if exact and exact["name"]:
        return exact["name"]
    phone = jid.split("@")[0]
    like = conn.execute("SELECT name FROM chats WHERE instr(jid, ?) > 0 LIMIT 1", (phone,)).fetchone()
    return (like["name"] if like else None) or jid


_QUERIES: dict[str, Callable[..., Any]] = {
    "identity": _identity,
    "messages": _messages,
    "context": _context,
    "chats": _chats,
    "chat": _chat,
    "contacts": _contacts,
    "directChat": _direct_chat,
    "contactChats": _contact_chats,
    "lastInteraction": _last_interaction,
    "senderName": _sender_name,
}


def query(name: str, args: Optional[dict] = None) -> Any:
    """Run the named read against messages.db and return its value."""
    handler = _QUERIES.get(name)
    if handler is None:
        raise StoreError(f"Unknown WhatsApp store read: {name}")
    conn = connect()
    try:
        return handler(conn, **(args or {}))
    except sqlite3.Error as exc:
        raise StoreError(f"WhatsApp store read {name} failed: {exc}") from exc
    finally:
        conn.close()


def from_ms(value: Optional[float]) -> Optional[datetime]:
    """Epoch milliseconds to a timezone-aware local datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000).astimezone()


def to_ms(value: datetime) -> int:
    """A datetime (naive means local time) to epoch milliseconds."""
    return int(value.astimezone().timestamp() * 1000)
