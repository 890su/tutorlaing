from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .catalog import ScenarioCatalog
from .contracts import Keyboard, MenuStore
from .difficulty import practice_level
from .i18n import tr
from .language_support import LanguageSupport
from .learner_profile import AGE_BANDS, LIFE_ROLES, LearnerProfileService
from .navigation import back_row, home_row, reply_navigation
from .privacy import CONSENT_VERSION
from .progress_service import ProgressService
from .reminders import next_reminder_at
from .ui import card, progress
from .workspace import TelegramWorkspace


LANGUAGE_LABELS = {
    "ru": "Русский",
    "uk": "Українська",
    "en": "English",
    "pl": "Polski",
}
REMINDER_MODES = ("off", "gentle", "normal", "intensive", "aggressive")
REPLY_KEYBOARD_VERSION = "navigation-v5"


class LearnerMenu:
    """Home, settings, progress and discovery screens."""

    def __init__(
        self,
        store: MenuStore,
        workspace: TelegramWorkspace,
        catalog: ScenarioCatalog,
        language_support: LanguageSupport,
        progress_service: ProgressService,
        learner_profiles: LearnerProfileService,
    ):
        self.store = store
        self.workspace = workspace
        self.catalog = catalog
        self.language_support = language_support
        self.progress_service = progress_service
        self.learner_profiles = learner_profiles

    def home(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        due_count = len(self.store.pending_reviews(chat_id))
        review_label = tr(language, "action.reviews")
        if due_count:
            review_label = f"{review_label} · {due_count}"
        scenarios = self.catalog.for_user(user)
        snapshot = self.progress_service.build(chat_id, scenarios)
        resume = self.resume_action(user)
        if resume:
            primary = [
                {
                    "text": tr(language, "action.continue"),
                    "callback_data": resume,
                }
            ]
            status = tr(language, "home.status.active", due=due_count)
        elif due_count:
            primary = [
                {
                    "text": review_label,
                    "callback_data": "reviews:list",
                }
            ]
            status = tr(language, "home.status.reviews", due=due_count)
        elif snapshot.first_planned_scenario_id and snapshot.planned:
            topic = snapshot.planned[0]
            primary = [
                {
                    "text": tr(language, "action.start_plan_short"),
                    "callback_data": f"scenario:{snapshot.first_planned_scenario_id}",
                }
            ]
            status = tr(language, "home.status.plan", topic=topic)
        else:
            primary = [
                {
                    "text": tr(language, "action.start_situation"),
                    "callback_data": "scenarios:list",
                }
            ]
            status = tr(language, "home.status.empty")

        keyboard: Keyboard = [primary]
        open_activity_count = getattr(
            self.store, "open_activity_count", lambda _chat_id: 0
        )(chat_id)
        if open_activity_count:
            keyboard.append(
                [
                    {
                        "text": tr(language, "action.activities"),
                        "callback_data": "activities:list",
                    }
                ]
            )
        keyboard.extend(
            [
                [
                    {
                        "text": tr(language, "navigation.practice"),
                        "callback_data": "practice",
                    }
                ],
                [
                    {
                        "text": tr(language, "action.progress"),
                        "callback_data": "progress",
                    },
                    {
                        "text": tr(language, "navigation.settings"),
                        "callback_data": "settings",
                    },
                ],
            ]
        )
        self.refresh_navigation(chat_id)
        self.workspace.show(
            chat_id,
            card(
                tr(language, f"home.title.{user['target_language']}"),
                status,
            ),
            keyboard,
            surface="home",
        )

    def refresh_navigation(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        signature = f"{REPLY_KEYBOARD_VERSION}:{language}"
        if str(user["reply_keyboard_version"] or "") == signature:
            return
        self.workspace.set_reply_keyboard(
            chat_id,
            reply_navigation(language),
            tr(language, "navigation.placeholder"),
            tr(language, "navigation.ready"),
        )
        self.store.set_user_state(chat_id, reply_keyboard_version=signature)

    def start(self, chat_id: int, first_name: str = "") -> None:
        user = self.store.ensure_user(chat_id, first_name)
        if self.has_current_consent(user):
            self.home(chat_id)
            return
        self.workspace.show(
            chat_id,
            "Cześć! Я помогу подготовиться к реальным разговорам на новом языке.\n\n"
            "Alpha сохраняет ваши текстовые ответы, результаты и Telegram ID. "
            "Добровольно заполненные цели и жизненный контекст, а также вопросы "
            "преподавателю тоже сохраняются в вашем профиле. "
            "Для персональной проверки учебная реплика и минимальный контекст "
            "отправляются OpenAI или Google Gemini. Имя и Telegram ID в AI не передаются. "
            "Голос не записывается. Все данные можно удалить командой /delete_me.\n\n"
            "Продолжить?",
            [
                [{"text": "✅ Согласен и начать", "callback_data": "consent:accept"}],
                [{"text": "ℹ️ Подробнее", "callback_data": "privacy"}],
            ],
            surface="consent",
        )

    @staticmethod
    def has_current_consent(user: Any) -> bool:
        return bool(user["consent_at"]) and int(user["consent_version"]) >= CONSENT_VERSION

    def show_settings(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        values = {
            key: LANGUAGE_LABELS.get(str(user[key]), str(user[key]))
            for key in ("instruction_language", "translation_language", "target_language")
        }
        reminder_label = tr(language, f"reminder.{user['reminder_mode']}")
        self.workspace.show(
            chat_id,
            card(
                tr(language, "settings.title"),
                tr(
                    language,
                    "settings.main_summary",
                    instruction=values["instruction_language"],
                    translation=values["translation_language"],
                    target=values["target_language"],
                    level=practice_level(user),
                    reminders=reminder_label,
                ),
            ),
            [
                [{"text": tr(language, "settings.languages"), "callback_data": "settings:languages"}],
                [{"text": tr(language, "profile.settings"), "callback_data": "settings:profile"}],
                [{"text": tr(language, "action.reminders"), "callback_data": "reminders"}],
                [{"text": tr(language, "navigation.help"), "callback_data": "help"}],
                [
                    {
                        "text": tr(language, "settings.privacy"),
                        "callback_data": "privacy:settings",
                    }
                ],
                home_row(language),
            ],
            surface="settings",
        )

    def show_learner_profile(self, chat_id: int) -> None:
        language = self._language(chat_id)
        profile = self.learner_profiles.get(chat_id)
        adaptive = tr(
            language,
            "profile.adaptive.on" if profile.adaptive_level_enabled else "profile.adaptive.off",
        )
        body = tr(
            language,
            "profile.summary",
            age=tr(language, f"profile.age.{profile.age_band}"),
            role=tr(language, f"profile.role.{profile.life_role}"),
            weekly=profile.weekly_context or tr(language, "profile.not_set"),
            goal=profile.current_goal or tr(language, "profile.not_set"),
            adaptive=adaptive,
        )
        self.workspace.show(
            chat_id,
            card(tr(language, "profile.title"), body),
            [
                [{"text": tr(language, "profile.age"), "callback_data": "profile:age"}],
                [{"text": tr(language, "profile.role"), "callback_data": "profile:role"}],
                [{"text": tr(language, "profile.weekly"), "callback_data": "profile:input:weekly"}],
                [{"text": tr(language, "profile.goal"), "callback_data": "profile:input:goal"}],
                [{"text": tr(language, "profile.adaptive", value=adaptive), "callback_data": "profile:adaptive:toggle"}],
                back_row(language, "settings", "settings"),
            ],
            surface="learner_profile",
        )

    def show_profile_choices(self, chat_id: int, kind: str) -> None:
        language = self._language(chat_id)
        profile = self.learner_profiles.get(chat_id)
        values = AGE_BANDS if kind == "age" else LIFE_ROLES
        current = profile.age_band if kind == "age" else profile.life_role
        keyboard: Keyboard = [
            [
                {
                    "text": ("✓ " if value == current else "")
                    + tr(language, f"profile.{kind}.{value}"),
                    "callback_data": f"profile:set:{kind}:{value}",
                }
            ]
            for value in values
        ]
        keyboard.append(back_row(language, "settings:profile", "profile"))
        self.workspace.show(
            chat_id,
            tr(language, f"profile.choose_{kind}"),
            keyboard,
            surface=f"profile_{kind}",
        )

    def show_learning_settings(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        values = {
            key: LANGUAGE_LABELS.get(str(user[key]), str(user[key]))
            for key in ("instruction_language", "translation_language", "target_language")
        }
        self.workspace.show(
            chat_id,
            card(
                tr(language, "settings.languages_title"),
                tr(
                    language,
                    "settings.summary",
                    instruction=values["instruction_language"],
                    translation=values["translation_language"],
                    target=values["target_language"],
                    level=practice_level(user),
                ),
            ),
            [
                [{"text": tr(language, "settings.target"), "callback_data": "settings:target"}],
                [{"text": tr(language, "settings.instruction"), "callback_data": "settings:instruction"}],
                [{"text": tr(language, "settings.translation"), "callback_data": "settings:translation"}],
                [{"text": tr(language, "settings.level"), "callback_data": "settings:level"}],
                back_row(language, "settings", "settings"),
            ],
            surface="learning_settings",
        )

    def show_language_choices(self, chat_id: int, kind: str) -> None:
        if kind == "target":
            choices = [("pl", "🇵🇱 Polski"), ("en", "🇬🇧 English")]
            heading = self.text(chat_id, "settings.choose_target")
        else:
            choices = [
                ("ru", "🇷🇺 Русский"),
                ("uk", "🇺🇦 Українська"),
                ("en", "🇬🇧 English"),
                ("pl", "🇵🇱 Polski"),
            ]
            heading = self.text(chat_id, "settings.choose")
        keyboard: Keyboard = [
            [{"text": label, "callback_data": f"settings:set:{kind}:{code}"}]
            for code, label in choices
        ]
        keyboard.append(back_row(self._language(chat_id), "settings:languages", "languages"))
        self.workspace.show(
            chat_id, heading, keyboard, surface="language_settings"
        )

    def show_level_choices(self, chat_id: int) -> None:
        current = str(self.store.get_user(chat_id)["learner_level"])
        keyboard: Keyboard = [
            [
                {
                    "text": ("✓ " if level == current else "") + level,
                    "callback_data": f"settings:set:level:{level}",
                }
                for level in ("A0", "A1", "A2")
            ],
            [
                {
                    "text": ("✓ " if level == current else "") + level,
                    "callback_data": f"settings:set:level:{level}",
                }
                for level in ("B1", "B2", "C1")
            ],
            back_row(self._language(chat_id), "settings:languages", "languages"),
        ]
        self.workspace.show(
            chat_id,
            card(
                self.text(chat_id, "settings.level"),
                self.text(chat_id, "progress.disclaimer"),
            ),
            keyboard,
            surface="level",
        )

    def show_progress(self, chat_id: int) -> None:
        scenarios = self.catalog.for_user(self.store.get_user(chat_id))
        snapshot = self.progress_service.build(chat_id, scenarios)
        language = self._language(chat_id)
        empty = self.text(chat_id, "progress.empty")

        def lines(items: tuple[str, ...]) -> str:
            return "\n".join(f"• {item}" for item in items) if items else empty

        body = (
            f"{self.text(chat_id, 'progress.profile_level', level=snapshot.level)}\n"
            f"{self.text(chat_id, 'progress.level', level=snapshot.practice_level)}\n"
            f"{progress('', len(snapshot.mastered), snapshot.total_scenarios).splitlines()[-1]}\n\n"
            f"{self.text(chat_id, 'progress.mastered')}\n{lines(snapshot.mastered)}\n\n"
            f"{self.text(chat_id, 'progress.focus')}\n{lines(snapshot.focus[:4])}\n\n"
            f"{self.text(chat_id, 'progress.plan')}\n{lines(snapshot.planned)}\n\n"
            f"{self.text(chat_id, 'progress.disclaimer')}"
        )
        keyboard: Keyboard = []
        if snapshot.first_planned_scenario_id:
            keyboard.append(
                [
                    {
                        "text": tr(language, "action.start_plan_short"),
                        "callback_data": f"scenario:{snapshot.first_planned_scenario_id}",
                    }
                ]
            )
        keyboard.extend(
            [
                [{"text": self.text(chat_id, "settings.level"), "callback_data": "settings:level"}],
                [{"text": tr(language, "progress.outcomes"), "callback_data": "outcomes:list"}],
                [{"text": tr(language, "navigation.settings"), "callback_data": "settings"}],
                home_row(self._language(chat_id)),
            ]
        )
        self.workspace.show(
            chat_id,
            card(self.text(chat_id, "progress.title"), body),
            keyboard,
            surface="progress",
        )

    def show_practice_hub(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        language = str(user["instruction_language"])
        due_count = len(self.store.pending_reviews(chat_id))
        review_label = tr(language, "action.reviews")
        if due_count:
            review_label = f"{review_label} · {due_count}"
        keyboard: Keyboard = []
        open_activity_count = getattr(
            self.store, "open_activity_count", lambda _chat_id: 0
        )(chat_id)
        if open_activity_count:
            keyboard.append(
                [
                    {
                        "text": tr(language, "action.activities"),
                        "callback_data": "activities:list",
                    }
                ]
            )
        keyboard.extend(
            [
                [
                    {
                        "text": tr(language, "action.scenarios"),
                        "callback_data": "scenarios:list",
                    },
                    {
                        "text": tr(language, "practice.missions"),
                        "callback_data": "quests:list",
                    },
                ],
                [
                    {
                        "text": tr(language, "background.open"),
                        "callback_data": "background:menu:practice",
                    }
                ],
                [
                    {"text": review_label, "callback_data": "reviews:list"},
                    {
                        "text": tr(language, "practice.focus"),
                        "callback_data": "drill:start",
                    },
                ],
                home_row(language),
            ]
        )
        self.workspace.show(
            chat_id,
            card(
                tr(language, "practice.title"),
                tr(language, "practice.summary"),
            ),
            keyboard,
            surface="practice_hub",
        )

    def show_help(self, chat_id: int) -> None:
        language = self._language(chat_id)
        self.workspace.show(
            chat_id,
            card(
                tr(language, "help.title"),
                tr(language, "help.summary"),
            ),
            [home_row(language)],
            surface="help",
        )

    def show_reminders(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        mode = str(user["reminder_mode"])
        language = str(user["instruction_language"])
        paused = user["reminder_paused_until"]
        pause_text = (
            f"\n{tr(language, 'reminders.paused', until=paused)}" if paused else ""
        )
        next_text = ""
        if user["reminder_next_at"] and mode != "off":
            scheduled = datetime.fromisoformat(str(user["reminder_next_at"]))
            local = scheduled.astimezone(ZoneInfo(str(user["timezone"])))
            next_text = "\n" + tr(
                language,
                "reminders.next",
                date=local.strftime("%d.%m"),
                time=local.strftime("%H:%M"),
            )
        keyboard: Keyboard = []
        for value in REMINDER_MODES:
            label = tr(language, f"reminder.{value}")
            marker = "✓ " if value == mode else ""
            keyboard.append(
                [
                    {
                        "text": marker + label.capitalize(),
                        "callback_data": f"reminder:set:{value}",
                    }
                ]
            )
        keyboard.append(
            [
                {
                    "text": tr(language, "reminders.test"),
                    "callback_data": "reminder:test",
                }
            ]
        )
        if mode != "off":
            keyboard.append(
                [{"text": tr(language, "action.pause"), "callback_data": "reminder:pause"}]
            )
        keyboard.append(back_row(language, "settings", "settings"))
        self.workspace.show(
            chat_id,
            card(
                tr(language, "reminders.title"),
                f"{tr(language, 'reminders.current', mode=tr(language, f'reminder.{mode}'))}\n"
                f"{tr(language, f'reminder.desc.{mode}')}{next_text}{pause_text}\n\n"
                f"{tr(language, 'reminders.quiet')}\n\n"
                f"{tr(language, 'reminders.reengagement_note')}",
            ),
            keyboard,
            surface="reminders",
        )

    def set_reminder_mode(self, chat_id: int, mode: str) -> None:
        user = self.store.get_user(chat_id)
        next_at = next_reminder_at(mode, timezone_name=str(user["timezone"]))
        self.store.set_reminder_mode(chat_id, mode, next_at)
        self.show_reminders(chat_id)

    def continuation_text(self, chat_id: int) -> str:
        mode = str(self.store.get_user(chat_id)["reminder_mode"])
        key = "task.next_manual" if mode == "off" else "task.next_or_wait"
        return self.text(chat_id, key)

    def show_scenarios(self, chat_id: int) -> None:
        user = self.store.get_user(chat_id)
        scenarios = self.catalog.for_user(user)
        language = str(user["instruction_language"])
        keyboard: Keyboard = [
            [
                {
                    "text": (
                        f"{scenario.title_ru} · {scenario.title_pl}"
                        if language == "ru"
                        else scenario.title_pl
                    ),
                    "callback_data": f"scenario:{scenario.id}",
                }
            ]
            for scenario in scenarios.values()
        ]
        open_activity_count = getattr(
            self.store, "open_activity_count", lambda _chat_id: 0
        )
        if open_activity_count(chat_id):
            keyboard.insert(
                0,
                [{"text": tr(language, "action.activities"), "callback_data": "activities:list"}],
            )
        keyboard.append(home_row(language))
        self.workspace.show(
            chat_id,
            tr(language, "task.choose_scenario"),
            keyboard,
            surface="scenario_list",
        )

    def show_privacy(self, chat_id: int, *, back_to_settings: bool = False) -> None:
        language = self._language(chat_id)
        self.workspace.show(
            chat_id,
            tr(language, "privacy.summary"),
            [
                back_row(language, "settings", "settings")
                if back_to_settings
                else home_row(language)
            ],
            surface="privacy",
        )

    def text(self, chat_id: int, key: str, **values: object) -> str:
        language = str(self.store.get_user(chat_id)["instruction_language"])
        return tr(language, key, **values)

    def _language(self, chat_id: int) -> str:
        return str(self.store.get_user(chat_id)["instruction_language"])

    @staticmethod
    def resume_action(user: Any) -> str | None:
        if user["toolkit_input_mode"]:
            return "toolkit:translate:" + str(user["toolkit_input_mode"])
        stage = str(user["stage"])
        if stage in {"scenario", "practice", "review"}:
            return "task:resume"
        if stage == "quest" and user["current_quest"]:
            return "quest:resume"
        if stage == "waiting" and user["pending_assignment"]:
            return "assignment:next"
        if stage == "drill" and user["current_drill"]:
            return "drill:resume"
        return None
