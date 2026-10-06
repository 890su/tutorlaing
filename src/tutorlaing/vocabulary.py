"""Photo vocabulary contract and deterministic, resumable recall scheduler.

Images are transient inputs to the AI adapter; only approved word material and
learning state are persisted. The target of this mode is always English.
"""

from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .ai import AIError, DrillEvaluation, DrillItem
from .engine import normalize


VOCABULARY_BATCH_SIZE = 20
OCR_PAGE_SIZE = 50
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def vocabulary_key(text: str) -> str:
    """Preserve Cyrillic and internal spelling; the lesson normalizer is Polish-only."""
    text = unicodedata.normalize("NFC", text.casefold().replace("’", "'"))
    return re.sub(r"\s+", " ", text).strip().strip(".!?")


def text_entries(text: str) -> list[str]:
    if not text.strip():
        raise ValueError("Use a nonempty list")
    entries = [re.sub(r"^(?:\d+[.)]|[-•])\s*", "", line.strip())
               for line in re.split(r"[\n,;]+", text) if line.strip()]
    if not entries or any(not entry or len(entry) > 220 for entry in entries):
        raise ValueError("Use words, phrases or pairs, one entry per line")
    return entries


@dataclass(frozen=True)
class VocabularyWord:
    source: str
    source_language: str
    english: str
    accepted_answers: tuple[str, ...]
    hint: str
    explanation: str
    example_gap: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VocabularyWord:
        def field(name: str, limit: int) -> str:
            value = data.get(name, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"Invalid vocabulary field: {name}")
            return value.strip()

        source, english = field("source", 100), field("english", 100)
        language = field("source_language", 5)
        if not source or not english or language not in {"ru", "pl"}:
            raise ValueError("Words need a Polish/Russian source and English answer")
        answers = data.get("accepted_answers", [])
        if not isinstance(answers, (list, tuple)) or len(answers) > 6:
            raise ValueError("Invalid accepted translations")
        if any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in answers):
            raise ValueError("Invalid accepted translation")
        accepted = tuple(dict.fromkeys((english, *(a.strip() for a in answers))))
        hint, gap = field("hint", 250), field("example_gap", 300)
        # A hint/context may guide recall, but cannot contain an accepted answer.
        def reveals(text: str) -> bool:
            return any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in accepted)

        if reveals(hint):
            hint = ""
        if "___" not in gap or reveals(gap):
            gap = ""
        return cls(source, language, english, accepted, hint, field("explanation", 500), gap)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def drill(self, kind: str) -> DrillItem:
        return DrillItem(
            type="free_recall", skill="photo_vocabulary",
            prompt=self.example_gap if kind == "context" else self.source,
            context=f"Translate {self.source_language} to English. Meaning: {self.source}",
            options=(), correct_answer=self.english,
            accepted_answers=(self.english,) if kind == "context" else self.accepted_answers,
            explanation=self.explanation, hint=self.hint, difficulty=1,
        )


@dataclass(frozen=True)
class VocabularyList:
    words: tuple[VocabularyWord, ...]
    warnings: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VocabularyList:
        items = data.get("words")
        if not isinstance(items, list) or not items:
            raise ValueError("Use a nonempty vocabulary list")
        words: list[VocabularyWord] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid vocabulary entry")
            word = VocabularyWord.from_dict(item)
            key = word.source_language, vocabulary_key(word.source), vocabulary_key(word.english)
            if key not in seen:
                words.append(word)
                seen.add(key)
        warnings = data.get("warnings", "")
        if not isinstance(warnings, str):
            raise ValueError("Invalid OCR warnings")
        return cls(tuple(words), warnings)


class VocabularyAI(Protocol):
    def prepare_text_vocabulary(
        self, text: str, source_language: str, instruction_language: str,
        heartbeat: Callable[[], None] | None = None,
    ) -> VocabularyList: ...

    def extract_vocabulary(
        self, image: bytes, mime_type: str, source_language: str,
        instruction_language: str, heartbeat: Callable[[], None] | None = None,
    ) -> VocabularyList: ...

    def evaluate_drill_answer(
        self, item: DrillItem, response: str, instruction_language: str,
        target_language: str,
    ) -> DrillEvaluation: ...


class VocabularyStore(Protocol):
    def get_user(self, chat_id: int) -> Any: ...
    def set_user_state(self, chat_id: int, **values: Any) -> None: ...
    def append_vocabulary_input(self, chat_id: int, entries: list[str]) -> list[str]: ...
    def create_vocabulary_deck(self, chat_id: int, words: list[dict[str, Any]], warnings: str) -> int: ...
    def vocabulary_deck(self, chat_id: int, deck_id: int) -> Any: ...
    def vocabulary_decks(self, chat_id: int, limit: int = 20, offset: int = 0) -> list[Any]: ...
    def active_vocabulary_deck(self, chat_id: int) -> Any | None: ...
    def activate_vocabulary_deck(self, chat_id: int, deck_id: int) -> Any: ...
    def pause_vocabulary(self, chat_id: int) -> None: ...
    def update_vocabulary_deck(
        self, chat_id: int, deck_id: int, version: int, state: dict[str, Any],
        words: list[dict[str, Any]] | None = None,
    ) -> bool: ...


def initial_state(count: int, warnings: str = "") -> dict[str, Any]:
    return {"phase": "confirm", "warnings": warnings, "queue": [], "position": 0,
            "stats": [{"streak": 0, "due": None, "mastered": False} for _ in range(count)],
            "helped": False, "feedback": "", "correct": False}


def start_round(words: list[VocabularyWord], state: dict[str, Any], now: datetime) -> bool:
    queue = []
    for index, stats in enumerate(state["stats"]):
        if stats["mastered"] or (stats["due"] and datetime.fromisoformat(stats["due"]) > now):
            continue
        queue.append({"index": index, "kind": "translate", "retry": False})
        if words[index].example_gap:
            queue.append({"index": index, "kind": "context", "retry": False})
    if not queue:
        return False
    # First translate all words, then retrieve them again in sentence context.
    queue.sort(key=lambda task: task["kind"] == "context")
    state.update(phase="recall", mode="practice", queue=queue, position=0, helped=False,
                 round_results={}, feedback="", correct=False)
    return True


def record_answer(state: dict[str, Any], evaluation: DrillEvaluation) -> None:
    task = state["queue"][state["position"]]
    correct = evaluation.correct and evaluation.score >= 0.75 and not state["helped"]
    key = str(task["index"])
    results = state["round_results"].setdefault(key, [])
    results.append(correct)
    if not correct and not task["retry"]:
        state["queue"].append({**task, "retry": True})
    state.update(phase="feedback", feedback=evaluation.feedback[:600], correct=correct)


def advance(state: dict[str, Any], now: datetime) -> None:
    state["position"] += 1
    state.update(helped=False, feedback="", correct=False)
    if state["position"] < len(state["queue"]):
        state["phase"] = "recall"
        return
    for key, results in state["round_results"].items():
        if state.get("mode") == "revision":
            continue
        stats = state["stats"][int(key)]
        stats["streak"] = stats["streak"] + 1 if all(results) else 0
        stats["mastered"] = stats["streak"] >= 3
        delay = timedelta(days=(1, 3, 7)[min(stats["streak"] - 1, 2)]) if stats["streak"] else timedelta(minutes=10)
        stats["due"] = None if stats["mastered"] else (now + delay).isoformat()
    state["phase"] = "finished"


def parse_edit(text: str, existing: list[VocabularyWord], source_language: str) -> list[VocabularyWord]:
    """Replace a draft from one `source = English` pair per line, without AI."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Use a nonempty list of pairs")
    lookup = {vocabulary_key(word.source): word for word in existing}
    words = []
    for line in lines:
        if "=" not in line:
            raise ValueError("Expected source = English")
        source, english = [part.strip() for part in line.split("=", 1)]
        old = lookup.get(vocabulary_key(source))
        if old and normalize(old.english) == normalize(english):
            words.append(old)
        else:
            language = old.source_language if old else "ru" if re.search("[а-яё]", source, re.I) else source_language
            if language == "auto":
                language = "ru" if re.search("[а-яё]", source, re.I) else "pl"
            words.append(VocabularyWord.from_dict({"source": source, "english": english, "source_language": language}))
    return list(VocabularyList.from_dict({"words": [w.to_dict() for w in words]}).words)


def parse_text_pairs(text: str, source_language: str) -> VocabularyList | None:
    """Explicit `source = English` pairs work offline. Other lists need translation."""
    entries = text_entries(text)
    if not all("=" in entry for entry in entries):
        return None
    pairs = []
    for entry in entries:
        left, right = [part.strip() for part in entry.split("=", 1)]
        # Also accept common English = Russian pasted school lists.
        if re.search("[а-яё]", right, re.I) and not re.search("[а-яё]", left, re.I):
            left, right = right, left
        pairs.append(f"{left} = {right}")
    return VocabularyList(tuple(parse_edit("\n".join(pairs), [], source_language)))


def start_exam(words: list[VocabularyWord], state: dict[str, Any]) -> None:
    queue = [{"index": index, "kind": "translate", "retry": False} for index in range(len(words))]
    random.SystemRandom().shuffle(queue)
    state.update(phase="recall", mode="exam", queue=queue, position=0,
                 helped=False, feedback="", correct=False, exam_results=[])


def record_exam_answer(words: list[VocabularyWord], state: dict[str, Any], response: str) -> None:
    index = state["queue"][state["position"]]["index"]
    # School vocabulary tests require the supplied English spelling; a synonym
    # or a generous AI semantic score must not mask a misspelt target word.
    correct = vocabulary_key(response) == vocabulary_key(words[index].english)
    state["exam_results"].append({"index": index, "answer": response[:500], "correct": correct})
    state["position"] += 1
    if state["position"] == len(state["queue"]):
        state["phase"] = "finished"
        state["last_exam"] = {"total": len(state["queue"]),
                              "correct": sum(result["correct"] for result in state["exam_results"]),
                              "mistakes": [result for result in state["exam_results"] if not result["correct"]]}


def start_revision(words: list[VocabularyWord], state: dict[str, Any]) -> bool:
    mistakes = state.get("last_exam", {}).get("mistakes", [])
    if not mistakes:
        return False
    queue = [{"index": result["index"], "kind": "translate", "retry": False} for result in mistakes]
    queue += [{**task, "kind": "context"} for task in queue if words[task["index"]].example_gap]
    state.update(phase="recall", mode="revision", queue=queue, position=0,
                 helped=False, round_results={}, feedback="", correct=False)
    return True


def evaluate(
    ai: VocabularyAI | None, word: VocabularyWord, kind: str, response: str,
    instruction_language: str,
) -> DrillEvaluation:
    accepted = (word.english,) if kind == "context" else word.accepted_answers
    if normalize(response) in {normalize(answer) for answer in accepted}:
        return DrillEvaluation(True, 1.0, "", word.english)
    if ai:
        try:
            return ai.evaluate_drill_answer(word.drill(kind), response, instruction_language, "en")
        except AIError:
            pass
    return DrillEvaluation(False, 0.0, "vocabulary_ai_unavailable", word.english)
