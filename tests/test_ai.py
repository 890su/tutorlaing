import json
import unittest

from tutorlaing.ai import DrillPack, GeminiClient
from tutorlaing.content import load_scenarios


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class GeminiClientTests(unittest.TestCase):
    def test_structured_analysis_is_parsed_and_bounded(self) -> None:
        result = {
            "task_achieved": True,
            "score": 1.2,
            "confidence": 0.9,
            "positive_feedback": "Смысл понятен.",
            "meaning_gaps": [],
            "critical_corrections": [],
            "optional_improvements": ["Лучше использовать neutral form."],
            "natural_response": "Od dwóch dni nie mam gorączki.",
            "alternatives": [
                {"text": "Nie mam gorączki od dwóch dni.", "register": "neutral", "nuance": "Нейтрально"}
            ],
            "grammar_chunks": [{"text": "od dwóch dni", "label": "duration"}],
            "pragmatic_note": "",
            "explanation": "Ответ достаточен.",
        }
        envelope = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
        }
        client = GeminiClient("test-key", opener=lambda *_args, **_kwargs: FakeHTTPResponse(envelope))
        step = load_scenarios()["pharmacy"].steps[1]

        analysis = client.analyze_response(step, "Od dwóch dni.", "ru", "pl", 0.5)

        self.assertTrue(analysis.task_achieved)
        self.assertEqual(1.0, analysis.score)
        self.assertEqual("od dwóch dni", analysis.grammar_chunks[0].text)
        self.assertEqual("gemini-3.5-flash", analysis.model)

    def test_drill_pack_requires_variety_and_active_recall(self) -> None:
        base = {
            "skill": "case",
            "context": "W aptece",
            "correct_answer": "dwóch",
            "accepted_answers": ["dwóch"],
            "explanation": "Po od używamy dopełniacza.",
            "hint": "od ilu?",
            "difficulty": 1,
        }
        data = {
            "title": "Przypadki",
            "focus": "od dwóch dni",
            "items": [
                {**base, "type": "choose_form", "prompt": "Od ___ dni", "options": ["dwóch", "dwa"]},
                {**base, "type": "meaning_choice", "prompt": "Wybierz sens", "options": ["dwóch", "dwa"]},
                {**base, "type": "fill_ending", "prompt": "Uzupełnij", "options": []},
                {**base, "type": "transform", "prompt": "Zmień formę", "options": []},
                {**base, "type": "free_recall", "prompt": "Odpowiedz", "options": []},
            ],
        }
        pack = DrillPack.from_dict(data)
        self.assertEqual(5, len(pack.items))
        self.assertGreaterEqual(len({item.type for item in pack.items}), 3)

    def test_glossary_returns_only_exact_terms_and_skips_c1(self) -> None:
        payload = {
            "notes": [
                {"term": "zaświadczenie", "translation": "справка", "cefr": "C1"},
                {"term": "absent", "translation": "лишнее", "cefr": "C1"},
            ]
        }
        envelope = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]
        }
        calls = []

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeHTTPResponse(envelope)

        client = GeminiClient("test-key", opener=opener)
        notes = client.glossary_notes(
            "Potrzebuję zaświadczenie.", "B1", "pl", "ru"
        )

        self.assertEqual([{"term": "zaświadczenie", "translation": "справка", "cefr": "C1"}], notes)
        self.assertEqual([], client.glossary_notes("Dowolny tekst", "C1", "pl", "ru"))
        self.assertEqual(1, len(calls))

    def test_phrase_translation_returns_natural_variants(self) -> None:
        payload = {
            "primary": "Czy może pan mówić wolniej?",
            "alternatives": [
                {
                    "text": "Czy mógłby pan mówić trochę wolniej?",
                    "register": "formal",
                    "nuance": "Более вежливая просьба.",
                }
            ],
            "usage_note": "Форма pan подходит для незнакомого мужчины.",
        }
        envelope = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]
        }
        client = GeminiClient(
            "test-key", opener=lambda *_args, **_kwargs: FakeHTTPResponse(envelope)
        )

        result = client.translate_with_variants(
            "Можете говорить медленнее?", "ru", "pl", "ru"
        )

        self.assertEqual("Czy może pan mówić wolniej?", result.primary)
        self.assertEqual("formal", result.alternatives[0].register)
        self.assertIn("незнакомого мужчины", result.usage_note)

    def test_flashcard_pack_enforces_five_four_option_cards(self) -> None:
        phrases = (
            ("Dzień dobry", "Добрый день"),
            ("Od dwóch dni", "Уже два дня"),
            ("Proszę powtórzyć", "Повторите, пожалуйста"),
            ("Piątek mi pasuje", "Пятница мне подходит"),
            ("Kran przecieka", "Кран протекает"),
        )
        items = [
            {
                "type": "flashcard",
                "skill": "meaning",
                "prompt": "Что означает фраза?",
                "context": phrase,
                "options": [meaning, "Вариант 2", "Вариант 3", "Вариант 4"],
                "correct_answer": meaning,
                "accepted_answers": [meaning],
                "explanation": "Практическое значение.",
                "hint": "Вспомните ситуацию.",
                "difficulty": 1,
            }
            for phrase, meaning in phrases
        ]
        envelope = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "title": "Фразы",
                                        "focus": "Бытовые ситуации",
                                        "items": items,
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        client = GeminiClient(
            "test-key", opener=lambda *_args, **_kwargs: FakeHTTPResponse(envelope)
        )

        pack = client.generate_toolkit_pack("cards", {}, "ru", "pl", "ru")

        self.assertEqual(5, len(pack.items))
        self.assertTrue(all(item.type == "flashcard" for item in pack.items))
        self.assertTrue(all(len(item.options) == 4 for item in pack.items))


if __name__ == "__main__":
    unittest.main()
