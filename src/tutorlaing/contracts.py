"""Application ports shared by use cases and infrastructure adapters.

Protocols are intentionally narrow.  A service depends only on the operations it
uses, while ``Storage`` and ``TelegramAPI`` remain replaceable adapters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


Keyboard = list[list[dict[str, str]]]


class TransportError(RuntimeError):
    """A recoverable failure reported by an outbound transport adapter."""


class TelegramGateway(Protocol):
    def send_message(
        self, chat_id: int, text: str, keyboard: Keyboard | None = None
    ) -> Any: ...

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: Keyboard | None = None,
    ) -> Any: ...

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...


class WorkspaceStore(Protocol):
    def get_user(self, chat_id: int) -> Any: ...

    def set_user_state(self, chat_id: int, **values: Any) -> None: ...

    def event(
        self,
        chat_id: int | None,
        event_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...


class LanguageStore(Protocol):
    def get_user(self, chat_id: int) -> Any: ...

    def add_ai_analysis(
        self,
        chat_id: int,
        operation: str,
        source_text: str,
        result: dict[str, Any],
        provider: str,
        model: str,
        prompt_version: str,
        latency_ms: int,
        **values: Any,
    ) -> int: ...

    def event(
        self,
        chat_id: int | None,
        event_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...


class ProgressStore(Protocol):
    def progress_evidence(self, chat_id: int) -> dict[str, Any]: ...


class FeedbackStore(LanguageStore, Protocol):
    def get_ai_analysis(self, analysis_id: int, chat_id: int) -> Any: ...

    def latest_ai_analysis(
        self, chat_id: int, target_language: str | None = None
    ) -> Any | None: ...


class MenuStore(WorkspaceStore, ProgressStore, Protocol):
    def ensure_user(self, chat_id: int, first_name: str = "") -> Any: ...

    def pending_reviews(
        self, chat_id: int, include_future: bool = False
    ) -> list[Any]: ...

    def set_reminder_mode(
        self, chat_id: int, mode: str, next_at: datetime | None
    ) -> None: ...


class ReminderStore(Protocol):
    def due_reminder_users(self, now: datetime) -> list[Any]: ...

    def reserve_next_reminder(
        self, chat_id: int, expected_at: str, sent_at: datetime, next_at: datetime
    ) -> bool: ...

    def schedule_next_reminder(
        self, chat_id: int, next_at: datetime | None
    ) -> None: ...

    def retry_failed_reminder(
        self, chat_id: int, expected_at: datetime, retry_at: datetime
    ) -> bool: ...

    def record_reminder_delivery(
        self, chat_id: int, outcome: str, mode: str
    ) -> None: ...


class ReminderDelivery(Protocol):
    def send_scheduled_reminder(self, chat_id: int, mode: str) -> None: ...


class UpdateStore(Protocol):
    def claim_update(self, update_id: int) -> bool: ...

    def release_update(self, update_id: int) -> None: ...

    def complete_update(self, update_id: int) -> None: ...


class UpdateTarget(Protocol):
    def handle_text(self, chat_id: int, first_name: str, text: str) -> None: ...

    def handle_callback(
        self, chat_id: int, first_name: str, callback_id: str, data: str
    ) -> None: ...


class ToolkitStore(Protocol):
    def get_user(self, chat_id: int) -> Any: ...

    def set_user_state(self, chat_id: int, **values: Any) -> None: ...

    def suspend_activity(self, chat_id: int) -> bool: ...

    def active_drill(self, chat_id: int) -> Any | None: ...

    def problem_history(
        self, chat_id: int, target_language: str, limit: int = 12
    ) -> dict[str, list[dict[str, Any]]]: ...

    def start_drill(
        self,
        chat_id: int,
        source_analysis_id: int | None,
        title: str,
        focus: str,
        items: list[dict[str, Any]],
        mode: str = "adaptive",
        replace_active: bool = True,
    ) -> str: ...

    def add_ai_analysis(
        self,
        chat_id: int,
        operation: str,
        source_text: str,
        result: dict[str, Any],
        provider: str,
        model: str,
        prompt_version: str,
        latency_ms: int,
        **values: Any,
    ) -> int: ...

    def event(
        self,
        chat_id: int | None,
        event_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...


class ToolkitDelivery(Protocol):
    def send_drill_item(
        self, chat_id: int, drill_id: str, scheduled: bool = False
    ) -> None: ...

    def schedule_next_assignment(self, chat_id: int) -> None: ...
