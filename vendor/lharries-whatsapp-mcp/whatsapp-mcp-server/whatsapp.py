from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio
import convex_client

# WhatsApp data (chats, messages, reactions, receipts, and the account's
# LID/phone identity map) is read from Near's Convex deployment; see
# convex_client.py for the URL and read-token settings.
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


def _flag(value):
    # The SQLite store reported booleans as 0/1; keep that output shape.
    return None if value is None else int(bool(value))


def _load_identity_context():
    context = {
        "lid_to_phone": {},
        "phone_to_lid": {},
        "contact_names": {},
        "chat_names": {},
    }

    identity = convex_client.query("identity") or {}
    for chat in identity.get("chats") or []:
        jid, name = chat.get("jid"), chat.get("name")
        if isinstance(jid, str) and isinstance(name, str) and name:
            context["chat_names"][jid] = name

    for pair in identity.get("lids") or []:
        lid_user = _jid_user(pair.get("lid"))
        phone_user = _jid_user(pair.get("pn"))
        if lid_user and phone_user and phone_user.isdigit():
            context["lid_to_phone"][lid_user] = phone_user
            context["phone_to_lid"][phone_user] = lid_user

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
    return convex_client.query("senderName", {"jid": sender_jid}) or sender_jid


def attach_message_metadata(message: Message, row: dict) -> Message:
    """Map the reactions and receipts Convex returns with a message row."""
    for reaction in row.get("reactions") or []:
        sender_timestamp_ms = reaction.get("senderTimestampMs")
        is_from_me = reaction.get("isFromMe")
        message.reactions.append(MessageReaction(
            sender=reaction.get("sender"),
            emoji=reaction.get("emoji"),
            timestamp=convex_client.from_ms(reaction.get("timestamp")),
            reaction_message_id=reaction.get("reactionMessageId"),
            target_message_id=reaction.get("targetMessageId"),
            target_sender=reaction.get("targetSender") or None,
            grouping_key=reaction.get("groupingKey"),
            sender_timestamp_ms=int(sender_timestamp_ms) if sender_timestamp_ms is not None else None,
            is_from_me=bool(is_from_me) if is_from_me is not None else None,
        ))

    for receipt_row in row.get("receipts") or []:
        receipt = MessageReceipt(
            sender=receipt_row.get("sender"),
            type=receipt_row.get("type"),
            timestamp=convex_client.from_ms(receipt_row.get("timestamp")),
            message_id=receipt_row.get("messageId"),
            chat_jid=receipt_row.get("chatJid"),
            message_sender=receipt_row.get("messageSender") or None,
        )
        message.receipts.append(receipt)
        if receipt.type == "read":
            message.seen_by.append(receipt)
    return message


def row_to_message(row: dict) -> Message:
    message = Message(
        timestamp=convex_client.from_ms(row["timestamp"]),
        sender=row["sender"],
        chat_name=row.get("chatName"),
        content=row.get("content"),
        is_from_me=_flag(row.get("isFromMe")),
        chat_jid=row["chatJid"],
        id=row["id"],
        media_type=row.get("mediaType"),
        reply_to_message_id=row.get("replyToMessageId"),
        reply_to_sender=row.get("replySender"),
        reply_preview=row.get("replyContent"),
        reply_media_type=row.get("replyMediaType"),
        edited_at=convex_client.from_ms(row.get("editedAt")),
    )
    return attach_message_metadata(message, row)


def row_to_chat(row: dict) -> Chat:
    return Chat(
        jid=row["jid"],
        name=row.get("name"),
        last_message_time=convex_client.from_ms(row.get("lastMessageTime")) if row.get("lastMessageTime") else None,
        last_message=row.get("lastMessage"),
        last_sender=row.get("lastSender"),
        last_is_from_me=_flag(row.get("lastIsFromMe")),
    )


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
    """Get messages matching the specified criteria with optional context.

    A text `query` runs Convex full-text search and then keeps messages whose
    content contains the text (case-insensitive).
    """
    identity_context = _load_identity_context()
    args = {"limit": limit, "offset": page * limit}

    if after:
        try:
            args["after"] = convex_client.to_ms(datetime.fromisoformat(after))
        except ValueError:
            raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")

    if before:
        try:
            args["before"] = convex_client.to_ms(datetime.fromisoformat(before))
        except ValueError:
            raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")

    if sender_phone_number:
        args["sender"] = sender_phone_number

    if chat_jid:
        args["chatJids"] = (
            _equivalent_direct_chat_jids(chat_jid, identity_context)
            if expand_identity
            else [chat_jid]
        )

    if query:
        args["query"] = query

    result = []
    for row in convex_client.query("messages", args) or []:
        message = row_to_message(row)
        message.chat_name = _resolved_chat_name(
            message.chat_jid,
            message.chat_name,
            identity_context,
        )
        result.append(message)

    if include_context and result:
        # Add context for each message
        messages_with_context = []
        for msg in result:
            context = get_message_context(msg.id, context_before, context_after)
            messages_with_context.extend(context.before)
            messages_with_context.append(context.message)
            messages_with_context.extend(context.after)

        return messages_with_context

    return result


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    value = convex_client.query("context", {"id": message_id, "before": before, "after": after})
    if not value:
        raise ValueError(f"Message with ID {message_id} not found")

    return MessageContext(
        message=row_to_message(value["message"]),
        before=[row_to_message(row) for row in value.get("before") or []],
        after=[row_to_message(row) for row in value.get("after") or []],
    )


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    client_filter_query = query.strip() if query else None
    rows = convex_client.query("chats", {"includeLast": include_last_message}) or []
    identity_context = _load_identity_context()

    result = []
    for row in rows:
        chat = row_to_chat(row)
        raw_name = chat.name
        chat.name = _resolved_chat_name(chat.jid, raw_name, identity_context)
        result.append((raw_name, chat))

    if client_filter_query:
        result = [
            (raw_name, chat)
            for raw_name, chat in result
            if _chat_matches_query(chat, client_filter_query, identity_context)
        ]

    if sort_by == "last_active":
        result.sort(
            key=lambda item: item[1].last_message_time.timestamp() if item[1].last_message_time else 0,
            reverse=True,
        )
    elif client_filter_query:
        result.sort(key=lambda item: (item[1].name or "").casefold())
    else:
        # Stored (unresolved) name, missing names first, as the chat store ordered it.
        result.sort(key=lambda item: (item[0] is not None, item[0] or ""))

    start = page * limit
    return [chat for _, chat in result[start:start + limit]]


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    return [
        Contact(
            phone_number=row["jid"].split('@')[0],
            name=row.get("name"),
            jid=row["jid"],
        )
        for row in convex_client.query("contacts", {"query": query}) or []
    ]


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    rows = convex_client.query("contactChats", {"jid": jid, "limit": limit, "offset": page * limit})
    return [row_to_chat(row) for row in rows or []]


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    row = convex_client.query("lastInteraction", {"jid": jid})
    if not row:
        return None
    return format_message(row_to_message(row))


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    row = convex_client.query("chat", {"jid": chat_jid, "includeLast": include_last_message})
    return row_to_chat(row) if row else None


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    row = convex_client.query("directChat", {"phone": sender_phone_number})
    return row_to_chat(row) if row else None

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
