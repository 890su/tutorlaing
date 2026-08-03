---
project_id: tutorlaing
project_type: telegram-service
status: alpha
updated: 2026-08-03
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
- OpenAI GPT-5.6 Sol adapter и Gemini 3.5 Flash failover реализованы. Production
  использует OpenAI основным provider и Gemini резервным.
  Доступны naturalness/variants, перевод по запросу и grammar drill-down.
- Rule-based диагностика остаётся независимым fallback при отказе или
  невалидном ответе обоих AI routes.
- Языковая модель профиля разделяет instruction, translation и target language;
  explanation/translation поддерживают `ru/uk/en/pl`, target — `pl/en`.
- AI формирует 8-заданийные контекстные drill packs минимум 4 типов; выбор
  проверяется локально, свободная формулировка — AI с допустимыми вариантами.
- Напоминания opt-in: off/gentle/normal/intensive/aggressive, quiet hours
  `Europe/Warsaw`, пауза на день и защита от повторной доставки. Scheduler
  видит состояния `idle`, `waiting`, `scenario`, `practice`, `review` и `drill`:
  один tick доставляет ровно одно задание. Ошибка доставки планирует retry через
  пять минут и записывается в audit events; есть ручная проверка из настроек.
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
- Уровень также управляет видимой опорой scenario, ожидаемой самостоятельностью
  ответа, AI evaluation/drill prompts и сложностью локального topic fallback.
- Постоянная нижняя клавиатура содержит только `Учиться`, `Инструменты`,
  `Прогресс`, `Настройки`. Контекстные действия и A/B/C/D остаются inline.
  Полные варианты находятся в тексте сообщения, потому что Telegram не
  переносит длинный текст inline-кнопок.
- Reply-keyboard устанавливается через удаляемое служебное сообщение; короткие
  stale-state notices удаляются автоматически. Неудачная правка workspace
  удаляет устаревшую карточку перед созданием замены.
- `/tools` открывает интерактивную мастерскую: наборы по 10 карточек по курируемым chunks
  с четырьмя вариантами и `Не помню`, перевод собственной фразы
  translation↔target с natural/formal/informal вариантами и тематический
  drill pack без обязательной предыдущей сессии. Все packs используют общую
  drill state machine и reminder delivery. Карточки и topic packs имеют
  локальный curated fallback и доступны без внешнего AI. Карточки в первую
  очередь берут проблемные фразы из истории scenario/drill, остальные
  выбирают случайно; правильные варианты программно перемешиваются.
- Phrase translation является stateless overlay и не заменяет `stage`.
  Карточки, тематический pack и `Мои ошибки` сохраняют основную активность в
  `suspended_activity_json`, работают как временный drill и атомарно
  восстанавливают scenario/review/waiting/drill после complete или stop.
  Scheduler не доставляет напоминание, пока ожидается фраза для перевода.
- SQLite для пользователей, попыток, повторений, drill sessions, outcomes и событий.
- Health endpoint на порту 8080.
- Docker image публикуется в ghcr.io/890su/tutorlaing.
- Целевой runtime: srv-150, ~/services/tutorlaing.
- Production bot: @brnai_bot.
- Telegram webhook: https://brain.sekond.pl/telegram/webhook.
- Public health: https://brain.sekond.pl/health.
- Production OpenAI и Gemini keys хранятся только в защищённом server-side
  `.env` с mode 600. OpenAI route `gpt-5.6-sol` с `reasoning.effort=low`
  включён, реальный translation smoke пройден; consent v3 сообщает об обоих
  processors.
- Application разбит на контрактные модули: catalog, workspace, menu,
  language support, progress, response evaluation, AI feedback, reminder и
  Telegram update dispatch. Composition/state orchestration остаётся в app;
  SQLite скрыт за узкими Protocol-портами. Архитектура описана в
  docs/ARCHITECTURE.md и защищена regression test.
- OpenAI smoke: русско-польский перевод с alternatives и анализ польского ответа
  успешны. Локально проходят 87 тестов, включая hybrid UI, level-aware tasks,
  history-aware cards и reminder retry.
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
