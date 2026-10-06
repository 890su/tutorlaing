import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tutorlaing.ai import AIError, DrillEvaluation, FailoverAIClient, GeminiClient, OpenAIClient
from tutorlaing.app import TutorlaingBot
from tutorlaing.config import Settings
from tutorlaing.privacy import CONSENT_VERSION
from tutorlaing.storage import Storage
from tutorlaing.telegram_api import TelegramAPI, TelegramError
from tutorlaing.vocabulary import (
    MAX_IMAGE_BYTES, VocabularyList, VocabularyWord, advance, initial_state,
    parse_text_pairs, record_answer, record_exam_answer, start_exam, start_revision,
    start_round, text_entries,
)
from test_app import FakeAI, FakeTelegram


WORD = {"source": "яблоко", "source_language": "ru", "english": "apple",
        "accepted_answers": ["an apple"], "hint": "Фрукт, который растёт на дереве.",
        "explanation": "Apple — яблоко.", "example_gap": "I ate an ___ for lunch."}
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


class PhotoAI(FakeAI):
    def __init__(self):
        self.extract_calls = []
        self.evaluate_calls = []
        self.text_calls = []
        self.error = False

    def extract_vocabulary(self, *args):
        self.extract_calls.append(args)
        if self.error:
            raise AIError("Unavailable")
        return VocabularyList.from_dict({"words": [WORD], "warnings": ""})

    def evaluate_drill_answer(self, item, response, instruction_language, target_language):
        self.evaluate_calls.append((item, response, instruction_language, target_language))
        return DrillEvaluation(False, 0.0, "Другое значение", item.correct_answer)

    def prepare_text_vocabulary(self, *args):
        self.text_calls.append(args)
        if self.error:
            raise AIError("Unavailable")
        return VocabularyList.from_dict({"words": [WORD], "warnings": ""})


class PhotoTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.downloads = []

    def download_image(self, file_id, max_bytes):
        self.downloads.append((file_id, max_bytes))
        return b"test image"


class VocabularyFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "words.sqlite3"
        self.storage = Storage(self.path)
        self.telegram, self.ai = PhotoTelegram(), PhotoAI()
        self.settings = Settings(
            telegram_bot_token="test", allowed_chat_ids=None, data_dir=Path(self.temp.name),
            health_host="127.0.0.1", health_port=0, poll_timeout=5, log_level="INFO",
            telegram_webhook_url="", telegram_webhook_secret="",
        )
        self.bot = TutorlaingBot(self.settings, self.storage, self.telegram, self.ai)
        self.bot.vocabulary.clock = lambda: NOW
        self.storage.ensure_user(42, "Learner")
        self.storage.accept_consent(42, CONSENT_VERSION)

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def state(self):
        row = self.storage.vocabulary_decks(42)[0]
        return row, json.loads(row["state_json"])

    def callback(self, action):
        row, _ = self.state()
        self.bot.handle_callback(42, "Learner", "cb", f"words:{action}:{row['id']}:{row['version']}")

    def import_word(self):
        self.bot.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "message_id": 500,
            "from": {"first_name": "Learner"}, "photo": [
                {"file_id": "small", "width": 100, "height": 100},
                {"file_id": "large", "width": 800, "height": 800}]}})

    def test_photo_routes_best_image_and_is_deduplicated(self):
        self.import_word()
        self.import_word()
        self.assertEqual([("large", MAX_IMAGE_BYTES)], self.telegram.downloads)
        self.assertEqual(1, len(self.storage.vocabulary_decks(42)))
        self.assertEqual("confirm", self.state()[1]["phase"])
        self.assertIn("яблоко → apple", self.telegram.messages[-1]["text"])

    def test_image_document_and_source_preference_are_supported(self):
        self.bot.handle_callback(42, "Learner", "lang", "words:source:pl")
        self.bot.handle_update({"update_id": 2, "message": {"chat": {"id": 42},
            "document": {"file_id": "png", "mime_type": "image/png", "file_size": 123}}})
        self.assertEqual(("image/png", "pl", "ru"), self.ai.extract_calls[0][1:])
        self.assertEqual("pl", self.storage.get_user(42)["target_language"])

    def test_word_answer_has_priority_and_does_not_change_lesson_or_languages(self):
        self.bot.begin_scenario(42, "pharmacy")
        before = dict(self.storage.get_user(42))
        self.import_word()
        self.callback("start")
        self.assertNotIn("apple", self.telegram.messages[-1]["text"])
        self.bot.handle_text(42, "Learner", "apple", message_id=501)
        self.assertTrue(self.state()[1]["correct"])
        self.callback("next")
        self.assertIn("I ate an ___", self.telegram.messages[-1]["text"])
        self.bot.handle_text(42, "Learner", "wrong")
        self.assertEqual("en", self.ai.evaluate_calls[-1][-1])
        after = self.storage.get_user(42)
        for key in ("current_session", "current_step", "stage", "target_language", "instruction_language", "translation_language"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(0, self.storage.response_count(before["current_session"], "scenario"))

    def test_draft_edit_is_explicit_and_can_add_remove_correct(self):
        self.import_word()
        self.callback("edit")
        self.bot.handle_text(42, "Learner", "яблоко = pear\njutro = tomorrow")
        row, state = self.state()
        words = json.loads(row["words_json"])
        self.assertEqual("confirm", state["phase"])
        self.assertEqual(["pear", "tomorrow"], [w["english"] for w in words])
        self.assertEqual("pl", words[1]["source_language"])
        self.assertEqual("", words[0]["example_gap"])
        self.callback("edit")
        version = self.state()[0]["version"]
        self.bot.handle_text(42, "Learner", "not a pair")
        self.assertEqual(version, self.state()[0]["version"])

    def test_resume_survives_restart_and_menu_pauses_text_capture(self):
        self.import_word()
        self.callback("start")
        self.bot.handle_text(42, "Learner", "apple")
        row, _ = self.state()
        self.bot.handle_text(42, "Learner", "/words")
        self.assertIsNone(self.storage.active_vocabulary_deck(42))
        self.storage.close()
        self.storage = Storage(self.path)
        bot = TutorlaingBot(self.settings, self.storage, self.telegram, self.ai)
        bot.handle_callback(42, "Learner", "open", f"words:open:{row['id']}")
        self.assertEqual("feedback", self.state()[1]["phase"])
        self.assertIn("apple", self.telegram.messages[-1]["text"])

    def test_stale_callbacks_cannot_advance_and_owner_cannot_open_other_deck(self):
        self.import_word()
        row, _ = self.state()
        old = f"words:start:{row['id']}:{row['version']}"
        self.callback("start")
        self.bot.handle_callback(42, "Learner", "old", old)
        self.assertEqual("recall", self.state()[1]["phase"])
        self.assertEqual(0, self.state()[1]["position"])
        self.storage.ensure_user(43)
        self.storage.accept_consent(43, CONSENT_VERSION)
        self.bot.handle_callback(43, "Other", "bad", f"words:open:{row['id']}")
        self.assertIsNone(self.storage.active_vocabulary_deck(43))

    def test_large_photo_and_no_consent_never_download(self):
        self.bot.handle_photo(42, "Learner", "big", "image/jpeg", MAX_IMAGE_BYTES + 1)
        self.bot.handle_photo(99, "New", "no-consent", "image/jpeg")
        self.assertFalse(self.telegram.downloads)
        self.assertFalse(self.storage.vocabulary_decks(42))

    def test_ai_failure_preserves_existing_deck_and_delete_user_removes_material(self):
        self.import_word()
        row, _ = self.state()
        self.ai.error = True
        self.bot.handle_photo(42, "Learner", "retry", "image/jpeg")
        self.assertEqual(row["id"], self.state()[0]["id"])
        self.storage.delete_user(42)
        self.assertFalse(self.storage.vocabulary_decks(42))

    def test_scheduled_cards_do_not_overwrite_active_vocabulary(self):
        self.import_word()
        self.callback("start")
        messages = len(self.telegram.messages)
        self.bot.send_scheduled_reminder(42, "hourly")
        self.assertEqual(messages, len(self.telegram.messages))
        self.assertEqual("recall", self.state()[1]["phase"])

    def test_context_variants_use_ai_and_reminder_query_suppresses_open_round(self):
        self.import_word()
        self.callback("start")
        self.bot.handle_text(42, "Learner", "apple")
        self.callback("next")
        self.bot.handle_text(42, "Learner", "an apple")
        self.assertEqual("an apple", self.ai.evaluate_calls[-1][1])
        self.assertFalse(self.state()[1]["correct"])
        self.storage.set_reminder_mode(42, "hourly", NOW)
        self.assertFalse(self.storage.due_reminder_users(NOW))
        self.bot.handle_callback(42, "Learner", "leave", "words")
        self.assertEqual([42], [u["chat_id"] for u in self.storage.due_reminder_users(NOW)])

    def test_completed_round_routes_new_phrase_to_actions_not_preserved_lesson(self):
        self.bot.begin_scenario(42, "pharmacy")
        session_id = self.storage.get_user(42)["current_session"]
        self.import_word()
        self.callback("start")
        for _ in range(2):
            self.bot.handle_text(42, "Learner", "apple")
            self.callback("next")
        self.assertEqual("finished", self.state()[1]["phase"])
        self.bot.handle_text(42, "Learner", "I want to buy fruit.")
        self.assertTrue(any(button["callback_data"].startswith("text:check:") for row in self.telegram.messages[-1]["keyboard"] for button in row))
        self.assertEqual(0, self.storage.response_count(session_id, "scenario"))

    def test_text_pairs_work_without_ai_and_distinct_russian_words_survive(self):
        self.bot.vocabulary.ai = None
        self.bot.begin_scenario(42, "pharmacy")
        session_id = self.storage.get_user(42)["current_session"]
        self.bot.handle_callback(42, "Learner", "paste", "words:paste")
        self.bot.handle_text(42, "Learner", "яблоко = apple\nгруша = pear\nrower = bicycle")
        row, state = self.state()
        self.assertEqual(3, len(json.loads(row["words_json"])))
        self.assertEqual("confirm", state["phase"])
        self.assertIsNone(self.storage.get_user(42)["vocabulary_input_mode"])
        self.assertFalse(self.ai.text_calls)
        self.assertEqual(0, self.storage.response_count(session_id, "scenario"))

    def test_english_list_uses_text_import_and_slash_payload_is_supported(self):
        self.bot.handle_text(42, "Learner", "/words apple")
        self.assertEqual("apple", self.ai.text_calls[0][0])
        self.assertEqual("confirm", self.state()[1]["phase"])
        self.assertIn("яблоко → apple", self.telegram.messages[-1]["text"])
        self.assertEqual("pl", self.storage.get_user(42)["target_language"])

    def test_input_can_be_cancelled_and_invalid_or_failed_import_can_be_retried(self):
        self.bot.handle_callback(42, "Learner", "paste", "words:paste")
        messages = len(self.telegram.messages)
        self.bot.send_scheduled_reminder(42, "hourly")
        self.assertEqual(messages, len(self.telegram.messages))
        self.bot.handle_text(42, "Learner", "apple\n" * 41)
        self.assertFalse(self.storage.vocabulary_decks(42))
        self.assertEqual("list", self.storage.get_user(42)["vocabulary_input_mode"])
        self.ai.error = True
        self.bot.handle_text(42, "Learner", "apple")
        self.assertEqual("list", self.storage.get_user(42)["vocabulary_input_mode"])
        self.bot.handle_text(42, "Learner", "📚 Учиться")
        self.assertIsNone(self.storage.get_user(42)["vocabulary_input_mode"])
        self.bot.handle_text(42, "Learner", "ordinary phrase")
        self.assertEqual(1, len(self.ai.text_calls))

    def test_exam_shows_no_answers_or_hints_until_end_and_mistakes_can_be_retried(self):
        self.bot.handle_text(42, "Learner", "/words яблоко = apple\nгруша = pear")
        self.callback("exam")
        self.assertNotIn("apple", self.telegram.messages[-1]["text"])
        self.assertNotIn("pear", self.telegram.messages[-1]["text"])
        callbacks = [b["callback_data"] for row in self.telegram.messages[-1]["keyboard"] for b in row]
        self.assertFalse(any(":hint:" in value or ":reveal:" in value for value in callbacks))
        self.callback("hint")  # A forged callback cannot reveal an exam answer.
        self.assertFalse(self.state()[1]["helped"])
        self.bot.handle_text(42, "Learner", "wrong")
        self.assertEqual("recall", self.state()[1]["phase"])
        self.assertNotIn("apple", self.telegram.messages[-1]["text"])
        self.assertNotIn("pear", self.telegram.messages[-1]["text"])
        row, state = self.state()
        current = state["queue"][state["position"]]["index"]
        answer = json.loads(row["words_json"])[current]["english"]
        self.bot.handle_text(42, "Learner", answer)
        self.assertIn("1/2", self.telegram.messages[-1]["text"])
        self.assertEqual("finished", self.state()[1]["phase"])
        self.assertTrue(all(s["streak"] == 0 for s in self.state()[1]["stats"]))
        self.callback("mistakes")
        self.assertEqual("revision", self.state()[1]["mode"])
        self.assertEqual(1, len(self.state()[1]["queue"]))

    def test_unfinished_test_resumes_and_keeps_result_after_navigation(self):
        self.bot.handle_text(42, "Learner", "/words яблоко = apple\nгруша = pear")
        self.callback("exam")
        self.bot.handle_text(42, "Learner", "wrong")
        row, state = self.state()
        self.assertEqual(1, state["position"])
        self.bot.handle_text(42, "Learner", "/words")
        self.bot.handle_callback(42, "Learner", "open", f"words:open:{row['id']}")
        self.assertEqual(1, self.state()[1]["position"])
        self.assertEqual(1, len(self.state()[1]["exam_results"]))


class VocabularyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.words = [VocabularyWord.from_dict(WORD)]

    def run_round(self, state, now, correct=True):
        self.assertTrue(start_round(self.words, state, now))
        while state["phase"] == "recall":
            record_answer(state, DrillEvaluation(correct, float(correct), "", "apple"))
            advance(state, now)

    def test_three_spaced_successes_master_without_immediate_repetition(self):
        state = initial_state(1)
        self.run_round(state, NOW)
        self.assertEqual((NOW + timedelta(days=1)).isoformat(), state["stats"][0]["due"])
        self.assertFalse(start_round(self.words, state, NOW))
        self.run_round(state, NOW + timedelta(days=1))
        self.assertFalse(state["stats"][0]["mastered"])
        self.run_round(state, NOW + timedelta(days=4))
        self.assertTrue(state["stats"][0]["mastered"])
        self.assertFalse(start_round(self.words, state, NOW + timedelta(days=100)))

    def test_errors_repeat_at_end_but_cannot_loop_forever(self):
        state = initial_state(1)
        self.run_round(state, NOW, correct=False)
        self.assertEqual(4, len(state["queue"]))
        self.assertEqual(0, state["stats"][0]["streak"])
        self.assertEqual((NOW + timedelta(minutes=10)).isoformat(), state["stats"][0]["due"])

    def test_help_prevents_mastery_credit(self):
        state = initial_state(1)
        start_round(self.words, state, NOW)
        state["helped"] = True
        record_answer(state, DrillEvaluation(True, 1, "", "apple"))
        self.assertFalse(state["correct"])
        self.assertTrue(state["queue"][-1]["retry"])

    def test_duplicate_and_answer_leaks_are_cleaned_without_losing_entries(self):
        word = {**WORD, "hint": "Try an apple", "example_gap": "Apple is an ___"}
        result = VocabularyList.from_dict({"words": [word, word]})
        self.assertEqual(1, len(result.words))
        self.assertEqual("", result.words[0].hint)
        self.assertEqual("", result.words[0].example_gap)
        with self.assertRaises(ValueError):
            VocabularyList.from_dict({"words": []})
        with self.assertRaises(ValueError):
            VocabularyWord.from_dict({**WORD, "source_language": "en"})

    def test_text_formats_and_reverse_russian_pairs(self):
        self.assertEqual(["apple", "look after", "pear"], text_entries("1. apple, look after;\n• pear"))
        result = parse_text_pairs("apple = яблоко; груша = pear; rower = bicycle", "auto")
        self.assertEqual(["apple", "pear", "bicycle"], [w.english for w in result.words])
        self.assertIsNone(parse_text_pairs("apple, look after", "auto"))
        self.assertEqual("ru", parse_text_pairs("яблоко = apple", "pl").words[0].source_language)

    def test_exam_covers_mastered_and_future_words_and_never_changes_mastery(self):
        state = initial_state(1)
        state["stats"][0].update(streak=3, mastered=True, due=(NOW + timedelta(days=7)).isoformat())
        expected = json.loads(json.dumps(state["stats"]))
        start_exam(self.words, state)
        record_exam_answer(self.words, state, "aple")
        self.assertEqual(0, state["last_exam"]["correct"])
        self.assertEqual(expected, state["stats"])
        self.assertTrue(start_revision(self.words, state))
        while state["phase"] == "recall":
            record_answer(state, DrillEvaluation(True, 1, "", "apple"))
            advance(state, NOW)
        self.assertEqual(expected, state["stats"])

    def test_exam_checks_target_spelling_instead_of_synonyms_or_ai(self):
        state = initial_state(1)
        start_exam(self.words, state)
        record_exam_answer(self.words, state, "APPLE")
        self.assertEqual(1, state["last_exam"]["correct"])
        start_exam(self.words, state)
        record_exam_answer(self.words, state, "an apple")
        self.assertEqual(0, state["last_exam"]["correct"])


class VocabularyAdapterTests(unittest.TestCase):
    def test_text_adapters_keep_phrases_and_reject_an_incomplete_school_list(self):
        for client_type in (OpenAIClient, GeminiClient):
            requests = []
            complete = {"words": [WORD, {**WORD, "source": "присматривать", "english": "look after",
                                         "accepted_answers": [], "example_gap": "I ___ my sister."}], "warnings": ""}
            def opener(request, timeout):
                requests.append(json.loads(request.data))
                raw = json.dumps(complete)
                envelope = {"output": [{"content": [{"type": "output_text", "text": raw}]}]} if client_type is OpenAIClient else {"candidates": [{"content": {"parts": [{"text": raw}]}}]}
                return io.BytesIO(json.dumps(envelope).encode())
            client = client_type("fake-key", opener=opener)
            result = client.prepare_text_vocabulary("apple, look after", "auto", "ru")
            self.assertEqual(["apple", "look after"], [word.english for word in result.words])
            payload = requests[0]
            prompt = payload["input"] if client_type is OpenAIClient else payload["contents"][0]["parts"][0]["text"]
            self.assertEqual(["apple", "look after"], json.loads(prompt)["entries"])
            complete["words"] = [WORD]
            with self.assertRaises(AIError):
                client.prepare_text_vocabulary("apple, look after", "auto", "ru")
        primary, fallback = PhotoAI(), PhotoAI()
        primary.error = True
        with self.assertLogs("tutorlaing.ai", level="WARNING"):
            result = FailoverAIClient(primary, fallback).prepare_text_vocabulary("apple", "auto", "ru")
        self.assertEqual("apple", result.words[0].english)
        self.assertEqual(primary.text_calls, fallback.text_calls)

    def test_both_providers_send_images_and_failover_preserves_image(self):
        for client_type in (OpenAIClient, GeminiClient):
            requests = []
            def opener(request, timeout):
                requests.append(json.loads(request.data))
                text = json.dumps({"words": [WORD], "warnings": ""})
                envelope = {"output": [{"content": [{"type": "output_text", "text": text}]}]} if client_type is OpenAIClient else {"candidates": [{"content": {"parts": [{"text": text}]}}]}
                return io.BytesIO(json.dumps(envelope).encode())
            client = client_type("fake-key", opener=opener)
            result = client.extract_vocabulary(b"image", "image/png", "auto", "ru")
            self.assertEqual("apple", result.words[0].english)
            payload = requests[0]
            if client_type is OpenAIClient:
                self.assertFalse(payload["store"])
                self.assertEqual("data:image/png;base64,aW1hZ2U=", payload["input"][0]["content"][1]["image_url"])
            else:
                self.assertEqual({"mimeType": "image/png", "data": "aW1hZ2U="}, payload["contents"][0]["parts"][1]["inlineData"])
        primary, fallback = PhotoAI(), PhotoAI()
        primary.error = True
        result = FailoverAIClient(primary, fallback).extract_vocabulary(b"image", "image/jpeg", "pl", "ru")
        self.assertEqual("apple", result.words[0].english)
        self.assertEqual(primary.extract_calls, fallback.extract_calls)

    def test_download_is_bounded_and_error_contains_no_secret_url(self):
        telegram = TelegramAPI("test-secret")
        telegram.call = lambda *_args, **_kwargs: {"file_path": "photos/p.jpg", "file_size": 3}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"12345")):
            with self.assertRaises(TelegramError):
                telegram.download_image("file", 4)
        with patch("urllib.request.urlopen", side_effect=OSError("https://bot/test-secret")):
            with self.assertRaises(TelegramError) as error:
                telegram.download_image("file", 4)
            self.assertNotIn("test-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
