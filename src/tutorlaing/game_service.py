"""Extensible two-player game domain, starting with tic-tac-toe."""

from __future__ import annotations

import json
import re
import secrets
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

    def cancel_game_link_invitation(self, token: str, host_chat_id: int) -> bool: ...

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
    description: str = ""

    def initial_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def move(
        self, state: dict[str, Any], marker: str, position: int
    ) -> tuple[dict[str, Any], str | None, bool]:
        raise NotImplementedError

    def public_state(self, state: dict[str, Any], marker: str) -> dict[str, Any]:
        return state

    def action(
        self,
        state: dict[str, Any],
        marker: str,
        action: str,
        card: str = "",
        target: str = "",
    ) -> "GameTransition":
        raise GameError("У этой игры нет такого действия.")


@dataclass(frozen=True)
class GameTransition:
    state: dict[str, Any]
    turn_marker: str | None
    winner_marker: str | None = None
    draw: bool = False


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
        super().__init__("tic_tac_toe", "Крестики-нолики", 2, "Классика · 3 в ряд")

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


class DurakDefinition(GameDefinition):
    """Two-player podkidnoy Durak with server-owned deck and hidden hands."""

    ranks = ("6", "7", "8", "9", "10", "J", "Q", "K", "A")
    suits = ("C", "D", "H", "S")
    rank_value = {rank: index for index, rank in enumerate(ranks)}

    def __init__(self) -> None:
        super().__init__("durak", "Дурак", 2, "Подкидной · 36 карт")

    def initial_state(self) -> dict[str, Any]:
        deck = [f"{rank}{suit}" for suit in self.suits for rank in self.ranks]
        secrets.SystemRandom().shuffle(deck)
        hands = {"X": [], "O": []}
        for _ in range(6):
            for marker in ("X", "O"):
                hands[marker].append(deck.pop(0))
        trump_suit = deck[-1][-1]
        lowest = {
            marker: min(
                (self.rank_value[card[:-1]] for card in hand if card[-1] == trump_suit),
                default=99,
            )
            for marker, hand in hands.items()
        }
        attacker = "X" if lowest["X"] <= lowest["O"] else "O"
        defender = "O" if attacker == "X" else "X"
        return {
            "deck": deck,
            "trump_suit": trump_suit,
            "trump_card": deck[-1],
            "hands": hands,
            "table": [],
            "attacker": attacker,
            "defender": defender,
            "phase": "attack",
            "attack_limit": min(6, len(hands[defender])),
        }

    def public_state(self, state: dict[str, Any], marker: str) -> dict[str, Any]:
        self._validate_state(state)
        attacker = str(state["attacker"])
        defender = str(state["defender"])
        table = [
            {"attack": str(pair["attack"]), "defense": str(pair.get("defense", ""))}
            for pair in state["table"]
        ]
        all_defended = bool(table) and all(pair["defense"] for pair in table)
        phase = str(state["phase"])
        can_throw = (
            marker == attacker
            and phase in {"defend", "take"}
            and len(table) < int(state["attack_limit"])
            and bool(self._throwable_cards(state, marker))
        )
        return {
            "game": "durak",
            "trump_suit": str(state["trump_suit"]),
            "trump_card": str(state["trump_card"]),
            "deck_count": len(state["deck"]),
            "hand": self._sorted_hand(state["hands"][marker], str(state["trump_suit"])),
            "opponent_cards": len(state["hands"][self._other(marker)]),
            "table": table,
            "attacker": attacker,
            "defender": defender,
            "phase": phase,
            "attack_limit": int(state["attack_limit"]),
            "can": {
                "attack": marker == attacker and phase == "attack",
                "throw": can_throw,
                "beat": marker == defender and phase == "defend" and any(
                    not pair["defense"] for pair in table
                ),
                "take": marker == defender and phase == "defend",
                "finish_round": marker == attacker
                and (phase == "take" or (phase == "defend" and all_defended)),
            },
        }

    def action(
        self,
        state: dict[str, Any],
        marker: str,
        action: str,
        card: str = "",
        target: str = "",
    ) -> GameTransition:
        self._validate_state(state)
        next_state = json.loads(json.dumps(state))
        if action == "attack":
            self._attack(next_state, marker, card)
        elif action == "throw":
            self._throw(next_state, marker, card)
        elif action == "beat":
            self._beat(next_state, marker, card, target)
        elif action == "take":
            self._take(next_state, marker)
        elif action == "finish_round":
            self._finish_round(next_state, marker)
        else:
            raise GameError("Неизвестное действие в этой партии.")
        winner, draw = self._winner(next_state)
        return GameTransition(next_state, None if winner or draw else self._turn_marker(next_state), winner, draw)

    def _attack(self, state: dict[str, Any], marker: str, card: str) -> None:
        if marker != state["attacker"] or state["phase"] != "attack":
            raise GameError("Сейчас атакует соперник.")
        self._remove_hand_card(state, marker, card)
        state["table"].append({"attack": card, "defense": ""})
        state["phase"] = "defend"

    def _throw(self, state: dict[str, Any], marker: str, card: str) -> None:
        if marker != state["attacker"] or state["phase"] not in {"defend", "take"}:
            raise GameError("Подкидывать сейчас нельзя.")
        if len(state["table"]) >= int(state["attack_limit"]):
            raise GameError("Больше карт в этом раунде подкинуть нельзя.")
        if card not in self._throwable_cards(state, marker):
            raise GameError("Подкинуть можно только карту ранга, уже лежащего на столе.")
        self._remove_hand_card(state, marker, card)
        state["table"].append({"attack": card, "defense": ""})

    def _beat(self, state: dict[str, Any], marker: str, card: str, target: str) -> None:
        if marker != state["defender"] or state["phase"] != "defend":
            raise GameError("Сейчас отбивается соперник.")
        self._remove_hand_card(state, marker, card)
        pair = next(
            (
                item
                for item in state["table"]
                if item["attack"] == target and not item.get("defense")
            ),
            None,
        )
        if pair is None:
            state["hands"][marker].append(card)
            raise GameError("Эта карта атаки уже закрыта.")
        if not self._beats(card, str(pair["attack"]), str(state["trump_suit"])):
            state["hands"][marker].append(card)
            raise GameError("Эта карта не бьёт выбранную карту.")
        pair["defense"] = card

    def _take(self, state: dict[str, Any], marker: str) -> None:
        if marker != state["defender"] or state["phase"] != "defend":
            raise GameError("Взять карты может только защищающийся игрок.")
        state["phase"] = "take"

    def _finish_round(self, state: dict[str, Any], marker: str) -> None:
        attacker = str(state["attacker"])
        defender = str(state["defender"])
        defended = bool(state["table"]) and all(pair.get("defense") for pair in state["table"])
        if marker != attacker or (state["phase"] != "take" and not defended):
            raise GameError("Раунд можно завершить после отбоя или решения взять карты.")
        if state["phase"] == "take":
            state["hands"][defender].extend(
                card for pair in state["table"] for card in (pair["attack"], pair.get("defense", "")) if card
            )
            next_attacker = attacker
        else:
            next_attacker = defender
        self._refill(state, attacker, defender)
        state["table"] = []
        state["attacker"] = next_attacker
        state["defender"] = self._other(next_attacker)
        state["phase"] = "attack"
        state["attack_limit"] = min(6, len(state["hands"][state["defender"]]))

    def _refill(self, state: dict[str, Any], attacker: str, defender: str) -> None:
        for marker in (attacker, defender):
            while len(state["hands"][marker]) < 6 and state["deck"]:
                state["hands"][marker].append(state["deck"].pop(0))

    def _winner(self, state: dict[str, Any]) -> tuple[str | None, bool]:
        if state["deck"]:
            return None, False
        empty = [marker for marker in ("X", "O") if not state["hands"][marker]]
        if len(empty) == 1:
            return empty[0], False
        return None, len(empty) == 2

    def _turn_marker(self, state: dict[str, Any]) -> str:
        if state["phase"] in {"attack", "take"}:
            return str(state["attacker"])
        if all(pair.get("defense") for pair in state["table"]):
            return str(state["attacker"])
        return str(state["defender"])

    def _throwable_cards(self, state: dict[str, Any], marker: str) -> list[str]:
        table_ranks = {card[:-1] for pair in state["table"] for card in (pair["attack"], pair.get("defense", "")) if card}
        return [card for card in state["hands"][marker] if card[:-1] in table_ranks]

    def _remove_hand_card(self, state: dict[str, Any], marker: str, card: str) -> None:
        if card not in state["hands"][marker]:
            raise GameError("Этой карты нет у вас в руке.")
        state["hands"][marker].remove(card)

    def _beats(self, defense: str, attack: str, trump_suit: str) -> bool:
        defense_rank, defense_suit = defense[:-1], defense[-1]
        attack_rank, attack_suit = attack[:-1], attack[-1]
        return (defense_suit == attack_suit and self.rank_value[defense_rank] > self.rank_value[attack_rank]) or (
            defense_suit == trump_suit and attack_suit != trump_suit
        )

    def _sorted_hand(self, cards: list[str], trump_suit: str) -> list[str]:
        return sorted(
            cards,
            key=lambda card: (
                card[-1] == trump_suit,
                self.rank_value[card[:-1]],
                card[-1],
            ),
        )

    @staticmethod
    def _other(marker: str) -> str:
        return "O" if marker == "X" else "X"

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state.get("deck"), list)
            or not isinstance(state.get("hands"), dict)
            or set(state["hands"]) != {"X", "O"}
            or state.get("attacker") not in {"X", "O"}
            or state.get("defender") not in {"X", "O"}
            or state["attacker"] == state["defender"]
            or state.get("phase") not in {"attack", "defend", "take"}
            or state.get("trump_suit") not in self.suits
            or not isinstance(state.get("trump_card"), str)
            or not isinstance(state.get("table"), list)
        ):
            raise GameError("Состояние партии повреждено.")


class BattleshipDefinition(GameDefinition):
    """Two-player Battleship with server-owned fleet layouts and shot history."""

    size = 10
    fleet = (4, 3, 3, 2, 2, 2, 1, 1, 1, 1)
    columns = "ABCDEFGHIJ"

    def __init__(self) -> None:
        super().__init__("battleship", "Морской бой", 2, "Классика · поле 10 × 10")

    def initial_state(self) -> dict[str, Any]:
        return {
            "boards": {"X": self._place_fleet(), "O": self._place_fleet()},
            "shots": {"X": [], "O": []},
            "turn": "X",
            "last_shot": None,
        }

    def public_state(self, state: dict[str, Any], marker: str) -> dict[str, Any]:
        self._validate_state(state)
        opponent = self._other(marker)
        own_shots = set(state["shots"][marker])
        opponent_shots = set(state["shots"][opponent])
        own_cells = {cell for ship in state["boards"][marker] for cell in ship}
        opponent_cells = {cell for ship in state["boards"][opponent] for cell in ship}

        own = [
            {
                "cell": cell,
                "result": "hit" if cell in opponent_shots else "ship",
            }
            for cell in sorted(own_cells | opponent_shots, key=self._cell_index)
        ]
        target = [
            {
                "cell": cell,
                "result": "hit" if cell in opponent_cells else "miss",
            }
            for cell in sorted(own_shots, key=self._cell_index)
        ]
        return {
            "game": "battleship",
            "size": self.size,
            "own": own,
            "target": target,
            "your_fleet": self._afloat(state["boards"][marker], opponent_shots),
            "opponent_fleet": self._afloat(state["boards"][opponent], own_shots),
            "can_fire": marker == state["turn"],
            "last_shot": state["last_shot"],
        }

    def action(
        self,
        state: dict[str, Any],
        marker: str,
        action: str,
        card: str = "",
        target: str = "",
    ) -> GameTransition:
        self._validate_state(state)
        if action != "fire":
            raise GameError("В морском бое доступен только выстрел по клетке.")
        if marker != state["turn"]:
            raise GameError("Сейчас ход соперника.")
        cell = target.upper().strip()
        if not self._is_cell(cell):
            raise GameError("Выберите клетку игрового поля.")
        if cell in state["shots"][marker]:
            raise GameError("По этой клетке вы уже стреляли.")
        next_state = json.loads(json.dumps(state))
        next_state["shots"][marker].append(cell)
        opponent = self._other(marker)
        hit = cell in {item for ship in next_state["boards"][opponent] for item in ship}
        next_state["last_shot"] = {"by": marker, "cell": cell, "result": "hit" if hit else "miss"}
        winner = (
            marker
            if all(
                ship_cells <= set(next_state["shots"][marker])
                for ship_cells in map(set, next_state["boards"][opponent])
            )
            else None
        )
        if not winner:
            next_state["turn"] = opponent
        return GameTransition(next_state, None if winner else opponent, winner)

    def _place_fleet(self) -> list[list[str]]:
        randomizer = secrets.SystemRandom()
        ships: list[list[str]] = []
        blocked: set[int] = set()
        for length in self.fleet:
            for _ in range(500):
                horizontal = bool(randomizer.randrange(2))
                row = randomizer.randrange(self.size)
                column = randomizer.randrange(self.size)
                if horizontal and column + length > self.size:
                    continue
                if not horizontal and row + length > self.size:
                    continue
                cells = [
                    (row * self.size + column + offset)
                    if horizontal
                    else ((row + offset) * self.size + column)
                    for offset in range(length)
                ]
                if any(cell in blocked for cell in cells):
                    continue
                ships.append([self._index_cell(cell) for cell in cells])
                for cell in cells:
                    cell_row, cell_column = divmod(cell, self.size)
                    for row_offset in (-1, 0, 1):
                        for column_offset in (-1, 0, 1):
                            near_row = cell_row + row_offset
                            near_column = cell_column + column_offset
                            if 0 <= near_row < self.size and 0 <= near_column < self.size:
                                blocked.add(near_row * self.size + near_column)
                break
            else:
                raise GameError("Не удалось расставить корабли. Создайте новую партию.")
        return ships

    def _afloat(self, ships: list[list[str]], shots: set[str]) -> int:
        return sum(not set(ship).issubset(shots) for ship in ships)

    def _is_cell(self, cell: str) -> bool:
        return len(cell) in {2, 3} and cell[0] in self.columns and cell[1:].isdigit() and 1 <= int(cell[1:]) <= self.size

    def _cell_index(self, cell: str) -> int:
        return (int(cell[1:]) - 1) * self.size + self.columns.index(cell[0])

    def _index_cell(self, index: int) -> str:
        row, column = divmod(index, self.size)
        return f"{self.columns[column]}{row + 1}"

    @staticmethod
    def _other(marker: str) -> str:
        return "O" if marker == "X" else "X"

    def _validate_state(self, state: dict[str, Any]) -> None:
        boards = state.get("boards")
        shots = state.get("shots")
        if (
            not isinstance(boards, dict)
            or not isinstance(shots, dict)
            or set(boards) != {"X", "O"}
            or set(shots) != {"X", "O"}
            or state.get("turn") not in {"X", "O"}
            or any(not isinstance(boards[marker], list) for marker in ("X", "O"))
            or any(not isinstance(shots[marker], list) for marker in ("X", "O"))
        ):
            raise GameError("Состояние партии повреждено.")


GAME_REGISTRY: dict[str, GameDefinition] = {
    "tic_tac_toe": TicTacToeDefinition(),
    "durak": DurakDefinition(),
    "battleship": BattleshipDefinition(),
}


class GameService:
    """Coordinates identities, invitations and game rules without HTTP concerns."""

    def __init__(self, store: GameStore) -> None:
        self.store = store

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.key,
                "title": item.title,
                "players": item.min_players,
                "description": item.description,
            }
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

    def cancel_link_invitation(self, chat_id: int, token: str) -> dict[str, Any]:
        if not token or not self.store.cancel_game_link_invitation(token, chat_id):
            raise GameError("Эта ссылка уже использована, закрыта или вам не принадлежит.")
        return {"token": token, "status": "cancelled"}

    def accept(self, chat_id: int, game_id: str) -> dict[str, Any]:
        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "pending" or int(row["guest_chat_id"]) != chat_id:
            raise GameError("Это приглашение уже нельзя принять.")
        state = json.loads(str(row["state_json"]))
        definition = GAME_REGISTRY.get(str(row["kind"]))
        if definition is None:
            raise GameError("Правила этой игры недоступны.")
        first_marker = str(state.get("attacker", state.get("turn", "X")))
        first_turn = int(row["host_chat_id"]) if first_marker == "X" else int(row["guest_chat_id"])
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status="active",
            state=state,
            turn_chat_id=first_turn,
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
        if not isinstance(definition, TicTacToeDefinition):
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

    def action(
        self,
        chat_id: int,
        game_id: str,
        action: str,
        card: str = "",
        target: str = "",
    ) -> dict[str, Any]:
        """Apply a game-specific action without exposing hidden state to the client."""

        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "active":
            raise GameError("Эта партия уже завершена.")
        definition = GAME_REGISTRY.get(str(row["kind"]))
        if definition is None:
            raise GameError("Правила этой игры недоступны.")
        host_id = int(row["host_chat_id"])
        guest_id = int(row["guest_chat_id"])
        marker = "X" if chat_id == host_id else "O"
        transition = definition.action(
            json.loads(str(row["state_json"])), marker, action, card, target
        )
        winner_id = (
            host_id
            if transition.winner_marker == "X"
            else guest_id
            if transition.winner_marker == "O"
            else None
        )
        status = "finished" if transition.winner_marker or transition.draw else "active"
        turn_chat_id = (
            None
            if status == "finished" or transition.turn_marker is None
            else host_id
            if transition.turn_marker == "X"
            else guest_id
        )
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status=status,
            state=transition.state,
            turn_chat_id=turn_chat_id,
            winner_chat_id=winner_id,
            event_type=f"game_{action}",
            event_payload={"card": card, "target": target, "marker": marker},
        )
        if updated is None:
            raise GameError("Игра уже изменилась. Обновите экран.")
        return self._public_game(updated, chat_id)

    def resign(self, chat_id: int, game_id: str) -> dict[str, Any]:
        """Finish an active game, awarding the win to the other player."""

        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "active":
            raise GameError("Сдаться можно только в активной партии.")
        host_id = int(row["host_chat_id"])
        guest_id = int(row["guest_chat_id"])
        winner_id = guest_id if chat_id == host_id else host_id
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status="finished",
            state=json.loads(str(row["state_json"])),
            turn_chat_id=None,
            winner_chat_id=winner_id,
            event_type="resigned",
        )
        if updated is None:
            raise GameError("Игра уже изменилась. Обновите экран.")
        return self._public_game(updated, chat_id)

    def finish(self, chat_id: int, game_id: str) -> dict[str, Any]:
        """Close an active game without assigning a winner."""

        row = self.store.game_for_player(game_id, chat_id)
        if str(row["status"]) != "active":
            raise GameError("Завершить можно только активную партию.")
        updated = self.store.update_game(
            game_id,
            chat_id,
            int(row["version"]),
            status="cancelled",
            state=json.loads(str(row["state_json"])),
            turn_chat_id=None,
            event_type="finished_by_player",
        )
        if updated is None:
            raise GameError("Игра уже изменилась. Обновите экран.")
        return self._public_game(updated, chat_id)

    def _public_game(self, row: Any, chat_id: int) -> dict[str, Any]:
        host_id = int(row["host_chat_id"])
        guest_id = int(row["guest_chat_id"])
        mine = chat_id == host_id
        opponent = str((row["guest_nickname"] if mine else row["host_nickname"]) or "игрок")
        status = str(row["status"])
        definition = GAME_REGISTRY[str(row["kind"])]
        marker = "X" if mine else "O"
        return {
            "id": str(row["id"]),
            "kind": str(row["kind"]),
            "title": definition.title,
            "status": status,
            "state": definition.public_state(json.loads(str(row["state_json"])), marker),
            "you": {
                "marker": marker,
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
