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
    start_round, text_entries, resume_progress, progress_totals, word_status,
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
        self.assertEqual(("image/png", "pl", "ru"), self.ai.extract_calls[0][1:4])
        self.assertEqual("pl", self.storage.get_user(42)["target_language"])

    def test_word_answer_has_priority_and_does_not_change_lesson_or_languages(self):
        self.bot.begin_scenario(42, "pharmacy")
        before = dict(self.storage.get_user(42))
        self.import_word()
        self.callback("start")
        self.assertNotIn("apple", self.telegram.messages[-1]["text"])
        self.bot.handle_text(42, "Learner", "apple", message_id=501)
        self.assertEqual([True], self.state()[1]["round_results"]["0"])
        self.assertEqual(1, self.state()[1]["position"])
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
        self.bot.handle_callback(42, "Learner", "edit-ready", "words:process")
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
        self.assertEqual("recall", self.state()[1]["phase"])
        self.assertEqual(1, self.state()[1]["position"])
        self.assertIn("I ate an ___", self.telegram.messages[-1]["text"])

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
        self.bot.handle_text(42, "Learner", "an apple")
        self.assertEqual("an apple", self.ai.evaluate_calls[-1][1])
        self.assertFalse(self.state()[1]["round_results"]["0"][-1])
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
        self.bot.handle_callback(42, "Learner", "ready", "words:process")
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
        self.bot.handle_text(42, "Learner", "a" * 221)
        self.assertFalse(self.storage.vocabulary_decks(42))
        self.assertEqual("list", self.storage.get_user(42)["vocabulary_input_mode"])
        self.ai.error = True
        self.bot.handle_text(42, "Learner", "apple")
        self.bot.handle_callback(42, "Learner", "ready", "words:process")
        self.assertEqual("list", self.storage.get_user(42)["vocabulary_input_mode"])
        self.bot.handle_text(42, "Learner", "📚 Учиться")
        self.assertIsNone(self.storage.get_user(42)["vocabulary_input_mode"])
        self.bot.handle_text(42, "Learner", "ordinary phrase")
        self.assertEqual(1, len(self.ai.text_calls))

    def test_exam_corrects_previous_mistake_but_never_reveals_next_answer(self):
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
        row, state = self.state()
        previous = state["exam_results"][-1]["index"]
        self.assertIn(json.loads(row["words_json"])[previous]["english"], self.telegram.messages[-1]["text"])
        current = state["queue"][state["position"]]["index"]
        answer = json.loads(row["words_json"])[current]["english"]
        self.assertNotIn(answer, self.telegram.messages[-1]["text"])
        self.bot.handle_text(42, "Learner", answer)
        self.assertIn("1/2", self.telegram.messages[-1]["text"])
        self.assertEqual("finished", self.state()[1]["phase"])
        self.assertTrue(all(s["streak"] == 0 for s in self.state()[1]["stats"]))
        self.callback("mistakes")
        self.assertEqual("revision", self.state()[1]["mode"])
        self.assertEqual(1, len(self.state()[1]["queue"]))
        self.bot.handle_text(42, "Learner", json.loads(row["words_json"])[previous]["english"])
        self.assertEqual("finished", self.state()[1]["phase"])
        self.assertTrue(all(s["streak"] == 0 for s in self.state()[1]["stats"]))

    def test_correct_answer_advances_atomically_without_next_button_or_double_skip(self):
        self.import_word()
        self.callback("start")
        row, _ = self.state()
        old_next = f"words:next:{row['id']}:{row['version']}"
        messages = len(self.telegram.messages)
        self.bot.handle_text(42, "Learner", "apple")
        current, state = self.state()
        self.assertEqual(row["version"] + 1, current["version"])
        self.assertEqual(("recall", 1), (state["phase"], state["position"]))
        self.assertEqual(messages + 1, len(self.telegram.messages))
        self.assertIn("✓ Верно.", self.telegram.messages[-1]["text"])
        self.assertIn("I ate an ___", self.telegram.messages[-1]["text"])
        self.assertFalse(any(":next:" in b["callback_data"] for buttons in self.telegram.messages[-1]["keyboard"] for b in buttons))
        self.bot.handle_callback(42, "Learner", "old-next", old_next)
        self.assertEqual(1, self.state()[1]["position"])

    def test_wrong_answer_shows_correction_then_next_and_preserves_failed_word(self):
        self.import_word()
        self.callback("start")
        self.bot.handle_text(42, "Learner", "pear")
        _, state = self.state()
        self.assertEqual(("recall", 1), (state["phase"], state["position"]))
        message = self.telegram.messages[-1]["text"]
        self.assertLess(message.index("Правильный вариант: яблоко → apple"), message.index("I ate an ___"))
        self.assertEqual([False], state["round_results"]["0"])
        self.assertTrue(state["queue"][-1]["retry"])
        self.bot.handle_text(42, "Learner", "apple")
        self.bot.handle_text(42, "Learner", "apple")
        _, state = self.state()
        self.assertEqual("finished", state["phase"])
        self.assertEqual(0, state["stats"][0]["streak"])
        self.assertEqual((NOW + timedelta(minutes=10)).isoformat(), state["stats"][0]["due"])

    def test_helped_correct_answer_advances_but_is_not_independent_recall(self):
        self.import_word()
        self.callback("start")
        self.callback("hint")
        self.bot.handle_text(42, "Learner", "apple")
        _, state = self.state()
        self.assertEqual(1, state["position"])
        self.assertEqual([False], state["round_results"]["0"])
        self.assertTrue(state["queue"][-1]["retry"])
        self.assertIn("Верно, с подсказкой", self.telegram.messages[-1]["text"])
        self.assertFalse(state["helped"])

    def test_pre_upgrade_feedback_resumes_directly_to_next_task_once(self):
        self.import_word()
        self.callback("start")
        row, state = self.state()
        record_answer(state, DrillEvaluation(True, 1, "", "apple"))
        self.storage.update_vocabulary_deck(42, row["id"], row["version"], state)
        self.bot.handle_callback(42, "Learner", "open-old", f"words:open:{row['id']}")
        self.assertEqual(("recall", 1), (self.state()[1]["phase"], self.state()[1]["position"]))
        self.assertIn("I ate an ___", self.telegram.messages[-1]["text"])
        self.bot.handle_callback(42, "Learner", "open-again", f"words:open:{row['id']}")
        self.assertEqual(1, self.state()[1]["position"])

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

    def test_full_list_from_several_messages_is_kept_and_test_contains_every_word(self):
        self.bot.handle_callback(42, "Learner", "paste", "words:paste")
        for start, end in ((0, 55), (55, 100), (100, 137)):
            self.bot.handle_text(42, "Learner", "\n".join(f"слово {i} = word{i}" for i in range(start, end)))
        self.assertEqual(137, len(json.loads(self.storage.get_user(42)["vocabulary_input_entries"])))
        self.assertFalse(self.storage.vocabulary_decks(42))
        self.bot.handle_callback(42, "Learner", "all-ready", "words:process")
        row, _ = self.state()
        words = json.loads(row["words_json"])
        self.assertEqual(137, len(words))
        self.assertEqual("word136", words[-1]["english"])
        self.assertIn("137 слов", self.telegram.messages[-1]["text"])
        self.callback("exam")
        self.assertEqual(set(range(137)), {task["index"] for task in self.state()[1]["queue"]})

    def test_append_text_and_photo_extend_one_draft_without_discarding_prior_words(self):
        self.bot.handle_text(42, "Learner", "/words " + "\n".join(f"слово {i} = word{i}" for i in range(60)))
        deck_id = self.state()[0]["id"]
        self.callback("add")
        self.bot.handle_text(42, "Learner", "\n".join(f"слово {i} = word{i}" for i in range(60, 110)))
        self.bot.handle_callback(42, "Learner", "ready", "words:process")
        self.assertEqual(deck_id, self.state()[0]["id"])
        self.callback("add")
        self.bot.handle_photo(42, "Learner", "more", "image/jpeg")
        self.assertEqual(deck_id, self.state()[0]["id"])
        self.assertEqual(111, len(json.loads(self.state()[0]["words_json"])))
        self.assertEqual(1, len(self.storage.vocabulary_decks(42)))

    def test_pending_parts_survive_restart_and_edit_can_use_multiple_messages(self):
        self.bot.handle_callback(42, "Learner", "paste", "words:paste")
        self.bot.handle_text(42, "Learner", "\n".join(f"слово {i} = word{i}" for i in range(80)))
        self.storage.close()
        self.storage = Storage(self.path)
        self.bot = TutorlaingBot(self.settings, self.storage, self.telegram, self.ai)
        self.bot.handle_text(42, "Learner", "/words")
        self.bot.handle_callback(42, "Learner", "resume-input", "words:input:resume")
        self.bot.handle_callback(42, "Learner", "ready", "words:process")
        self.callback("edit")
        self.bot.handle_text(42, "Learner", "\n".join(f"новое {i} = newword{i}" for i in range(50)))
        self.bot.handle_text(42, "Learner", "\n".join(f"новое {i} = newword{i}" for i in range(50, 95)))
        self.bot.handle_callback(42, "Learner", "edited-ready", "words:process")
        self.assertEqual(95, len(json.loads(self.state()[0]["words_json"])))
        self.assertEqual("confirm", self.state()[1]["phase"])

    def test_cancelled_processing_does_not_overwrite_navigation_or_lose_pending_text(self):
        def cancelled(*args):
            self.bot.handle_text(42, "Learner", "/words")
            args[-1]()  # The batch heartbeat notices cancellation.
            raise AssertionError("Must not finish cancelled import")
        self.ai.prepare_text_vocabulary = cancelled
        self.bot.handle_callback(42, "Learner", "paste", "words:paste")
        self.bot.handle_text(42, "Learner", "apple")
        self.bot.handle_callback(42, "Learner", "ready", "words:process")
        self.assertFalse(self.storage.vocabulary_decks(42))
        self.assertEqual(["apple"], json.loads(self.storage.get_user(42)["vocabulary_input_entries"]))
        self.assertIsNone(self.storage.get_user(42)["vocabulary_input_mode"])

    def test_partial_word_progress_and_shuffled_queue_survive_restart(self):
        self.bot.handle_text(42, "Learner", "/words яблоко = apple\nгруша = pear\nбанан = banana")
        self.callback("start")
        row, state = self.state()
        index = state["queue"][0]["index"]
        answer = json.loads(row["words_json"])[index]["english"]
        queue = state["queue"]
        self.bot.handle_text(42, "Learner", answer)
        row, state = self.state()
        self.assertEqual(1, state["stats"][index]["streak"])
        self.assertEqual(1, state["stats"][index]["attempts"])
        self.assertEqual("recall", state["phase"])
        self.assertIn("закрепление 1/3", self.telegram.messages[-1]["text"])
        self.assertIn("новых 2 · учим 1", self.telegram.messages[-1]["text"])
        self.bot.handle_text(42, "Learner", "/words")
        self.storage.close()
        self.storage = Storage(self.path)
        self.bot = TutorlaingBot(self.settings, self.storage, self.telegram, self.ai)
        self.bot.handle_callback(42, "Learner", "open", f"words:open:{row['id']}")
        _, state = self.state()
        self.assertEqual(queue, state["queue"])
        self.assertEqual(1, state["position"])
        self.assertEqual(1, state["stats"][index]["streak"])

    def test_completed_list_word_status_pages_cover_the_whole_list(self):
        self.bot.handle_text(42, "Learner", "/words " + "\n".join(f"слово {i} = word{i}" for i in range(22)))
        self.callback("start")
        while self.state()[1]["phase"] == "recall":
            row, state = self.state()
            index = state["queue"][state["position"]]["index"]
            self.bot.handle_text(42, "Learner", json.loads(row["words_json"])[index]["english"])
        self.assertEqual(22, progress_totals(self.state()[1])["learning"])
        for page, source in ((1, "слово 10"), (2, "слово 21")):
            row, _ = self.state()
            self.bot.handle_callback(42, "Learner", "page", f"words:page:{row['id']}:{row['version']}:{page}")
            display = (self.telegram.edits or self.telegram.messages)[-1]["text"]
            self.assertIn(source + ": учим · 1/3", display)


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
        result = VocabularyList.from_dict({"words": [WORD, {**WORD, "english": "fruit"}], "warnings": "x" * 1000})
        self.assertEqual(2, len(result.words))
        self.assertEqual(1000, len(result.warnings))
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

    def test_practice_and_revision_shuffle_each_pass_without_losing_tasks(self):
        words = [VocabularyWord.from_dict({**WORD, "source": f"слово {i}", "english": f"word{i}"}) for i in range(5)]
        state = initial_state(5)
        with patch("tutorlaing.vocabulary.random.SystemRandom.shuffle", side_effect=lambda values: values.reverse()) as shuffle:
            start_round(words, state, NOW)
            self.assertEqual([4, 3, 2, 1, 0], [task["index"] for task in state["queue"][:5]])
            self.assertEqual(set(range(5)), {task["index"] for task in state["queue"][5:]})
            self.assertNotEqual(state["queue"][4]["index"], state["queue"][5]["index"])
            self.assertGreaterEqual(shuffle.call_count, 2)
            state["last_exam"] = {"mistakes": [{"index": i} for i in range(5)]}
            original_stats = json.loads(json.dumps(state["stats"]))
            self.assertTrue(start_revision(words, state))
            self.assertEqual([4, 3, 2, 1, 0], [task["index"] for task in state["queue"][:5]])
            self.assertEqual(original_stats, state["stats"])

    def test_word_credit_is_applied_before_the_list_finishes_and_only_once(self):
        words = [VocabularyWord.from_dict({**WORD, "source": f"слово {i}", "english": f"word{i}", "example_gap": ""}) for i in range(3)]
        state = initial_state(3)
        start_round(words, state, NOW)
        first = state["queue"][0]["index"]
        record_answer(state, DrillEvaluation(True, 1, "", ""))
        advance(state, NOW)
        self.assertEqual("recall", state["phase"])
        self.assertEqual(1, state["stats"][first]["streak"])
        self.assertEqual((NOW + timedelta(days=1)).isoformat(), state["stats"][first]["due"])
        self.assertEqual({"new": 2, "learning": 1, "repeat": 0, "mastered": 0}, progress_totals(state))
        advance(state, NOW)  # An already-consumed feedback must not advance twice.
        self.assertEqual(1, state["position"])
        while state["phase"] == "recall":
            record_answer(state, DrillEvaluation(True, 1, "", ""))
            advance(state, NOW)
        self.assertEqual([1, 1, 1], [stats["streak"] for stats in state["stats"]])

    def test_context_word_tracks_attempts_immediately_but_requires_both_checks(self):
        state = initial_state(1)
        start_round(self.words, state, NOW)
        record_answer(state, DrillEvaluation(True, 1, "", "apple"))
        advance(state, NOW)
        self.assertEqual("learning", word_status(state["stats"][0]))
        self.assertEqual((1, 1, 0), tuple(state["stats"][0][key] for key in ("attempts", "correct_answers", "streak")))
        record_answer(state, DrillEvaluation(True, 1, "", "apple"))
        advance(state, NOW)
        self.assertEqual((2, 2, 1), tuple(state["stats"][0][key] for key in ("attempts", "correct_answers", "streak")))

    def test_error_or_help_resets_only_that_word_immediately(self):
        for helped in (False, True):
            words = [VocabularyWord.from_dict({**WORD, "source": f"слово {i}", "english": f"word{i}"}) for i in range(2)]
            state = initial_state(2)
            for stats in state["stats"]:
                stats["streak"] = 2
            start_round(words, state, NOW)
            index = state["queue"][0]["index"]
            other = 1 - index
            state["helped"] = helped
            record_answer(state, DrillEvaluation(helped, float(helped), "", ""))
            advance(state, NOW)
            stats = state["stats"][index]
            self.assertEqual("repeat", word_status(stats))
            self.assertEqual(0, stats["streak"])
            self.assertEqual(1, stats["attempts"])
            self.assertEqual(int(helped), stats["assisted_answers"])
            self.assertEqual((NOW + timedelta(minutes=10)).isoformat(), stats["due"])
            self.assertEqual(2, state["stats"][other]["streak"])

    def test_legacy_partial_round_upgrades_once_and_keeps_visible_question(self):
        state = initial_state(5)
        for key in ("progress_version", "order_version", "round_applied"):
            state.pop(key)
        state["stats"] = [{"streak": 0, "mastered": False, "due": None} for _ in range(5)]
        state.update(phase="recall", mode="practice", position=1,
                     queue=[{"index": i, "kind": "translate", "retry": False} for i in range(5)],
                     round_results={"0": [True]})
        with patch("tutorlaing.vocabulary.random.SystemRandom.shuffle", side_effect=lambda values: values.reverse()):
            self.assertTrue(resume_progress(state, NOW))
        self.assertEqual([0, 1, 4, 3, 2], [task["index"] for task in state["queue"]])
        self.assertEqual(1, state["stats"][0]["streak"])
        self.assertEqual(1, state["stats"][0]["attempts"])
        snapshot = json.loads(json.dumps(state))
        self.assertFalse(resume_progress(state, NOW + timedelta(days=1)))
        self.assertEqual(snapshot, state)

    def test_same_day_repetition_cannot_create_three_mastery_credits(self):
        words = [VocabularyWord.from_dict({**WORD, "example_gap": ""})]
        state = initial_state(1)
        for offset in (0, 10, 20):
            state["stats"][0]["due"] = None  # Even a forced early round cannot farm credits.
            start_round(words, state, NOW + timedelta(minutes=offset))
            record_answer(state, DrillEvaluation(True, 1, "", "apple"))
            advance(state, NOW + timedelta(minutes=offset))
        self.assertEqual(1, state["stats"][0]["streak"])
        self.assertFalse(state["stats"][0]["mastered"])
        self.assertEqual(3, state["stats"][0]["attempts"])


class VocabularyAdapterTests(unittest.TestCase):
    def test_large_text_and_photo_are_batched_without_a_deck_size_limit(self):
        class LargeClient(GeminiClient):
            def __init__(self):
                super().__init__("fake")
                self.ocr_calls = 0
                self.batch_sizes = []

            def _generate(self, _system, prompt, _schema, image=None):
                values = json.loads(prompt)
                if image:
                    self.ocr_calls += 1
                    start = values["already_read"]
                    end = min(start + 50, 123)
                    return {"entries": [f"слово {i} = word{i}" for i in range(start, end)],
                            "has_more": end < 123, "cursor": f"row{end}", "total_entries": 123,
                            "warnings": ""}, 0, {}
                entries = values["entries"]
                self.batch_sizes.append(len(entries))
                words = []
                for entry in entries:
                    if "=" in entry:
                        source, english = [part.strip() for part in entry.split("=", 1)]
                    else:
                        source, english = f"слово {entry[4:]}", entry
                    words.append({**WORD, "source": source, "english": english, "accepted_answers": [], "hint": "", "example_gap": ""})
                return {"words": words, "warnings": ""}, 0, {}
        client = LargeClient()
        result = client.prepare_text_vocabulary("\n".join(f"word{i}" for i in range(123)), "auto", "ru")
        self.assertEqual(123, len(result.words))
        self.assertEqual([20, 20, 20, 20, 20, 20, 3], client.batch_sizes)
        client.batch_sizes = []
        result = client.extract_vocabulary(b"image", "image/png", "auto", "ru")
        self.assertEqual(3, client.ocr_calls)
        self.assertEqual(123, len(result.words))
        self.assertEqual("word122", result.words[-1].english)
        self.assertEqual([20, 20, 20, 20, 20, 20, 3], client.batch_sizes)

    def test_photo_cannot_silently_stop_before_total_or_loop_on_a_repeated_page(self):
        class RepeatedClient(GeminiClient):
            def _generate(self, *args, **kwargs):
                return {"entries": ["яблоко = apple"], "has_more": False,
                        "cursor": "same-row", "total_entries": 80, "warnings": ""}, 0, {}
        with self.assertRaisesRegex(AIError, "repeated"):
            RepeatedClient("fake").extract_vocabulary(b"image", "image/png", "auto", "ru")

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
                payload = requests[-1]
                vision = isinstance(payload.get("input"), list) if client_type is OpenAIClient else len(payload["contents"][0]["parts"]) > 1
                result = {"entries": ["яблоко = apple"], "warnings": "", "has_more": False, "cursor": "end", "total_entries": 1} if vision else {"words": [WORD], "warnings": ""}
                text = json.dumps(result)
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
