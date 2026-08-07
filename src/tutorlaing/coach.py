"""Side-channel teacher linked to, but isolated from, a learning activity."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .ai import AIClient, AIError, CoachResponse


LOGGER = logging.getLogger(__name__)
COACH_OPERATIONS = {"question", "hint", "say", "translate", "explain"}


class CoachStore(Protocol):
    def open_coach_session(
        self,
        chat_id: int,
        activity_kind: str,
        activity_id: str,
        context: dict[str, Any],
    ) -> str: ...

    def coach_session(self, chat_id: int, session_id: str) -> Any: ...

    def add_coach_exchange(
        self,
        chat_id: int,
        session_id: str,
        operation: str,
        question: str,
        response: dict[str, Any],
        provider: str,
    ) -> int: ...

    def event(
        self,
        chat_id: int | None,
        event_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class CoachSession:
    id: str
    activity_kind: str
    activity_id: str
    context: dict[str, Any]


class CoachService:
    """Answers language questions without applying a turn to the main activity."""

    def __init__(self, store: CoachStore, ai: AIClient | None):
        self.store = store
        self.ai = ai

    def open(
        self,
        chat_id: int,
        activity_kind: str,
        activity_id: str,
        context: dict[str, Any],
    ) -> CoachSession:
        session_id = self.store.open_coach_session(
            chat_id, activity_kind, activity_id, context
        )
        return CoachSession(session_id, activity_kind, activity_id, dict(context))

    def get(self, chat_id: int, session_id: str) -> CoachSession:
        import json

        row = self.store.coach_session(chat_id, session_id)
        return CoachSession(
            id=str(row["id"]),
            activity_kind=str(row["activity_kind"]),
            activity_id=str(row["activity_id"]),
            context=json.loads(str(row["context_json"])),
        )

    def answer(
        self, chat_id: int, session_id: str, operation: str, question: str
    ) -> CoachResponse:
        if operation not in COACH_OPERATIONS:
            raise ValueError(f"Unsupported coach operation: {operation}")
        session = self.get(chat_id, session_id)
        response: CoachResponse
        provider = "local"
        if self.ai is not None:
            try:
                response = self.ai.coach(session.context, question, operation)
                provider = self.ai.provider
                if operation == "hint" and (
                    response.disclosed_help_level > 2
                    or response.suggested_phrases
                    or response.translation
                ):
                    self.store.event(
                        chat_id,
                        "coach_help_policy_rejected",
                        {"operation": operation, "provider": provider},
                    )
                    response = self._fallback(session, operation)
                    provider = "local-policy"
            except (AIError, AttributeError):
                LOGGER.exception("AI coach failed; using bounded local help")
                response = self._fallback(session, operation)
        else:
            response = self._fallback(session, operation)
        self.store.add_coach_exchange(
            chat_id,
            session_id,
            operation,
            question,
            {
                "answer": response.answer,
                "suggested_phrases": [
                    {
                        "text": phrase.text,
                        "register": phrase.register,
                        "nuance": phrase.nuance,
                    }
                    for phrase in response.suggested_phrases
                ],
                "translation": response.translation,
                "disclosed_help_level": response.disclosed_help_level,
            },
            provider,
        )
        self.store.event(
            chat_id,
            "coach_answered",
            {
                "activity_kind": session.activity_kind,
                "operation": operation,
                "help_level": response.disclosed_help_level,
                "provider": provider,
            },
        )
        return response

    @staticmethod
    def _fallback(session: CoachSession, operation: str) -> CoachResponse:
        context = session.context
        if operation == "hint" and context.get("hint"):
            answer = str(context["hint"])
        else:
            task = str(context.get("task") or "").strip()
            answer = task or "Сосредоточьтесь на цели реплики и обязательных деталях."
        return CoachResponse(
            answer=answer,
            suggested_phrases=(),
            translation="",
            disclosed_help_level=1,
        )
