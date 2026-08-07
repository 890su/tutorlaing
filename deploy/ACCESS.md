# Доступ к srv-150

Этот документ хранит только маршрутизацию и ссылки на секреты. Private keys,
пароли, токены и содержимое `.env` в Tutorlaing не копируются.

Проверено 2026-08-07 по инфраструктурной документации и ключам из
`D:\aibrain\04_projects\brainless`.

## Подключение

| Поле | Значение |
|---|---|
| Server ID | `srv-150` |
| Ожидаемый hostname | `890brain` |
| SSH user | `admin890brain` |
| WireGuard | `10.0.0.1:22` |
| LAN | `192.168.0.150:22` |
| Внешний адрес | `78.10.222.36:22` — закрыт или недоступен |
| Runtime path | `/home/admin890brain/services/tutorlaing` |
| Compose service/container | `tutorlaing` |
| Local health | `http://127.0.0.1:8080/health` |
| Public health | `https://brain.sekond.pl/health` |

Secret reference:

```text
server/srv-150/ssh-admin890brain
```

Текущий локальный источник ключа находится вне Tutorlaing:

```text
D:\aibrain\04_projects\brainless\.ssh\id_ed25519
```

Резервный `server_key.bak` имеет тот же публичный fingerprint:

```text
SHA256:3nTHxkZX4jdsRjYxWekVb01RfDZtQOBQ8yhh9n2nA/Q
```

Исходные файлы имеют слишком широкие Windows ACL для прямого использования
OpenSSH. Скрипт `connect-srv150.ps1` создаёт временную копию в `%TEMP%`, снимает
наследование ACL, оставляет чтение текущему Windows user, проверяет fingerprint
и удаляет копию в `finally`.

## Проверка доступа

Из корня Tutorlaing:

```powershell
.\deploy\connect-srv150.ps1 -RemoteCommand "hostname"
```

Если VPN недоступен, но машина находится в той же LAN:

```powershell
.\deploy\connect-srv150.ps1 `
  -ServerHost 192.168.0.150 `
  -RemoteCommand "hostname"
```

Успешный результат должен вернуть `890brain`.

## Deploy exact image

Публикуемый образ для commit `ac98eb2`:

```text
ghcr.io/890su/tutorlaing:sha-ac98eb2
sha256:8d4552bb4b77e21c78fbbc34be8fdf44cdd349e82e4521f6c3a2b7e5d6b194c8
```

Runtime compose сейчас использует `latest`, который указывает на тот же digest.
После восстановления SSH:

```powershell
.\deploy\connect-srv150.ps1 -RemoteCommand @'
set -eu
cd /home/admin890brain/services/tutorlaing
docker compose pull tutorlaing
docker compose up -d --no-deps tutorlaing
docker compose ps tutorlaing
curl -fsS http://127.0.0.1:8080/health
docker compose logs --tail 80 tutorlaing
'@
```

Перед обновлением нужно проверить свободное место. После обновления обязательны
health, отсутствие migration/Telegram ошибок в логах и внешний health.

## Текущее состояние канала

На 2026-08-07:

- public health отвечает HTTP 200;
- `10.0.0.1:22` и `192.168.0.150:22` дают timeout с текущей машины;
- WireGuard interface `890ai_brainless_fp` поднят, client address `10.0.0.4`,
  но handshake не восстанавливается;
- известная причина — нестабильная UDP mapping за CGNAT;
- новый контейнер опубликован в GHCR, но сервер ещё не подтвердил pull/recreate.

Долговременное исправление: Tailscale/NetBird либо Cloudflare Access SSH.
До него public health не является доказательством, что новая версия развёрнута.
