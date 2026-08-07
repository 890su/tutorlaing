"""Canonical Telegram slash-command catalog.

The registered command menu and the text router share this catalog so a command
cannot be advertised without being part of the supported public interface.
"""

from __future__ import annotations


PUBLIC_COMMANDS = (
    "/start",
    "/activities",
    "/practice",
    "/tools",
    "/progress",
    "/settings",
    "/help",
    "/grammar",
    "/privacy",
    "/delete_me",
)


COMMAND_DESCRIPTIONS = {
    "ru": (
        "Открыть экран «Сегодня»",
        "Продолжить сохранённое занятие",
        "Выбрать формат практики",
        "Слова, смысл, перевод и карточки",
        "Посмотреть прогресс",
        "Языки, профиль и напоминания",
        "Как устроено управление",
        "Объяснить фрагмент фразы",
        "Как хранятся данные",
        "Удалить мои данные",
    ),
    "uk": (
        "Відкрити екран «Сьогодні»",
        "Продовжити збережене заняття",
        "Обрати формат практики",
        "Слова, значення, переклад і картки",
        "Переглянути прогрес",
        "Мови, профіль і нагадування",
        "Як влаштоване керування",
        "Пояснити фрагмент фрази",
        "Як зберігаються дані",
        "Видалити мої дані",
    ),
    "en": (
        "Open Today",
        "Continue a saved activity",
        "Choose a practice format",
        "Words, meaning, translation and cards",
        "View learning progress",
        "Languages, profile and reminders",
        "How navigation works",
        "Explain part of a phrase",
        "How data is stored",
        "Delete my data",
    ),
    "pl": (
        "Otwórz ekran Dzisiaj",
        "Kontynuuj zapisane zajęcie",
        "Wybierz rodzaj ćwiczenia",
        "Słowa, znaczenia, tłumaczenie i fiszki",
        "Zobacz postępy",
        "Języki, profil i przypomnienia",
        "Jak działa nawigacja",
        "Wyjaśnij fragment wypowiedzi",
        "Jak przechowywane są dane",
        "Usuń moje dane",
    ),
}


def command_payload(language: str) -> list[dict[str, str]]:
    descriptions = COMMAND_DESCRIPTIONS.get(language, COMMAND_DESCRIPTIONS["ru"])
    return [
        {"command": command.removeprefix("/"), "description": description}
        for command, description in zip(PUBLIC_COMMANDS, descriptions, strict=True)
    ]


def parse_command(text: str) -> str:
    """Return a lowercase command without an optional Telegram @bot suffix."""

    token = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    if not token.startswith("/"):
        return ""
    return token.split("@", 1)[0]
