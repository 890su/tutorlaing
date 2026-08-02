from __future__ import annotations

from typing import Any

from .contracts import TelegramGateway, UpdateStore, UpdateTarget


class TelegramUpdateDispatcher:
    """Normalize Telegram updates and provide exactly-once local dispatch."""

    def __init__(
        self,
        store: UpdateStore,
        telegram: TelegramGateway,
        target: UpdateTarget,
    ):
        self.store = store
        self.telegram = telegram
        self.target = target

    def dispatch(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if update_id is not None and not self.store.claim_update(int(update_id)):
            return
        try:
            self._route(update)
        except Exception:
            if update_id is not None:
                self.store.release_update(int(update_id))
            raise
        else:
            if update_id is not None:
                self.store.complete_update(int(update_id))

    def _route(self, update: dict[str, Any]) -> None:
        if "message" in update:
            message = update["message"]
            chat_id = int(message["chat"]["id"])
            text = message.get("text")
            if not text:
                self.telegram.send_message(
                    chat_id,
                    "В этой версии используйте текстовые ответы. "
                    "Голос появится после проверки качества.",
                )
                return
            first_name = str(message.get("from", {}).get("first_name", ""))
            self.target.handle_text(chat_id, first_name, str(text))
            return

        if "callback_query" in update:
            callback = update["callback_query"]
            message = callback.get("message")
            if message:
                self.target.handle_callback(
                    int(message["chat"]["id"]),
                    str(callback.get("from", {}).get("first_name", "")),
                    str(callback["id"]),
                    str(callback.get("data", "")),
                )
