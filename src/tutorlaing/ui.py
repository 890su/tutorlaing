from __future__ import annotations


ROUTE_STAGES = ("СИТУАЦИЯ", "ФРАЗА", "ЗАКРЕПЛЕНИЕ", "ПОВТОР")


def route(current: str | None = None) -> str:
    parts = []
    for stage in ROUTE_STAGES:
        marker = "●" if stage == current else "○"
        parts.append(f"{marker} {stage}")
    return "  →  ".join(parts)


def card(title: str, body: str, current_stage: str | None = None) -> str:
    sections = [title.strip().upper()]
    if current_stage:
        sections.append(route(current_stage))
    if body.strip():
        sections.append(body.strip())
    return "\n\n".join(sections)


def progress(title: str, current: int, total: int) -> str:
    filled = "●" * current
    empty = "○" * max(0, total - current)
    return f"{title.upper()} · {current}/{total}\n{filled}{empty}"
