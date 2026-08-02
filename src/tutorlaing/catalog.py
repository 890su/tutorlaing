from __future__ import annotations

from typing import Any

from .content import Scenario, load_scenarios


class ScenarioCatalog:
    """Read-only catalog of curated courses, keyed by target language."""

    def __init__(self, courses: dict[str, dict[str, Scenario]] | None = None):
        self._courses = courses or {
            "pl": load_scenarios("pl"),
            "en": load_scenarios("en"),
        }

    @property
    def supported_languages(self) -> tuple[str, ...]:
        return tuple(self._courses)

    def for_language(self, target_language: str) -> dict[str, Scenario]:
        try:
            return self._courses[target_language]
        except KeyError as exc:
            raise ValueError(f"Unsupported target language: {target_language}") from exc

    def for_user(self, user: Any) -> dict[str, Scenario]:
        return self.for_language(str(user["target_language"]))
