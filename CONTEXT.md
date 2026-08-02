---
project_id: tutorlaing
project_type: telegram-service
status: alpha
updated: 2026-08-02
---

# Tutorlaing context

Telegram-first адаптивный тренер практического языка для мигранта в Польше;
первая пара target languages — польский и английский. Главная единица ценности — повторно выполненная жизненная задача без
критической подсказки, а не число уроков.

## Current implementation

- Python 3.11+, без runtime-зависимостей.
- Telegram Bot API через production webhook; long polling оставлен локальным
  fallback.
- 8 курируемых бытовых ситуаций в польской и английской версиях.
- Rule-based диагностика коммуникативных смысловых групп.
- Gemini 3.5 Flash анализирует каждый содержательный ответ; доступны
  naturalness/variants, перевод по запросу и grammar drill-down.
- Rule-based диагностика остаётся fallback при отказе или невалидном ответе AI.
- Языковая модель профиля разделяет instruction, translation и target language;
  explanation/translation поддерживают `ru/uk/en/pl`, target — `pl/en`.
- Gemini формирует 5-заданийные контекстные drill packs минимум 3 типов; выбор
  проверяется локально, свободная формулировка — AI с допустимыми вариантами.
- Напоминания opt-in: off/gentle/normal/intensive/aggressive, quiet hours
  `Europe/Warsaw`, пауза на день и защита от повторной доставки. Scheduler
  видит состояния `idle`, `waiting` и `drill`: один tick доставляет ровно одно
  queued assignment или продолжает один drill item.
- Основные Telegram-экраны используют стабильный маршрут
  `СИТУАЦИЯ → ФРАЗА → ЗАКРЕПЛЕНИЕ → ПОВТОР`.
- Home работает как компактный dashboard: один приоритетный action выбирается
  из resume → due review → ближайший план → каталог. Настройки разделены на
  языки/уровень, напоминания и приватность; parent-back называет назначение,
  активное занятие закрывается через подтверждение.
- Основной интерфейс, задания и объяснения следуют instruction language
  (`ru/uk/en/pl`). Inline-переходы живут в редактируемом workspace-message;
  свободный ответ создаёт одну видимую feedback-card под сообщением ученика, а
  scheduled reminder — одно новое сообщение для Telegram notification.
- Профиль хранит рабочий уровень A0–C1. AI может добавить до двух сносок на
  translation language только для target-language материала минимум на два
  уровня сложнее. Экран прогресса показывает закреплённое, фокус и три
  ближайшие ситуации; это не официальный CEFR.
- `/tools` открывает интерактивную мастерскую: AI-карточки по курируемым chunks
  с четырьмя вариантами и `Не помню`, перевод собственной фразы
  translation↔target с natural/formal/informal вариантами и тематический
  drill pack без обязательной предыдущей сессии. Все packs используют общую
  drill state machine и reminder delivery.
- SQLite для пользователей, попыток, повторений, drill sessions, outcomes и событий.
- Health endpoint на порту 8080.
- Docker image публикуется в ghcr.io/890su/tutorlaing.
- Целевой runtime: srv-150, ~/services/tutorlaing.
- Production bot: @brnai_bot.
- Telegram webhook: https://brain.sekond.pl/telegram/webhook.
- Public health: https://brain.sekond.pl/health.
- Production AI key хранится только в защищённом server-side `.env`; Pro-квота
  старого ключа равна нулю, полный smoke-test проходит на gemini-3.5-flash.
- Application разбит на контрактные модули: catalog, workspace, menu,
  language support, progress, response evaluation, AI feedback, reminder и
  Telegram update dispatch. Composition/state orchestration остаётся в app;
  SQLite скрыт за узкими Protocol-портами. Архитектура описана в
  docs/ARCHITECTURE.md и защищена regression test.
- Production drill smoke: 5 заданий 5 типов, 2 active-recall; AI-проверка
  свободного ответа успешна. Локально проходят 62 теста.
- На VM сохранён Brainless MCP как management bridge, потому что прямой SSH с
  операторской машины нестабилен.

## Sources of truth

- README.md — продукт и использование.
- ROADMAP.md — продуктовые gates.
- PLAN.md — фактическая реализация и backlog.
- decisions.md — устойчивые технические и продуктовые решения.
- deploy/README.md — операции и rollback.
- docs/ARCHITECTURE.md — границы модулей, контракты и технический backlog.
- docs/UX_NAVIGATION.md — информационная архитектура и UX-invariants.

## Constraints

- Не хранить секреты в Git или документации.
- Не считать собственное тестирование подтверждением рынка.
- Не открывать beta до ручной проверки польского контента.
- LLM-интеграция одобрена владельцем; rule-based fallback и воспроизводимый
  audit trail обязательны.
- Не изменять и не удалять локальный D:\aibrain\04_projects\brainless.
