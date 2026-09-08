#!/usr/bin/env python3
"""Composable WhatsApp CLI for Codex."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get("WHATSAPP_SOURCE_ROOT", str(DEFAULT_SOURCE_ROOT))).expanduser()
SCRIPT_DIR = SOURCE_ROOT / "scripts"
RUN_CLI = SCRIPT_DIR / "run_cli.sh"
START_BRIDGE = SCRIPT_DIR / "start_bridge.sh"
STOP_BRIDGE = SCRIPT_DIR / "stop_bridge.sh"
STATUS_BRIDGE = SCRIPT_DIR / "status_bridge.sh"
SETUP = SCRIPT_DIR / "setup.sh"
RESET_SYNC = SCRIPT_DIR / "reset_sync.sh"
DEFAULT_DRAFTS_DB_PATH = Path(
    os.environ.get("WHATSAPP_DRAFTS_DB_PATH", "~/.local/share/codex-whatsapp/drafts.db")
).expanduser()
DEFAULT_TRANSCRIPTS_DB_PATH = Path(
    os.environ.get("WHATSAPP_TRANSCRIPTS_DB_PATH", "~/.local/share/codex-whatsapp/transcripts.db")
).expanduser()
TRANSCRIPT_PROVIDER = "elevenlabs"
AUDIO_MEDIA_TYPES = {"audio", "ptt", "voice", "voice_message"}
PERMANENT_TRANSCRIBE_FAILURE_CODES = {
    "media_expired",
    "message_chat_mismatch",
    "message_not_found",
    "not_audio_media",
}
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# The CLI can create drafts and transcript state without starting the bridge.
# Make those files private even when this process did not come through the
# lifecycle shell scripts.
os.umask(0o077)


class CliError(Exception):
    def __init__(self, message: str, *, code: str = "error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def ensure_private_directory(path: Path, *, migrate_existing: bool = False) -> None:
    existed = path.exists()
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    # A custom database can intentionally live in a shared existing parent
    # such as /tmp. Secure directories we create, but never chmod that parent.
    if migrate_existing or not existed:
        path.chmod(PRIVATE_DIRECTORY_MODE)


def enforce_private_tree(path: Path) -> None:
    """Create or migrate a state tree without following symlinks."""
    ensure_private_directory(path, migrate_existing=True)
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            child.chmod(PRIVATE_DIRECTORY_MODE)
        elif child.is_file():
            child.chmod(PRIVATE_FILE_MODE)


def enforce_private_database_files(path: Path) -> None:
    """Migrate a SQLite database and any existing journal sidecars."""
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-shm"),
        Path(f"{path}-wal"),
    ):
        if candidate.is_file() and not candidate.is_symlink():
            candidate.chmod(PRIVATE_FILE_MODE)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_process(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(SOURCE_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError(f"Missing executable: {args[0]}", code="missing_executable", details={"argv": args}) from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"Command timed out: {' '.join(args)}", code="timeout", details={"timeout": timeout}) from exc


def ensure_source() -> None:
    if not SOURCE_ROOT.exists():
        raise CliError("WhatsApp source root does not exist.", code="missing_source_root", details={"path": str(SOURCE_ROOT)})
    if not RUN_CLI.exists():
        raise CliError("WhatsApp CLI backend script is missing.", code="missing_backend", details={"path": str(RUN_CLI)})


def parse_json_output(process: subprocess.CompletedProcess[str], *, code: str = "backend_failed") -> Any:
    stdout = process.stdout.strip()
    stderr = process.stderr.strip()
    if process.returncode != 0:
        raise CliError(
            "WhatsApp backend command failed.",
            code=code,
            details={"returncode": process.returncode, "stdout": stdout[-4000:], "stderr": stderr[-4000:]},
        )
    if not stdout:
        return {}
    if "Database error:" in stdout:
        raise CliError(
            "WhatsApp backend reported a database error.",
            code="backend_database_error",
            details={"stdout": stdout[-4000:], "stderr": stderr[-4000:]},
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        lines = stdout.splitlines()
        for index, line in enumerate(lines):
            if not line.lstrip().startswith(("{", "[")):
                continue
            candidate = "\n".join(lines[index:])
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        raise CliError(
            "WhatsApp backend returned non-JSON output.",
            code="invalid_backend_json",
            details={"message": str(exc), "stdout": stdout[-4000:], "stderr": stderr[-4000:]},
        ) from exc


def backend_json(*args: str, timeout: int = 120) -> Any:
    ensure_source()
    return parse_json_output(run_process(["/bin/zsh", str(RUN_CLI), *args], timeout=timeout))


def script_output(script: Path, *args: str, timeout: int = 120) -> dict[str, Any]:
    ensure_source()
    if not script.exists():
        raise CliError("WhatsApp lifecycle script is missing.", code="missing_script", details={"path": str(script)})
    process = run_process(["/bin/zsh", str(script), *args], timeout=timeout)
    payload = {
        "returncode": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }
    if process.returncode != 0:
        raise CliError("WhatsApp lifecycle command failed.", code="lifecycle_failed", details=payload)
    return payload


def script_passthrough(script: Path, *args: str, timeout: int = 300) -> dict[str, Any]:
    ensure_source()
    if not script.exists():
        raise CliError("WhatsApp lifecycle script is missing.", code="missing_script", details={"path": str(script)})
    try:
        process = subprocess.run(
            ["/bin/zsh", str(script), *args],
            cwd=str(SOURCE_ROOT),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError("Missing executable: /bin/zsh", code="missing_executable") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"Command timed out: {script.name}",
            code="timeout",
            details={"timeout": timeout},
        ) from exc
    payload = {"returncode": process.returncode}
    if process.returncode != 0:
        raise CliError("WhatsApp lifecycle command failed.", code="lifecycle_failed", details=payload)
    return payload


def bridge_status() -> dict[str, Any]:
    ensure_source()
    process = run_process(["/bin/zsh", str(STATUS_BRIDGE)], timeout=30)
    status = parse_json_output(process, code="status_failed")
    if isinstance(status, dict):
        log_tail = status.pop("log_tail", None)
        if isinstance(log_tail, list):
            status["log_tail_redacted"] = True
            status["log_tail_line_count"] = len(log_tail)
    return status


def command_doctor(_args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "source_root": str(SOURCE_ROOT),
        "source_exists": SOURCE_ROOT.exists(),
        "backend_script": str(RUN_CLI),
        "backend_script_exists": RUN_CLI.exists(),
        "tools": {
            "python3": command_exists("python3"),
            "pnpm": command_exists("pnpm"),
            "go": command_exists("go"),
            "node": command_exists("node"),
        },
        "bridge": None,
        "db_path": None,
        "errors": [],
    }
    try:
        report["bridge"] = bridge_status()
    except CliError as exc:
        report["errors"].append({"code": exc.code, "message": str(exc), "details": exc.details})
    try:
        db = backend_json("db-path", timeout=30)
        report["db_path"] = db.get("messages_db_path") if isinstance(db, dict) else db
    except CliError as exc:
        report["errors"].append({"code": exc.code, "message": str(exc), "details": exc.details})
    return report


def command_bridge_status(_args: argparse.Namespace) -> dict[str, Any]:
    return bridge_status()


def command_bridge_start(_args: argparse.Namespace) -> dict[str, Any]:
    result = script_output(START_BRIDGE, timeout=90)
    return {"command": result, "status": bridge_status()}


def command_bridge_stop(_args: argparse.Namespace) -> dict[str, Any]:
    result = script_output(STOP_BRIDGE, timeout=60)
    return {"command": result, "status": bridge_status()}


def command_bridge_setup(args: argparse.Namespace) -> dict[str, Any]:
    if args.json:
        result = script_output(SETUP, timeout=900)
    else:
        result = script_passthrough(SETUP, timeout=900)
    return {"command": result, "status": bridge_status()}


def command_bridge_relink(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm:
        raise CliError("relink requires --confirm because it deletes local WhatsApp sync state.", code="confirm_required")
    phone_pairing = os.environ.get("WHATSAPP_USE_PHONE_PAIRING", "").strip() == "1"
    if not args.json:
        print("Resetting local WhatsApp bridge state...", flush=True)
    reset_result = script_output(RESET_SYNC, timeout=60)

    if not args.json:
        if phone_pairing:
            print(
                "Starting WhatsApp setup. Use Linked devices -> Link with phone number instead, then enter the code shown below.",
                flush=True,
            )
        else:
            print(
                "Starting WhatsApp setup. Use Linked devices -> Link a device, then scan the QR code shown below.",
                flush=True,
            )
        setup_result = script_passthrough(SETUP, timeout=900)
    else:
        setup_result = script_output(SETUP, timeout=900)

    if not args.json:
        print("Starting durable WhatsApp bridge...", flush=True)
    start_result = script_output(START_BRIDGE, timeout=90)
    status = bridge_status()
    return {
        "reset": reset_result,
        "setup": setup_result,
        "start": start_result,
        "status": status,
    }


def command_bridge_reset_sync(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm:
        raise CliError("reset-sync requires --confirm because it deletes local WhatsApp sync state.", code="confirm_required")
    result = script_output(RESET_SYNC, timeout=60)
    return {"command": result}


def command_contacts_search(args: argparse.Namespace) -> dict[str, Any]:
    return {"contacts": backend_json("search-contacts", args.query)}


def identifier_type(jid: str | None) -> str:
    if not jid:
        return "unknown"
    if jid == "status@broadcast" or jid.endswith("@broadcast"):
        return "broadcast"
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@s.whatsapp.net"):
        return "phone"
    if jid.endswith("@lid"):
        return "lid"
    return "unknown"


def chat_type_for_identifier(kind: str) -> str:
    if kind in {"phone", "lid"}:
        return "direct"
    if kind in {"group", "broadcast"}:
        return kind
    return "unknown"


def phone_from_jid(jid: str | None) -> str | None:
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return None
    phone = jid.split("@", 1)[0]
    return phone if phone.isdigit() else None


def jid_user(jid: str | None) -> str | None:
    if not jid:
        return None
    user = jid.split("@", 1)[0]
    return user.split(":", 1)[0]


def participant_jid_for_sender(
    sender: Any,
    context: dict[str, dict[str, str]] | None = None,
) -> str | None:
    if not isinstance(sender, str):
        return None

    value = sender.strip()
    if not value:
        return None
    if "@" in value:
        return value

    user = value.split(":", 1)[0]
    context = context or {"lid_to_phone": {}, "contact_names": {}, "chat_names": {}}
    if user in context.get("lid_to_phone", {}):
        return f"{user}@lid"
    if user.isdigit():
        return f"{user}@s.whatsapp.net"
    return value


def is_numeric_label(value: Any) -> bool:
    return isinstance(value, str) and value.isdigit()


def first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def chat_timestamp(chat: dict[str, Any]) -> datetime:
    value = chat.get("last_message_time")
    if not isinstance(value, str) or not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def useful_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.isdigit():
        return None
    return value


NAME_QUALITY_RANK = {
    "resolved": 60,
    "resolved_lid": 50,
    "group_name": 45,
    "system_broadcast": 40,
    "phone_number": 20,
    "missing_name": 10,
    "missing_group_name": 5,
    "unresolved_lid": 0,
}


def name_score(chat: dict[str, Any]) -> tuple[int, int, str]:
    display_name = chat.get("display_name")
    display_bonus = 10 if useful_name(display_name) else 0
    quality = chat.get("name_quality")
    quality_rank = NAME_QUALITY_RANK.get(quality, 0) if isinstance(quality, str) else 0
    return (quality_rank + display_bonus, len(display_name or ""), display_name or "")


def chat_name_sort_key(chat: Any) -> tuple[str, str, str]:
    if not isinstance(chat, dict):
        return ("", "", "")
    label = first_text(chat.get("display_name"), chat.get("name"), chat.get("phone_number"), chat.get("jid")) or ""
    phone_number = chat.get("phone_number") if isinstance(chat.get("phone_number"), str) else ""
    jid = chat.get("jid") if isinstance(chat.get("jid"), str) else ""
    return (label.casefold(), phone_number, jid)


def chat_last_active_sort_key(chat: Any) -> datetime:
    if not isinstance(chat, dict):
        return datetime.min.replace(tzinfo=timezone.utc)
    return chat_timestamp(chat)


def chat_matches_query(chat: Any, query: str | None) -> bool:
    if not query or not query.strip():
        return True
    if not isinstance(chat, dict):
        return False

    needle = query.casefold().strip()
    fields = [
        chat.get("display_name"),
        chat.get("name"),
        chat.get("phone_number"),
        chat.get("jid"),
    ]
    return any(isinstance(value, str) and needle in value.casefold() for value in fields)


def append_unique_chats(output: list[Any], chats: Any, seen: set[str]) -> None:
    if not isinstance(chats, list):
        return
    for chat in chats:
        if not isinstance(chat, dict):
            output.append(chat)
            continue
        jid = chat.get("jid")
        if isinstance(jid, str):
            if jid in seen:
                continue
            seen.add(jid)
        output.append(chat)


def direct_chat_key(chat: dict[str, Any]) -> tuple[str, str] | None:
    if chat.get("chat_type") != "direct":
        return None
    phone_number = chat.get("phone_number")
    if not isinstance(phone_number, str) or not phone_number.isdigit():
        return None
    return ("direct_phone", phone_number)


def choose_primary_chat(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_time = chat_timestamp(left)
    right_time = chat_timestamp(right)
    if right_time > left_time:
        return right
    if left_time > right_time:
        return left
    if right.get("identifier_type") == "phone" and left.get("identifier_type") != "phone":
        return right
    return left


def merge_direct_chat(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    primary = choose_primary_chat(left, right)
    secondary = left if primary is right else right
    best_name_chat = max([left, right], key=name_score)

    merged = dict(primary)
    phone_number = first_text(primary.get("phone_number"), secondary.get("phone_number"))
    if phone_number:
        merged["phone_number"] = phone_number

    display_name = first_text(
        useful_name(best_name_chat.get("display_name")),
        useful_name(best_name_chat.get("name")),
        best_name_chat.get("display_name"),
        primary.get("display_name"),
        phone_number,
    )
    if display_name:
        merged["display_name"] = display_name

    raw_name = first_text(
        useful_name(best_name_chat.get("name")),
        useful_name(primary.get("name")),
        display_name,
    )
    if raw_name:
        merged["name"] = raw_name

    if merged.get("identifier_type") == "lid" and phone_number:
        merged["name_quality"] = "resolved_lid" if useful_name(display_name) else "phone_number"
    elif name_score(best_name_chat) > name_score(primary):
        merged["name_quality"] = best_name_chat.get("name_quality")

    return merged


def load_identity_context() -> dict[str, dict[str, str]]:
    context: dict[str, dict[str, str]] = {
        "lid_to_phone": {},
        "contact_names": {},
        "chat_names": {},
    }

    try:
        db_info = backend_json("db-path", timeout=30)
    except CliError:
        return context

    if not isinstance(db_info, dict) or not db_info.get("messages_db_path"):
        return context

    messages_db_path = Path(db_info["messages_db_path"])
    whatsapp_db_path = messages_db_path.with_name("whatsapp.db")

    try:
        with sqlite3.connect(messages_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT jid, name FROM chats WHERE name IS NOT NULL AND name != ''")
            for jid, name in cursor.fetchall():
                if isinstance(jid, str) and isinstance(name, str) and name:
                    context["chat_names"][jid] = name
    except sqlite3.Error:
        pass

    if not whatsapp_db_path.exists():
        return context

    try:
        with sqlite3.connect(whatsapp_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT lid, pn FROM whatsmeow_lid_map")
            for lid, phone in cursor.fetchall():
                lid_user = jid_user(lid)
                phone_user = jid_user(phone)
                if lid_user and phone_user and phone_user.isdigit():
                    context["lid_to_phone"][lid_user] = phone_user

            cursor.execute(
                """
                SELECT their_jid, full_name, first_name, business_name, push_name
                FROM whatsmeow_contacts
                """
            )
            for their_jid, full_name, first_name, business_name, push_name in cursor.fetchall():
                name = first_text(full_name, first_name, business_name, push_name)
                if isinstance(their_jid, str) and name:
                    context["contact_names"][their_jid] = name
    except sqlite3.Error:
        pass

    return context


def name_for_mapped_lid(jid: str, phone_number: str, context: dict[str, dict[str, str]]) -> str | None:
    phone_jid = f"{phone_number}@s.whatsapp.net"
    return first_text(
        context["chat_names"].get(phone_jid),
        context["contact_names"].get(phone_jid),
        context["contact_names"].get(jid),
    )


def annotate_chat(chat: Any, context: dict[str, dict[str, str]] | None = None) -> Any:
    if not isinstance(chat, dict):
        return chat

    context = context or {"lid_to_phone": {}, "contact_names": {}, "chat_names": {}}
    jid = chat.get("jid")
    identity_kind = identifier_type(jid)
    kind = chat_type_for_identifier(identity_kind)
    phone_number = phone_from_jid(jid)
    name = chat.get("name")

    display_name = name
    name_quality = "resolved"
    if kind == "group":
        name_quality = "group_name" if name else "missing_group_name"
    elif kind == "broadcast":
        display_name = "WhatsApp Status/Broadcast"
        name_quality = "system_broadcast"
    elif phone_number and (not name or is_numeric_label(name)):
        display_name = phone_number
        name_quality = "phone_number"
    elif identity_kind == "lid":
        lid_user = jid_user(jid)
        mapped_phone = context["lid_to_phone"].get(lid_user or "")
        if mapped_phone:
            phone_number = mapped_phone
            display_name = name_for_mapped_lid(jid, mapped_phone, context) or (
                None if is_numeric_label(name) else name
            ) or mapped_phone
            name_quality = "resolved_lid"
        elif not name or is_numeric_label(name):
            display_name = None
            name_quality = "unresolved_lid"
    elif not name:
        display_name = phone_number
        name_quality = "missing_name"

    if kind == "direct" and display_name and (
        not isinstance(name, str) or not name.strip() or is_numeric_label(name)
    ):
        name = display_name

    return {
        **chat,
        "name": name,
        "chat_type": kind,
        "identifier_type": identity_kind,
        "display_name": display_name,
        "phone_number": phone_number,
        "name_quality": name_quality,
    }


def annotate_chats(chats: Any, context: dict[str, dict[str, str]] | None = None) -> Any:
    if isinstance(chats, list):
        return [annotate_chat(chat, context) for chat in chats]
    return chats


def annotate_message(message: Any, context: dict[str, dict[str, str]]) -> Any:
    if not isinstance(message, dict):
        return message

    chat_jid = message.get("chat_jid")
    if not isinstance(chat_jid, str):
        return message

    chat = annotate_chat(
        {
            "jid": chat_jid,
            "name": message.get("chat_name"),
            "last_message_time": message.get("timestamp"),
        },
        context,
    )
    if isinstance(chat, dict):
        display_name = first_text(chat.get("display_name"), chat.get("name"))
        if display_name:
            return {**message, "chat_name": display_name}

    return message


def annotate_messages_payload(payload: Any, context: dict[str, dict[str, str]]) -> Any:
    if not isinstance(payload, dict):
        return payload
    formatted = payload.get("formatted")
    if not isinstance(formatted, list):
        return payload
    return {**payload, "formatted": [annotate_message(message, context) for message in formatted]}


def dedupe_identity_chats(chats: list[Any]) -> list[Any]:
    merged_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    output_order: list[tuple[str, str] | tuple[str, int]] = []

    for index, chat in enumerate(chats):
        if not isinstance(chat, dict):
            output_order.append(("row", index))
            merged_by_key[("row", index)] = chat
            continue

        key = direct_chat_key(chat)
        if key is None:
            output_order.append(("row", index))
            merged_by_key[("row", index)] = chat
            continue

        if key not in merged_by_key:
            output_order.append(key)
            merged_by_key[key] = chat
        else:
            merged_by_key[key] = merge_direct_chat(merged_by_key[key], chat)

    return [merged_by_key[key] for key in output_order]


def raw_chat_limit_for_deduped_page(limit: int, page: int) -> int:
    requested_end = (page + 1) * limit
    return max(200, requested_end * 4, requested_end + 50)


def command_chats_list(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise CliError("chats list --limit must be greater than zero.", code="invalid_limit")
    if args.page < 0:
        raise CliError("chats list --page must be zero or greater.", code="invalid_page")

    needs_client_query = bool(args.query and args.query.strip())
    backend_limit = raw_chat_limit_for_deduped_page(args.limit, args.page) if (
        needs_client_query or not args.include_identity_duplicates
    ) else args.limit
    backend_page = 0 if (needs_client_query or not args.include_identity_duplicates) else args.page

    cmd = ["list-chats", "--limit", str(backend_limit), "--page", str(backend_page), "--sort-by", args.sort_by]
    if not needs_client_query and args.query:
        cmd.extend(["--query", args.query])
    if args.no_last_message:
        cmd.append("--no-last-message")

    raw_chats = backend_json(*cmd)
    if needs_client_query:
        query_cmd = [
            "list-chats",
            "--limit",
            str(backend_limit),
            "--page",
            "0",
            "--sort-by",
            args.sort_by,
            "--query",
            args.query,
        ]
        if args.no_last_message:
            query_cmd.append("--no-last-message")
        combined: list[Any] = []
        seen: set[str] = set()
        append_unique_chats(combined, backend_json(*query_cmd), seen)
        append_unique_chats(combined, raw_chats, seen)
        raw_chats = combined

    chats = annotate_chats(raw_chats, load_identity_context())
    if isinstance(chats, list):
        if needs_client_query:
            chats = [chat for chat in chats if chat_matches_query(chat, args.query)]
        if not args.include_identity_duplicates:
            chats = dedupe_identity_chats(chats)
        if args.sort_by == "name":
            chats = sorted(chats, key=chat_name_sort_key)
        elif needs_client_query:
            chats = sorted(chats, key=chat_last_active_sort_key, reverse=True)
        start = args.page * args.limit
        chats = chats[start : start + args.limit]
    return {"chats": chats}


def command_chats_get(args: argparse.Namespace) -> dict[str, Any]:
    cmd = ["get-chat", args.chat_jid]
    if args.no_last_message:
        cmd.append("--no-last-message")
    return {"chat": annotate_chat(backend_json(*cmd), load_identity_context())}


def command_chats_direct(args: argparse.Namespace) -> dict[str, Any]:
    return {"chat": annotate_chat(backend_json("get-direct-chat", args.sender_phone_number), load_identity_context())}


def command_chats_contact(args: argparse.Namespace) -> dict[str, Any]:
    return {"chats": annotate_chats(backend_json("get-contact-chats", args.jid, "--limit", str(args.limit), "--page", str(args.page)), load_identity_context())}


def command_chats_last_interaction(args: argparse.Namespace) -> dict[str, Any]:
    return backend_json("get-last-interaction", args.jid)


def command_messages_list(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise CliError("messages list --limit must be greater than zero.", code="invalid_limit")
    if args.page < 0:
        raise CliError("messages list --page must be zero or greater.", code="invalid_page")

    context = load_identity_context()
    cmd = ["list-messages", "--limit", str(args.limit), "--page", str(args.page)]
    optional = {
        "--after": args.after,
        "--before": args.before,
        "--sender-phone-number": args.sender_phone_number,
        "--chat-jid": args.chat_jid,
        "--query": args.query,
    }
    for flag, value in optional.items():
        if value is not None:
            cmd.extend([flag, value])
    if args.include_context:
        cmd.extend([
            "--include-context",
            "--context-before",
            str(args.context_before),
            "--context-after",
            str(args.context_after),
        ])
    if args.include_identity_duplicates:
        cmd.append("--include-identity-duplicates")
    return annotate_messages_payload(backend_json(*cmd), context)


def command_messages_context(args: argparse.Namespace) -> dict[str, Any]:
    return {"context": backend_json("get-message-context", args.message_id, "--before", str(args.before), "--after", str(args.after))}


def command_media_download(args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = backend_json("download-media", args.message_id, args.chat_jid, timeout=180)
    except CliError as exc:
        details = json.dumps(exc.details, ensure_ascii=True).lower()
        if any(f"download failed with status code {status}" in details for status in (404, 410)):
            raise CliError(
                "WhatsApp media is no longer available from the CDN.",
                code="media_expired",
                details=exc.details,
            ) from exc
        raise
    return result if isinstance(result, dict) else {"result": result}


def transcripts_db_path() -> Path:
    if os.environ.get("WHATSAPP_TRANSCRIPTS_DB_PATH"):
        return DEFAULT_TRANSCRIPTS_DB_PATH
    try:
        db_info = backend_json("db-path", timeout=30)
        if isinstance(db_info, dict) and db_info.get("messages_db_path"):
            return Path(db_info["messages_db_path"]).with_name("transcripts.db")
    except CliError:
        pass
    return DEFAULT_TRANSCRIPTS_DB_PATH


def transcript_store_dir() -> Path:
    return transcripts_db_path().with_name("transcripts")


def transcript_lock_dir() -> Path:
    return transcripts_db_path().with_name("transcript-locks")


@contextmanager
def transcript_message_lock(message_id: str, chat_jid: str):
    """Serialize paid transcription work for one WhatsApp message."""
    lock_dir = transcript_lock_dir()
    enforce_private_tree(lock_dir)
    identity = hashlib.sha256(f"{chat_jid}\0{message_id}".encode("utf-8")).hexdigest()
    lock_path = lock_dir / f"{identity}.lock"
    with lock_path.open("a+") as handle:
        lock_path.chmod(PRIVATE_FILE_MODE)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def open_transcripts_db() -> sqlite3.Connection:
    path = transcripts_db_path()
    ensure_private_directory(path.parent)
    enforce_private_tree(path.with_name("transcripts"))
    enforce_private_tree(path.with_name("transcript-locks"))
    enforce_private_database_files(path)
    conn = sqlite3.connect(path)
    enforce_private_database_files(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_transcripts (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            sender TEXT,
            timestamp TEXT,
            media_type TEXT,
            media_path TEXT NOT NULL,
            transcript_path TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            language TEXT,
            response_format TEXT NOT NULL,
            options_hash TEXT NOT NULL,
            options_json TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(message_id, chat_jid, provider, model, options_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_media_transcripts_chat_updated
        ON media_transcripts (chat_jid, updated_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_transcript_failures (
            message_id TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            last_code TEXT,
            last_error TEXT,
            first_attempt_at TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            PRIMARY KEY (message_id, chat_jid)
        )
        """
    )
    conn.commit()
    enforce_private_database_files(path)
    return conn


def row_to_transcript(row: sqlite3.Row) -> dict[str, Any]:
    try:
        options = json.loads(row["options_json"])
    except json.JSONDecodeError:
        options = {}
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "chat_jid": row["chat_jid"],
        "sender": row["sender"],
        "timestamp": row["timestamp"],
        "media_type": row["media_type"],
        "media_path": row["media_path"],
        "transcript_path": row["transcript_path"],
        "provider": row["provider"],
        "model": row["model"],
        "language": row["language"],
        "response_format": row["response_format"],
        "options_hash": row["options_hash"],
        "options": options,
        "text": row["text"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def transcript_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "language": args.language,
        "response_format": args.response_format,
        "diarize": bool(args.diarize),
        "num_speakers": args.num_speakers,
        "keyterms": sorted(args.keyterm or []),
        "no_verbatim": bool(args.no_verbatim),
    }


def transcript_options_hash(options: dict[str, Any]) -> str:
    encoded = json.dumps(options, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def transcript_file_extension(response_format: str) -> str:
    return "txt" if response_format in {"text", "diarized_text"} else "json"


def safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    return safe[:120] or "unknown"


def transcript_output_path(message_id: str, chat_jid: str, response_format: str, options_hash: str) -> Path:
    filename = (
        f"{safe_path_part(chat_jid)}__{safe_path_part(message_id)}__"
        f"{options_hash[:12]}.transcript.{transcript_file_extension(response_format)}"
    )
    return transcript_store_dir() / filename


def cached_transcript(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    chat_jid: str,
    provider: str,
    model: str,
    options_hash: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM media_transcripts
        WHERE message_id = ? AND chat_jid = ? AND provider = ? AND model = ? AND options_hash = ?
        """,
        (message_id, chat_jid, provider, model, options_hash),
    ).fetchone()
    return row_to_transcript(row) if row else None


def cached_transcript_by_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    chat_jid: str | None,
) -> dict[str, Any]:
    clauses = ["message_id = ?"]
    values: list[Any] = [message_id]
    if chat_jid:
        clauses.append("chat_jid = ?")
        values.append(chat_jid)
    row = conn.execute(
        f"SELECT * FROM media_transcripts WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 1",
        values,
    ).fetchone()
    if row is None:
        raise CliError(
            "Cached transcript not found.",
            code="transcript_not_found",
            details={"message_id": message_id, "chat_jid": chat_jid},
        )
    return row_to_transcript(row)


def media_message_metadata(message_id: str, chat_jid: str) -> dict[str, Any]:
    context = backend_json(
        "get-message-context",
        message_id,
        "--before",
        "0",
        "--after",
        "0",
        timeout=30,
    )
    message = context.get("message") if isinstance(context, dict) else None
    if not isinstance(message, dict) or not message.get("id"):
        raise CliError("Media message was not found.", code="message_not_found", details={"message_id": message_id})
    if message.get("chat_jid") != chat_jid:
        raise CliError(
            "Media message belongs to a different chat.",
            code="message_chat_mismatch",
            details={"message_id": message_id, "chat_jid": chat_jid, "message_chat_jid": message.get("chat_jid")},
        )
    return message_summary(message, load_identity_context())


def elevenlabs_transcribe_command() -> list[str]:
    """Resolve the ElevenLabs transcription entry point.

    Transcription belongs to the elevenlabs plugin, which exposes it as the
    `elevenlabs` CLI on PATH. Never reach into that plugin's install layout:
    its version directory and marketplace cache path are not ours to know.
    """
    override = os.environ.get("ELEVENLABS_TRANSCRIBE_SCRIPT")
    if override:
        return ["python3", str(Path(override).expanduser())]
    cli = shutil.which("elevenlabs")
    if cli:
        return [cli, "transcribe"]
    raise CliError(
        "The elevenlabs CLI was not found on PATH.",
        code="missing_elevenlabs_cli",
        details={
            "install": "Install an ElevenLabs implementation and put its elevenlabs CLI on PATH.",
            "override_env": "ELEVENLABS_TRANSCRIBE_SCRIPT",
        },
    )


def run_elevenlabs_transcribe(audio_path: Path, output_path: Path, args: argparse.Namespace) -> str:
    enforce_private_tree(output_path.parent)
    cmd = [
        *elevenlabs_transcribe_command(),
        str(audio_path),
        "--model",
        args.model,
        "--response-format",
        args.response_format,
        "--out",
        str(output_path),
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.language:
        cmd.extend(["--language", args.language])
    if args.diarize:
        cmd.append("--diarize")
    if args.num_speakers is not None:
        cmd.extend(["--num-speakers", str(args.num_speakers)])
    if args.no_verbatim:
        cmd.append("--no-verbatim")
    for keyterm in args.keyterm or []:
        cmd.extend(["--keyterm", keyterm])

    process = run_process(cmd, timeout=args.timeout_seconds + 30)
    if process.returncode != 0:
        raise CliError(
            "ElevenLabs transcription failed.",
            code="elevenlabs_failed",
            details={
                "returncode": process.returncode,
                "stdout": process.stdout.strip()[-4000:],
                "stderr": process.stderr.strip()[-4000:],
            },
        )
    if output_path.is_file() and not output_path.is_symlink():
        output_path.chmod(PRIVATE_FILE_MODE)
    try:
        return output_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(
            "ElevenLabs completed but transcript output could not be read.",
            code="transcript_read_failed",
            details={"transcript_path": str(output_path), "error": str(exc)},
        ) from exc


def _command_media_transcribe_locked(args: argparse.Namespace) -> dict[str, Any]:
    options = transcript_options(args)
    options_hash = transcript_options_hash(options)
    with open_transcripts_db() as conn:
        if not args.refresh:
            existing = cached_transcript(
                conn,
                message_id=args.message_id,
                chat_jid=args.chat_jid,
                provider=TRANSCRIPT_PROVIDER,
                model=args.model,
                options_hash=options_hash,
            )
            if existing:
                conn.execute(
                    "DELETE FROM media_transcript_failures WHERE message_id = ? AND chat_jid = ?",
                    (args.message_id, args.chat_jid),
                )
                conn.commit()
                return {
                    "cached": True,
                    "transcript": existing,
                    "transcripts_db_path": str(transcripts_db_path()),
                }

        message = media_message_metadata(args.message_id, args.chat_jid)
        media_type = (message.get("media_type") or "").lower()
        if media_type not in AUDIO_MEDIA_TYPES and not args.allow_non_audio:
            raise CliError(
                "Refusing to transcribe non-audio media. Pass --allow-non-audio to override.",
                code="not_audio_media",
                details={"message_id": args.message_id, "media_type": message.get("media_type")},
            )

        download = command_media_download(args)
        media_path_value = first_text(download.get("file_path"), download.get("path"), download.get("media_path"))
        if not media_path_value:
            raise CliError("Downloaded media path was missing.", code="missing_download_path", details={"download": download})
        media_path = Path(media_path_value).expanduser()
        if not media_path.exists():
            raise CliError(
                "Downloaded media path does not exist.",
                code="download_missing",
                details={"media_path": str(media_path), "download": download},
            )

        output_path = transcript_output_path(args.message_id, args.chat_jid, args.response_format, options_hash)
        text = run_elevenlabs_transcribe(media_path, output_path, args)
        now = utc_now()
        transcript_id = f"transcript_{uuid.uuid4().hex[:12]}"
        options_json = json.dumps(options, ensure_ascii=True, sort_keys=True)
        conn.execute(
            """
            INSERT INTO media_transcripts (
                id, message_id, chat_jid, sender, timestamp, media_type, media_path,
                transcript_path, provider, model, language, response_format,
                options_hash, options_json, text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, chat_jid, provider, model, options_hash)
            DO UPDATE SET
                sender = excluded.sender,
                timestamp = excluded.timestamp,
                media_type = excluded.media_type,
                media_path = excluded.media_path,
                transcript_path = excluded.transcript_path,
                language = excluded.language,
                response_format = excluded.response_format,
                options_json = excluded.options_json,
                text = excluded.text,
                updated_at = excluded.updated_at
            """,
            (
                transcript_id,
                args.message_id,
                args.chat_jid,
                message.get("sender"),
                message.get("timestamp"),
                message.get("media_type"),
                str(media_path),
                str(output_path),
                TRANSCRIPT_PROVIDER,
                args.model,
                args.language,
                args.response_format,
                options_hash,
                options_json,
                text,
                now,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM media_transcript_failures WHERE message_id = ? AND chat_jid = ?",
            (args.message_id, args.chat_jid),
        )
        conn.commit()
        transcript = cached_transcript(
            conn,
            message_id=args.message_id,
            chat_jid=args.chat_jid,
            provider=TRANSCRIPT_PROVIDER,
            model=args.model,
            options_hash=options_hash,
        )
    return {
        "cached": False,
        "transcript": transcript,
        "download": download,
        "transcripts_db_path": str(transcripts_db_path()),
    }


def command_media_transcribe(args: argparse.Namespace) -> dict[str, Any]:
    with transcript_message_lock(args.message_id, args.chat_jid):
        return _command_media_transcribe_locked(args)


def resolve_messages_db_path() -> Path:
    db_info = backend_json("db-path", timeout=30)
    if not isinstance(db_info, dict) or not db_info.get("messages_db_path"):
        raise CliError("Could not resolve the WhatsApp messages database path.", code="missing_messages_db")
    path = Path(db_info["messages_db_path"]).expanduser()
    if not path.exists():
        raise CliError(
            "The WhatsApp messages database does not exist yet.",
            code="messages_db_missing",
            details={"messages_db_path": str(path)},
        )
    return path


MAX_TRANSCRIBE_ATTEMPTS = 3


def pending_audio_messages(
    *,
    chat_jid: str | None,
    since: str | None,
    limit: int,
    retry_failed: bool = False,
    exclude_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Audio messages in the bridge store that have no cached transcript yet.

    Pending means no transcript row exists for the message at all, whatever
    options produced it. Backfill never re-spends on an already transcribed
    message; `media transcribe --refresh` remains the way to redo one.

    Only failures classified as permanent drop out of automatic reconciliation.
    Transient API, bridge, metadata, credential, or network failures remain
    eligible on later bridge reconciliation runs regardless of attempt count.
    WhatsApp CDN expiry is permanent and must not be retried forever.
    """
    transcribed: set[tuple[str, str]] = set()
    permanent_failures: set[tuple[str, str]] = set()
    with open_transcripts_db() as conn:
        for row in conn.execute("SELECT message_id, chat_jid FROM media_transcripts"):
            transcribed.add((row["message_id"], row["chat_jid"]))
        for row in conn.execute(
            "SELECT message_id, chat_jid, last_code FROM media_transcript_failures"
        ):
            if row["last_code"] in PERMANENT_TRANSCRIBE_FAILURE_CODES:
                permanent_failures.add((row["message_id"], row["chat_jid"]))

    audio_types = sorted(AUDIO_MEDIA_TYPES)
    clauses = ["LOWER(COALESCE(media_type, '')) IN ({})".format(",".join("?" for _ in audio_types))]
    values: list[Any] = list(audio_types)
    if chat_jid:
        clauses.append("chat_jid = ?")
        values.append(chat_jid)
    if since:
        clauses.append("timestamp >= ?")
        values.append(since)

    conn = sqlite3.connect(f"file:{resolve_messages_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT id, chat_jid, sender, timestamp, media_type FROM messages "
            f"WHERE {' AND '.join(clauses)} ORDER BY timestamp DESC",
            values,
        ).fetchall()
    finally:
        conn.close()

    row_keys = {(row["id"], row["chat_jid"]) for row in rows}
    exhausted = (permanent_failures & row_keys) - transcribed
    excluded = exclude_keys or set()
    pending = [
        {
            "message_id": row["id"],
            "chat_jid": row["chat_jid"],
            "sender": row["sender"],
            "timestamp": row["timestamp"],
            "media_type": row["media_type"],
        }
        for row in rows
        if (row["id"], row["chat_jid"]) not in transcribed
        and (retry_failed or (row["id"], row["chat_jid"]) not in exhausted)
        and (row["id"], row["chat_jid"]) not in excluded
    ]
    return pending[:limit], len(pending), len(exhausted)


def record_transcribe_failure(message_id: str, chat_jid: str, code: str, error: str) -> int:
    now = utc_now()
    attempts = MAX_TRANSCRIBE_ATTEMPTS if code in PERMANENT_TRANSCRIBE_FAILURE_CODES else 1
    with open_transcripts_db() as conn:
        conn.execute(
            """
            INSERT INTO media_transcript_failures (
                message_id, chat_jid, attempts, last_code, last_error, first_attempt_at, last_attempt_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, chat_jid) DO UPDATE SET
                attempts = CASE
                    WHEN excluded.attempts >= ? THEN ?
                    ELSE attempts + 1
                END,
                last_code = excluded.last_code,
                last_error = excluded.last_error,
                last_attempt_at = excluded.last_attempt_at
            """,
            (
                message_id,
                chat_jid,
                attempts,
                code,
                error[:2000],
                now,
                now,
                MAX_TRANSCRIBE_ATTEMPTS,
                MAX_TRANSCRIBE_ATTEMPTS,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT attempts FROM media_transcript_failures WHERE message_id = ? AND chat_jid = ?",
            (message_id, chat_jid),
        ).fetchone()
    return row["attempts"] if row else 1


def command_media_transcribe_pending(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise CliError("media transcribe-pending --limit must be greater than zero.", code="invalid_limit")

    pending, pending_total, gave_up = pending_audio_messages(
        chat_jid=args.chat_jid,
        since=args.since,
        limit=args.limit,
        retry_failed=args.retry_failed,
    )
    result: dict[str, Any] = {
        "pending_total": pending_total,
        "selected": len(pending),
        "gave_up": gave_up,
        "transcripts_db_path": str(transcripts_db_path()),
    }
    if args.dry_run:
        result["dry_run"] = True
        result["messages"] = pending
        return result

    transcribed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    attempted_keys: set[tuple[str, str]] = set()
    stopped_on_error = False

    while pending:
        for item in pending:
            attempted_keys.add((item["message_id"], item["chat_jid"]))
            call_args = argparse.Namespace(
                message_id=item["message_id"],
                chat_jid=item["chat_jid"],
                language=args.language,
                model=args.model,
                response_format=args.response_format,
                diarize=args.diarize,
                num_speakers=args.num_speakers,
                keyterm=args.keyterm,
                no_verbatim=args.no_verbatim,
                refresh=False,
                allow_non_audio=False,
                timeout_seconds=args.timeout_seconds,
            )
            try:
                outcome = command_media_transcribe(call_args)
            except CliError as exc:
                attempts = record_transcribe_failure(
                    item["message_id"], item["chat_jid"], exc.code, str(exc)
                )
                failed.append(
                    {
                        **item,
                        "error": str(exc),
                        "code": exc.code,
                        "attempts": attempts,
                        "gave_up": exc.code in PERMANENT_TRANSCRIBE_FAILURE_CODES,
                    }
                )
                if args.stop_on_error:
                    stopped_on_error = True
                    break
                continue
            transcript = outcome.get("transcript") or {}
            transcribed.append({**item, "transcript_path": transcript.get("transcript_path")})

        if stopped_on_error or not args.drain:
            break

        pending, _, gave_up = pending_audio_messages(
            chat_jid=args.chat_jid,
            since=args.since,
            limit=args.limit,
            retry_failed=args.retry_failed,
            exclude_keys=attempted_keys,
        )

    _, remaining, gave_up = pending_audio_messages(
        chat_jid=args.chat_jid,
        since=args.since,
        limit=1,
        retry_failed=args.retry_failed,
    )

    result["transcribed"] = transcribed
    result["failed"] = failed
    result["attempted"] = len(attempted_keys)
    result["selected"] = len(attempted_keys)
    result["gave_up"] = gave_up
    result["remaining"] = remaining
    return result


def command_media_arrival_hook(args: argparse.Namespace) -> dict[str, Any]:
    """Transcribe one just-arrived audio message.

    Invoked by the bridge on the message-arrival event, so a voice note is
    readable within seconds. Failures are retried here and then recorded: the
    bridge must not be destabilized by a transcription problem, while its
    start/reconnect reconciliation catches anything still pending later.
    """
    if args.media_type.lower() not in AUDIO_MEDIA_TYPES:
        return {"skipped": True, "reason": "not_audio", "media_type": args.media_type}

    call_args = argparse.Namespace(
        message_id=args.message_id,
        chat_jid=args.chat_jid,
        language=args.language,
        model=args.model,
        response_format="text",
        diarize=False,
        num_speakers=None,
        keyterm=[],
        no_verbatim=False,
        refresh=False,
        allow_non_audio=False,
        timeout_seconds=args.timeout_seconds,
    )

    retry_delays = [5.0, 30.0]
    override = os.environ.get("WHATSAPP_TRANSCRIBE_RETRY_DELAYS")
    if override is not None:
        try:
            retry_delays = [max(float(value.strip()), 0.0) for value in override.split(",") if value.strip()]
        except ValueError as exc:
            raise CliError(
                "WHATSAPP_TRANSCRIBE_RETRY_DELAYS must be comma-separated seconds.",
                code="invalid_retry_delays",
            ) from exc

    outcome: dict[str, Any] | None = None
    attempts = 0
    last_error: CliError | None = None
    for attempt_index in range(len(retry_delays) + 1):
        try:
            outcome = command_media_transcribe(call_args)
            break
        except CliError as exc:
            last_error = exc
            attempts = record_transcribe_failure(args.message_id, args.chat_jid, exc.code, str(exc))
            if exc.code in PERMANENT_TRANSCRIBE_FAILURE_CODES:
                break
            if attempt_index < len(retry_delays):
                time.sleep(retry_delays[attempt_index])

    if outcome is None:
        return {
            "transcribed": False,
            "message_id": args.message_id,
            "code": last_error.code if last_error else "transcription_failed",
            "attempts": attempts,
        }

    if outcome.get("cached"):
        return {"skipped": True, "reason": "already_cached", "message_id": args.message_id}

    transcript = outcome.get("transcript") or {}
    return {
        "transcribed": True,
        "message_id": args.message_id,
        "transcript_path": transcript.get("transcript_path"),
    }


def command_media_transcripts_list(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise CliError("media transcripts list --limit must be greater than zero.", code="invalid_limit")
    clauses: list[str] = []
    values: list[Any] = []
    if args.chat_jid:
        clauses.append("chat_jid = ?")
        values.append(args.chat_jid)
    if args.message_id:
        clauses.append("message_id = ?")
        values.append(args.message_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with open_transcripts_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM media_transcripts {where} ORDER BY updated_at DESC LIMIT ?",
            (*values, args.limit),
        ).fetchall()
    return {"transcripts": [row_to_transcript(row) for row in rows], "transcripts_db_path": str(transcripts_db_path())}


def command_media_transcripts_show(args: argparse.Namespace) -> dict[str, Any]:
    with open_transcripts_db() as conn:
        transcript = cached_transcript_by_message(conn, message_id=args.message_id, chat_jid=args.chat_jid)
    return {"transcript": transcript, "transcripts_db_path": str(transcripts_db_path())}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_send_target(chat_jid: str) -> None:
    if not chat_jid or not isinstance(chat_jid, str):
        raise CliError("A chat JID is required.", code="missing_chat_jid")
    if identifier_type(chat_jid) == "broadcast":
        raise CliError("Refusing to create or send messages to WhatsApp Status/Broadcast.", code="broadcast_not_supported")


def validate_message_parts(text: str | None, media_path: str | None) -> None:
    if not (text or "").strip() and not media_path:
        raise CliError("Message text or --media-path is required.", code="empty_message")
    if media_path and not Path(media_path).expanduser().exists():
        raise CliError("Media path does not exist.", code="missing_media", details={"media_path": media_path})


def add_text_input_args(parser: argparse.ArgumentParser) -> None:
    text_input = parser.add_mutually_exclusive_group()
    text_input.add_argument("--text", help="Message text. For multiline text, prefer --text-stdin or --text-file.")
    text_input.add_argument("--text-stdin", action="store_true", help="Read message text from stdin.")
    text_input.add_argument("--text-file", help="Read message text from a UTF-8 file.")


def read_text_input(args: argparse.Namespace, *, fallback: str | None = "") -> str:
    if getattr(args, "text_stdin", False):
        return sys.stdin.read()
    text_file = getattr(args, "text_file", None)
    if text_file:
        path = Path(text_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CliError("Text file does not exist.", code="missing_text_file", details={"text_file": str(path)}) from exc
        except OSError as exc:
            raise CliError("Failed to read text file.", code="text_file_read_failed", details={"text_file": str(path), "error": str(exc)}) from exc
    text = getattr(args, "text", None)
    if text is not None:
        return text
    return fallback or ""


def text_warnings(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    sequences = [sequence for sequence in ("\\n", "\\r") if sequence in text]
    if not sequences:
        return []
    return [
        {
            "code": "literal_backslash_escape",
            "message": "Text contains literal backslash escape sequences. Use --text-stdin or --text-file for real newlines.",
            "sequences": sequences,
        }
    ]


def validate_literal_escape_send(text: str, *, confirm: bool, allow_literal_escapes: bool) -> None:
    warnings = text_warnings(text)
    if confirm and warnings and not allow_literal_escapes:
        raise CliError(
            "Refusing to send text containing literal backslash escape sequences. Use --text-stdin or --text-file for real newlines, or pass --allow-literal-escapes if the literal characters are intentional.",
            code="literal_escape_send_guard",
            details={"warnings": warnings},
        )


def message_summary(
    message: dict[str, Any],
    context: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": message.get("id"),
        "chat_jid": message.get("chat_jid"),
        "sender": participant_jid_for_sender(message.get("sender"), context)
        or message.get("sender"),
        "content": message.get("content"),
        "media_type": message.get("media_type"),
        "timestamp": message.get("timestamp"),
        "is_from_me": message.get("is_from_me"),
    }


def resolve_reply_target(chat_jid: str, reply_to_message_id: str | None) -> dict[str, Any] | None:
    if not reply_to_message_id:
        return None

    context = backend_json(
        "get-message-context",
        reply_to_message_id,
        "--before",
        "0",
        "--after",
        "0",
        timeout=30,
    )
    message = context.get("message") if isinstance(context, dict) else None
    if not isinstance(message, dict) or not message.get("id"):
        raise CliError(
            "Reply target message was not found.",
            code="reply_target_not_found",
            details={"reply_to_message_id": reply_to_message_id},
        )
    if message.get("chat_jid") != chat_jid:
        raise CliError(
            "Reply target belongs to a different chat.",
            code="reply_target_chat_mismatch",
            details={
                "chat_jid": chat_jid,
                "reply_chat_jid": message.get("chat_jid"),
                "reply_to_message_id": reply_to_message_id,
            },
        )
    return message_summary(message, load_identity_context())


def draft_db_path() -> Path:
    try:
        db_info = backend_json("db-path", timeout=30)
        if isinstance(db_info, dict) and db_info.get("messages_db_path"):
            return Path(db_info["messages_db_path"]).with_name("drafts.db")
    except CliError:
        pass
    return DEFAULT_DRAFTS_DB_PATH


def open_draft_db() -> sqlite3.Connection:
    path = draft_db_path()
    ensure_private_directory(path.parent)
    enforce_private_database_files(path)
    conn = sqlite3.connect(path)
    enforce_private_database_files(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            chat_jid TEXT NOT NULL,
            chat_display_name TEXT,
            chat_phone_number TEXT,
            chat_type TEXT,
            text TEXT NOT NULL DEFAULT '',
            media_path TEXT,
            reply_to_message_id TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            sent_message TEXT
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    if "reply_to_message_id" not in columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN reply_to_message_id TEXT")
    conn.commit()
    enforce_private_database_files(path)
    return conn


def row_to_draft(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_jid": row["chat_jid"],
        "chat_display_name": row["chat_display_name"],
        "chat_phone_number": row["chat_phone_number"],
        "chat_type": row["chat_type"],
        "text": row["text"],
        "media_path": row["media_path"],
        "reply_to_message_id": row["reply_to_message_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sent_at": row["sent_at"],
        "sent_message": row["sent_message"],
    }


def fetch_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise CliError("Draft not found.", code="draft_not_found", details={"draft_id": draft_id})
    return row_to_draft(row)


def chat_metadata(chat_jid: str) -> dict[str, str | None]:
    try:
        chat = annotate_chat(backend_json("get-chat", chat_jid, "--no-last-message"), load_identity_context())
    except CliError:
        chat = {}
    if not isinstance(chat, dict):
        chat = {}
    return {
        "chat_display_name": chat.get("display_name") if isinstance(chat.get("display_name"), str) else None,
        "chat_phone_number": chat.get("phone_number") if isinstance(chat.get("phone_number"), str) else None,
        "chat_type": chat.get("chat_type") if isinstance(chat.get("chat_type"), str) else chat_type_for_identifier(identifier_type(chat_jid)),
    }


def bridge_api_base_url() -> str:
    env_url = os.environ.get("WHATSAPP_MCP_API_BASE_URL") or os.environ.get("WHATSAPP_API_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    status = bridge_status()
    port = status.get("http_port") if isinstance(status, dict) else None
    if not port:
        raise CliError("WhatsApp bridge HTTP port is unavailable.", code="bridge_port_missing")
    return f"http://127.0.0.1:{port}"


def bridge_post(path: str, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any]:
    url = f"{bridge_api_base_url()}{path}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CliError(
            "WhatsApp bridge request failed.",
            code="bridge_http_error",
            details={"status": exc.code, "body": body[-2000:], "url": url},
        ) from exc
    except urllib.error.URLError as exc:
        raise CliError("WhatsApp bridge is unreachable.", code="bridge_unreachable", details={"url": url, "reason": str(exc)}) from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CliError("WhatsApp bridge returned non-JSON output.", code="invalid_bridge_json", details={"body": body[-2000:]}) from exc
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def send_message(
    chat_jid: str,
    text: str,
    media_path: str | None,
    reply_to_message_id: str | None,
    *,
    dry_run: bool,
    confirm: bool,
    allow_literal_escapes: bool = False,
) -> dict[str, Any]:
    validate_send_target(chat_jid)
    media_path = str(Path(media_path).expanduser()) if media_path else None
    validate_message_parts(text, media_path)
    validate_literal_escape_send(text, confirm=confirm, allow_literal_escapes=allow_literal_escapes)
    reply_target = resolve_reply_target(chat_jid, reply_to_message_id)
    warnings = text_warnings(text)
    payload = {
        "recipient": chat_jid,
        "message": text or "",
    }
    if media_path:
        payload["media_path"] = media_path
    if reply_target:
        payload["reply_to_message_id"] = reply_target["id"]
        payload["reply_to_sender"] = reply_target["sender"] or ""
        payload["reply_to_content"] = reply_target["content"] or ""
        payload["reply_to_media_type"] = reply_target["media_type"] or ""
    preview = {
        "chat_jid": chat_jid,
        "text": text or "",
        "media_path": media_path,
        "reply_to_message_id": reply_to_message_id,
        "reply_target": reply_target,
        "warnings": warnings,
    }
    if dry_run:
        return {"dry_run": True, "message": preview}
    if not confirm:
        raise CliError("Live WhatsApp send requires --confirm. Use --dry-run to preview.", code="confirm_required")
    response = bridge_post("/api/send", payload, timeout=180 if media_path else 60)
    return {"dry_run": False, "message": preview, "bridge_response": response}


def command_messages_send(args: argparse.Namespace) -> dict[str, Any]:
    text = read_text_input(args)
    return send_message(
        args.chat_jid,
        text,
        args.media_path,
        args.reply_to_message_id,
        dry_run=args.dry_run,
        confirm=args.confirm,
        allow_literal_escapes=args.allow_literal_escapes,
    )


def command_drafts_create(args: argparse.Namespace) -> dict[str, Any]:
    validate_send_target(args.chat_jid)
    text = read_text_input(args)
    media_path = str(Path(args.media_path).expanduser()) if args.media_path else None
    validate_message_parts(text, media_path)
    resolve_reply_target(args.chat_jid, args.reply_to_message_id)
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    metadata = chat_metadata(args.chat_jid)
    with open_draft_db() as conn:
        conn.execute(
            """
            INSERT INTO drafts (
                id, chat_jid, chat_display_name, chat_phone_number, chat_type,
                text, media_path, reply_to_message_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                draft_id,
                args.chat_jid,
                metadata["chat_display_name"],
                metadata["chat_phone_number"],
                metadata["chat_type"],
                text,
                media_path,
                args.reply_to_message_id,
                now,
                now,
            ),
        )
        conn.commit()
        draft = fetch_draft(conn, draft_id)
    return {"draft": draft, "warnings": text_warnings(text), "drafts_db_path": str(draft_db_path())}


def command_drafts_list(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise CliError("drafts list --limit must be greater than zero.", code="invalid_limit")
    clauses: list[str] = []
    values: list[Any] = []
    if args.status != "all":
        clauses.append("status = ?")
        values.append(args.status)
    if args.chat_jid:
        clauses.append("chat_jid = ?")
        values.append(args.chat_jid)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with open_draft_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM drafts {where} ORDER BY updated_at DESC LIMIT ?",
            (*values, args.limit),
        ).fetchall()
    return {"drafts": [row_to_draft(row) for row in rows], "drafts_db_path": str(draft_db_path())}


def command_drafts_show(args: argparse.Namespace) -> dict[str, Any]:
    with open_draft_db() as conn:
        draft = fetch_draft(conn, args.draft_id)
    return {"draft": draft, "drafts_db_path": str(draft_db_path())}


def command_drafts_update(args: argparse.Namespace) -> dict[str, Any]:
    if args.clear_media and args.media_path:
        raise CliError("Use either --media-path or --clear-media, not both.", code="invalid_update")
    if args.clear_reply and args.reply_to_message_id:
        raise CliError("Use either --reply-to-message-id or --clear-reply, not both.", code="invalid_update")
    with open_draft_db() as conn:
        existing = fetch_draft(conn, args.draft_id)
        if existing["status"] != "draft":
            raise CliError("Only open drafts can be updated.", code="draft_not_open", details={"status": existing["status"]})
        chat_jid = args.chat_jid or existing["chat_jid"]
        text = read_text_input(args, fallback=existing["text"])
        media_path = existing["media_path"]
        reply_to_message_id = (
            existing["reply_to_message_id"]
            if args.reply_to_message_id is None
            else args.reply_to_message_id
        )
        if args.clear_media:
            media_path = None
        elif args.media_path:
            media_path = str(Path(args.media_path).expanduser())
        if args.clear_reply:
            reply_to_message_id = None
        validate_send_target(chat_jid)
        validate_message_parts(text, media_path)
        resolve_reply_target(chat_jid, reply_to_message_id)
        metadata = chat_metadata(chat_jid) if chat_jid != existing["chat_jid"] else {
            "chat_display_name": existing["chat_display_name"],
            "chat_phone_number": existing["chat_phone_number"],
            "chat_type": existing["chat_type"],
        }
        conn.execute(
            """
            UPDATE drafts
            SET chat_jid = ?, chat_display_name = ?, chat_phone_number = ?, chat_type = ?,
                text = ?, media_path = ?, reply_to_message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                chat_jid,
                metadata["chat_display_name"],
                metadata["chat_phone_number"],
                metadata["chat_type"],
                text,
                media_path,
                reply_to_message_id,
                utc_now(),
                args.draft_id,
            ),
        )
        conn.commit()
        draft = fetch_draft(conn, args.draft_id)
    return {"draft": draft, "warnings": text_warnings(text), "drafts_db_path": str(draft_db_path())}


def command_drafts_delete(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm:
        raise CliError("Deleting a WhatsApp draft requires --confirm.", code="confirm_required")
    with open_draft_db() as conn:
        draft = fetch_draft(conn, args.draft_id)
        conn.execute("DELETE FROM drafts WHERE id = ?", (args.draft_id,))
        conn.commit()
    return {"deleted": draft}


def command_drafts_send(args: argparse.Namespace) -> dict[str, Any]:
    with open_draft_db() as conn:
        draft = fetch_draft(conn, args.draft_id)
        if draft["status"] != "draft":
            raise CliError("Only open drafts can be sent.", code="draft_not_open", details={"status": draft["status"]})
        result = send_message(
            draft["chat_jid"],
            draft["text"],
            draft["media_path"],
            draft["reply_to_message_id"],
            dry_run=args.dry_run,
            confirm=args.confirm,
            allow_literal_escapes=args.allow_literal_escapes,
        )
        if not args.dry_run:
            now = utc_now()
            conn.execute(
                """
                UPDATE drafts
                SET status = 'sent', sent_at = ?, sent_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, json.dumps(result.get("bridge_response", {}), sort_keys=True), now, args.draft_id),
            )
            conn.commit()
            draft = fetch_draft(conn, args.draft_id)
    return {"draft": draft, "send": result, "drafts_db_path": str(draft_db_path())}


def add_page_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--page", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whatsapp", description="Read WhatsApp chats, draft replies, and send approved messages through the local bridge.")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON envelope to stdout.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Verify source root, tools, bridge status, and database path.").set_defaults(func=command_doctor)

    bridge_parser = subparsers.add_parser("bridge", help="Manage the local read-only WhatsApp bridge.")
    bridge_sub = bridge_parser.add_subparsers(dest="bridge_command", required=True)
    bridge_sub.add_parser("status", help="Show bridge health and state paths.").set_defaults(func=command_bridge_status)
    bridge_sub.add_parser("start", help="Start the durable bridge if a session is linked.").set_defaults(func=command_bridge_start)
    bridge_sub.add_parser("stop", help="Stop the durable bridge and stale MCP helper processes.").set_defaults(func=command_bridge_stop)
    bridge_sub.add_parser("setup", help="Relink WhatsApp by running the setup flow.").set_defaults(func=command_bridge_setup)
    relink = bridge_sub.add_parser(
        "relink",
        help="Reset local sync state, run linked-device pairing setup, then start the bridge.",
    )
    relink.add_argument("--confirm", action="store_true")
    relink.set_defaults(func=command_bridge_relink)
    reset = bridge_sub.add_parser("reset-sync", help="Delete local sync state before relinking.")
    reset.add_argument("--confirm", action="store_true")
    reset.set_defaults(func=command_bridge_reset_sync)

    contacts = subparsers.add_parser("contacts", help="Search contacts.")
    contacts_sub = contacts.add_subparsers(dest="contacts_command", required=True)
    contacts_search = contacts_sub.add_parser("search", help="Search contacts by name or phone.")
    contacts_search.add_argument("query")
    contacts_search.set_defaults(func=command_contacts_search)

    chats = subparsers.add_parser("chats", help="List and inspect chats.")
    chats_sub = chats.add_subparsers(dest="chats_command", required=True)
    chats_list = chats_sub.add_parser("list", help="List chats.")
    chats_list.add_argument("--query")
    chats_list.add_argument("--sort-by", choices=["last_active", "name"], default="last_active")
    chats_list.add_argument("--no-last-message", action="store_true")
    chats_list.add_argument(
        "--include-identity-duplicates",
        action="store_true",
        help="Show raw phone/LID identity rows instead of collapsing direct chats by phone number.",
    )
    add_page_args(chats_list)
    chats_list.set_defaults(func=command_chats_list)
    chats_get = chats_sub.add_parser("get", help="Get a chat by JID.")
    chats_get.add_argument("chat_jid")
    chats_get.add_argument("--no-last-message", action="store_true")
    chats_get.set_defaults(func=command_chats_get)
    chats_direct = chats_sub.add_parser("direct", help="Get a direct chat by sender phone number.")
    chats_direct.add_argument("sender_phone_number")
    chats_direct.set_defaults(func=command_chats_direct)
    chats_contact = chats_sub.add_parser("contact", help="Get chats involving a contact JID.")
    chats_contact.add_argument("jid")
    add_page_args(chats_contact)
    chats_contact.set_defaults(func=command_chats_contact)
    chats_last = chats_sub.add_parser("last-interaction", help="Get the most recent interaction involving a contact JID.")
    chats_last.add_argument("jid")
    chats_last.set_defaults(func=command_chats_last_interaction)

    messages = subparsers.add_parser("messages", help="Read messages and nearby context.")
    messages_sub = messages.add_subparsers(dest="messages_command", required=True)
    messages_list = messages_sub.add_parser("list", help="List messages in a chat, date range, sender, or query.")
    messages_list.add_argument("--after")
    messages_list.add_argument("--before")
    messages_list.add_argument("--sender-phone-number")
    messages_list.add_argument("--chat-jid")
    messages_list.add_argument("--query")
    messages_list.add_argument(
        "--include-identity-duplicates",
        action="store_true",
        help="Read only the exact raw chat JID instead of expanding equivalent phone/LID identities.",
    )
    messages_list.add_argument("--include-context", action="store_true")
    messages_list.add_argument("--context-before", type=int, default=1)
    messages_list.add_argument("--context-after", type=int, default=1)
    add_page_args(messages_list)
    messages_list.set_defaults(func=command_messages_list)
    messages_context = messages_sub.add_parser("context", help="Get context around one message.")
    messages_context.add_argument("message_id")
    messages_context.add_argument("--before", type=int, default=5)
    messages_context.add_argument("--after", type=int, default=5)
    messages_context.set_defaults(func=command_messages_context)
    messages_send = messages_sub.add_parser("send", help="Send a text/media message to an exact chat JID.")
    messages_send.add_argument("--chat-jid", required=True)
    add_text_input_args(messages_send)
    messages_send.add_argument("--media-path")
    messages_send.add_argument("--reply-to-message-id")
    messages_send.add_argument(
        "--allow-literal-escapes",
        action="store_true",
        help="Allow live sends with literal backslash escape sequences such as \\n.",
    )
    send_mode = messages_send.add_mutually_exclusive_group()
    send_mode.add_argument("--dry-run", action="store_true", help="Preview the payload without sending.")
    send_mode.add_argument("--confirm", action="store_true", help="Actually send the message.")
    messages_send.set_defaults(func=command_messages_send)

    media = subparsers.add_parser("media", help="Download message media for inspection.")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    media_download = media_sub.add_parser("download", help="Download media from a message.")
    media_download.add_argument("message_id")
    media_download.add_argument("chat_jid")
    media_download.set_defaults(func=command_media_download)
    media_transcribe = media_sub.add_parser(
        "transcribe",
        help="Transcribe one requested audio message with ElevenLabs and cache the result.",
    )
    media_transcribe.add_argument("message_id")
    media_transcribe.add_argument("chat_jid")
    media_transcribe.add_argument("--language", help="Optional language hint such as 'es'.")
    media_transcribe.add_argument("--model", default="scribe_v2")
    media_transcribe.add_argument(
        "--response-format",
        default="text",
        choices=["text", "json", "diarized_text", "segments_json"],
    )
    media_transcribe.add_argument("--diarize", action="store_true")
    media_transcribe.add_argument("--num-speakers", type=int)
    media_transcribe.add_argument("--keyterm", action="append", default=[])
    media_transcribe.add_argument("--no-verbatim", action="store_true")
    media_transcribe.add_argument("--refresh", action="store_true", help="Ignore a cached transcript and call ElevenLabs again.")
    media_transcribe.add_argument(
        "--allow-non-audio",
        action="store_true",
        help="Allow transcription attempts for media types other than WhatsApp audio.",
    )
    media_transcribe.add_argument("--timeout-seconds", type=int, default=1800)
    media_transcribe.set_defaults(func=command_media_transcribe)
    media_transcribe_pending = media_sub.add_parser(
        "transcribe-pending",
        help="Transcribe audio messages that have no cached transcript yet.",
    )
    media_transcribe_pending.add_argument("--chat-jid", help="Restrict the backfill to one chat.")
    media_transcribe_pending.add_argument("--since", help="Only messages at or after this timestamp, such as '2026-08-01'.")
    media_transcribe_pending.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum messages to transcribe in one run (default 25). Each one is a paid ElevenLabs call.",
    )
    media_transcribe_pending.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the pending backlog without calling ElevenLabs.",
    )
    media_transcribe_pending.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop at the first failure instead of continuing through the batch.",
    )
    media_transcribe_pending.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Include messages already given up on after repeated failures. "
            "Media expired from WhatsApp's CDN never becomes available again."
        ),
    )
    media_transcribe_pending.add_argument(
        "--drain",
        action="store_true",
        help="Process every eligible pending audio message in bounded sequential batches.",
    )
    media_transcribe_pending.add_argument("--language", help="Optional language hint such as 'es'.")
    media_transcribe_pending.add_argument("--model", default="scribe_v2")
    media_transcribe_pending.add_argument(
        "--response-format",
        default="text",
        choices=["text", "json", "diarized_text", "segments_json"],
    )
    media_transcribe_pending.add_argument("--diarize", action="store_true")
    media_transcribe_pending.add_argument("--num-speakers", type=int)
    media_transcribe_pending.add_argument("--keyterm", action="append", default=[])
    media_transcribe_pending.add_argument("--no-verbatim", action="store_true")
    media_transcribe_pending.add_argument("--timeout-seconds", type=int, default=1800)
    media_transcribe_pending.set_defaults(func=command_media_transcribe_pending)
    media_arrival_hook = media_sub.add_parser(
        "arrival-hook",
        help="Transcribe a just-arrived audio message. Invoked by the bridge, not by hand.",
    )
    media_arrival_hook.add_argument("media_type")
    media_arrival_hook.add_argument("message_id")
    media_arrival_hook.add_argument("chat_jid")
    media_arrival_hook.add_argument("--language", help="Optional language hint such as 'es'.")
    media_arrival_hook.add_argument("--model", default="scribe_v2")
    media_arrival_hook.add_argument("--timeout-seconds", type=int, default=1800)
    media_arrival_hook.set_defaults(func=command_media_arrival_hook)
    media_transcripts = media_sub.add_parser("transcripts", help="Inspect cached media transcripts.")
    media_transcripts_sub = media_transcripts.add_subparsers(dest="media_transcripts_command", required=True)
    media_transcripts_list = media_transcripts_sub.add_parser("list", help="List cached media transcripts.")
    media_transcripts_list.add_argument("--chat-jid")
    media_transcripts_list.add_argument("--message-id")
    media_transcripts_list.add_argument("--limit", type=int, default=20)
    media_transcripts_list.set_defaults(func=command_media_transcripts_list)
    media_transcripts_show = media_transcripts_sub.add_parser("show", help="Show the latest cached transcript for a message.")
    media_transcripts_show.add_argument("message_id")
    media_transcripts_show.add_argument("--chat-jid")
    media_transcripts_show.set_defaults(func=command_media_transcripts_show)

    drafts = subparsers.add_parser("drafts", help="Create, review, update, and send local WhatsApp drafts.")
    drafts_sub = drafts.add_subparsers(dest="drafts_command", required=True)
    drafts_create = drafts_sub.add_parser("create", help="Create a local draft for an exact chat JID.")
    drafts_create.add_argument("--chat-jid", required=True)
    add_text_input_args(drafts_create)
    drafts_create.add_argument("--media-path")
    drafts_create.add_argument("--reply-to-message-id")
    drafts_create.set_defaults(func=command_drafts_create)
    drafts_list = drafts_sub.add_parser("list", help="List local drafts.")
    drafts_list.add_argument("--status", choices=["draft", "sent", "all"], default="draft")
    drafts_list.add_argument("--chat-jid")
    drafts_list.add_argument("--limit", type=int, default=20)
    drafts_list.set_defaults(func=command_drafts_list)
    drafts_show = drafts_sub.add_parser("show", help="Show one local draft.")
    drafts_show.add_argument("draft_id")
    drafts_show.set_defaults(func=command_drafts_show)
    drafts_update = drafts_sub.add_parser("update", help="Update one open local draft.")
    drafts_update.add_argument("draft_id")
    drafts_update.add_argument("--chat-jid")
    add_text_input_args(drafts_update)
    drafts_update.add_argument("--media-path")
    drafts_update.add_argument("--clear-media", action="store_true")
    drafts_update.add_argument("--reply-to-message-id")
    drafts_update.add_argument("--clear-reply", action="store_true")
    drafts_update.set_defaults(func=command_drafts_update)
    drafts_delete = drafts_sub.add_parser("delete", help="Delete one local draft.")
    drafts_delete.add_argument("draft_id")
    drafts_delete.add_argument("--confirm", action="store_true")
    drafts_delete.set_defaults(func=command_drafts_delete)
    drafts_send = drafts_sub.add_parser("send", help="Send one local draft through the WhatsApp bridge.")
    drafts_send.add_argument("draft_id")
    drafts_send.add_argument(
        "--allow-literal-escapes",
        action="store_true",
        help="Allow live sends with literal backslash escape sequences such as \\n.",
    )
    draft_send_mode = drafts_send.add_mutually_exclusive_group()
    draft_send_mode.add_argument("--dry-run", action="store_true", help="Preview the send payload without sending.")
    draft_send_mode.add_argument("--confirm", action="store_true", help="Actually send the draft.")
    drafts_send.set_defaults(func=command_drafts_send)

    return parser


def emit_result(result: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"ok": True, "data": result}, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True))


def emit_error(error: CliError, *, json_mode: bool) -> None:
    payload = {"ok": False, "error": {"code": error.code, "message": str(error), "details": error.details}}
    if json_mode:
        print(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except CliError as exc:
        emit_error(exc, json_mode=args.json)
        return 1
    emit_result(result, json_mode=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
