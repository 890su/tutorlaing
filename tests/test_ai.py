import json
import unittest

from tutorlaing.ai import GeminiClient
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


if __name__ == "__main__":
    unittest.main()
