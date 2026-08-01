# Notification Service

Standalone NestJS HTTP service for multi-channel notifications. Other apps in this repo (`fu-strategy`, `pump-fun`, `signals-scrapper`, etc.) call it over HTTP instead of embedding notification logic.

## Channels

| Channel | Provider |
|---------|----------|
| `TELEGRAM` | Telegram Bot API (grammy) |
| `EMAIL` | Resend |
| `SMS` | Pindo |
| `WHATSAPP` | Meta Cloud API |

Delivery history is stored in a local SQLite database via **TypeORM**.

## Quick start

```bash
cd notification-service
cp .env.example .env
# Fill in credentials and recipient lists
npm install
npm run start:dev
```

The service listens on `PORT` (default `3010`). SQLite data is written to `./data/notifications.db`.

## API

### `POST /notifications`

Send a notification to all env-configured recipients for each requested channel.

**Auth:** When `NOTIFICATION_API_KEY` is set, pass `Authorization: Bearer <key>` or `X-API-Key: <key>`.

```bash
curl -X POST http://localhost:3010/notifications \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NOTIFICATION_API_KEY" \
  -d '{
    "subject": "EUR/USD signal",
    "message": "<b>BUY</b> EUR/USD @ 1.0850",
    "contentType": "html",
    "channels": ["EMAIL", "TELEGRAM", "SMS"],
    "source": "fu-strategy"
  }'
```

**Request fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `message` | yes | Plain text or HTML body |
| `channels` | yes | At least one of `TELEGRAM`, `EMAIL`, `SMS`, `WHATSAPP` |
| `source` | yes | Calling service identifier |
| `subject` | no | Used for email; prepended for other channels |
| `contentType` | no | `text` (default) or `html` |

**Response `201`:**

```json
{
  "requestId": "uuid",
  "deliveryIds": ["uuid", "..."],
  "deliveriesAttempted": 4
}
```

When `NOTIFICATIONS_ENABLED=false`, returns `200` with `status: "skipped"`.

### `GET /notifications`

List delivery records. Query params: `limit`, `source`, `recipient`, `status`, `channel`.

### `GET /notifications/:id`

Get a single delivery record by ID.

### `GET /webhooks/whatsapp` / `POST /webhooks/whatsapp`

Meta WhatsApp webhook for verification and delivery status updates (`delivered`, `read`, etc.).

## Environment variables

### Service

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3010` | HTTP port |
| `DATABASE_URL` | `file:./data/notifications.db` | SQLite path (`file:` prefix stripped for TypeORM) |
| `NOTIFICATIONS_ENABLED` | `true` | Master on/off switch |
| `NOTIFICATION_API_KEY` | — | Optional API key; unset = no auth |

### Recipients (comma-separated)

| Variable | Used by |
|----------|---------|
| `NOTIFICATION_PHONES` | SMS, WHATSAPP |
| `NOTIFICATION_EMAILS` | EMAIL |
| `NOTIFICATION_TELEGRAM_CHAT_IDS` | TELEGRAM |

### Channel credentials

| Variable | Channel |
|----------|---------|
| `TG_BOT_TOKEN` | Telegram |
| `RESEND_API_KEY`, `NOTIFICATION_FROM_EMAIL` | Email |
| `PINDO_API_URL`, `PINDO_TOKEN`, `PINDO_SENDER_ID` | SMS |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_API_VERSION`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | WhatsApp |

## Development

```bash
npm run build
npm test
npm run start:prod
```

## Architecture

One `POST /notifications` call creates a `NotificationRequest` row, then one `NotificationDelivery` row per `(channel × recipient)`. Sends run in parallel; failures on one delivery do not block others.
