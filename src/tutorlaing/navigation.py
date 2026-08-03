from __future__ import annotations

from .i18n import tr


REPLY_ACTION_KEYS = {
    "learn": "navigation.learn",
    "tools": "navigation.tools",
    "progress": "navigation.progress",
    "settings": "navigation.settings",
}


def action_button(
    language: str,
    label_key: str,
    callback_data: str,
    **values: object,
) -> dict[str, str]:
    """Create a localized callback button with one explicit destination."""
    return {
        "text": tr(language, label_key, **values),
        "callback_data": callback_data,
    }


def home_row(language: str) -> list[dict[str, str]]:
    return [action_button(language, "action.home", "home")]


def back_row(
    language: str, callback_data: str, destination: str
) -> list[dict[str, str]]:
    return [
        action_button(
            language,
            f"action.back_to_{destination}",
            callback_data,
        )
    ]


def reply_navigation(language: str) -> list[list[str]]:
    """Stable bottom navigation; task-specific actions remain inline."""

    return [
        [tr(language, REPLY_ACTION_KEYS["learn"]), tr(language, REPLY_ACTION_KEYS["tools"])],
        [
            tr(language, REPLY_ACTION_KEYS["progress"]),
            tr(language, REPLY_ACTION_KEYS["settings"]),
        ],
    ]


def reply_action(text: str) -> str | None:
    normalized = text.strip()
    for language in ("ru", "uk", "en", "pl"):
        for action, key in REPLY_ACTION_KEYS.items():
            if normalized == tr(language, key):
                return action
    return None
