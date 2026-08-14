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
        self.games.sync_telegram_username(10, "alice_1")
        self.games.sync_telegram_username(20, "bob_2")

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

    def test_telegram_username_is_normalized_and_unique(self) -> None:
        with self.assertRaises(GameError):
            self.games.sync_telegram_username(20, "alice_1")
        with self.assertRaises(GameError):
            self.games.invite(10, "tic_tac_toe", "No spaces")

    def test_link_invitation_can_be_claimed_once_without_a_username(self) -> None:
        link = self.games.create_link_invitation(10, "tic_tac_toe")
        self.assertEqual("tic_tac_toe", link["kind"])
        claimed = self.games.claim_link_invitation(20, link["token"])
        self.assertTrue(claimed["can_accept"])
        with self.assertRaises(GameError):
            self.games.claim_link_invitation(20, link["token"])


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

    def init_data(self, chat_id: int, *, username: str = "alice_1") -> str:
        values = {
            "auth_date": str(int(time.time())),
            "query_id": "query",
            "user": json.dumps({"id": chat_id, "first_name": "Alice", "username": username}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", self.token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_static_app_and_authenticated_state_uses_telegram_username(self) -> None:
        page = self.web.get("/games")
        self.assertIsNotNone(page)
        self.assertIn("игровой стол", page.body.decode("utf-8").lower())

        response = self.web.post("/games/api/state", b"{}", self.init_data(10))
        self.assertEqual(200, response.status)
        self.assertEqual(
            "alice_1", json.loads(response.body)["result"]["profile"]["telegram_username"]
        )

    def test_link_claim_creates_a_game_for_the_first_recipient(self) -> None:
        self.web = GamesWebApp(self.storage, self.token, bot_username="TutorlaingBot")
        created = self.web.post(
            "/games/api/link-invitations", b'{"kind":"tic_tac_toe"}', self.init_data(10)
        )
        payload = json.loads(created.body)["result"]
        self.assertIn("https://t.me/tutorlaingbot?start=game_", payload["url"])
        self.storage.ensure_user(20, "Bob")
        self.storage.accept_consent(20, CONSENT_VERSION)
        response = self.web.post(
            "/games/api/claim-link",
            json.dumps({"token": payload["token"]}).encode(),
            self.init_data(20, username=""),
        )
        self.assertEqual(200, response.status)
        self.assertTrue(json.loads(response.body)["result"]["can_accept"])

    def test_api_rejects_missing_or_untrusted_telegram_identity(self) -> None:
        response = self.web.post("/games/api/state", b"{}", "")
        self.assertEqual(403, response.status)
        response = self.web.post("/games/api/state", b"{}", "auth_date=1&hash=no")
        self.assertEqual(403, response.status)


if __name__ == "__main__":
    unittest.main()
