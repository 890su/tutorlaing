from __future__ import annotations

from .i18n import tr


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
