# Decisions

## 2026-08-01 — Telegram-first alpha

Первая вертикаль реализуется в Telegram, потому что готовый бот позволяет
быстрее проверить сценарий → bottleneck → тренировка → повторение. PWA
остаётся следующим интерфейсом после product signal.

## 2026-08-01 — Rule-based before AI

Alpha не использует LLM и speech-to-text. Решения объяснимы, себестоимость
предсказуема, а ошибки можно воспроизвести. AI добавляется только для проблемы,
которую нельзя достаточно хорошо решить курируемыми сценариями и правилами.

Это решение описывает первый bootstrap. После проверки рабочей вертикали
владелец продукта отдельно одобрил AI-анализ как следующий alpha increment;
правила сохраняются как fallback и независимый сигнал.

## 2026-08-01 — GitHub and GHCR are deployment sources

Исходный код хранится в github.com/890su/tutorlaing. GitHub Actions проверяет
тесты и публикует контейнер в GHCR. VM хранит только runtime-конфигурацию и
данные, поэтому Google Drive mirror не является deploy-источником.

## 2026-08-01 — Preserve local Brainless

Локальный проект D:\aibrain\04_projects\brainless остаётся неизменным.
Разрешение на очистку относится к старому runtime на srv-150. SSH, сеть,
Docker и Cloudflare сохраняются как инфраструктурный слой.

## 2026-08-01 — SQLite for alpha

SQLite с WAL достаточно для одного Telegram bot process и малой alpha-когорты.
Переход на PostgreSQL рассматривается при нескольких процессах, существенной
конкурентной нагрузке или необходимости сложной аналитики.

## 2026-08-01 — Production webhook on the existing tunnel

Production использует @brnai_bot и webhook на brain.sekond.pl. Освобождённый
Cloudflare route localhost:5678 направлен в контейнерный порт 8080 без изменения
системного tunnel config. Webhook проверяет Telegram secret header. Polling
остаётся локальным fallback и не используется в production.

## 2026-08-01 — Keep the management bridge

Старые n8n, Google MCP и Twenty CRM runtime/data удалены. Контейнер
n8n-mcp-projects-1 временно сохранён как management SSH bridge, потому что
прямой WireGuard/SSH с Windows нестабилен. Его удаление возможно только после
появления другого проверенного административного канала.

## 2026-08-01 — Gemini-first hybrid analysis

Первый AI provider — Gemini через собственный provider-agnostic gateway;
качественная модель по умолчанию — `gemini-2.5-pro`. Рабочий ключ из защищённой
серверной конфигурации Brainless проверен с production VM, но не переносится в
Git или документацию. AI анализирует каждый содержательный ответ. Rule-based
оценка остаётся обязательным fallback при timeout, rate limit, ошибке provider
или невалидной структуре результата.

## 2026-08-01 — Separate language preferences and on-demand translation

Профиль хранит отдельно язык заданий/объяснений, язык перевода и изучаемый
язык. Перевод задания или комментария показывается только по явному запросу,
чтобы не формировать постоянную зависимость от L1. Первый поддерживаемый
изучаемый язык остаётся польским; отделение target language является
архитектурной границей, а не обещанием готового курса для других языков.
