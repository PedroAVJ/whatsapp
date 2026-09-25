import importlib.util
import io
import json
import os
import pathlib
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CLIENT = load(
    "convex_client_under_test",
    ROOT / "vendor" / "lharries-whatsapp-mcp" / "whatsapp-mcp-server" / "convex_client.py",
)
CLI = load("whatsapp_cli_convex_under_test", ROOT / "cli" / "whatsapp_cli.py")


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def respond(body):
    return Response(json.dumps(body).encode("utf-8"))


class ConvexClientTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"WHATSAPP_CONVEX_URL": "http://convex.test/"}, clear=False)
        self.env.start()
        os.environ.pop("WHATSAPP_CONVEX_READ_TOKEN", None)
        CLIENT._keychain_token = None

    def tearDown(self):
        self.env.stop()
        CLIENT._keychain_token = None

    def test_query_posts_the_function_path_args_and_token(self):
        os.environ["WHATSAPP_CONVEX_READ_TOKEN"] = "env-token"
        seen = {}

        def urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            return respond({"status": "success", "value": ["ok"]})

        with mock.patch.object(CLIENT.urllib.request, "urlopen", urlopen):
            self.assertEqual(["ok"], CLIENT.query("chats", {"includeLast": True}))

        self.assertEqual("http://convex.test/api/query", seen["url"])
        self.assertEqual(
            {"path": "whatsapp:chats", "args": {"includeLast": True, "token": "env-token"}, "format": "json"},
            seen["body"],
        )

    def test_token_falls_back_to_the_keychain_once(self):
        keychain = mock.Mock(return_value=mock.Mock(returncode=0, stdout="keychain-token\n"))
        with mock.patch.object(CLIENT.subprocess, "run", keychain):
            self.assertEqual("keychain-token", CLIENT.read_token())
            self.assertEqual("keychain-token", CLIENT.read_token())

        keychain.assert_called_once()
        self.assertEqual(
            ["security", "find-generic-password", "-a", "near", "-s", "n4.convex-read", "-w"],
            keychain.call_args.args[0],
        )

    def test_missing_token_is_a_clear_error(self):
        with mock.patch.object(CLIENT.subprocess, "run", return_value=mock.Mock(returncode=44, stdout="")):
            with self.assertRaisesRegex(CLIENT.ConvexError, "WHATSAPP_CONVEX_READ_TOKEN"):
                CLIENT.read_token()

    def test_error_status_unreachable_and_http_failures_raise(self):
        os.environ["WHATSAPP_CONVEX_READ_TOKEN"] = "env-token"
        cases = [
            (lambda *a, **k: respond({"status": "error", "errorMessage": "Unauthorized"}), "whatsapp:identity failed: Unauthorized"),
            (mock.Mock(side_effect=urllib.error.URLError("Connection refused")), "not reachable: Connection refused"),
            (
                mock.Mock(side_effect=urllib.error.HTTPError("http://convex.test/api/query", 502, "Bad Gateway", {}, io.BytesIO(b"oops"))),
                "HTTP 502",
            ),
        ]
        for urlopen, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(CLIENT.urllib.request, "urlopen", urlopen):
                    with self.assertRaisesRegex(CLIENT.ConvexError, message):
                        CLIENT.query("identity")

    def test_times_are_epoch_milliseconds_and_local_aware(self):
        value = CLIENT.from_ms(1775754918000.0)
        self.assertIsNotNone(value.tzinfo)
        self.assertEqual(1775754918000, CLIENT.to_ms(value))
        self.assertIsNone(CLIENT.from_ms(None))


class CliIdentityTests(unittest.TestCase):
    def test_identity_context_comes_from_convex(self):
        identity = {
            "chats": [{"jid": "15551230001@s.whatsapp.net", "name": "Acme Ops"}],
            "lids": [{"lid": "99900123456789", "pn": "15551230001"}, {"lid": "1@lid", "pn": "not-a-phone"}],
            "self": "15550000000:1@s.whatsapp.net",
        }
        with mock.patch.object(CLI, "convex_query", return_value=identity) as query:
            context = CLI.load_identity_context()

        query.assert_called_once_with("identity")
        self.assertEqual({"99900123456789": "15551230001"}, context["lid_to_phone"])
        self.assertEqual({"15551230001@s.whatsapp.net": "Acme Ops"}, context["chat_names"])
        chat = CLI.annotate_chat({"jid": "99900123456789@lid", "name": "99900123456789"}, context)
        self.assertEqual("Acme Ops", chat["display_name"])
        self.assertEqual("resolved_lid", chat["name_quality"])

    def test_convex_failures_become_cli_errors(self):
        client = CLI.convex_client()
        with mock.patch.object(client, "query", side_effect=client.ConvexError("Convex at x is not reachable")):
            with self.assertRaises(CLI.CliError) as caught:
                CLI.convex_query("identity")
        self.assertEqual("convex_error", caught.exception.code)

    def test_local_stores_stay_in_the_bridge_store_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_TRANSCRIPTS_DB_PATH", None)
            with mock.patch.object(CLI, "backend_json", return_value={"convex_url": "u", "store_dir": "/state/store"}) as backend:
                self.assertEqual(pathlib.Path("/state/store/transcripts.db"), CLI.transcripts_db_path())
                self.assertEqual(pathlib.Path("/state/store/drafts.db"), CLI.draft_db_path())
        backend.assert_called_with("store-info", timeout=30)


if __name__ == "__main__":
    unittest.main()
