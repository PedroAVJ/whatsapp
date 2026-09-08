import importlib.util
import pathlib
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock


CLI_PATH = pathlib.Path(__file__).parents[1] / "cli" / "whatsapp_cli.py"
SPEC = importlib.util.spec_from_file_location("whatsapp_cli", CLI_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def permission_bits(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class PrivateStatePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.root.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_transcript_state_is_migrated_to_private_modes(self):
        transcripts = self.root / "transcripts.db"
        transcripts.touch(mode=0o644)
        transcripts.chmod(0o644)
        transcript_store = self.root / "transcripts"
        transcript_store.mkdir(mode=0o755)
        existing_transcript = transcript_store / "existing.transcript.txt"
        existing_transcript.write_text("private transcript", encoding="utf-8")
        existing_transcript.chmod(0o644)

        with mock.patch.object(MODULE, "transcripts_db_path", return_value=transcripts):
            connection = MODULE.open_transcripts_db()
            connection.close()

        # A custom DB may be under a shared existing parent. Do not chmod it.
        self.assertEqual(permission_bits(self.root), 0o755)
        self.assertEqual(permission_bits(transcripts), 0o600)
        self.assertEqual(permission_bits(transcript_store), 0o700)
        self.assertEqual(permission_bits(existing_transcript), 0o600)
        self.assertEqual(permission_bits(self.root / "transcript-locks"), 0o700)

    def test_draft_database_and_lock_files_are_private(self):
        drafts = self.root / "drafts.db"
        drafts.touch(mode=0o644)
        drafts.chmod(0o644)

        with mock.patch.object(MODULE, "draft_db_path", return_value=drafts):
            connection = MODULE.open_draft_db()
            connection.close()
        with mock.patch.object(
            MODULE, "transcript_lock_dir", return_value=self.root / "transcript-locks"
        ):
            with MODULE.transcript_message_lock("message-1", "chat-1"):
                pass

        lock_files = list((self.root / "transcript-locks").glob("*.lock"))
        self.assertEqual(permission_bits(drafts), 0o600)
        self.assertEqual(len(lock_files), 1)
        self.assertEqual(permission_bits(lock_files[0]), 0o600)


class ElevenLabsResolutionTests(unittest.TestCase):
    def test_uses_elevenlabs_cli_from_path(self):
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            MODULE.os.environ.pop("ELEVENLABS_TRANSCRIBE_SCRIPT", None)
            with mock.patch.object(MODULE.shutil, "which", return_value="/usr/local/bin/elevenlabs"):
                self.assertEqual(
                    MODULE.elevenlabs_transcribe_command(),
                    ["/usr/local/bin/elevenlabs", "transcribe"],
                )

    def test_resolution_is_independent_of_plugin_version_layout(self):
        """The old resolver guessed sibling plugin paths pinned to one version."""
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            MODULE.os.environ.pop("ELEVENLABS_TRANSCRIBE_SCRIPT", None)
            with mock.patch.object(MODULE.shutil, "which", return_value="/opt/bin/elevenlabs") as which:
                command = MODULE.elevenlabs_transcribe_command()
            which.assert_called_once_with("elevenlabs")
            self.assertNotIn("0.1.0", " ".join(command))
            self.assertNotIn("cache", " ".join(command))

    def test_env_override_wins_over_path(self):
        with mock.patch.dict(MODULE.os.environ, {"ELEVENLABS_TRANSCRIBE_SCRIPT": "/tmp/custom.py"}):
            with mock.patch.object(MODULE.shutil, "which", return_value="/usr/local/bin/elevenlabs"):
                self.assertEqual(
                    MODULE.elevenlabs_transcribe_command(),
                    ["python3", "/tmp/custom.py"],
                )

    def test_missing_cli_raises_actionable_error(self):
        with mock.patch.dict(MODULE.os.environ, {}, clear=False):
            MODULE.os.environ.pop("ELEVENLABS_TRANSCRIBE_SCRIPT", None)
            with mock.patch.object(MODULE.shutil, "which", return_value=None):
                with self.assertRaises(MODULE.CliError) as caught:
                    MODULE.elevenlabs_transcribe_command()
        self.assertEqual(caught.exception.code, "missing_elevenlabs_cli")


class ArrivalTranscriptionTests(unittest.TestCase):
    def args(self):
        return MODULE.argparse.Namespace(
            media_type="audio",
            message_id="message-1",
            chat_jid="chat-1",
            language=None,
            model="scribe_v2",
            timeout_seconds=30,
        )

    def test_retries_transient_failures_then_succeeds(self):
        failure = MODULE.CliError("not ready", code="download_failed")
        success = {"cached": False, "transcript": {"transcript_path": "/tmp/result.txt"}}
        with mock.patch.dict(MODULE.os.environ, {"WHATSAPP_TRANSCRIBE_RETRY_DELAYS": "0,0"}):
            with mock.patch.object(MODULE, "command_media_transcribe", side_effect=[failure, failure, success]) as transcribe:
                with mock.patch.object(MODULE, "record_transcribe_failure", side_effect=[1, 2]) as record:
                    result = MODULE.command_media_arrival_hook(self.args())

        self.assertTrue(result["transcribed"])
        self.assertEqual(transcribe.call_count, 3)
        self.assertEqual(record.call_count, 2)

    def test_duplicate_delivery_uses_cached_result(self):
        with mock.patch.dict(MODULE.os.environ, {"WHATSAPP_TRANSCRIBE_RETRY_DELAYS": "0,0"}):
            with mock.patch.object(MODULE, "command_media_transcribe", return_value={"cached": True}):
                with mock.patch.object(MODULE, "record_transcribe_failure") as record:
                    result = MODULE.command_media_arrival_hook(self.args())

        self.assertEqual(result["reason"], "already_cached")
        record.assert_not_called()

    def test_expired_media_is_not_retried(self):
        failure = MODULE.CliError("expired", code="media_expired")
        with mock.patch.dict(MODULE.os.environ, {"WHATSAPP_TRANSCRIBE_RETRY_DELAYS": "0,0"}):
            with mock.patch.object(MODULE, "command_media_transcribe", side_effect=failure) as transcribe:
                with mock.patch.object(MODULE, "record_transcribe_failure", return_value=3):
                    result = MODULE.command_media_arrival_hook(self.args())

        self.assertFalse(result["transcribed"])
        self.assertEqual(result["code"], "media_expired")
        transcribe.assert_called_once()

    def test_download_404_and_410_are_classified_as_expired(self):
        args = MODULE.argparse.Namespace(message_id="message-1", chat_jid="chat-1")
        for status in (404, 410):
            with self.subTest(status=status):
                backend_error = MODULE.CliError(
                    "backend failed",
                    code="backend_failed",
                    details={"stdout": f"download failed with status code {status}"},
                )
                with mock.patch.object(MODULE, "backend_json", side_effect=backend_error):
                    with self.assertRaises(MODULE.CliError) as caught:
                        MODULE.command_media_download(args)

                self.assertEqual(caught.exception.code, "media_expired")


class PendingTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.messages = self.root / "messages.db"
        self.transcripts = self.root / "transcripts.db"
        connection = sqlite3.connect(self.messages)
        connection.execute(
            """
            CREATE TABLE messages (
                id TEXT,
                chat_jid TEXT,
                sender TEXT,
                timestamp TEXT,
                media_type TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO messages VALUES ('message-1', 'chat-1', 'sender-1', '2026-08-11T10:00:00Z', 'audio')"
        )
        connection.commit()
        connection.close()
        self.patches = [
            mock.patch.object(MODULE, "transcripts_db_path", return_value=self.transcripts),
            mock.patch.object(MODULE, "resolve_messages_db_path", return_value=self.messages),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_transient_failures_remain_automatically_eligible_after_many_attempts(self):
        for _ in range(5):
            MODULE.record_transcribe_failure(
                "message-1", "chat-1", "download_failed", "temporary outage"
            )

        pending, total, gave_up = MODULE.pending_audio_messages(
            chat_jid=None, since=None, limit=10
        )

        self.assertEqual(["message-1"], [item["message_id"] for item in pending])
        self.assertEqual(1, total)
        self.assertEqual(0, gave_up)

    def test_permanent_failure_requires_explicit_retry_override(self):
        MODULE.record_transcribe_failure(
            "message-1", "chat-1", "media_expired", "CDN returned 404"
        )

        pending, total, gave_up = MODULE.pending_audio_messages(
            chat_jid=None, since=None, limit=10
        )
        retried, retry_total, retry_gave_up = MODULE.pending_audio_messages(
            chat_jid=None, since=None, limit=10, retry_failed=True
        )

        self.assertEqual([], pending)
        self.assertEqual(0, total)
        self.assertEqual(1, gave_up)
        self.assertEqual(["message-1"], [item["message_id"] for item in retried])
        self.assertEqual(1, retry_total)
        self.assertEqual(1, retry_gave_up)

    def test_drain_processes_later_batches_without_retrying_failures_in_a_tight_loop(self):
        items = [
            {
                "message_id": f"message-{index}",
                "chat_jid": "chat-1",
                "sender": "sender-1",
                "timestamp": f"2026-08-11T10:0{index}:00Z",
                "media_type": "audio",
            }
            for index in range(1, 4)
        ]
        completed: set[tuple[str, str]] = set()

        def pending_stub(**kwargs):
            excluded = kwargs.get("exclude_keys") or set()
            eligible = [
                item
                for item in items
                if (item["message_id"], item["chat_jid"]) not in completed
                and (item["message_id"], item["chat_jid"]) not in excluded
            ]
            return eligible[: kwargs["limit"]], len(eligible), 0

        def transcribe_stub(args):
            key = (args.message_id, args.chat_jid)
            if args.message_id == "message-1":
                raise MODULE.CliError("temporary outage", code="download_failed")
            completed.add(key)
            return {"cached": False, "transcript": {"transcript_path": "/tmp/result.txt"}}

        args = MODULE.argparse.Namespace(
            limit=1,
            chat_jid=None,
            since=None,
            retry_failed=False,
            dry_run=False,
            stop_on_error=False,
            drain=True,
            language=None,
            model="scribe_v2",
            response_format="text",
            diarize=False,
            num_speakers=None,
            keyterm=[],
            no_verbatim=False,
            timeout_seconds=30,
        )

        with mock.patch.object(MODULE, "pending_audio_messages", side_effect=pending_stub):
            with mock.patch.object(MODULE, "command_media_transcribe", side_effect=transcribe_stub) as transcribe:
                with mock.patch.object(MODULE, "record_transcribe_failure", return_value=1):
                    result = MODULE.command_media_transcribe_pending(args)

        self.assertEqual(3, result["attempted"])
        self.assertEqual(2, len(result["transcribed"]))
        self.assertEqual(1, len(result["failed"]))
        self.assertEqual(1, result["remaining"])
        self.assertEqual(3, transcribe.call_count)


if __name__ == "__main__":
    unittest.main()
