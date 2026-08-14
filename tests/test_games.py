import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tutorlaing.game_service import GameError, GameService
from tutorlaing.games_web import GamesWebApp
from tutorlaing.privacy import CONSENT_VERSION
from tutorlaing.storage import Storage


class GameServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "games.sqlite3")
        for chat_id, name in ((10, "Alice"), (20, "Bob")):
            self.storage.ensure_user(chat_id, name)
            self.storage.accept_consent(chat_id, CONSENT_VERSION)
        self.games = GameService(self.storage)
        self.games.set_nickname(10, "alice_1")
        self.games.set_nickname(20, "bob_2")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_tic_tac_toe_keeps_players_and_turns_owner_scoped(self) -> None:
        invitation = self.games.invite(10, "tic_tac_toe", "@bob_2")
        self.assertEqual("pending", invitation["status"])
        accepted = self.games.accept(20, invitation["id"])
        self.assertTrue(accepted["your_turn"] is False)

        game = self.games.move(10, invitation["id"], 0)
        self.assertEqual(["X", "", "", "", "", "", "", "", ""], game["state"]["board"])
        with self.assertRaises(GameError):
            self.games.move(10, invitation["id"], 1)
        game = self.games.move(20, invitation["id"], 4)
        self.assertTrue(game["your_turn"] is False)
        self.games.move(10, invitation["id"], 1)
        self.games.move(20, invitation["id"], 3)
        finished = self.games.move(10, invitation["id"], 2)
        self.assertEqual("finished", finished["status"])
        self.assertEqual("you", finished["winner"])

    def test_nickname_is_private_to_games_and_unique(self) -> None:
        with self.assertRaises(GameError):
            self.games.set_nickname(20, "alice_1")
        with self.assertRaises(GameError):
            self.games.set_nickname(20, "No spaces")


class GamesWebAppTests(unittest.TestCase):
    token = "test-token"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "web.sqlite3")
        self.storage.ensure_user(10, "Alice")
        self.storage.accept_consent(10, CONSENT_VERSION)
        self.web = GamesWebApp(self.storage, self.token)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def init_data(self, chat_id: int) -> str:
        values = {
            "auth_date": str(int(time.time())),
            "query_id": "query",
            "user": json.dumps({"id": chat_id, "first_name": "Alice"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", self.token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_static_app_and_authenticated_profile_api(self) -> None:
        page = self.web.get("/games")
        self.assertIsNotNone(page)
        self.assertIn("игровой стол", page.body.decode("utf-8").lower())

        response = self.web.post(
            "/games/api/profile",
            b'{"nickname":"alice_1"}',
            self.init_data(10),
        )
        self.assertEqual(200, response.status)
        self.assertEqual("alice_1", json.loads(response.body)["result"]["nickname"])

    def test_api_rejects_missing_or_untrusted_telegram_identity(self) -> None:
        response = self.web.post("/games/api/state", b"{}", "")
        self.assertEqual(403, response.status)
        response = self.web.post("/games/api/state", b"{}", "auth_date=1&hash=no")
        self.assertEqual(403, response.status)


if __name__ == "__main__":
    unittest.main()
