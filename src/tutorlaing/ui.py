from __future__ import annotations


ROUTE_STAGES = {
    "ru": ("СИТУАЦИЯ", "ФРАЗА", "ЗАКРЕПЛЕНИЕ", "ПОВТОР"),
    "uk": ("СИТУАЦІЯ", "ФРАЗА", "ЗАКРІПЛЕННЯ", "ПОВТОРЕННЯ"),
    "en": ("SITUATION", "PHRASE", "PRACTICE", "REVIEW"),
    "pl": ("SYTUACJA", "FRAZA", "UTRWALENIE", "POWTÓRKA"),
}
ROUTE_KEYS = ("SITUATION", "PHRASE", "PRACTICE", "REVIEW")


def route(current: str | None = None, language: str = "ru") -> str:
    parts = []
    labels = ROUTE_STAGES.get(language, ROUTE_STAGES["ru"])
    current_upper = (current or "").upper()
    for key, label in zip(ROUTE_KEYS, labels):
        marker = "●" if current_upper in {key, label.upper()} else "○"
        parts.append(f"{marker} {label}")
    return "  →  ".join(parts)


def card(
    title: str,
    body: str,
    current_stage: str | None = None,
    language: str = "ru",
) -> str:
    sections = [title.strip().upper()]
    if current_stage:
        sections.append(route(current_stage, language))
    if body.strip():
        sections.append(body.strip())
    return "\n\n".join(sections)


def progress(title: str, current: int, total: int) -> str:
    filled = "●" * current
    empty = "○" * max(0, total - current)
    return f"{title.upper()} · {current}/{total}\n{filled}{empty}"
