"""Telegram presentation/use case for imported vocabulary, isolated from lessons."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ai import AIError
from .contracts import TelegramGateway, TransportError
from .i18n import tr
from .ui import card
from .vocabulary import (
    MAX_IMAGE_BYTES, VocabularyAI, VocabularyList, VocabularyStore, VocabularyWord,
    advance, evaluate, initial_state, parse_edit, parse_text_pairs, record_answer,
    record_exam_answer, start_exam, start_revision, start_round, text_entries,
)
from .workspace import TelegramWorkspace


class VocabularyFlow:
    def __init__(
        self, store: VocabularyStore, telegram: TelegramGateway,
        workspace: TelegramWorkspace, ai: VocabularyAI | None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store, self.telegram, self.workspace, self.ai = store, telegram, workspace, ai
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def text(self, chat_id: int, key: str, **values: Any) -> str:
        return tr(str(self.store.get_user(chat_id)["instruction_language"]), key, **values)

    @staticmethod
    def decode(row: Any) -> tuple[list[VocabularyWord], dict[str, Any]]:
        return ([VocabularyWord.from_dict(word) for word in json.loads(row["words_json"])],
                json.loads(row["state_json"]))

    def button(self, chat_id: int, key: str, data: str) -> dict[str, str]:
        return {"text": self.text(chat_id, key), "callback_data": data}

    def show_menu(self, chat_id: int, page: int = 0) -> None:
        self.store.pause_vocabulary(chat_id)
        keyboard = [[self.button(chat_id, "words.paste", "words:paste")],
                    [self.button(chat_id, "words.upload", "words:upload")]]
        now = self.clock()
        page = max(0, page)
        rows = self.store.vocabulary_decks(chat_id, limit=11, offset=page * 10)
        for row in rows[:10]:
            words, state = self.decode(row)
            mastered = sum(stats["mastered"] for stats in state["stats"])
            due = sum(not stats["mastered"] and (not stats["due"] or datetime.fromisoformat(stats["due"]) <= now) for stats in state["stats"])
            if state["phase"] in {"confirm", "edit"}:
                status_key = "words.draft"
            elif state["phase"] in {"recall", "feedback"}:
                status_key = "words.continue"
            elif mastered == len(words):
                status_key = "words.all_mastered"
            else:
                status_key = "words.wait"
            status = self.text(chat_id, status_key)
            if state["phase"] == "finished" and due:
                status = self.text(chat_id, "words.due", count=due)
            label = self.text(chat_id, "words.saved", id=row["id"], name=words[0].source[:20], status=status, mastered=mastered, total=len(words))
            keyboard.append([{"text": label, "callback_data": f"words:open:{row['id']}"}])
        if page:
            keyboard.append([self.button(chat_id, "action.back", f"words:list:{page - 1}")])
        if len(rows) > 10:
            keyboard.append([self.button(chat_id, "words.next_page", f"words:list:{page + 1}")])
        keyboard.append([self.button(chat_id, "action.back", "practice")])
        self.workspace.show(chat_id, card(self.text(chat_id, "words.title"), self.text(chat_id, "words.intro")), keyboard, surface="vocabulary_menu")

    def show_upload(self, chat_id: int) -> None:
        self.store.pause_vocabulary(chat_id)
        source = str(self.store.get_user(chat_id)["vocabulary_source_language"])
        keyboard = [[{"text": ("✓ " if lang == source else "") + (self.text(chat_id, "words.auto") if lang == "auto" else "Polski" if lang == "pl" else "Русский"), "callback_data": f"words:source:{lang}"}] for lang in ("auto", "pl", "ru")]
        keyboard.append([self.button(chat_id, "action.back", "words")])
        self.workspace.show(chat_id, card(self.text(chat_id, "words.title"), self.text(chat_id, "words.intro")), keyboard, surface="vocabulary_upload")

    def import_photo(self, chat_id: int, file_id: str, mime_type: str, size: int) -> None:
        if size > MAX_IMAGE_BYTES:
            self.telegram.send_message(chat_id, self.text(chat_id, "words.size"))
            return
        if self.ai is None:
            self.telegram.send_message(chat_id, self.text(chat_id, "words.no_ai"))
            return
        self.telegram.send_chat_action(chat_id)
        user = self.store.get_user(chat_id)
        try:
            image = self.telegram.download_image(file_id, MAX_IMAGE_BYTES)
            result = self.ai.extract_vocabulary(image, mime_type, str(user["vocabulary_source_language"]), str(user["instruction_language"]))
        except (AIError, TransportError, ValueError):
            self.telegram.send_message(chat_id, self.text(chat_id, "words.error"), [[self.button(chat_id, "words.title", "words")]])
            return
        self.save_import(chat_id, result)

    def ask_text(self, chat_id: int) -> None:
        self.store.pause_vocabulary(chat_id)
        self.store.set_user_state(chat_id, vocabulary_input_mode="list")
        self.workspace.show(
            chat_id, card(self.text(chat_id, "words.title"), self.text(chat_id, "words.paste_prompt")),
            [[self.button(chat_id, "action.cancel", "words")]], surface="vocabulary_text_input",
        )

    def import_text(self, chat_id: int, text: str) -> None:
        user = self.store.get_user(chat_id)
        try:
            text_entries(text)
            result = parse_text_pairs(text, str(user["vocabulary_source_language"]))
            if result is None:
                if self.ai is None:
                    self.telegram.send_message(chat_id, self.text(chat_id, "words.text_no_ai"))
                    return
                self.telegram.send_chat_action(chat_id)
                result = self.ai.prepare_text_vocabulary(text, str(user["vocabulary_source_language"]), str(user["instruction_language"]))
        except ValueError:
            self.telegram.send_message(chat_id, self.text(chat_id, "words.text_invalid"))
            return
        except AIError:
            self.telegram.send_message(chat_id, self.text(chat_id, "words.text_error"))
            return
        self.save_import(chat_id, result)

    def save_import(self, chat_id: int, result: VocabularyList) -> None:
        self.store.set_user_state(chat_id, vocabulary_input_mode=None)
        deck_id = self.store.create_vocabulary_deck(chat_id, [w.to_dict() for w in result.words], result.warnings)
        self.workspace.start_new_surface(chat_id)
        self.show_deck(chat_id, self.store.vocabulary_deck(chat_id, deck_id))

    def show_deck(self, chat_id: int, row: Any, page: int = 0, *, force_new: bool = False) -> None:
        words, state = self.decode(row)
        prefix = f"{row['id']}:{row['version']}"
        phase = state["phase"]
        keyboard = []
        if phase == "confirm":
            pages = (len(words) + 9) // 10
            page = max(0, min(page, pages - 1))
            pairs = "\n".join(f"{i + 1}. {word.source} → {word.english}" for i, word in enumerate(words[page * 10:page * 10 + 10], page * 10))
            body = self.text(chat_id, "words.preview", count=len(words), page=page + 1, pages=pages, pairs=pairs, warnings=state["warnings"])
            keyboard = [[self.button(chat_id, "words.confirm", f"words:start:{prefix}")],
                        [self.button(chat_id, "words.exam", f"words:exam:{prefix}")],
                        [self.button(chat_id, "words.edit", f"words:edit:{prefix}")]]
            if pages > 1:
                keyboard.append([self.button(chat_id, "words.next_page", f"words:page:{prefix}:{(page + 1) % pages}")])
        elif phase == "edit":
            body = self.text(chat_id, "words.edit_prompt")
            keyboard = [[self.button(chat_id, "action.cancel", f"words:confirm:{prefix}")]]
        elif phase in {"recall", "feedback"}:
            task = state["queue"][state["position"]]
            word = words[task["index"]]
            if phase == "recall":
                body = self.text(chat_id, "words.context" if task["kind"] == "context" else "words.translate", source=word.source, gap=word.example_gap)
                body += "\n\n" + self.text(chat_id, "words.position", position=state["position"] + 1, total=len(state["queue"]))
                if state.get("mode") == "exam":
                    body += "\n\n" + self.text(chat_id, "words.exam_note")
                else:
                    keyboard = [[self.button(chat_id, "action.hint", f"words:hint:{prefix}"), self.button(chat_id, "words.reveal", f"words:reveal:{prefix}")]]
            else:
                body = self.text(chat_id, "words.result", source=word.source, answer=word.english, feedback=state["feedback"], explanation=word.explanation)
                keyboard = [[self.button(chat_id, "action.next", f"words:next:{prefix}")]]
        else:
            dates = [datetime.fromisoformat(stats["due"]) for stats in state["stats"] if stats["due"] and not stats["mastered"]]
            zone = ZoneInfo(str(self.store.get_user(chat_id)["timezone"]))
            due = min(dates).astimezone(zone).strftime("%d.%m %H:%M") if dates else self.text(chat_id, "words.all_mastered")
            body = self.text(chat_id, "words.finished", mastered=sum(s["mastered"] for s in state["stats"]), total=len(words), due=due)
            if state.get("mode") == "exam":
                exam = state["last_exam"]
                body = self.text(chat_id, "words.exam_result", correct=exam["correct"], total=exam["total"], percent=round(100 * exam["correct"] / exam["total"]))
                mistakes = exam["mistakes"]
                pages = max(1, (len(mistakes) + 4) // 5)
                page = max(0, min(page, pages - 1))
                for mistake in mistakes[page * 5:page * 5 + 5]:
                    word = words[mistake["index"]]
                    answer = mistake["answer"].replace("\n", " ")[:80]
                    body += "\n\n" + self.text(chat_id, "words.exam_mistake", source=word.source, given=answer, answer=word.english)
                if pages > 1:
                    keyboard.append([self.button(chat_id, "words.next_page", f"words:page:{prefix}:{(page + 1) % pages}")])
            elif state.get("mode") == "revision":
                body = self.text(chat_id, "words.revision_done")
            if state.get("last_exam", {}).get("mistakes"):
                keyboard.append([self.button(chat_id, "words.mistakes", f"words:mistakes:{prefix}")])
            if any(date <= self.clock() for date in dates):
                keyboard.append([self.button(chat_id, "words.review", f"words:start:{prefix}")])
            keyboard.append([self.button(chat_id, "words.exam", f"words:exam:{prefix}")])
        keyboard.append([self.button(chat_id, "words.pause", "words")])
        title = self.text(chat_id, "words.title")
        if phase == "feedback":
            title = self.text(chat_id, "words.correct" if state["correct"] else "words.repeat")
        self.workspace.show(chat_id, card(title, body.strip()), keyboard, force_new=force_new, surface="vocabulary_" + phase)
        if phase == "edit":
            # Plain pairs can be copied into the replacement message; keep each
            # Telegram message below its limit even for long phrase lists.
            chunk = ""
            for word in words:
                line = f"{word.source} = {word.english}\n"
                if len(chunk) + len(line) > 3500:
                    self.telegram.send_message(chat_id, chunk.rstrip())
                    chunk = ""
                chunk += line
            if chunk:
                self.telegram.send_message(chat_id, chunk.rstrip())

    def handle_callback(self, chat_id: int, data: str) -> None:
        if data == "words":
            self.show_menu(chat_id)
            return
        if data == "words:upload":
            self.show_upload(chat_id)
            return
        if data == "words:paste":
            self.ask_text(chat_id)
            return
        if data.startswith("words:source:"):
            source = data.rsplit(":", 1)[1]
            if source in {"auto", "pl", "ru"}:
                self.store.set_user_state(chat_id, vocabulary_source_language=source)
            self.show_upload(chat_id)
            return
        if data.startswith("words:list:"):
            try:
                page = int(data.rsplit(":", 1)[1])
            except ValueError:
                page = 0
            self.show_menu(chat_id, page)
            return
        parts = data.split(":")
        try:
            if len(parts) < 3:
                raise ValueError("Malformed vocabulary callback")
            action, deck_id = parts[1], int(parts[2])
            row = self.store.vocabulary_deck(chat_id, deck_id)
            if action == "open":
                self.store.set_user_state(chat_id, vocabulary_input_mode=None)
                row = self.store.activate_vocabulary_deck(chat_id, deck_id)
                self.show_deck(chat_id, row)
                return
            if len(parts) < 4 or int(parts[3]) != row["version"] or not row["active"]:
                raise ValueError("Stale vocabulary callback")
            words, state = self.decode(row)
            phase = state["phase"]
            if action == "page" and (phase == "confirm" or (phase == "finished" and state.get("mode") == "exam")):
                self.show_deck(chat_id, row, int(parts[4]))
                return
            if action == "start" and phase in {"confirm", "finished"}:
                start_round(words, state, self.clock())
            elif action == "exam" and phase in {"confirm", "finished"}:
                start_exam(words, state)
            elif action == "mistakes" and phase == "finished":
                if not start_revision(words, state):
                    raise ValueError("No mistakes to practise")
            elif action == "edit" and phase == "confirm":
                state["phase"] = "edit"
            elif action == "confirm" and phase == "edit":
                state["phase"] = "confirm"
            elif action == "next" and phase == "feedback":
                advance(state, self.clock())
            elif action in {"hint", "reveal"} and phase == "recall" and state.get("mode") != "exam":
                state["helped"] = True
            else:
                raise ValueError("Invalid vocabulary action")
            if not self.store.update_vocabulary_deck(chat_id, deck_id, row["version"], state):
                raise ValueError("State changed")
            row = self.store.vocabulary_deck(chat_id, deck_id)
            self.show_deck(chat_id, row)
            if action in {"hint", "reveal"}:
                word = words[state["queue"][state["position"]]["index"]]
                text = self.text(chat_id, "words.help" if action == "reveal" else "words.hint_text", hint=word.hint or self.text(chat_id, "words.no_hint"), answer=word.english, explanation=word.explanation)
                self.telegram.send_message(chat_id, text)
        except (KeyError, ValueError, IndexError):
            self.telegram.send_message(chat_id, self.text(chat_id, "words.stale"))
            current = self.store.active_vocabulary_deck(chat_id)
            if current:
                self.show_deck(chat_id, current)
            else:
                self.show_menu(chat_id)

    def handle_text(self, chat_id: int, text: str) -> bool:
        if self.store.get_user(chat_id)["vocabulary_input_mode"] == "list":
            self.import_text(chat_id, text)
            return True
        row = self.store.active_vocabulary_deck(chat_id)
        if row is None:
            return False
        words, state = self.decode(row)
        if state["phase"] == "finished":
            return False
        if state["phase"] == "edit":
            try:
                words = parse_edit(text, words, str(self.store.get_user(chat_id)["vocabulary_source_language"]))
            except ValueError:
                self.telegram.send_message(chat_id, self.text(chat_id, "words.edit_invalid"))
                return True
            if self.store.update_vocabulary_deck(chat_id, row["id"], row["version"], initial_state(len(words)), [w.to_dict() for w in words]):
                self.show_deck(chat_id, self.store.vocabulary_deck(chat_id, row["id"]), force_new=True)
        elif state["phase"] == "recall":
            task = state["queue"][state["position"]]
            word = words[task["index"]]
            if state.get("mode") == "exam":
                record_exam_answer(words, state, text[:500])
            else:
                user = self.store.get_user(chat_id)
                self.telegram.send_chat_action(chat_id)
                result = evaluate(self.ai, word, task["kind"], text[:500], str(user["instruction_language"]))
                record_answer(state, result)
                if state["feedback"] == "vocabulary_ai_unavailable":
                    state["feedback"] = self.text(chat_id, "words.no_check")
            if self.store.update_vocabulary_deck(chat_id, row["id"], row["version"], state):
                self.show_deck(chat_id, self.store.vocabulary_deck(chat_id, row["id"]), force_new=True)
        else:
            self.show_deck(chat_id, row)
        return True
