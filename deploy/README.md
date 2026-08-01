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
Required values are TELEGRAM_BOT_TOKEN, AI_PROVIDER, GEMINI_API_KEY and
GEMINI_MODEL and, for a closed alpha, TELEGRAM_ALLOWED_CHAT_IDS.
Production compose sets TELEGRAM_WEBHOOK_URL. The random
TELEGRAM_WEBHOOK_SECRET is stored in the server-only .env with mode 600.
Local development may leave all webhook settings empty for polling.

Generate the secret once on the VM without printing it and append it to the
protected .env:

    chmod 600 .env
    secret="$(openssl rand -hex 32)"
    printf '\nTELEGRAM_WEBHOOK_SECRET=%s\n' "$secret" >> .env
    unset secret

Do not paste the generated value into GitHub, documentation or command output.
The Gemini key is copied directly from an existing protected server-side secret
store into this `.env`; never print it. The verified model for the current key
is `gemini-3.5-flash`. Pro models currently return a zero-quota response until
billing is enabled.

Port 5678 is mapped to the app because the existing Cloudflare route for
brain.sekond.pl already targets localhost:5678. Port 8080 remains available
locally for direct health checks.

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

## Current production state

- Bot: @brnai_bot
- Public health: https://brain.sekond.pl/health
- Webhook: https://brain.sekond.pl/telegram/webhook
- Runtime containers: tutorlaing and n8n-mcp-projects-1
- Persistent app volume: tutorlaing_tutorlaing-data
- AI: Gemini enabled, model gemini-3.5-flash, consent v2 required
- Adaptive drills: enabled; production smoke passed for 5 items / 5 types
- Reminder scheduler: enabled; user mode defaults to off, Europe/Warsaw quiet hours
- Old n8n, Google MCP and Twenty CRM containers/volumes: removed
- Management bridge is intentionally retained until direct SSH is stable
