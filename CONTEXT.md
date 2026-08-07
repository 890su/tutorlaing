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
- Постоянная нижняя клавиатура v5 содержит только `Сегодня`, `Мои занятия`,
  `Практика`, `Инструменты`. Контекстные действия и A/B/C/D остаются inline.
  Полные варианты находятся в тексте сообщения, потому что Telegram не
  переносит длинный текст inline-кнопок.
- Reply-keyboard устанавливается через version-gated carrier, который нельзя
  удалять без риска скрыть клавиатуру в Telegram-клиенте; короткие
  stale-state notices удаляются автоматически. Неудачная правка workspace
  удаляет устаревшую карточку перед созданием замены.
- Каноническое командное меню локализовано на ru/uk/en/pl: `/start`,
  `/activities`, `/practice`, `/tools`, `/progress`, `/settings`, `/help`,
  `/grammar`, `/privacy`, `/delete_me`. Неизвестная slash-команда и устаревший
  callback не маршрутизируются как ответ заданию.
- `/tools` открывает интерактивную мастерскую: наборы по 10 карточек по курируемым chunks
  с четырьмя вариантами и `Не помню`, перевод собственной фразы
  translation↔target с natural/formal/informal вариантами и тематический
  drill pack без обязательной предыдущей сессии. Все packs используют общую
  drill state machine и reminder delivery. Карточки и topic packs имеют
  локальный curated fallback и доступны без внешнего AI. Карточки в первую
  очередь берут проблемные фразы из истории scenario/drill, остальные
  выбирают случайно; правильные варианты программно перемешиваются.
- Phrase translation является stateless overlay и не заменяет `stage`.
  Карточки, тематический pack и `Мои ошибки` создают обычный сохраняемый drill,
  не скрывают другую session и после complete/stop предлагают `Мои занятия`.
  Только явно выбранный foreground принимает следующий свободный ответ.
  Scheduler не доставляет напоминание, пока ожидается фраза для перевода.
- SQLite для пользователей, попыток, повторений, drill sessions, outcomes и событий.
- Health endpoint на порту 8080.
- Docker image публикуется в ghcr.io/890su/tutorlaing.
- Подготовленный server runtime: srv-150, ~/services/tutorlaing; текущая ветка
  разработки не является production-релизом и не развёртывается автоматически.
- Ранее использованные технические endpoints: @brnai_bot,
  https://brain.sekond.pl/telegram/webhook и https://brain.sekond.pl/health.
- При серверном запуске OpenAI и Gemini keys хранятся только в защищённом server-side
  `.env` с mode 600. OpenAI route `gpt-5.6-sol` с `reasoning.effort=low`
  включён, реальный translation smoke пройден; consent v4 сообщает об обоих
  processors, добровольном learner context и истории вопросов преподавателю.
- Application разбит на контрактные модули: catalog, workspace, menu,
  language support, progress, response evaluation, AI feedback, reminder и
  Telegram update dispatch. Composition/state orchestration остаётся в app;
  SQLite скрыт за узкими Protocol-портами. Архитектура описана в
  docs/ARCHITECTURE.md и защищена regression test.
- OpenAI smoke: русско-польский перевод с alternatives и анализ польского ответа
  ранее были успешны. Локально проходят 159 тестов, включая hybrid UI, level-aware tasks,
  learner profile, resumable activity projection, teacher side-channel,
  activity-linked background selector 60/25/15, history-aware cards и reminder retry.
- Маршруты `srv-150`, SSH user, secret reference и безопасный ACL-wrapper
  зафиксированы в `deploy/ACCESS.md`. Private key остаётся вне Tutorlaing в
  secret-source проекта Brainless. На 2026-08-07 public health отвечает HTTP
  200, но VPN `10.0.0.1` и LAN `192.168.0.150` дают timeout из-за известной
  нестабильности WireGuard/CGNAT; новый GHCR image ещё не подтверждён на VM.

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
