#!/usr/bin/env python3
"""Build the bridge's messages.db (schema version 2) from earlier WhatsApp stores.

Releases 0.11.14 to 0.12.1 kept WhatsApp data in a Near Convex deployment;
releases before that kept it in a messages.db with text timestamps. This tool
moves either or both into the current local store without losing history:

  export-convex  Read the whatsapp tables out of a Convex deployment through its
                 streaming export API (read-only). Without --cursor it takes a
                 full snapshot; with --cursor it reads the changes since then.
  build          Write a new messages.db from a pre-0.11.14 messages.db and/or
                 Convex exports (a snapshot followed by any deltas). Rows are
                 deduplicated by key, Convex winning over the older file, and
                 the per-table counts are verified before the file is kept.

Stop the bridge before the final export and build, then put the result at
<store>/messages.db (keep the old file as a backup).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "vendor" / "lharries-whatsapp-mcp" / "whatsapp-bridge" / "schema.sql"
SCHEMA_VERSION = 2
TABLES = ["waChats", "waMessages", "waReactions", "waReceipts", "waLids", "waSelf"]

# Each table's key: rows sharing it are one row in messages.db.
KEYS = {
    "chats": ("jid",),
    "messages": ("chat_jid", "id"),
    "message_reactions": ("chat_jid", "target_message_id", "reaction_sender"),
    "message_receipts": ("chat_jid", "message_id", "receipt_type", "receipt_sender", "message_sender"),
    "lid_map": ("lid",),
    "self": ("id",),
}
CONVEX_TABLE = {
    "waChats": "chats",
    "waMessages": "messages",
    "waReactions": "message_reactions",
    "waReceipts": "message_receipts",
    "waLids": "lid_map",
    "waSelf": "self",
}


# ---- export ----

def admin_key() -> str:
    key = os.environ.get("WHATSAPP_CONVEX_ADMIN_KEY", "").strip()
    if key:
        return key
    process = subprocess.run(
        ["security", "find-generic-password", "-a", "near", "-s", "n4.convex-admin", "-w"],
        capture_output=True, text=True, check=False,
    )
    if process.returncode != 0 or not process.stdout.strip():
        sys.exit("No Convex admin key: set WHATSAPP_CONVEX_ADMIN_KEY or add the Keychain item (account near, service n4.convex-admin).")
    return process.stdout.strip()


def convex_get(url: str, key: str, endpoint: str, params: dict) -> dict:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/{endpoint}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Convex {key}"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)


def command_export(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = admin_key()
    counts: dict[str, int] = {}
    if args.cursor is None:
        snapshot = None
        for table in TABLES:
            path = out / f"{table}.jsonl"
            cursor = None
            counts[table] = 0
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
                while True:
                    params = {"tableName": table, "format": "json"}
                    if snapshot is not None:
                        params["snapshot"] = snapshot
                    if cursor is not None:
                        params["cursor"] = cursor
                    page = convex_get(args.url, key, "list_snapshot", params)
                    snapshot = page["snapshot"]
                    for value in page["values"]:
                        f.write(json.dumps(value, ensure_ascii=False) + "\n")
                        counts[table] += 1
                    cursor = page.get("cursor")
                    if not page.get("hasMore"):
                        break
        manifest = {"kind": "snapshot", "cursor": snapshot, "counts": counts}
    else:
        cursor = args.cursor
        path = out / "deltas.jsonl"
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            while True:
                page = convex_get(args.url, key, "document_deltas", {"cursor": cursor, "format": "json"})
                for value in page["values"]:
                    if value.get("_table") in TABLES:
                        f.write(json.dumps(value, ensure_ascii=False) + "\n")
                        name = value["_table"] + (" deleted" if value.get("_deleted") else "")
                        counts[name] = counts.get(name, 0) + 1
                cursor = page["cursor"]
                if not page.get("hasMore"):
                    break
        manifest = {"kind": "deltas", "since": args.cursor, "cursor": cursor, "counts": counts}
    with open(os.open(out / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(manifest, f, indent=1)
    print(json.dumps(manifest))


# ---- conversion ----

_TIME = re.compile(r"^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?")


def legacy_ms(value) -> int | None:
    """A go-sqlite3 TIMESTAMP ("2006-01-02 15:04:05.999999999-07:00") as epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _TIME.match(str(value).strip())
    if not match:
        raise ValueError(f"unparseable legacy time {value!r}")
    date, clock, fraction, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else zone
    if len(zone) == 5:
        zone = zone[:3] + ":" + zone[3:]
    parsed = datetime.fromisoformat(f"{date}T{clock}{zone}")
    whole = int(parsed.timestamp()) * 1000
    return whole + int((fraction or "0")[:3].ljust(3, "0"))


def none_if_empty(value):
    return None if value in (None, "", b"") else value


def b64(value):
    return base64.b64decode(value) if value else None


def ms(value):
    return None if value is None else int(round(value))


def legacy_rows(path: Path) -> dict[str, dict[tuple, dict]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables: dict[str, dict[tuple, dict]] = {name: {} for name in KEYS}

    def add(table, row):
        tables[table][tuple(row[k] for k in KEYS[table])] = row

    for c in conn.execute("SELECT * FROM chats"):
        add("chats", {"jid": c["jid"], "name": none_if_empty(c["name"]), "last_message_time": legacy_ms(c["last_message_time"])})
    for m in conn.execute("SELECT * FROM messages"):
        add("messages", {
            "id": m["id"], "chat_jid": m["chat_jid"], "sender": m["sender"] or "", "content": m["content"] or "",
            "timestamp": legacy_ms(m["timestamp"]) or 0, "is_from_me": int(bool(m["is_from_me"])),
            "media_type": none_if_empty(m["media_type"]),
            "reply_to_message_id": none_if_empty(m["reply_to_message_id"]),
            "reply_to_sender": none_if_empty(m["reply_to_sender"]),
            "reply_to_content": none_if_empty(m["reply_to_content"]),
            "reply_to_media_type": none_if_empty(m["reply_to_media_type"]),
            "filename": none_if_empty(m["filename"]), "url": none_if_empty(m["url"]),
            "media_key": none_if_empty(m["media_key"]), "file_sha256": none_if_empty(m["file_sha256"]),
            "file_enc_sha256": none_if_empty(m["file_enc_sha256"]),
            "file_length": m["file_length"] or None, "edited_at": legacy_ms(m["edited_at"]),
        })
    for r in conn.execute("SELECT * FROM message_reactions"):
        add("message_reactions", {
            "chat_jid": r["chat_jid"], "target_message_id": r["target_message_id"], "target_sender": r["target_sender"] or "",
            "reaction_sender": r["reaction_sender"], "emoji": r["emoji"],
            "reaction_message_id": none_if_empty(r["reaction_message_id"]), "grouping_key": none_if_empty(r["grouping_key"]),
            "sender_timestamp_ms": r["sender_timestamp_ms"] or None, "timestamp": legacy_ms(r["timestamp"]),
            "is_from_me": int(bool(r["is_from_me"])),
        })
    for r in conn.execute("SELECT * FROM message_receipts"):
        add("message_receipts", {
            "message_id": r["message_id"], "chat_jid": r["chat_jid"], "receipt_type": r["receipt_type"],
            "receipt_sender": r["receipt_sender"], "message_sender": r["message_sender"] or "",
            "timestamp": legacy_ms(r["timestamp"]),
        })
    conn.close()
    return tables


def convex_row(table: str, d: dict) -> dict:
    if table == "waChats":
        return {"jid": d["jid"], "name": none_if_empty(d.get("name")), "last_message_time": ms(d.get("lastMessageTime"))}
    if table == "waMessages":
        return {
            "id": d["id"], "chat_jid": d["chatJid"], "sender": d.get("sender") or "", "content": d.get("content") or "",
            "timestamp": ms(d["timestamp"]), "is_from_me": int(bool(d.get("isFromMe"))),
            "media_type": none_if_empty(d.get("mediaType")),
            "reply_to_message_id": none_if_empty(d.get("replyToMessageId")),
            "reply_to_sender": none_if_empty(d.get("replyToSender")),
            "reply_to_content": none_if_empty(d.get("replyToContent")),
            "reply_to_media_type": none_if_empty(d.get("replyToMediaType")),
            "filename": none_if_empty(d.get("filename")), "url": none_if_empty(d.get("url")),
            "media_key": b64(d.get("mediaKey")), "file_sha256": b64(d.get("fileSha256")),
            "file_enc_sha256": b64(d.get("fileEncSha256")),
            "file_length": ms(d.get("fileLength")) or None, "edited_at": ms(d.get("editedAt")),
        }
    if table == "waReactions":
        return {
            "chat_jid": d["chatJid"], "target_message_id": d["targetMessageId"], "target_sender": d.get("targetSender") or "",
            "reaction_sender": d["reactionSender"], "emoji": d["emoji"],
            "reaction_message_id": none_if_empty(d.get("reactionMessageId")), "grouping_key": none_if_empty(d.get("groupingKey")),
            "sender_timestamp_ms": ms(d.get("senderTimestampMs")), "timestamp": ms(d.get("timestamp")),
            "is_from_me": int(bool(d.get("isFromMe"))),
        }
    if table == "waReceipts":
        return {
            "message_id": d["messageId"], "chat_jid": d["chatJid"], "receipt_type": d["receiptType"],
            "receipt_sender": d["receiptSender"], "message_sender": d.get("messageSender") or "",
            "timestamp": ms(d.get("timestamp")),
        }
    if table == "waLids":
        return {"lid": d["lid"], "pn": d["pn"]}
    if table == "waSelf":
        return {"id": 1, "jid": d["jid"]}
    raise KeyError(table)


def convex_documents(directories: list[Path]) -> dict[str, dict[str, dict]]:
    """The Convex documents as of the last export: a snapshot, then deltas applied in order."""
    documents: dict[str, dict[str, dict]] = {table: {} for table in TABLES}
    for index, directory in enumerate(directories):
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["kind"] == "snapshot":
            if index != 0:
                sys.exit(f"{directory}: a snapshot must come first")
            for table in TABLES:
                with open(directory / f"{table}.jsonl", encoding="utf-8") as f:
                    for line in f:
                        doc = json.loads(line)
                        documents[table][doc["_id"]] = doc
                if len(documents[table]) != manifest["counts"][table]:
                    sys.exit(f"{directory}/{table}.jsonl holds {len(documents[table])} documents, manifest says {manifest['counts'][table]}")
        else:
            if index == 0:
                sys.exit(f"{directory}: deltas need a snapshot before them")
            with open(directory / "deltas.jsonl", encoding="utf-8") as f:
                for line in f:
                    doc = json.loads(line)
                    if doc.get("_deleted"):
                        documents[doc["_table"]].pop(doc["_id"], None)
                    else:
                        documents[doc["_table"]][doc["_id"]] = doc
    return documents


def command_build(args: argparse.Namespace) -> None:
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} already exists; choose a new path")
    legacy = legacy_rows(Path(args.legacy)) if args.legacy else {name: {} for name in KEYS}

    convex: dict[str, dict[tuple, dict]] = {name: {} for name in KEYS}
    if args.convex:
        for table, docs in convex_documents([Path(d) for d in args.convex]).items():
            target = CONVEX_TABLE[table]
            for doc in docs.values():
                row = convex_row(table, doc)
                key = tuple(row[k] for k in KEYS[target])
                if key in convex[target]:
                    sys.exit(f"duplicate Convex {table} key {key}")
                convex[target][key] = row

    merged = {name: {**legacy[name], **convex[name]} for name in KEYS}

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    conn = sqlite3.connect(tmp)
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    for table, rows in merged.items():
        for row in rows.values():
            columns = list(row)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [row[c] for c in columns],
            )
    conn.commit()

    report = {}
    ok = True
    for table in KEYS:
        stored = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        legacy_only = len(set(legacy[table]) - set(convex[table]))
        expected = len(convex[table]) + legacy_only
        report[table] = {
            "legacy": len(legacy[table]),
            "convex": len(convex[table]),
            "legacy_only": legacy_only,
            "expected": expected,
            "stored": stored,
        }
        ok = ok and stored == expected
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    report["integrity"] = integrity
    print(json.dumps(report, indent=1))
    if not ok or integrity != "ok":
        sys.exit(f"verification failed; left {tmp} for inspection")
    os.replace(tmp, out)
    print(f"wrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-convex", help="Export the whatsapp tables from a Convex deployment (read-only)")
    export.add_argument("--out", required=True, help="New directory for the export")
    export.add_argument("--url", default=os.environ.get("WHATSAPP_CONVEX_URL", "http://127.0.0.1:3210"))
    export.add_argument("--cursor", type=int, help="Export only the changes since this cursor (a previous export's)")
    export.set_defaults(func=command_export)
    build = sub.add_parser("build", help="Write a new messages.db from older stores")
    build.add_argument("--out", required=True, help="Path of the new messages.db (must not exist)")
    build.add_argument("--legacy", help="A pre-0.11.14 messages.db")
    build.add_argument("--convex", action="append", help="A Convex export directory; give the snapshot first, then deltas")
    build.set_defaults(func=command_build)
    args = parser.parse_args()
    os.umask(0o077)
    args.func(args)


if __name__ == "__main__":
    main()
