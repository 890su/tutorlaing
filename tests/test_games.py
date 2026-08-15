import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tutorlaing.game_service import BattleshipDefinition, DurakDefinition, GameError, GameService
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
        self.assertEqual([], self.games.snapshot(10)["share_links"])
        with self.assertRaises(GameError):
            self.games.claim_link_invitation(20, link["token"])

    def test_host_can_close_pending_link_for_any_game(self) -> None:
        link = self.games.create_link_invitation(10, "battleship")
        result = self.games.cancel_link_invitation(10, link["token"])
        self.assertEqual("cancelled", result["status"])
        self.assertEqual([], self.games.snapshot(10)["share_links"])
        with self.assertRaises(GameError):
            self.games.claim_link_invitation(20, link["token"])
        with self.assertRaises(GameError):
            self.games.cancel_link_invitation(10, link["token"])

    def test_player_can_resign_or_finish_an_active_game(self) -> None:
        invitation = self.games.invite(10, "tic_tac_toe", "bob_2")
        self.games.accept(20, invitation["id"])
        resigned = self.games.resign(20, invitation["id"])
        self.assertEqual("finished", resigned["status"])
        self.assertEqual("opponent", resigned["winner"])
        with self.assertRaises(GameError):
            self.games.move(10, invitation["id"], 0)

        second = self.games.invite(10, "tic_tac_toe", "bob_2")
        self.games.accept(20, second["id"])
        cancelled = self.games.finish(10, second["id"])
        self.assertEqual("cancelled", cancelled["status"])
        self.assertIsNone(cancelled["winner"])

    def test_durak_keeps_hands_private_and_resolves_a_defended_round(self) -> None:
        state = {
            "deck": [],
            "trump_suit": "S",
            "trump_card": "6S",
            "hands": {"X": ["6H", "7S"], "O": ["8H", "9C"]},
            "table": [],
            "attacker": "X",
            "defender": "O",
            "phase": "attack",
            "attack_limit": 2,
        }
        row = self.storage.create_game_invitation("durak", 10, 20, state)
        self.games.accept(20, str(row["id"]))
        attacked = self.games.action(10, str(row["id"]), "attack", "6H")
        self.assertEqual("durak", attacked["state"]["game"])
        self.assertEqual(["7S"], attacked["state"]["hand"])
        self.assertNotIn("hands", attacked["state"])
        self.assertNotIn("deck", attacked["state"])

        defended = self.games.action(20, str(row["id"]), "beat", "8H", "6H")
        self.assertEqual("8H", defended["state"]["table"][0]["defense"])
        completed = self.games.action(10, str(row["id"]), "finish_round")
        self.assertEqual("attack", completed["state"]["phase"])
        self.assertEqual("O", completed["state"]["attacker"])

    def test_durak_rejects_illegal_defense(self) -> None:
        definition = DurakDefinition()
        state = {
            "deck": ["6S"],
            "trump_suit": "S",
            "trump_card": "6S",
            "hands": {"X": ["9H"], "O": ["8C"]},
            "table": [{"attack": "9H", "defense": ""}],
            "attacker": "X",
            "defender": "O",
            "phase": "defend",
            "attack_limit": 1,
        }
        with self.assertRaises(GameError):
            definition.action(state, "O", "beat", "8C", "9H")

    def test_durak_deals_a_complete_private_deck(self) -> None:
        definition = DurakDefinition()
        state = definition.initial_state()
        cards = [*state["deck"], *state["hands"]["X"], *state["hands"]["O"]]
        self.assertEqual(36, len(cards))
        self.assertEqual(36, len(set(cards)))
        public = definition.public_state(state, "X")
        self.assertEqual(6, len(public["hand"]))
        self.assertEqual(6, public["opponent_cards"])
        self.assertNotIn("deck", public)
        self.assertNotIn("hands", public)

    def test_battleship_keeps_fleet_private_and_alternates_shots(self) -> None:
        state = {
            "boards": {"X": [["A1"]], "O": [["B2", "C2"]]},
            "shots": {"X": [], "O": []},
            "turn": "X",
            "last_shot": None,
        }
        row = self.storage.create_game_invitation("battleship", 10, 20, state)
        accepted = self.games.accept(20, str(row["id"]))
        self.assertFalse(accepted["your_turn"])
        first = self.games.action(10, str(row["id"]), "fire", target="A2")
        self.assertEqual("miss", first["state"]["target"][0]["result"])
        self.assertNotIn("boards", first["state"])
        self.assertNotIn("shots", first["state"])
        with self.assertRaises(GameError):
            self.games.action(10, str(row["id"]), "fire", target="B2")
        self.games.action(20, str(row["id"]), "fire", target="J10")
        hit = self.games.action(10, str(row["id"]), "fire", target="B2")
        self.assertEqual("hit", hit["state"]["target"][-1]["result"])
        self.assertEqual(1, hit["state"]["opponent_fleet"])

    def test_battleship_deals_two_complete_non_overlapping_fleets(self) -> None:
        definition = BattleshipDefinition()
        state = definition.initial_state()
        for marker in ("X", "O"):
            cells = [cell for ship in state["boards"][marker] for cell in ship]
            self.assertEqual(20, len(cells))
            self.assertEqual(20, len(set(cells)))
        public = definition.public_state(state, "X")
        self.assertEqual(10, public["size"])
        self.assertNotIn("boards", public)
        self.assertNotIn("shots", public)


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

    def test_api_can_close_a_pending_link(self) -> None:
        self.web = GamesWebApp(self.storage, self.token, bot_username="TutorlaingBot")
        created = self.web.post(
            "/games/api/link-invitations", b'{"kind":"durak"}', self.init_data(10)
        )
        token = json.loads(created.body)["result"]["token"]
        closed = self.web.post(
            "/games/api/cancel-link",
            json.dumps({"token": token}).encode(),
            self.init_data(10),
        )
        self.assertEqual(200, closed.status)
        self.assertEqual("cancelled", json.loads(closed.body)["result"]["status"])
        snapshot = self.web.post("/games/api/state", b"{}", self.init_data(10))
        self.assertEqual([], json.loads(snapshot.body)["result"]["share_links"])

    def test_api_rejects_missing_or_untrusted_telegram_identity(self) -> None:
        response = self.web.post("/games/api/state", b"{}", "")
        self.assertEqual(403, response.status)
        response = self.web.post("/games/api/state", b"{}", "auth_date=1&hash=no")
        self.assertEqual(403, response.status)


if __name__ == "__main__":
    unittest.main()
