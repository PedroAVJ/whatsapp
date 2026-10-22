import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCHEMA = ROOT / "vendor" / "lharries-whatsapp-mcp" / "whatsapp-bridge" / "schema.sql"
MIGRATE = ROOT / "scripts" / "migrate_store.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


STORE = load(
    "message_store_under_test",
    ROOT / "vendor" / "lharries-whatsapp-mcp" / "whatsapp-mcp-server" / "message_store.py",
)
CLI = load("whatsapp_cli_store_under_test", ROOT / "cli" / "whatsapp_cli.py")

T0 = 1_775_754_918_000  # epoch milliseconds


def create_store(directory):
    """messages.db laid out exactly as the bridge creates it."""
    conn = sqlite3.connect(pathlib.Path(directory) / "messages.db")
    conn.executescript(SCHEMA.read_text())
    conn.execute("PRAGMA user_version = 2")
    return conn


def put(conn, table, **row):
    conn.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )


def message(conn, id, chat_jid, timestamp, content="", sender="15551230001@s.whatsapp.net", **extra):
    put(conn, "messages", id=id, chat_jid=chat_jid, sender=sender, content=content, timestamp=timestamp, **extra)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"WHATSAPP_MCP_STORE_DIR": self.temp.name})
        self.env.start()
        conn = create_store(self.temp.name)
        put(conn, "chats", jid="15551230001@s.whatsapp.net", name="Acme Ops", last_message_time=T0 + 5000)
        put(conn, "chats", jid="99900123456789@lid", name="99900123456789", last_message_time=T0 + 9000)
        put(conn, "chats", jid="group@g.us", name="Launch", last_message_time=T0 + 1000)
        put(conn, "chats", jid="15559990000@s.whatsapp.net", name=None, last_message_time=None)
        message(conn, "p-1", "15551230001@s.whatsapp.net", T0 + 1000, "Hola, ¿cómo estás?")
        message(conn, "p-2", "15551230001@s.whatsapp.net", T0 + 5000, "Bien", sender="me@s.whatsapp.net",
                is_from_me=1, reply_to_message_id="p-1", reply_to_content="stale preview", edited_at=T0 + 6000)
        message(conn, "l-1", "99900123456789@lid", T0 + 9000, "", sender="99900123456789@lid",
                media_type="audio", filename="a.ogg", media_key=b"\x01\x02", file_length=42)
        message(conn, "g-1", "group@g.us", T0 + 1000, "team HOLA", sender="15551230001@s.whatsapp.net")
        put(conn, "message_reactions", chat_jid="15551230001@s.whatsapp.net", target_message_id="p-2",
            target_sender="", reaction_sender="15551230001@s.whatsapp.net", emoji="+1", timestamp=T0 + 7000,
            sender_timestamp_ms=T0 + 6999, is_from_me=0)
        put(conn, "message_receipts", message_id="p-2", chat_jid="15551230001@s.whatsapp.net", receipt_type="read",
            receipt_sender="15551230001@s.whatsapp.net", message_sender="", timestamp=T0 + 8000)
        put(conn, "message_receipts", message_id="p-2", chat_jid="15551230001@s.whatsapp.net", receipt_type="delivered",
            receipt_sender="15551230001@s.whatsapp.net", message_sender="", timestamp=T0 + 5500)
        put(conn, "lid_map", lid="99900123456789", pn="15551230001")
        put(conn, "self", id=1, jid="15550000000@s.whatsapp.net")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class MessageStoreReadTests(StoreFixture):
    def test_identity(self):
        identity = STORE.query("identity")
        self.assertEqual("15550000000@s.whatsapp.net", identity["self"])
        self.assertEqual([{"lid": "99900123456789", "pn": "15551230001"}], identity["lids"])
        self.assertNotIn("15559990000@s.whatsapp.net", [c["jid"] for c in identity["chats"]])

    def test_messages_merge_chats_newest_first_with_metadata(self):
        rows = STORE.query("messages", {
            "chatJids": ["99900123456789@lid", "15551230001@s.whatsapp.net"], "limit": 10, "offset": 0,
        })
        self.assertEqual(["l-1", "p-2", "p-1"], [r["id"] for r in rows])
        reply = rows[1]
        self.assertEqual("Acme Ops", reply["chatName"])
        self.assertIs(True, reply["isFromMe"])
        self.assertEqual("Hola, ¿cómo estás?", reply["replyContent"], "the replied-to message fills the preview")
        self.assertEqual(T0 + 6000, reply["editedAt"])
        self.assertEqual(["+1"], [r["emoji"] for r in reply["reactions"]])
        self.assertIsNone(reply["reactions"][0]["targetSender"])
        self.assertEqual(["delivered", "read"], [r["type"] for r in reply["receipts"]])
        self.assertIsNone(reply["receipts"][0]["messageSender"])
        self.assertEqual("audio", rows[0]["mediaType"])

        page = STORE.query("messages", {"chatJids": ["99900123456789@lid", "15551230001@s.whatsapp.net"], "limit": 1, "offset": 1})
        self.assertEqual(["p-2"], [r["id"] for r in page])

    def test_messages_filters(self):
        def ids(**args):
            return [r["id"] for r in STORE.query("messages", {"limit": 10, "offset": 0, **args})]

        self.assertEqual(["g-1", "p-1"], sorted(ids(query="hola")), "text search ignores case")
        self.assertEqual(["p-1"], ids(query="CÓMO"))
        self.assertEqual(["p-2"], ids(after=T0 + 1000, before=T0 + 9000), "time bounds are exclusive")
        self.assertEqual(["me@s.whatsapp.net"], [r["sender"] for r in STORE.query("messages", {"limit": 10, "offset": 0, "sender": "me@s.whatsapp.net"})])
        self.assertEqual([], ids(chatJids=[]))

    def test_context(self):
        value = STORE.query("context", {"id": "p-2", "before": 5, "after": 5})
        self.assertEqual("p-2", value["message"]["id"])
        self.assertEqual(["p-1"], [r["id"] for r in value["before"]])
        self.assertEqual([], value["after"])
        self.assertIsNone(STORE.query("context", {"id": "missing", "before": 1, "after": 1}))

    def test_chats(self):
        chats = STORE.query("chats", {"includeLast": True})
        self.assertEqual(
            ["99900123456789@lid", "15551230001@s.whatsapp.net", "group@g.us", "15559990000@s.whatsapp.net"],
            [c["jid"] for c in chats],
        )
        self.assertEqual("Bien", chats[1]["lastMessage"])
        self.assertIs(True, chats[1]["lastIsFromMe"])
        self.assertIsNone(chats[3]["lastMessageTime"])
        bare = STORE.query("chat", {"jid": "group@g.us", "includeLast": False})
        self.assertIsNone(bare["lastMessage"])
        self.assertIsNone(STORE.query("chat", {"jid": "nobody@s.whatsapp.net", "includeLast": True}))

    def test_contacts_and_lookups(self):
        self.assertEqual(["15551230001@s.whatsapp.net"], [c["jid"] for c in STORE.query("contacts", {"query": "acme"})])
        self.assertEqual([], STORE.query("contacts", {"query": "launch"}), "groups are not contacts")
        self.assertEqual("15551230001@s.whatsapp.net", STORE.query("directChat", {"phone": "1555123"})["jid"])
        chats = STORE.query("contactChats", {"jid": "15551230001@s.whatsapp.net", "limit": 10, "offset": 0})
        self.assertEqual(["15551230001@s.whatsapp.net", "group@g.us"], [c["jid"] for c in chats])
        self.assertEqual("Acme Ops", STORE.query("senderName", {"jid": "15551230001@s.whatsapp.net"}))
        self.assertEqual("x@lid", STORE.query("senderName", {"jid": "x@lid"}))

    def test_last_interaction_is_newest_sent_or_in_chat(self):
        row = STORE.query("lastInteraction", {"jid": "15551230001@s.whatsapp.net"})
        self.assertEqual("p-2", row["id"])

    def test_reads_are_read_only(self):
        conn = STORE.connect()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM chats")
        conn.close()

    def test_missing_store_unknown_read_and_wrong_version_are_errors(self):
        with self.assertRaisesRegex(STORE.StoreError, "Unknown WhatsApp store read"):
            STORE.query("nope")
        conn = sqlite3.connect(pathlib.Path(self.temp.name) / "messages.db")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(STORE.StoreError, "migrate_store.py"):
            STORE.query("identity")
        with mock.patch.dict(os.environ, {"WHATSAPP_MCP_STORE_DIR": str(pathlib.Path(self.temp.name) / "absent")}):
            with self.assertRaisesRegex(STORE.StoreError, "not found"):
                STORE.query("identity")

    def test_store_dir_defaults_to_the_plugin_state_root(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_PLUGIN_STATE_ROOT": "/state"}):
            os.environ.pop("WHATSAPP_MCP_STORE_DIR", None)
            self.assertEqual(pathlib.Path("/state/upstream-store/messages.db"), STORE.messages_db_path())

    def test_times_are_epoch_milliseconds_and_local_aware(self):
        value = STORE.from_ms(1775754918000.0)
        self.assertIsNotNone(value.tzinfo)
        self.assertEqual(1775754918000, STORE.to_ms(value))
        self.assertIsNone(STORE.from_ms(None))


class CliStoreTests(StoreFixture):
    def test_identity_context_comes_from_the_store(self):
        context = CLI.load_identity_context()
        self.assertEqual({"99900123456789": "15551230001"}, context["lid_to_phone"])
        chat = CLI.annotate_chat({"jid": "99900123456789@lid", "name": "99900123456789"}, context)
        self.assertEqual("Acme Ops", chat["display_name"])
        self.assertEqual("resolved_lid", chat["name_quality"])

    def test_store_failures_become_cli_errors(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_MCP_STORE_DIR": "/nonexistent-store"}):
            with self.assertRaises(CLI.CliError) as caught:
                CLI.store_query("identity")
        self.assertEqual("store_error", caught.exception.code)

    def test_local_stores_stay_in_the_bridge_store_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_TRANSCRIPTS_DB_PATH", None)
            with mock.patch.object(CLI, "backend_json", return_value={"store_dir": "/state/store"}) as backend:
                self.assertEqual(pathlib.Path("/state/store/transcripts.db"), CLI.transcripts_db_path())
                self.assertEqual(pathlib.Path("/state/store/drafts.db"), CLI.draft_db_path())
        backend.assert_called_with("store-info", timeout=30)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def legacy(self):
        path = self.root / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
            CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
                is_from_me BOOLEAN, media_type TEXT, reply_to_message_id TEXT, reply_to_sender TEXT,
                reply_to_content TEXT, reply_to_media_type TEXT, filename TEXT, url TEXT, media_key BLOB,
                file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER, edited_at TIMESTAMP,
                PRIMARY KEY (id, chat_jid));
            CREATE TABLE message_reactions (chat_jid TEXT NOT NULL, target_message_id TEXT NOT NULL,
                target_sender TEXT NOT NULL DEFAULT '', reaction_sender TEXT NOT NULL, emoji TEXT NOT NULL,
                reaction_message_id TEXT, grouping_key TEXT, sender_timestamp_ms INTEGER, timestamp TIMESTAMP,
                is_from_me BOOLEAN, PRIMARY KEY (chat_jid, target_message_id, reaction_sender));
            CREATE TABLE message_receipts (message_id TEXT NOT NULL, chat_jid TEXT NOT NULL,
                receipt_type TEXT NOT NULL, receipt_sender TEXT NOT NULL, message_sender TEXT NOT NULL DEFAULT '',
                timestamp TIMESTAMP, PRIMARY KEY (message_id, chat_jid, receipt_type, receipt_sender, message_sender));
            INSERT INTO chats VALUES ('a@s.whatsapp.net', 'Old name', '2026-09-24 22:44:45-05:00');
            INSERT INTO chats VALUES ('old@s.whatsapp.net', 'Only old', '2023-07-11 16:02:56.123456789-05:00');
            INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_key)
                VALUES ('m-1', 'a@s.whatsapp.net', 'a', 'old text', '2026-09-24 22:44:45-05:00', 0, X'0102');
            INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)
                VALUES ('m-0', 'old@s.whatsapp.net', 'me', 'only old', '2023-07-11 16:02:56.5-05:00', 1);
            INSERT INTO message_receipts VALUES ('m-1', 'a@s.whatsapp.net', 'read', 'a', '', '2026-09-24 22:45:00-05:00');
        """)
        conn.commit()
        conn.close()
        return path

    def export(self, name, manifest, files):
        directory = self.root / name
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps(manifest))
        for file, docs in files.items():
            (directory / file).write_text("".join(json.dumps(doc) + "\n" for doc in docs))
        return directory

    def test_build_merges_legacy_and_convex_exports_without_loss(self):
        legacy = self.legacy()
        m1 = {"_id": "d1", "_table": "waMessages", "id": "m-1", "chatJid": "a@s.whatsapp.net", "sender": "a",
              "content": "edited text", "timestamp": 1790307885000.0, "isFromMe": False, "mediaKey": "AQI=",
              "editedAt": 1790307900000.0}
        snapshot = self.export("snap", {"kind": "snapshot", "cursor": 1, "counts": {
            "waChats": 1, "waMessages": 1, "waReactions": 1, "waReceipts": 0, "waLids": 1, "waSelf": 1}}, {
            "waChats.jsonl": [{"_id": "c1", "_table": "waChats", "jid": "a@s.whatsapp.net", "name": "New name", "lastMessageTime": 1790307900000.0}],
            "waMessages.jsonl": [m1],
            "waReactions.jsonl": [{"_id": "r1", "_table": "waReactions", "chatJid": "a@s.whatsapp.net", "targetMessageId": "m-1",
                                   "targetSender": "", "reactionSender": "b", "emoji": "x", "isFromMe": False}],
            "waReceipts.jsonl": [],
            "waLids.jsonl": [{"_id": "l1", "_table": "waLids", "lid": "999", "pn": "1555"}],
            "waSelf.jsonl": [{"_id": "s1", "_table": "waSelf", "jid": "1555@s.whatsapp.net"}],
        })
        deltas = self.export("delta", {"kind": "deltas", "since": 1, "cursor": 2, "counts": {}}, {"deltas.jsonl": [
            {"_id": "r1", "_table": "waReactions", "_deleted": True},
            {"_id": "d2", "_table": "waMessages", "id": "m-2", "chatJid": "a@s.whatsapp.net", "sender": "a",
             "content": "newest", "timestamp": 1790307999000.0, "isFromMe": False},
        ]})
        out = self.root / "store" / "messages.db"
        out.parent.mkdir()
        process = subprocess.run(
            [sys.executable, str(MIGRATE), "build", "--out", str(out), "--legacy", str(legacy),
             "--convex", str(snapshot), "--convex", str(deltas)],
            capture_output=True, text=True,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        report = json.loads(process.stdout[: process.stdout.rindex("}") + 1])
        self.assertEqual({"legacy": 2, "convex": 2, "legacy_only": 1, "expected": 3, "stored": 3}, report["messages"])
        self.assertEqual(0, report["message_reactions"]["stored"], "a reaction deleted in a delta stays deleted")
        self.assertEqual(1, report["message_receipts"]["legacy_only"])

        with mock.patch.dict(os.environ, {"WHATSAPP_MCP_STORE_DIR": str(out.parent)}):
            rows = {r["id"]: r for r in STORE.query("messages", {"limit": 10, "offset": 0})}
            self.assertEqual("edited text", rows["m-1"]["content"], "the Convex copy wins over the older file")
            self.assertEqual(1790307900000, rows["m-1"]["editedAt"])
            self.assertEqual("only old", rows["m-0"]["content"])
            self.assertEqual(1689109376500, rows["m-0"]["timestamp"], "legacy text times become epoch milliseconds")
            self.assertEqual("New name", STORE.query("chat", {"jid": "a@s.whatsapp.net", "includeLast": False})["name"])
            self.assertEqual(1689109376123, STORE.query("chat", {"jid": "old@s.whatsapp.net", "includeLast": False})["lastMessageTime"])
            self.assertEqual("1555@s.whatsapp.net", STORE.query("identity")["self"])
        conn = sqlite3.connect(out)
        self.assertEqual(b"\x01\x02", conn.execute("SELECT media_key FROM messages WHERE id = 'm-1'").fetchone()[0])
        conn.close()

    def test_build_refuses_to_overwrite(self):
        out = self.root / "messages.db"
        out.write_text("")
        process = subprocess.run([sys.executable, str(MIGRATE), "build", "--out", str(out)], capture_output=True, text=True)
        self.assertNotEqual(0, process.returncode)
        self.assertIn("already exists", process.stderr)


if __name__ == "__main__":
    unittest.main()
