# Deployment

Server routes, secret references and the ACL-safe connection helper are
documented in [ACCESS.md](./ACCESS.md). No private key is stored in this repo.

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
Required values are `TELEGRAM_BOT_TOKEN`, `AI_PROVIDER`, the selected provider
key/model and, for a closed alpha, `TELEGRAM_ALLOWED_CHAT_IDS`. The prepared server profile uses
`AI_PROVIDER=openai`, `AI_FALLBACK_PROVIDER=gemini`, `OPENAI_MODEL=gpt-5.6-sol`
and `OPENAI_REASONING_EFFORT=low`.
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
The OpenAI and Gemini keys exist only in this protected `.env`; never print
them. Verified routes are `gpt-5.6-sol` through the OpenAI Responses API and
`gemini-3.5-flash` as failover. Gemini may return `HTTP 429`; OpenAI is primary
so this does not make the learner tool unavailable.

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

## Last observed technical server state

Tutorlaing is not treated as a production product. The endpoints below describe
the previously prepared alpha runtime and are not evidence that the current
working tree has been deployed. On 2026-08-07 WireGuard SSH recovered and image
`sha-ac98eb2` was pulled and recreated. Remote digest, local/public health,
Docker health and logs were checked; details and exact evidence are in
`ACCESS.md`.

- Bot: @brnai_bot
- Public health: https://brain.sekond.pl/health
- Webhook: https://brain.sekond.pl/telegram/webhook
- Runtime containers: tutorlaing and n8n-mcp-projects-1
- Persistent app volume: tutorlaing_tutorlaing-data
- AI runtime: OpenAI GPT-5.6 Sol primary + Gemini 3.5 Flash failover; both
  credentials are stored only in the mode-600 server `.env`; real OpenAI
  translation smoke passed; current working tree requires consent v4
- Adaptive drills: 8 items / at least 4 types; A0–C1 level policy controls scaffolding and AI prompts
- Interactive toolkit: 10 history-aware flashcards, bidirectional phrase variants and level-aware topic drills
- Telegram UI on the last deployed image may lag behind the working tree. The
  working tree uses reply navigation v5 (`Today`, `My activities`, `Practice`,
  `Tools`) and a localized canonical command catalog.
- Reminder scheduler: active in all learning stages with five-minute retry; user mode defaults to off, Europe/Warsaw quiet hours
- Re-engagement: after 5/3/2/1 inactive days by reminder mode, one normal slot becomes a localized motivational card; other slots are suppressed until cooldown
- Old n8n, Google MCP and Twenty CRM containers/volumes: removed
- Management bridge is intentionally retained until direct SSH is stable
