#!/usr/bin/env python3

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WHATSAPP_MODULE_PATH = (
    PLUGIN_ROOT / "vendor" / "lharries-whatsapp-mcp" / "whatsapp-mcp-server" / "whatsapp.py"
)


def _load_whatsapp_module():
    spec = importlib.util.spec_from_file_location("codex_whatsapp_backend", WHATSAPP_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load WhatsApp backend from {WHATSAPP_MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(WHATSAPP_MODULE_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _normalize(val) for key, val in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {key: _normalize(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return str(value)
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_normalize(payload), indent=2, ensure_ascii=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Temporary WhatsApp direct-code access. This bypasses MCP because the current "
            "WhatsApp MCP can collide with other MCP servers in Codex/Claude."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("db-path", help="Print the resolved messages.db path")

    search_contacts = subparsers.add_parser("search-contacts", help="Search contacts by name or phone")
    search_contacts.add_argument("query")

    list_chats = subparsers.add_parser("list-chats", help="List chats")
    list_chats.add_argument("--query")
    list_chats.add_argument("--limit", type=int, default=20)
    list_chats.add_argument("--page", type=int, default=0)
    list_chats.add_argument("--sort-by", choices=["last_active", "name"], default="last_active")
    list_chats.add_argument("--no-last-message", action="store_true")

    get_chat = subparsers.add_parser("get-chat", help="Get a chat by JID")
    get_chat.add_argument("chat_jid")
    get_chat.add_argument("--no-last-message", action="store_true")

    get_direct_chat = subparsers.add_parser(
        "get-direct-chat", help="Get a direct chat by sender phone number"
    )
    get_direct_chat.add_argument("sender_phone_number")

    get_contact_chats = subparsers.add_parser(
        "get-contact-chats", help="Get chats involving a specific contact JID"
    )
    get_contact_chats.add_argument("jid")
    get_contact_chats.add_argument("--limit", type=int, default=20)
    get_contact_chats.add_argument("--page", type=int, default=0)

    last_interaction = subparsers.add_parser(
        "get-last-interaction", help="Get the most recent interaction involving a contact JID"
    )
    last_interaction.add_argument("jid")

    list_messages = subparsers.add_parser(
        "list-messages", help="List messages using the vendored Python backend"
    )
    list_messages.add_argument("--after")
    list_messages.add_argument("--before")
    list_messages.add_argument("--sender-phone-number")
    list_messages.add_argument("--chat-jid")
    list_messages.add_argument("--query")
    list_messages.add_argument("--include-identity-duplicates", action="store_true")
    list_messages.add_argument("--limit", type=int, default=20)
    list_messages.add_argument("--page", type=int, default=0)
    list_messages.add_argument("--include-context", action="store_true")
    list_messages.add_argument("--context-before", type=int, default=1)
    list_messages.add_argument("--context-after", type=int, default=1)

    get_context = subparsers.add_parser("get-message-context", help="Get context around a message")
    get_context.add_argument("message_id")
    get_context.add_argument("--before", type=int, default=5)
    get_context.add_argument("--after", type=int, default=5)

    download_media = subparsers.add_parser(
        "download-media", help="Download media through the local WhatsApp bridge"
    )
    download_media.add_argument("message_id")
    download_media.add_argument("chat_jid")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    whatsapp = _load_whatsapp_module()

    if args.command == "db-path":
        _print_json({"messages_db_path": whatsapp.MESSAGES_DB_PATH})
        return 0

    if args.command == "search-contacts":
        _print_json(whatsapp.search_contacts(args.query))
        return 0

    if args.command == "list-chats":
        _print_json(
            whatsapp.list_chats(
                query=args.query,
                limit=args.limit,
                page=args.page,
                include_last_message=not args.no_last_message,
                sort_by=args.sort_by,
            )
        )
        return 0

    if args.command == "get-chat":
        _print_json(
            whatsapp.get_chat(args.chat_jid, include_last_message=not args.no_last_message)
        )
        return 0

    if args.command == "get-direct-chat":
        _print_json(whatsapp.get_direct_chat_by_contact(args.sender_phone_number))
        return 0

    if args.command == "get-contact-chats":
        _print_json(whatsapp.get_contact_chats(args.jid, limit=args.limit, page=args.page))
        return 0

    if args.command == "get-last-interaction":
        _print_json({"message": whatsapp.get_last_interaction(args.jid)})
        return 0

    if args.command == "list-messages":
        _print_json(
            {
                "formatted": whatsapp.list_messages(
                    after=args.after,
                    before=args.before,
                    sender_phone_number=args.sender_phone_number,
                    chat_jid=args.chat_jid,
                    query=args.query,
                    limit=args.limit,
                    page=args.page,
                    include_context=args.include_context,
                    context_before=args.context_before,
                    context_after=args.context_after,
                    expand_identity=not args.include_identity_duplicates,
                )
            }
        )
        return 0

    if args.command == "get-message-context":
        _print_json(whatsapp.get_message_context(args.message_id, before=args.before, after=args.after))
        return 0

    if args.command == "download-media":
        path = whatsapp.download_media(args.message_id, args.chat_jid)
        _print_json({"success": bool(path), "file_path": path})
        return 0 if path else 1

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
