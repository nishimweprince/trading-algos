# Callisto: CallistoFX signal monitor and backtest

Watches the public Telegram channel [t.me/callistofx](https://t.me/callistofx). Every new post
(text plus images) goes to OpenAI, which decides whether it is a live trade signal. For each
new idea the monitor emits a payload: ticker, direction, entry, SL/TP/trailing from env, and
the signal's **age**. `backtest.py` runs the same pipeline over the last N days on Dukascopy
M1 bid/ask data.

## How it works

| Step | File | Notes |
|---|---|---|
| Read the channel | `channel.py` | Public web preview `t.me/s/callistofx`, no login. Paginates with `?before=`. Photos are public CDN URLs. |
| Classify | `classify.py` | OpenAI Responses API with a strict JSON schema. Kinds: `signal`, `closed_result`, `member_result`, `analysis`, `promo`, `other`. Reads MT5 position screenshots (`XAUUSD.s buy 24.00  4120.39 → 4125.17`) and "Premium Channel" screenshots (`BUY RANGE: 4121-4115`, clock `1:55 pm`). Cached by post id in `data/classified.jsonl`, so each post is billed once. |
| Group | `signals.py` | Folds the call and its progress screenshots into one **idea** and keeps the **earliest** post. Posts join an idea when ticker and side match, the idea's last post is within `GROUP_HOURS`, and entries (when known) are within `ENTRY_MATCH_PIPS`. A new idea whose exact position entry price was already shown is flagged `repost`: the channel recycles old profit screenshots, even on weekends. |
| Prices | `prices.py` | Dukascopy free M1 BID + ASK candles, cached per day in `data/prices/`. |
| Backtest | `backtest.py` | Replays each idea. See below. |
| Live | `monitor.py` | Polls every `POLL_SECONDS` (or uses Telethon push). Emits `signal` events for new ideas and logs `update`/`repost`/`ignored`. |

### Age
- `age.pips_in_profit`: how far the trade has already run when first seen, read from the image
  (current − entry) or from text ("Still IN 140 pips").
- `age.call_time` / `seconds_since_call`: from the clock printed in the screenshot. The
  screenshots come from phones in different time zones (UTC+8 and UTC+4 both appear), so the
  zone that puts the call closest to before the post is used.
- The backtest also measures age from prices: `age_pips` (market at post vs stated entry) and
  `age_min` (time since price last traded at the stated entry).

### Payload (`signal` event)
```json
{"event":"signal","ticker":"XAUUSD","direction":"SELL","entry":3901.99,"entry_range":null,
 "reference_price":3898.52,"stop_loss_pips":120,"take_profit_pips":150,
 "stop_loss":3910.52,"take_profit":3883.52,
 "trailing":{"start_pips":50,"distance_pips":30,"step_pips":10},
 "channel_sl":null,"channel_tp":null,
 "age":{"pips_in_profit":34.7,"call_time":null,"seconds_since_call":null,"seconds_since_post":4},
 "posted_at":"2026-11-13T07:11:54+00:00","post_id":77882,
 "post_url":"https://t.me/callistofx/77882","confidence":0.96,"note":"..."}
```
`stop_loss`/`take_profit` are anchored on the latest price shown in the post. An executor
should re-anchor the pip distances on its own fill.

## Backtest model
- **Follower**: market order `ENTRY_DELAY_SECONDS` after the earliest post, at the next M1 bar
  open (BUY at ask, SELL at bid). Exits: BUY on bid, SELL on ask.
- SL/TP/trailing in pips from the fill. Trailing: once `TRAIL_START_PIPS` in profit, the stop
  sits `TRAIL_DISTANCE_PIPS` behind the best price and moves in steps of at least
  `TRAIL_STEP_PIPS`. If SL and TP fall in the same bar, the SL counts. A gap through the stop
  fills at the open. `MAX_HOLD_HOURS` timeout.
- Skipped: `repost`, `market_closed` (no bar within 30 min), `stale` (stated entry more than
  150 pips from market at post time, `--stale-pips`), `too_old` (`MAX_AGE_PIPS`).
- **Channel** model (reference only): same rules from the channel's stated entry at the
  estimated call time. The call time is chosen as a moment when price was at the entry, so
  this model is optimistic. It shows what the channel's own book looks like, not what a
  follower gets.
- Gold pip = $0.10 (the channel's "500 pips = $5,000 per lot").

## Run
```bash
cd callisto
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env && chmod 600 .env       # fill OPENAI_API_KEY
.venv/bin/python backtest.py --days 7 --grid # scrape + classify + replay
.venv/bin/python backtest.py --offline       # re-run on cached posts (e.g. new SL/TP env)
.venv/bin/python monitor.py                  # live, web polling
.venv/bin/python monitor.py --source telethon  # live via Telegram user session (asks phone + code once)
.venv/bin/python -m pytest -q test_callisto.py
```
Outputs: `data/results/{ideas,trades_follower,trades_channel,grid}.csv`, `summary.json`,
`data/events.jsonl`.

Results of the first run are in [RESULTS.md](RESULTS.md).

## Limits
- The web preview lags Telegram by a few seconds. Telethon is push but needs a user login.
- Many calls only appear as images with SL/TP blurred, so the channel's own levels are
  rarely available. SL/TP come from env.
- Progress posts with no entry price join whichever open idea on the same side is most recent.
- The stale check needs prices, so it runs only in the backtest. Live, the `repost` rule and
  `MAX_AGE_PIPS` are the protection against old screenshots.
