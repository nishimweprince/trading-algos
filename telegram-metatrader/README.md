# Telegram to MetaTrader 5 Copier

Standalone Python service that reads one Telegram chat with a user MTProto session and copies fixed-lot market trade signals into a locally running MetaTrader 5 terminal.

The service is built for a Windows VPS or laptop where MetaTrader 5 is installed and open. It defaults to dry-run mode, so parsed signals are logged but no orders are sent unless `LIVE_TRADING_ENABLED=true`.

## Setup

```powershell
cd telegram-metatrader
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
copy .env.example .env
```

Edit `.env` with Telegram API credentials, `CHAT_ID`, optional MT5 account credentials, fixed volume, and `SYMBOL_MAP_JSON`.

## First Login

```powershell
telegram-metatrader-login
telegram-metatrader-verify-chat
```

The login command creates `TELEGRAM_SESSION_PATH`. Treat the session file or `TELEGRAM_SESSION_STRING` like a password.

## Run

```powershell
telegram-metatrader
```

Health endpoints:

- `GET /health` returns liveness.
- `GET /status` returns counters, current chat, dry-run/live mode, and cursor state.

## Safety

- First run primes the cursor to the latest Telegram message and skips old chat history.
- Deduplication uses `(chat_id, message_id)` so restarts do not duplicate orders.
- Live orders require `LIVE_TRADING_ENABLED=true`, MT5 initialization, symbol visibility/selection, `order_check`, and then `order_send`.
- Missing SL/TP are allowed, but logged as `missing_sl` and `missing_tp`.

## Logs and State

Runtime files are created under `STATE_DIR` and `LOGS_DIR`.

| Path | Purpose |
| --- | --- |
| `state/cursor.json` | Last processed Telegram message ID for the configured chat |
| `state/handled.json` | Recent handled message keys |
| `state/telegram.session` | Telethon session file if `TELEGRAM_SESSION_STRING` is not used |
| `logs/raw.jsonl` | Raw Telegram messages when `LOG_RAW_MESSAGES=true` |
| `logs/signals.jsonl` | Parsed trade signals |
| `logs/executions.jsonl` | Dry-run and live execution attempts |
| `logs/errors.jsonl` | Parser, Telegram, and MT5 failures |

## Signal Format

The parser targets straightforward fixed-lot market signals:

```text
BUY GOLD
SL 2312
TP1 2330
TP2 2342
```

It also accepts `XAUUSD`, `XAU/USD`, common forex pairs, `SELL`, `stop loss`, and `take profit`. If multiple TPs are present, TP1 is sent to MT5 and the remaining TPs are retained in logs.

## Tests

```powershell
pytest
```

