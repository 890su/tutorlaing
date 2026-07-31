# Deployment

GitHub is the source of truth. The VM only pulls the published container and
stores runtime data in a named Docker volume.

## Target

- Server: srv-150
- Runtime path: ~/services/tutorlaing
- Image: ghcr.io/890su/tutorlaing:latest
- Container: tutorlaing
- Health: http://127.0.0.1:8080/health
- Webhook: https://brain.sekond.pl/telegram/webhook

## Required secrets

Create ~/services/tutorlaing/.env on the VM using .env.example as the schema.
Required values are TELEGRAM_BOT_TOKEN and, for a closed alpha,
TELEGRAM_ALLOWED_CHAT_IDS.
Production also sets TELEGRAM_WEBHOOK_URL and a random
TELEGRAM_WEBHOOK_SECRET. Local development may leave both empty for polling.

Never commit the environment file. The existing Brainless bot process must be
stopped before Tutorlaing starts with the same token: Telegram permits only one
active long-polling consumer.

## First deployment

Copy compose.yaml to ~/services/tutorlaing/compose.yaml, create .env, then:

    cd ~/services/tutorlaing
    docker compose pull
    docker compose up -d
    docker compose ps
    curl -fsS http://127.0.0.1:8080/health
    docker compose logs --tail 100 tutorlaing

## Update

After CI publishes a new latest image:

    cd ~/services/tutorlaing
    docker compose pull
    docker compose up -d
    curl -fsS http://127.0.0.1:8080/health

## Rollback

Every publish also creates a sha-<commit> tag. Replace latest in compose.yaml
with the previous known-good SHA tag, pull and recreate the container. The
named volume tutorlaing-data is not removed by container recreation.

## Cleanup boundary

The authorized cleanup concerns the old application runtime on srv-150.
Do not remove:

- the local Windows folder D:\aibrain\04_projects\brainless;
- SSH access and authorized keys;
- WireGuard/network configuration;
- Docker Engine;
- Cloudflare Tunnel until all old routes are intentionally retired.

Before deleting old containers or volumes, inventory them and verify that no
Tutorlaing volume is included.
