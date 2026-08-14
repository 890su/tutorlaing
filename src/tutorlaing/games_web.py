"""Authenticated HTTP adapter for the Telegram Mini App games surface."""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import time
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from .game_service import GameError, GameService
from .privacy import CONSENT_VERSION
from .storage import Storage


MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60
ASSETS = {"/games": "index.html", "/games/": "index.html", "/games/app.js": "app.js", "/games/app.css": "app.css"}


@dataclass(frozen=True)
class WebResponse:
    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()


class MiniAppAuthError(PermissionError):
    pass


class GamesWebApp:
    """Serves static UI and a narrow JSON API; domain rules stay in ``games``."""

    def __init__(
        self,
        storage: Storage,
        telegram_token: str,
        allowed_chat_ids: frozenset[int] | None = None,
        notify: Callable[[int, str], None] | None = None,
    ) -> None:
        self.storage = storage
        self.telegram_token = telegram_token
        self.allowed_chat_ids = allowed_chat_ids
        self.games = GameService(storage)
        self.notify = notify

    def get(self, raw_path: str) -> WebResponse | None:
        path = urlsplit(raw_path).path
        filename = ASSETS.get(path)
        if filename is None:
            return None
        body = files("tutorlaing.games").joinpath(filename).read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return WebResponse(
            200,
            body,
            f"{content_type}; charset=utf-8",
            (
                ("Cache-Control", "no-store"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' https://telegram.org; "
                    "style-src 'self'; connect-src 'self'; img-src 'self' data:",
                ),
            ),
        )

    def post(self, raw_path: str, body: bytes, init_data: str) -> WebResponse | None:
        path = urlsplit(raw_path).path
        if not path.startswith("/games/api/"):
            return None
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                raise GameError("Ожидался объект запроса.")
            chat_id = self._authenticate(init_data)
            if path == "/games/api/state":
                result = self.games.snapshot(chat_id)
            elif path == "/games/api/profile":
                result = self.games.set_nickname(chat_id, str(data.get("nickname", "")))
            elif path == "/games/api/invitations":
                result = self.games.invite(
                    chat_id,
                    str(data.get("kind", "")),
                    str(data.get("nickname", "")),
                )
                self._notify_opponent(result, chat_id, "invite")
            elif path == "/games/api/accept":
                result = self.games.accept(chat_id, str(data.get("game_id", "")))
                self._notify_opponent(result, chat_id, "accept")
            elif path == "/games/api/decline":
                result = self.games.decline(chat_id, str(data.get("game_id", "")))
                self._notify_opponent(result, chat_id, "decline")
            elif path == "/games/api/move":
                result = self.games.move(
                    chat_id, str(data.get("game_id", "")), int(data.get("position", -1))
                )
                self._notify_opponent(result, chat_id, "move")
            else:
                return WebResponse(404, b"", "text/plain")
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, GameError) as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except MiniAppAuthError as exc:
            return self._json(403, {"ok": False, "error": str(exc)})
        return self._json(200, {"ok": True, "result": result})

    def _authenticate(self, init_data: str) -> int:
        try:
            values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        except ValueError as exc:
            raise MiniAppAuthError(
                "Telegram не подтвердил вход. Откройте игру из бота."
            ) from exc
        provided_hash = values.pop("hash", "")
        if not provided_hash:
            raise MiniAppAuthError("Telegram не подтвердил вход. Откройте игру из бота.")
        data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(
            b"WebAppData", self.telegram_token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret, data_check.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(provided_hash, expected_hash):
            raise MiniAppAuthError("Telegram не подтвердил вход. Откройте игру из бота.")
        try:
            auth_date = int(values["auth_date"])
            identity = json.loads(values["user"])
            chat_id = int(identity["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MiniAppAuthError("Не удалось определить игрока Telegram.") from exc
        if auth_date > int(time.time()) + 300 or int(time.time()) - auth_date > MAX_INIT_DATA_AGE_SECONDS:
            raise MiniAppAuthError("Сессия игры устарела. Откройте её заново из бота.")
        if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
            raise MiniAppAuthError("Игры пока доступны только участникам alpha.")
        user = self.storage.ensure_user(chat_id, str(identity.get("first_name", "")))
        if int(user["consent_version"]) != CONSENT_VERSION:
            raise MiniAppAuthError("Сначала откройте бот и подтвердите правила приватности.")
        return chat_id

    def _notify_opponent(self, game: dict[str, Any], actor_chat_id: int, action: str) -> None:
        if self.notify is None:
            return
        row = self.storage.game_for_player(str(game["id"]), actor_chat_id)
        opponent_id = int(row["guest_chat_id"] if int(row["host_chat_id"]) == actor_chat_id else row["host_chat_id"])
        texts = {
            "invite": "🎮 Вам пришло приглашение в крестики-нолики. Откройте «Игры вдвоём» в помощнике.",
            "accept": "🎮 Приглашение принято. Ваш ход уже отображается в игре.",
            "decline": "🎮 Приглашение в игру отклонено.",
            "move": "🎮 Соперник сделал ход. Откройте «Игры вдвоём» в помощнике.",
        }
        self.notify(opponent_id, texts[action])

    @staticmethod
    def _json(status: int, payload: dict[str, Any]) -> WebResponse:
        return WebResponse(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            (("Cache-Control", "no-store"),),
        )
