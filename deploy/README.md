# Deployment

Актуально на 2026-10-06. GitHub публикует image в GHCR; VM только скачивает
его и хранит SQLite в persistent volume. Private keys и `.env` в Git не
хранятся.

## Runtime

- Server: `srv-150`
- Runtime: `/home/admin890brain/services/tutorlaing`
- Service: `tutorlaing`
- Local health: `http://127.0.0.1:8080/health`
- Public health: `https://brain.sekond.pl/health`

Секретные маршруты, host-key fingerprint и безопасный PowerShell wrapper:
[ACCESS.md](ACCESS.md). Не копируйте ключ из secret-source в этот репозиторий.

## Update

После публикации нужного sha-image подключитесь через
`deploy/connect-srv150.ps1`, затем на VM:

```sh
set -eu
cd /home/admin890brain/services/tutorlaing
docker compose pull tutorlaing
docker compose up -d --no-deps tutorlaing
docker compose ps tutorlaing
curl -fsS http://127.0.0.1:8080/health
docker compose logs --tail 80 tutorlaing
```

Deploy считается подтверждённым только после remote image digest, local health
и проверки логов. Один public health не доказывает, что на VM работает новая
версия. Последняя подтверждённая версия: `sha-eb56f03`
(`sha256:76678ecb47dfd26a9585f9e7a5e379172f0e3e0291f746b574637933cf98da23`).
На VM подтверждены SSH, remote digest, local health и startup-логи; публичный
`/games` вернул HTTP 200, а API без Telegram `initData` — 403.
Для словарного режима 2026-10-06 подтверждены регистрация `/words`, schema,
`quick_check=ok`, webhook без очереди и живой OpenAI-import трёх слов без подмены.
Перед обновлением сделан согласованный SQLite backup в `/data/backups`.
В `63cf904` на сервере также проверены кнопка экзамена и её переход к текст/фото.
В `f57644a` подтверждены миграция буфера, живой AI-import всех 55 слов и изолированный
импорт 137 пар из трёх сообщений с полным экзаменом. CI и GHCR успешны; local/public
health ответили `status=ok`, `database=ok`.
Живой OCR синтетического двухколоночного PNG сохранил все 55 слов и их написание.
Backup перед обновлением: `/data/backups/tutorlaing-before-f57644a-20261006T201145Z.sqlite3`,
`quick_check=ok`. Пользовательские данные smoke-тесты не изменяли.

В `eb56f03` подтверждены CI (232 теста), GHCR, container label/digest, healthy
и local/public health. Изолированный server smoke подтвердил auto-advance,
исправление до следующего задания, assisted/retry/mastery, отсутствие раскрытия
следующего ответа в тесте и продолжение старого feedback без кнопки Next.
Backup: `/data/backups/tutorlaing-before-eb56f03-20261006T205749Z.sqlite3`,
`quick_check=ok`. Проверки не изменяли пользовательские данные и не отправляли
реальные Telegram-сообщения.

## Rollback

Ожидает deploy `sha-f0d1e5a` (случайная практика и инкрементальный word-progress):
CI (240 тестов) и GHCR успешны, но 2026-10-06 SSH через `10.0.0.1` и
`192.168.0.150` истёк по тайм-ауту. Public health прежнего сервиса — ok.
Новый backup и обновление контейнера для этой версии ещё не выполнены;
после восстановления доступа выполнить backup, pull/up и server smoke.

Укажите предыдущий известный sha-tag в runtime `compose.yaml`, затем выполните
тот же `pull`/`up`. Persistent volume не удаляется при rollback.
