"""Cached hourly language cards and safe fallback material."""

from __future__ import annotations

from typing import Any

from .ai import AIClient, AIError, HourlyCard


def fallback_cards(target_language: str) -> list[dict[str, Any]]:
    """Keep the hourly loop useful when an AI provider is unavailable."""

    if target_language == "en":
        cards = (
            ("word", "Как по-английски «чек из магазина»?", "receipt", (), "Receipt — чек из магазина. Полезно: Could I have the receipt, please?"),
            ("word", "Как по-английски «свободный термин / время»?", "available appointment", ("free appointment",), "Available appointment — свободное время для записи. Например: Is there an available appointment this week?"),
            ("synonym", "Как ещё вежливо сказать «Мне это подходит»?", "That works for me", ("That is fine for me",), "That works for me — естественно согласиться со временем или предложением."),
            ("synonym", "Как ещё сказать «Я не понял(а)»?", "I did not catch that", ("I did not understand",), "I did not catch that — мягкая просьба повторить сказанное."),
            ("phrase", "Как вежливо попросить говорить медленнее?", "Could you speak a little more slowly, please?", ("Could you speak more slowly, please?",), "Эта фраза уместна в офисе, магазине и на приёме."),
            ("phrase", "Как сказать, что товар вам не подходит?", "This item does not fit me", ("It does not fit me",), "Говорите так о размере одежды или другого подходящего предмета."),
        )
    else:
        cards = (
            ("word", "Как по-польски «чек из магазина»?", "paragon", (), "Paragon — чек из магазина. Полезно: Czy mogę prosić o paragon?"),
            ("word", "Как по-польски «свободный термин / время»?", "wolny termin", (), "Wolny termin — свободное время для записи. Например: Czy jest wolny termin w tym tygodniu?"),
            ("synonym", "Как ещё вежливо сказать «Мне это подходит»?", "ten termin mi odpowiada", ("pasuje mi",), "Ten termin mi odpowiada — естественно согласиться на предложенное время."),
            ("synonym", "Как ещё сказать «Я не понял(а)»?", "nie zrozumiałem", ("nie zrozumiałam", "nie rozumiem"), "Nie zrozumiałem / nie zrozumiałam — прямо сообщить о непонимании."),
            ("phrase", "Как вежливо попросить говорить медленнее?", "Czy może pan/pani mówić trochę wolniej?", ("Czy możesz mówić trochę wolniej?",), "В официальной ситуации используйте pan/pani, с близким человеком — możesz."),
            ("phrase", "Как сказать, что товар вам не подходит по размеру?", "Ten produkt mi nie pasuje", ("Ma niewłaściwy rozmiar",), "Так говорят о размере одежды или другого подходящего предмета."),
        )
    return [
        {"kind": kind, "cue": cue, "answer": answer, "accepted_answers": list(accepted) or [answer], "details": details}
        for kind, cue, answer, accepted, details in cards
    ]


class HourlyCardService:
    """Generates a bounded batch once and hands persistence to the storage adapter."""

    def __init__(self, ai: AIClient | None = None) -> None:
        self.ai = ai

    def generate(
        self,
        user: Any,
        recent_answers: list[str],
        profile: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], str, str]:
        target_language = str(user["target_language"])
        if self.ai is not None and callable(getattr(self.ai, "generate_hourly_cards", None)):
            try:
                batch = self.ai.generate_hourly_cards(
                    {
                        "learner_level": str(user["learner_level"]),
                        "current_scenario": str(user["current_scenario"] or ""),
                        "recent_answers": recent_answers[:12],
                        "learner_profile": profile or {},
                    },
                    str(user["instruction_language"]),
                    target_language,
                )
                return [card.to_dict() for card in batch.cards], self.ai.provider, self.ai.model
            except (AIError, AttributeError, ValueError):
                pass
        return fallback_cards(target_language), "fallback", "curated-hourly-v1"
