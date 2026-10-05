# Migration Racer Spec

Oct 5, 2026 · @Precieux Mugisha

## Summary

Best hypothesis: Cartasyl runs a **migration racer** on pump.fun. He pushes nearly-complete bonding curves over the line with small buys, then races to be the wallet that calls the permissionless `migrate` instruction. Something tied to that action pays him more than the buys lose.

Confidence is moderate. The behaviour fits everything he said and showed, but the exact payout to the caller of `migrate` is not confirmed from public docs. Section 7 lists the on-chain checks that would settle it.

## Evidence

Every statement and screenshot from the conversation on 4 Oct 2026 lines up with the migration-racer reading.

| What he said or showed | What it implies |
| --- | --- |
| Lost $30k trading memecoins in 10 days, wallet still up 37x | Trading PnL is not his income source |
| "I get paid $10 for losing $3" | A payout is triggered by losing trades and exceeds their cost |
| Position size $30–300 | Small buys, enough to finish a curve's last tokens, not to speculate |
| 1 hour: $19.3k bought, -$1.74k realized (about -9%) | Buys land at the top of a curve and dump after; the loss is the input cost |
| SOL balance rises in small regular steps over the same hour | Discrete payout per event, not price exposure |
| "I am abusing a mechanic, not trading" | A protocol rule pays him, not a counterparty |
| Talks about seeing transactions "before they are in the shred" | Shred-level data feed; latency is the competitive edge |
| "Why didn't you migrate the coin then? I migrated 1000000s of coins, it's a public transaction" | He calls `migrate` himself, at very high volume |

## The mechanic

On pump.fun, any wallet can call `migrate` once a bonding curve is complete, so graduating a coin is an open race.

- A curve is complete when its real token reserves hit zero, meaning every token on the curve has been sold. The `complete` flag is set automatically by the buy that empties it.
- `migrate` then moves the remaining liquidity into a canonical PumpSwap pool. The instruction's accounts include a pool token account for the calling user.
- Pump.fun also runs a separate volume-based incentive program, where traders claim token incentives in proportion to their share of daily volume.

Unconfirmed: what, if anything, the `migrate` caller receives directly. The candidates are covered in section 6.

Source: [pump public docs (DeepWiki)](https://deepwiki.com/pump-fun/pump-public-docs)

## System design

The pipeline turns a fast data feed into one atomic bundle per near-complete curve.

&#91;embedded content: migration racer pipeline · 6 steps, 2 decisions\]

The watcher only fires when the remaining buy costs less than the expected payout; every bundle that loses the race costs fees and tips.

## Economics

The strategy works when the payout per migration beats the cost of forcing and winning it.

```latex
N \cdot R \;>\; L_{\text{completion}} + F_{\text{trade}} + T_{\text{tips}} + C_{\text{failed}}
```

- N: migrations won per hour
- R: value received per migration won (the unknown)
- L completion: loss on the last-token buys, which dump after graduation (his hour: about $1.74k on $19.3k bought)
- F trade: platform fees on those buys
- T tips: priority fees and Jito tips to land first
- C failed: cost of attempts that lose the race or revert

His sizing fits this model. At $30–300 per buy, he only finishes curves that are already close to complete, which keeps L small per event. The steady staircase in his SOL balance suggests R arrives once per event in a similar amount each time.

## Alternative hypotheses

These explain the payout R differently, or replace the migration story entirely. Ranked by fit with the evidence.

| Hypothesis | Where R comes from | Fit | What would rule it out |
| --- | --- | --- | --- |
| Caller reward on migrate | SOL or LP tokens sent to the `migrate` caller | High | Migrate transactions show no value flowing to the caller |
| First position on the new pool | He bundles migrate with his own buy on the fresh pool and sells into the first wave | Medium | His post-migration buys are not consistently profitable |
| Volume incentives | Pump token incentives claimed against his share of daily volume | Medium | No `claim_token_incentives` calls from his wallets; payouts are SOL, not tokens |
| Paid entries | Devs pay his wallet to buy so copy-traders follow | Low | Incoming SOL is not from devs or early buyers of the coins he bought |
| Creator fees | He launches the coins and collects creator fees | Low | He is not the creator of the coins he migrates |

The first two can coexist: one bundle can collect a migration payout and take first position on the new pool.

## Verification plan

All of this can be settled from public chain data in an afternoon, without his help.

1. Pull a sample of recent pump.fun `migrate` transactions (Helius or a Yellowstone gRPC stream) and rank caller wallets by count. A migration racer shows up as a few wallets with very high counts.
2. For the top callers, diff pre- and post-balances in each migrate transaction. Any SOL, LP tokens or other assets landing in the caller's accounts is R.
3. Check the transactions or bundles just before each migrate. A completion buy from the same wallet cluster confirms the push-over-the-line step.
4. Check whether the same bundle also buys on the new PumpSwap pool, and whether those positions are sold within minutes.
5. Look for `claim_token_incentives` calls from those wallets to size the volume-incentive share.
6. If he ever shares a wallet address, match its incoming SOL steps to steps 2–5.

- [ ] Sample 1,000 recent migrate transactions and rank callers
- [ ] Balance-diff the top 10 callers' migrate transactions
- [ ] Decide which hypothesis survives

## Implications for our bot

Our bot was benchmarked against a profitable speed sniper who does not exist; the useful lesson is the bundle pattern, not his payout.

**Decision:** build a new variant of the bot that targets migrations. The existing launch sniper stays as it is, so the two can be compared on net PnL.

- **Stop presigning for migration.** Presigned buys fail because the pool accounts do not exist at signing time and blockhashes expire. If presigning is still needed, use a durable nonce.
- **Be the migration.** Send `migrate` and our buy in one atomic bundle. That guarantees first position on the new pool instead of reacting one block late.
- **Measure net, not gross.** Track PnL per trade after fees, tips, failed bundles and rugs, on a sample of at least a few hundred migrations.
- **Expect competition.** Cartasyl and others already run this race with shred-level data, so landing rate and tip sizing will decide who wins.

Open questions:

- What exactly does the `migrate` caller receive today?
- Do fresh PumpSwap pools rise often enough after graduation for first position to pay?
- What landing rate do we get on bundles at current tip levels?
