# Deployment

Актуально на 2026-08-07. GitHub публикует image в GHCR; VM только скачивает
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
версия. Последняя подтверждённая версия: `sha-ac98eb2`.

## Rollback

Укажите предыдущий известный sha-tag в runtime `compose.yaml`, затем выполните
тот же `pull`/`up`. Persistent volume не удаляется при rollback.
