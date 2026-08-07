# Доступ к srv-150

Актуально на 2026-08-07. Документ хранит только безопасные маршруты и secret
reference; private key, пароли, токены и содержимое `.env` в Tutorlaing не
копируются.

| Поле | Значение |
|---|---|
| Server ID / hostname | `srv-150` / `890brain` |
| SSH user | `admin890brain` |
| WireGuard | `10.0.0.1:22` |
| LAN fallback | `192.168.0.150:22` |
| Runtime | `/home/admin890brain/services/tutorlaing` |
| Service | `tutorlaing` |
| Secret reference | `server/srv-150/ssh-admin890brain` |

## Подключение

Из корня Tutorlaing:

```powershell
.\deploy\connect-srv150.ps1 -RemoteCommand "hostname"
```

Если WireGuard недоступен, но доступна LAN:

```powershell
.\deploy\connect-srv150.ps1 -ServerHost 192.168.0.150 -RemoteCommand "hostname"
```

Скрипт временно копирует key из внешнего secret-source с закрытым Windows ACL,
проверяет ожидаемый fingerprint и удаляет временную копию в `finally`. Не
заменяйте его ручным копированием ключа или отключением host-key verification.

## Состояние

Последняя подтверждённая deploy-версия — `sha-ac98eb2`
(`sha256:8d4552bb4b77…`): Docker был `running healthy`, local/public health
вернули `status=ok`, `database=ok`. Канал WireGuard за CGNAT нестабилен;
следующий deploy подтверждается remote digest, local health и логами, а не одним
публичным health endpoint.
