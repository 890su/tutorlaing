"""Extensible two-player game domain, starting with tic-tac-toe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


class GameError(ValueError):
    """A safe, player-facing game rule or state error."""


class GameStore(Protocol):
    def game_profile(self, chat_id: int) -> Any | None: ...

    def sync_game_telegram_username(self, chat_id: int, username: str) -> Any | None: ...

    def game_profile_by_telegram_username(self, username: str) -> Any | None: ...

    def create_game_invitation(
        self, kind: str, host_chat_id: int, guest_chat_id: int, state: dict[str, Any]
    ) -> Any: ...

    def game_for_player(self, game_id: str, chat_id: int) -> Any: ...

    def games_for_player(self, chat_id: int) -> list[Any]: ...

    def create_game_link_invitation(
        self, kind: str, host_chat_id: int, state: dict[str, Any]
    ) -> Any: ...

    def game_link_invitations_for_host(self, chat_id: int) -> list[Any]: ...

    def claim_game_link_invitation(self, token: str, guest_chat_id: int) -> Any | None: ...

    def update_game(
        self,
        game_id: str,
        actor_chat_id: int,
        expected_version: int,
        *,
        status: str,
        state: dict[str, Any],
        turn_chat_id: int | None,
        winner_chat_id: int | None = None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> Any | None: ...


TELEGRAM_USERNAME_RE = re.compile(r"^[a-z0-9_]{5,32}$")


def normalize_telegram_username(value: str) -> str:
    username = value.strip().removeprefix("@").lower()
    if not TELEGRAM_USERNAME_RE.fullmatch(username):
        raise GameError("Введите Telegram @username: 5–32 символа, только a-z, 0-9 и _.")
    return username


@dataclass(frozen=True)
class GameDefinition:
    key: str
    title: str
    min_players: int

    def initial_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def move(
        self, state: dict[str, Any], marker: str, position: int
    ) -> tuple[dict[str, Any], str | None, bool]:
        raise NotImplementedError


class TicTacToeDefinition(GameDefinition):
    _lines = (
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    )

    def __init__(self) -> None:
        super().__init__("tic_tac_toe", "Крестики-нолики", 2)

    def initial_state(self) -> dict[str, Any]:
        return {"board": ["" for _ in range(9)]}

    def move(
        self, state: dict[str, Any], marker: str, position: int
    ) -> tuple[dict[str, Any], str | None, bool]:
        if marker not in {"X", "O"}:
            raise GameError("Неверный игрок.")
        if not 0 <= position < 9:
            raise GameError("Выберите клетку на поле.")
        board = list(state.get("board", []))
        if len(board) != 9 or any(cell not in {"", "X", "O"} for cell in board):
            raise GameError("Состояние игры повреждено.")
        if board[position]:
            raise GameError("Эта клетка уже занята.")
        board[position] = marker
        winner = next(
            (
                marker
                for line in self._lines
                if all(board[index] == marker for index in line)
            ),
            None,
        )
        return {"board": board}, winner, winner is None and all(board)


GAME_REGISTRY: dict[str, GameDefinition] = {"tic_tac_toe": TicTacToeDefinition()}


class GameService:
    """Coordinates identities, invitations and game rules without HTTP concerns."""

    def __init__(self, store: GameStore) -> None:
        self.store = store

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"id": item.key, "title": item.title, "players": item.min_players}
            for item in GAME_REGISTRY.values()
        ]

    def sync_telegram_username(self, chat_id: int, username: str) -> None:
        """Refresh the discovery alias from a Telegram-signed identity."""

        if not username:
            return
        try:
            self.store.sync_game_telegram_username(
                chat_id, normalize_telegram_username(username)
            )
        except ValueError as exc:
            raise GameError(str(exc)) from exc

    def snapshot(self, chat_id: int) -> dict[str, Any]:
        profile = self.store.game_profile(chat_id)
        return {
            "profile": (
                {"telegram_username": str(profile["telegram_username"])}
                if profile and profile["telegram_username"]
                else None
            ),
            "catalog": self.catalog(),
            "games": [self._public_game(row, chat_id) for row in self.store.games_for_player(chat_id)],
            "share_links": [
                {
                    "token": str(row["token"]),
                    "kind": str(row["kind"]),
                    "title": GAME_REGISTRY[str(row["kind"])].title,
                    "expires_at": str(row["expires_at"]),
                }
                for row in self.store.game_link_invitations_for_host(chat_id)
            ],
        }

    def invite(self, chat_id: int, kind: str, username: str) -> dict[str, Any]:
        definition = GAME_REGISTRY.get(kind)
        if definition is None:
            raise GameError("Эта игра пока недоступна.")
        opponent = self.store.game_profile_by_telegram_username(
            normalize_telegram_username(username)
        )
        if opponent is None:
            raise GameError(
                "Этот @username ещё не открывал игры в боте. Отправьте ему ссылку ниже."
            )
        opponent_id = int(opponent["chat_id"])
        if opponent_id == chat_id:
            raise GameError("Нельзя пригласить самого себя.")
        row = self.store.create_game_invitation(
            kind, chat_id, opponent_id, definition.initial_state()
        )
        return self._public_game(self.store.game_for_player(str(row["id"]), chat_id), chat_id)

    def create_link_invitation(self, chat_id: int, kind: str) -> dict[str, Any]:
        definition = GAME_REGISTRY.get(kind)
        if definition is None:
            raise GameError("Эта игра пока недоступна.")
        row = self.store.create_game_link_invitation(kind, chat_id, definition.initial_state())
        return {
            "token": str(row["token"]),
            "kind": kind,
            "title": definition.title,
            "expires_at": str(row["expires_at"]),
        }

    def claim_link_invitation(self, chat_id: int, token: str) -> dict[str, Any]:
        row = self.store.claim_game_link_invitation(token, chat_id)
        if row is None:
            raise GameError("Эта ссылка уже использована, устарела или создана вами.")
        return self._public_game(row, chat_id)

    def accept(self, chat_id: int, game_id: str) -> dict[str, Any]:
        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "pending" or int(row["guest_chat_id"]) != chat_id:
            raise GameError("Это приглашение уже нельзя принять.")
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status="active",
            state=json.loads(str(row["state_json"])),
            turn_chat_id=int(row["host_chat_id"]),
            event_type="accepted",
        )
        if updated is None:
            raise GameError("Игра уже изменилась. Обновите экран.")
        return self._public_game(updated, chat_id)

    def decline(self, chat_id: int, game_id: str) -> dict[str, Any]:
        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "pending" or int(row["guest_chat_id"]) != chat_id:
            raise GameError("Это приглашение уже нельзя отклонить.")
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status="declined",
            state=json.loads(str(row["state_json"])),
            turn_chat_id=None,
            event_type="declined",
        )
        if updated is None:
            raise GameError("Игра уже изменилась. Обновите экран.")
        return self._public_game(updated, chat_id)

    def move(self, chat_id: int, game_id: str, position: int) -> dict[str, Any]:
        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "active" or int(row["turn_chat_id"] or 0) != chat_id:
            raise GameError("Сейчас ход соперника.")
        definition = GAME_REGISTRY.get(str(row["kind"]))
        if definition is None:
            raise GameError("Правила этой игры недоступны.")
        host_id = int(row["host_chat_id"])
        guest_id = int(row["guest_chat_id"])
        marker = "X" if chat_id == host_id else "O"
        state, winner_marker, draw = definition.move(
            json.loads(str(row["state_json"])), marker, position
        )
        winner_id = host_id if winner_marker == "X" else guest_id if winner_marker == "O" else None
        status = "finished" if winner_marker or draw else "active"
        next_turn = None if status == "finished" else guest_id if chat_id == host_id else host_id
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status=status,
            state=state,
            turn_chat_id=next_turn,
            winner_chat_id=winner_id,
            event_type="move",
            event_payload={"position": position, "marker": marker},
        )
        if updated is None:
            raise GameError("Ход уже сделан. Обновите поле.")
        return self._public_game(updated, chat_id)

    def _public_game(self, row: Any, chat_id: int) -> dict[str, Any]:
        host_id = int(row["host_chat_id"])
        guest_id = int(row["guest_chat_id"])
        mine = chat_id == host_id
        opponent = str((row["guest_nickname"] if mine else row["host_nickname"]) or "игрок")
        status = str(row["status"])
        return {
            "id": str(row["id"]),
            "kind": str(row["kind"]),
            "title": GAME_REGISTRY[str(row["kind"])].title,
            "status": status,
            "state": json.loads(str(row["state_json"])),
            "you": {
                "marker": "X" if mine else "O",
                "nickname": str(
                    (row["host_nickname"] if mine else row["guest_nickname"])
                    or "вы"
                ),
            },
            "opponent": {"marker": "O" if mine else "X", "nickname": opponent},
            "your_turn": status == "active" and int(row["turn_chat_id"] or 0) == chat_id,
            "can_accept": status == "pending" and guest_id == chat_id,
            "can_decline": status == "pending" and guest_id == chat_id,
            "winner": (
                "you"
                if row["winner_chat_id"] is not None and int(row["winner_chat_id"]) == chat_id
                else "opponent"
                if row["winner_chat_id"] is not None
                else "draw"
                if status == "finished"
                else None
            ),
            "version": int(row["version"]),
        }
