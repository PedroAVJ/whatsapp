import sqlite3
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

DEFAULT_MESSAGES_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..',
    'whatsapp-bridge',
    'store',
    'messages.db',
)
MESSAGES_DB_PATH = os.environ.get("WHATSAPP_MCP_MESSAGES_DB_PATH", DEFAULT_MESSAGES_DB_PATH)
WHATSAPP_API_BASE_URL = os.environ.get("WHATSAPP_MCP_API_BASE_URL", "http://127.0.0.1:8080/api")


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _jid_user(jid):
    if not isinstance(jid, str) or not jid:
        return None
    return jid.split("@", 1)[0].split(":", 1)[0]


def _is_numeric_label(value):
    return isinstance(value, str) and value.isdigit()


def _parse_optional_datetime(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _load_identity_context():
    context = {
        "lid_to_phone": {},
        "phone_to_lid": {},
        "contact_names": {},
        "chat_names": {},
    }

    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT jid, name FROM chats WHERE name IS NOT NULL AND name != ''")
        for jid, name in cursor.fetchall():
            if isinstance(jid, str) and isinstance(name, str) and name:
                context["chat_names"][jid] = name
    except sqlite3.Error:
        pass
    finally:
        if 'conn' in locals():
            conn.close()

    whatsapp_db_path = os.path.join(os.path.dirname(MESSAGES_DB_PATH), "whatsapp.db")
    if not os.path.exists(whatsapp_db_path):
        return context

    try:
        conn = sqlite3.connect(whatsapp_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT lid, pn FROM whatsmeow_lid_map")
        for lid, phone in cursor.fetchall():
            lid_user = _jid_user(lid)
            phone_user = _jid_user(phone)
            if lid_user and phone_user and phone_user.isdigit():
                context["lid_to_phone"][lid_user] = phone_user
                context["phone_to_lid"][phone_user] = lid_user

        cursor.execute(
            """
            SELECT their_jid, full_name, first_name, business_name, push_name
            FROM whatsmeow_contacts
            """
        )
        for their_jid, full_name, first_name, business_name, push_name in cursor.fetchall():
            name = _first_text(full_name, first_name, business_name, push_name)
            if isinstance(their_jid, str) and name:
                context["contact_names"][their_jid] = name
    except sqlite3.Error:
        pass
    finally:
        if 'conn' in locals():
            conn.close()

    return context


def _equivalent_direct_chat_jids(chat_jid, context):
    if not isinstance(chat_jid, str) or not chat_jid:
        return []

    equivalents = [chat_jid]
    if chat_jid.endswith("@lid"):
        mapped_phone = context["lid_to_phone"].get(_jid_user(chat_jid) or "")
        if mapped_phone:
            equivalents.append(f"{mapped_phone}@s.whatsapp.net")
    elif chat_jid.endswith("@s.whatsapp.net"):
        phone = _jid_user(chat_jid)
        mapped_lid = context.get("phone_to_lid", {}).get(phone or "")
        if mapped_lid:
            equivalents.append(f"{mapped_lid}@lid")

    deduped = []
    seen = set()
    for equivalent in equivalents:
        if equivalent in seen:
            continue
        deduped.append(equivalent)
        seen.add(equivalent)
    return deduped


def _resolved_chat_name(jid, name, context):
    if not isinstance(jid, str):
        return name

    if jid.endswith("@lid"):
        lid_user = _jid_user(jid)
        mapped_phone = context["lid_to_phone"].get(lid_user or "")
        if mapped_phone:
            phone_jid = f"{mapped_phone}@s.whatsapp.net"
            return _first_text(
                context["chat_names"].get(phone_jid),
                context["contact_names"].get(phone_jid),
                context["contact_names"].get(jid),
                None if _is_numeric_label(name) else name,
                mapped_phone,
            )

    if jid.endswith("@s.whatsapp.net"):
        return _first_text(context["contact_names"].get(jid), name)

    return name


def _chat_matches_query(chat, query, context):
    if not query:
        return True
    needle = query.casefold().strip()
    jid = chat.jid
    phone_number = None
    if isinstance(jid, str) and jid.endswith("@lid"):
        phone_number = context["lid_to_phone"].get(_jid_user(jid) or "")
    elif isinstance(jid, str) and jid.endswith("@s.whatsapp.net"):
        phone_number = _jid_user(jid)

    fields = [chat.name, chat.jid, phone_number]
    return any(isinstance(value, str) and needle in value.casefold() for value in fields)


@dataclass
class MessageReaction:
    sender: str
    emoji: str
    timestamp: Optional[datetime] = None
    reaction_message_id: Optional[str] = None
    target_message_id: Optional[str] = None
    target_sender: Optional[str] = None
    grouping_key: Optional[str] = None
    sender_timestamp_ms: Optional[int] = None
    is_from_me: Optional[bool] = None


@dataclass
class MessageReceipt:
    sender: str
    type: str
    timestamp: Optional[datetime] = None
    message_id: Optional[str] = None
    chat_jid: Optional[str] = None
    message_sender: Optional[str] = None


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    reply_to_sender: Optional[str] = None
    reply_preview: Optional[str] = None
    reply_media_type: Optional[str] = None
    # Set when the sender revised this message after sending it. The content
    # above is the current text; the text originally sent is not retained.
    edited_at: Optional[datetime] = None
    reactions: List[MessageReaction] = field(default_factory=list)
    receipts: List[MessageReceipt] = field(default_factory=list)
    seen_by: List[MessageReceipt] = field(default_factory=list)

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))

        result = cursor.fetchone()

        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid

            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))

            result = cursor.fetchone()

        if result and result[0]:
            return result[0]
        else:
            return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

MESSAGE_SELECT_COLUMNS = """
    {alias}.timestamp,
    {alias}.sender,
    chats.name,
    {alias}.content,
    {alias}.is_from_me,
    chats.jid,
    {alias}.id,
    {alias}.media_type,
    {alias}.reply_to_message_id,
    COALESCE(NULLIF(reply_target.sender, ''), NULLIF({alias}.reply_to_sender, '')),
    COALESCE(NULLIF(reply_target.content, ''), NULLIF({alias}.reply_to_content, '')),
    COALESCE(NULLIF(reply_target.media_type, ''), NULLIF({alias}.reply_to_media_type, '')),
    {edited_at}
"""


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


def message_select_columns(alias: str = "messages", has_edited_at: bool = True) -> str:
    # A store written by a bridge that predates edited_at still reads; the column
    # simply comes back empty rather than failing every message query.
    edited_at = f"{alias}.edited_at" if has_edited_at else "NULL"
    return MESSAGE_SELECT_COLUMNS.format(alias=alias, edited_at=edited_at)


def row_to_message(row: Tuple) -> Message:
    return Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=row[2],
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7],
        reply_to_message_id=row[8],
        reply_to_sender=row[9],
        reply_preview=row[10],
        reply_media_type=row[11],
        edited_at=datetime.fromisoformat(row[12]) if row[12] else None,
    )


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def attach_message_metadata(cursor, messages: List[Message]) -> None:
    if not messages:
        return

    keys = sorted({(message.chat_jid, message.id) for message in messages})
    key_to_messages = {}
    for message in messages:
        key_to_messages.setdefault((message.chat_jid, message.id), []).append(message)

    if _table_exists(cursor, "message_reactions"):
        clauses = " OR ".join(["(chat_jid = ? AND target_message_id = ?)"] * len(keys))
        params = []
        for chat_jid, message_id in keys:
            params.extend([chat_jid, message_id])

        cursor.execute(
            f"""
            SELECT
                chat_jid,
                target_message_id,
                target_sender,
                reaction_sender,
                emoji,
                reaction_message_id,
                grouping_key,
                sender_timestamp_ms,
                timestamp,
                is_from_me
            FROM message_reactions
            WHERE emoji != '' AND ({clauses})
            ORDER BY timestamp ASC, reaction_sender ASC
            """,
            tuple(params),
        )
        for (
            chat_jid,
            target_message_id,
            target_sender,
            reaction_sender,
            emoji,
            reaction_message_id,
            grouping_key,
            sender_timestamp_ms,
            timestamp,
            is_from_me,
        ) in cursor.fetchall():
            reaction = MessageReaction(
                sender=reaction_sender,
                emoji=emoji,
                timestamp=_parse_optional_datetime(timestamp),
                reaction_message_id=reaction_message_id,
                target_message_id=target_message_id,
                target_sender=target_sender or None,
                grouping_key=grouping_key,
                sender_timestamp_ms=sender_timestamp_ms,
                is_from_me=bool(is_from_me) if is_from_me is not None else None,
            )
            for message in key_to_messages.get((chat_jid, target_message_id), []):
                message.reactions.append(reaction)

    if _table_exists(cursor, "message_receipts"):
        clauses = " OR ".join(["(chat_jid = ? AND message_id = ?)"] * len(keys))
        params = []
        for chat_jid, message_id in keys:
            params.extend([chat_jid, message_id])

        cursor.execute(
            f"""
            SELECT
                message_id,
                chat_jid,
                receipt_type,
                receipt_sender,
                message_sender,
                timestamp
            FROM message_receipts
            WHERE {clauses}
            ORDER BY timestamp ASC, receipt_sender ASC
            """,
            tuple(params),
        )
        for message_id, chat_jid, receipt_type, receipt_sender, message_sender, timestamp in cursor.fetchall():
            receipt = MessageReceipt(
                sender=receipt_sender,
                type=receipt_type,
                timestamp=_parse_optional_datetime(timestamp),
                message_id=message_id,
                chat_jid=chat_jid,
                message_sender=message_sender or None,
            )
            for message in key_to_messages.get((chat_jid, message_id), []):
                message.receipts.append(receipt)
                if receipt.type == "read":
                    message.seen_by.append(receipt)


def format_reply_prefix(message: Message) -> str:
    if not message.reply_to_message_id:
        return ""

    reply_label = "reply"
    if message.reply_media_type:
        reply_label = f"reply to {message.reply_media_type}"

    reply_bits = [reply_label]
    reply_bits.append(f"Message ID: {message.reply_to_message_id}")

    if message.reply_to_sender:
        reply_sender = get_sender_name(message.reply_to_sender)
        reply_bits.append(f"From: {reply_sender}")

    if message.reply_preview:
        reply_bits.append(f"Preview: {message.reply_preview}")

    return "[" + " - ".join(reply_bits) + "] "

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""

    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "

    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    content_prefix += format_reply_prefix(message)

    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output

    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    expand_identity: bool = True,
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        identity_context = _load_identity_context()
        has_edited_at = _column_exists(cursor, "messages", "edited_at")

        # Build base query
        query_parts = [f"SELECT {message_select_columns('messages', has_edited_at)} FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        query_parts.append("LEFT JOIN messages AS reply_target ON reply_target.chat_jid = messages.chat_jid AND reply_target.id = messages.reply_to_message_id")
        where_clauses = []
        params = []

        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)

        if chat_jid:
            chat_jids = (
                _equivalent_direct_chat_jids(chat_jid, identity_context)
                if expand_identity
                else [chat_jid]
            )
            where_clauses.append(
                "messages.chat_jid IN (" + ",".join(["?"] * len(chat_jids)) + ")"
            )
            params.extend(chat_jids)

        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()

        result = []
        for msg in messages:
            message = row_to_message(msg)
            message.chat_name = _resolved_chat_name(
                message.chat_jid,
                message.chat_name,
                identity_context,
            )
            result.append(message)

        attach_message_metadata(cursor, result)

        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)

            return messages_with_context

        # Format and display messages without context
        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        has_edited_at = _column_exists(cursor, "messages", "edited_at")

        # Get the target message first
        cursor.execute(f"""
            SELECT {message_select_columns('messages', has_edited_at)}, messages.chat_jid
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            LEFT JOIN messages AS reply_target ON reply_target.chat_jid = messages.chat_jid AND reply_target.id = messages.reply_to_message_id
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = row_to_message(msg_data[:13])

        # Get messages before
        cursor.execute(f"""
            SELECT {message_select_columns('messages', has_edited_at)}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            LEFT JOIN messages AS reply_target ON reply_target.chat_jid = messages.chat_jid AND reply_target.id = messages.reply_to_message_id
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[13], msg_data[0], before))

        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(row_to_message(msg))

        # Get messages after
        cursor.execute(f"""
            SELECT {message_select_columns('messages', has_edited_at)}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            LEFT JOIN messages AS reply_target ON reply_target.chat_jid = messages.chat_jid AND reply_target.id = messages.reply_to_message_id
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[13], msg_data[0], after))

        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(row_to_message(msg))

        attach_message_metadata(cursor, [*before_messages, target_message, *after_messages])

        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        client_filter_query = query.strip() if query else None

        if include_last_message:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    messages.content as last_message,
                    messages.sender as last_sender,
                    messages.is_from_me as last_is_from_me
                FROM chats
            """]
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid
                AND chats.last_message_time = messages.timestamp
            """)
        else:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me
                FROM chats
            """]

        where_clauses = []
        params = []

        if query and not client_filter_query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")

        if not client_filter_query:
            offset = page * limit
            query_parts.append("LIMIT ? OFFSET ?")
            params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        identity_context = _load_identity_context()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=_resolved_chat_name(chat_data[0], chat_data[1], identity_context),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        if client_filter_query:
            result = [
                chat
                for chat in result
                if _chat_matches_query(chat, client_filter_query, identity_context)
            ]
            if sort_by == "last_active":
                result.sort(
                    key=lambda chat: chat.last_message_time.timestamp() if chat.last_message_time else 0,
                    reverse=True,
                )
            else:
                result.sort(key=lambda chat: (chat.name or "").casefold())
            start = page * limit
            result = result[start:start + limit]

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'

        cursor.execute("""
            SELECT DISTINCT
                jid,
                name
            FROM chats
            WHERE
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))

        contacts = cursor.fetchall()

        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=contact_data[1],
                jid=contact_data[0]
            )
            result.append(contact)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))

        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))

        msg_data = cursor.fetchone()

        if not msg_data:
            return None

        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )

        return format_message(message)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        if include_last_message:
            query = """
                SELECT
                    c.jid,
                    c.name,
                    c.last_message_time,
                    m.content as last_message,
                    m.sender as last_sender,
                    m.is_from_me as last_is_from_me
                FROM chats c
            """
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            """
        else:
            query = """
                SELECT
                    c.jid,
                    c.name,
                    c.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me
                FROM chats c
            """

        query += " WHERE c.jid = ?"

        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))

        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }

        response = requests.post(url, json=payload)

        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None

    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
