# zone-trend

A zone-trend swing strategy for Binance USDⓈ-M perpetuals:
- 1D/4H zones and trend
- 1H break-and-retest entries
- position sizing by percentage price movement

It ships its own Binance Vision backtester and a live runner, and both use the same pure strategy core.

- Spec: `Zone-Trend Strategy Spec v1.1` (kept outside the repo).
- Plan, milestones and spec-review notes: [PLAN.md](PLAN.md).
- Research on a VM (data download, walk-forward, M4 verdict): [RESEARCH-VM.md](RESEARCH-VM.md).
- Running the bot (shadow, live, status page): [DEPLOY.md](DEPLOY.md).

**Status:** scaffold only. The milestones are M1 data → M2 core → M3 backtester → M4 validation gate → M5 live/shadow → M6 ops. M5 and M6 are built only if M4 passes.

```bash
uv sync --package zone-trend --group dev
uv run --package zone-trend zt config            # validate config/default.yaml
uv run --package zone-trend pytest -m "not integration"
```

Live venue access goes through `plugins/binance-futures`, discovered by entry point, and alerts go through `ta-notify`. Secrets come only from the environment.
