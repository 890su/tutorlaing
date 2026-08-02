from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any


LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: int = 40
    ) -> Any:
        payload = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "TutorlaingBot/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            description = str(exc)
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                description = error_payload.get("description", description)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise TelegramError(
                f"Telegram {method} request failed: {description}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TelegramError(f"Telegram {method} request failed: {exc}") from exc
        if not data.get("ok"):
            raise TelegramError(
                f"Telegram {method} returned error: {data.get('description', 'unknown')}"
            )
        return data.get("result")

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", params)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        return self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard or []},
            },
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        self.call("answerCallbackQuery", params)

    def get_updates(self, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=poll_timeout + 10,
        )
