# Outbound notifications — operator handoff

v3.5 introduces a notification dispatcher that sends reminders for
clinical events and patient tasks through the contact channels each
PatientContact has consented to. This doc covers the operational
side: which knobs the operator flips, what DNS records have to exist,
and how to wire the inbound bounce webhook from the mailer provider.

## Overview

```
clinical_event / patient_task write
        │
        ▼  (post-commit listener, services/notifications/scheduling.py)
notification_dispatches  (one row per contact × offset × channel)
        │
        ▼  (cron every 5 min, workers/tasks/dispatch_notification.py)
dispatcher  (services/notifications/dispatcher.py)
        │
        ├──▶ EmailNotifier            → your SMTP relay (transactional email provider)
        ├──▶ ICSAttachmentNotifier    → same SMTP + .ics attachment
        ├──▶ TelegramNotifier         → api.telegram.org (per-bot)
        ├──▶ GenericWebhookNotifier   → caller's webhook_url (HMAC signed)
        └──▶ WhatsAppNotifier         → stub behind feature flag
```

Cancellable + idempotent + auditable: every queued dispatch is a row
in `notification_dispatches` with `(target_id, contact_id, offset,
channel)` as the natural key. Re-runs are no-ops; deletes / reschedules
of the source event/task cancel the matching pending rows.

## Channels

| Channel | Address column | Consent flag | Feature flag |
|---|---|---|---|
| `email` | `patient_contacts.email` | `consent_email` | `BVP_NOTIFICATIONS_EMAIL_ENABLED` |
| `ics_attachment` | `patient_contacts.email` | `consent_email` | `BVP_NOTIFICATIONS_EMAIL_ENABLED` |
| `webhook_telegram` | `telegram_chat_id` | `consent_telegram` | `BVP_NOTIFICATIONS_TELEGRAM_ENABLED` |
| `webhook_whatsapp` | `whatsapp_phone` | `consent_whatsapp` | `BVP_NOTIFICATIONS_WHATSAPP_ENABLED` |
| `webhook_generic` | `webhook_url` | `consent_webhook` | `BVP_NOTIFICATIONS_WEBHOOK_ENABLED` |

A dispatch only fires when **all four** are true:
1. Global `BVP_NOTIFICATIONS_ENABLED=true`
2. Channel feature flag true
3. `consent_to_contact` umbrella true
4. Per-channel `consent_<channel>` true (plus `email_delivery_state='active'`
   for email)

Defaults are conservative: every new contact lands with the per-channel
consent flags off. The recipient must opt in to each channel
explicitly (one-click via the email footer, or via the operator using
`configure_contact_channel` MCP tool / `/configure-channel` REST
endpoint).

## Environment variables

Set on the backend (`backend/`) and the worker (`workers/`) — both
read the same `BVP_*` env. Production secrets live in
`deploy/bvphoenix-production-k8s-secrets/secrets.env`.

| Variable | Default | Notes |
|---|---|---|
| `BVP_NOTIFICATIONS_ENABLED` | `true` | Global kill-switch. |
| `BVP_NOTIFICATIONS_DEFAULT_OFFSETS` | `-1440,-60` | Fallback offsets when an event/task does not specify `reminder_offsets_minutes`. Negative = minutes before anchor. Capped at 5 by the scheduler. |
| `BVP_NOTIFICATIONS_EMAIL_ENABLED` | `true` | |
| `BVP_NOTIFICATIONS_WEBHOOK_ENABLED` | `false` | Generic webhook. Stay off until the consuming integration is approved. |
| `BVP_NOTIFICATIONS_TELEGRAM_ENABLED` | `false` | Requires `BVP_TELEGRAM_BOT_TOKEN`. |
| `BVP_NOTIFICATIONS_WHATSAPP_ENABLED` | `false` | Returns `provider_disabled` until the business API account is approved. |
| `BVP_TELEGRAM_BOT_TOKEN` | empty | `123456:ABC…` issued by @BotFather. |
| `BVP_WEBHOOK_ENCRYPTION_KEY` | empty | Hex-encoded 32 bytes. pgcrypto's `pgp_sym_encrypt` rides on it for the per-contact `webhook_secret_encrypted` column. |
| `BVP_WEBHOOK_TIMEOUT_SECONDS` | `5` | Outbound webhook timeout. |
| `BVP_NOTIFICATIONS_OPT_OUT_BASE_URL` | empty | Public origin for the unsubscribe URL. Falls back to `BVP_PUBLIC_FRONTEND_URL` when empty. |
| `BVP_TEM_WEBHOOK_SECRET` | empty | Shared secret for the Scaleway TEM event webhook (`HMAC-SHA256` over the raw POST body, header `X-Signature`). Empty → endpoint accepts unsigned requests and logs loudly (dev convenience). |

The existing `BVP_SMTP_*` set (already in production via Scaleway
TEM) is reused as-is — the notifier does not add a new mailer.

## DNS records

Your transactional email provider manages SPF / DKIM / DMARC for
senders on your domain. With `bitvision.example` as a placeholder:

* SPF: `bitvision.example TXT "v=spf1 include:_spf.<provider> -all"`
* DKIM: the selector + public key your provider generates;
  add as `<selector>._domainkey.bitvision.example TXT "<key>"`
* DMARC: `_dmarc.bitvision.example TXT "v=DMARC1; p=quarantine;
  rua=mailto:dmarc@bitvision.example; pct=100"`

These are static — apply them once during the deploy of v3.5 and they
serve every subsequent send.

## Telegram bot setup (sprint D1)

The Telegram channel requires a Telegram bot owned by BitVision. The
user does NOT type their `chat_id` by hand — Telegram doesn't expose
it through any UI surface. Instead we run a deep-link dance: the
operator clicks "Collega Telegram" in the contact panel, we mint a
short-lived single-use code, build a `https://t.me/<bot>?start=<code>`
URL, the user opens it on Telegram, the bot's webhook receives
`/start <code>` and captures the resulting `chat_id`.

Operator setup, one-shot per environment:

1. **Create the bot** in Telegram by writing to [@BotFather](https://t.me/BotFather):

   ```
   /newbot
   <name shown to users>           e.g. BitVision Reminders
   <username>                       e.g. BitVisionRemindersBot
   ```

   BotFather replies with the bot token (`123456:ABC-DEF…`). Keep it
   secret — anybody who has it can impersonate the bot.

2. **Configure backend env vars** in production secrets:

   * `BVP_TELEGRAM_BOT_TOKEN=<the token>`
   * `BVP_TELEGRAM_BOT_USERNAME=<the username, no @>`
   * `BVP_TELEGRAM_WEBHOOK_SECRET=<random 32-byte hex>` —
     Telegram echoes this in `X-Telegram-Bot-Api-Secret-Token` on
     every update; mismatched secret → 401 from our webhook.
   * `BVP_NOTIFICATIONS_TELEGRAM_ENABLED=true`

3. **Register the webhook** with Telegram. From any host with
   `curl`:

   ```bash
   curl -sX POST \
     "https://api.telegram.org/bot${BVP_TELEGRAM_BOT_TOKEN}/setWebhook" \
     -H 'Content-Type: application/json' \
     -d '{
       "url": "https://api.bitvision.example/api/notifications/telegram/webhook",
       "secret_token": "'"${BVP_TELEGRAM_WEBHOOK_SECRET}"'",
       "allowed_updates": ["message"]
     }'
   ```

   Verify with `getWebhookInfo`:

   ```bash
   curl -s "https://api.telegram.org/bot${BVP_TELEGRAM_BOT_TOKEN}/getWebhookInfo"
   ```

   You should see `"pending_update_count": 0` and the URL you set.

4. **Test the binding flow** end-to-end:

   1. In the patient contacts panel, click "Canali" on a contact.
   2. Section "Telegram" → "Collega Telegram".
   3. The modal shows a `https://t.me/<bot>?start=<code>` link.
   4. Open the link on Telegram, press "START".
   5. The bot replies "Collegamento riuscito"; the modal flips to
      "Telegram collegato" within ~3 seconds of polling.
   6. Click "Invia test" to confirm reminders land in the bot chat.

## Scaleway TEM bounce webhook (optional)

TEM emits delivery / bounce / complaint events as POST callbacks to a
URL of our choosing. Wire it to land on
`https://api.bitvision.example/api/notifications/bounce-webhook`
with HMAC-SHA256 signature, header `X-Signature`.

1. In the TEM dashboard, "Events" → "Webhooks" → add the URL.
2. Set the shared secret; paste the same value into the production
   `BVP_TEM_WEBHOOK_SECRET` env var.
3. Subscribe to the event types: `hard_bounce`, `soft_bounce`,
   `complaint`, `dropped`. We map them onto
   `patient_contacts.email_delivery_state` (bounced / suppressed)
   which short-circuits future sends to the address.

Without the bounce webhook the email channel still works; we just
keep retrying against a permanently-broken address. For low-volume
deployments that's acceptable; the operator can manually reset
`email_delivery_state` from the admin tooling.

## Opt-out

RFC 8058 single-click unsubscribe. Every email carries a
`List-Unsubscribe` header pointing at:

```
https://app.bitvision.example/api/notifications/opt-out?token=<uuid>&channel=email
```

Gmail / Outlook render the native "Unsubscribe" button against the
header; the recipient clicks it once and lands on a small
confirmation page. The `opt_out_token` is patient-contact-scoped
UUID generated server-side.

`channel=all` is the nuclear option (umbrella opt-out, flips
`consent_to_contact=false`).

## Smoke test (dev)

After applying the migrations on disk (the notifications schema is
materialised inside `0001_initial_schema.py` post the OSS-release
rebase; see `data-model.md §9` for the numbering note):

```bash
# 1. Start dev infra (Postgres + Redis + Mailhog if you want a UI)
make up.infra

# 2. Apply migrations (already done if you followed the v3.5 series)
cd backend && uv run alembic upgrade head

# 3. Run the unit tests
uv run pytest tests/test_notifications_unit.py -x -q

# 4. Manual smoke via MCP (when you have the backend + mcp server running):
#    - configure_contact_channel patient_id=... contact_id=... channel=email
#      consent_email=true preferred_locale=it
#    - send_test_notification patient_id=... contact_id=... channel=email
#    - the dispatcher cron picks it up within 5 minutes;
#      check `logs/dev_emails.eml` if BVP_EMAIL_PROVIDER=stub.
```

## Sprint scope

* **In scope (sprint C)**: email + ICS attachment + dispatcher worker
  + safety-net cron + opt-out endpoint + bounce webhook + 5 MCP tools.
* **Out of scope**: WhatsApp Business API integration (stub returns
  `provider_disabled`); frontend channels panel (sprint D); the
  GenericWebhookNotifier per-contact secret resolver (currently the
  signature header is only attached when the dispatcher resolves a
  secret — wiring the pgcrypto decrypt from
  `webhook_secret_encrypted` is the last piece needed for production
  webhook use).
