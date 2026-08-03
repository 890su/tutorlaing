from __future__ import annotations

import logging

from .contracts import Keyboard, TelegramGateway, TransportError, WorkspaceStore
from .contracts import ReplyKeyboard


LOGGER = logging.getLogger(__name__)


class TelegramWorkspace:
    """Owns the one-current-card policy for a Telegram chat.

    Inline navigation edits the current card.  ``force_new`` is reserved for a
    learner text reply or a scheduled notification that must appear below the
    previous message.
    """

    def __init__(self, store: WorkspaceStore, telegram: TelegramGateway):
        self.store = store
        self.telegram = telegram

    def set_reply_keyboard(self, chat_id: int, keyboard: ReplyKeyboard) -> None:
        self.telegram.set_reply_keyboard(chat_id, keyboard)

    def show(
        self,
        chat_id: int,
        text: str,
        keyboard: Keyboard | None = None,
        *,
        force_new: bool = False,
        surface: str = "learning",
    ) -> int | None:
        user = self.store.get_user(chat_id)
        message_id = user["workspace_message_id"]
        if message_id and not force_new:
            try:
                self.telegram.edit_message(chat_id, int(message_id), text, keyboard)
                self.store.event(chat_id, "ui_message_edited", {"surface": surface})
                return int(message_id)
            except TransportError as exc:
                if "message is not modified" in str(exc).lower():
                    return int(message_id)
                LOGGER.info("Workspace edit failed; sending a new card", exc_info=True)
                try:
                    self.telegram.delete_message(chat_id, int(message_id))
                except TransportError:
                    LOGGER.debug("Could not delete obsolete workspace card", exc_info=True)

        result = self.telegram.send_message(chat_id, text, keyboard)
        new_message_id = (
            int(result["message_id"])
            if isinstance(result, dict) and result.get("message_id")
            else None
        )
        if new_message_id is not None:
            self.store.set_user_state(chat_id, workspace_message_id=new_message_id)
        self.store.event(
            chat_id,
            "ui_message_sent",
            {"surface": surface, "scheduled": force_new},
        )
        return new_message_id
