---
project_id: tutorlaing
project_type: telegram-service
status: alpha
updated: 2026-08-01
---

# Tutorlaing context

Telegram-first адаптивный тренер польского для русскоязычного мигранта в
Польше. Главная единица ценности — повторно выполненная жизненная задача без
критической подсказки, а не число уроков.

## Current implementation

- Python 3.11+, без runtime-зависимостей.
- Telegram Bot API через long polling.
- 8 курируемых JSON-сценариев.
- Rule-based диагностика коммуникативных смысловых групп.
- Следующий утверждённый increment: Gemini-анализ каждого содержательного
  ответа, naturalness/variants, перевод по запросу и grammar drill-down.
- Языковая модель профиля разделяет instruction, translation и target language;
  первый target остаётся польским.
- SQLite для пользователей, попыток, повторений, outcomes и событий.
- Health endpoint на порту 8080.
- Docker image публикуется в ghcr.io/890su/tutorlaing.
- Целевой runtime: srv-150, ~/services/tutorlaing.
- Production bot: @brnai_bot.
- Telegram webhook: https://brain.sekond.pl/telegram/webhook.
- Public health: https://brain.sekond.pl/health.
- На VM сохранён Brainless MCP как management bridge, потому что прямой SSH с
  операторской машины нестабилен.

## Sources of truth

- README.md — продукт и использование.
- ROADMAP.md — продуктовые gates.
- PLAN.md — фактическая реализация и backlog.
- decisions.md — устойчивые технические и продуктовые решения.
- deploy/README.md — операции и rollback.

## Constraints

- Не хранить секреты в Git или документации.
- Не считать собственное тестирование подтверждением рынка.
- Не открывать beta до ручной проверки польского контента.
- LLM-интеграция одобрена владельцем; rule-based fallback и воспроизводимый
  audit trail обязательны.
- Не изменять и не удалять локальный D:\aibrain\04_projects\brainless.
