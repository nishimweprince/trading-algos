# PumpSwap adjacent-idea replay

Generated from 744 WSOL paths; normal pools only: 575 (385 train / 190 chronological test).
Excluded 76 paths that hit the RPC pagination cap.
Position 0.500 SOL, 125 bps per side, 0.0011 SOL round-trip transaction/tip cost.

The entry and exit parameters were selected on the training prefix. The figures below are from the untouched chronological test suffix.

| family | test n | win % | mean % | median % | SE % | 95% bootstrap CI | p05 % |
|---|---:|---:|---:|---:|---:|---:|---:|
| breakout | 4 | 50.0 | -4.20 | -1.00 | 11.00 | -21.76..+13.36 | -31.61 |
| dip_rebound | 3 | 66.7 | -1.25 | +7.90 | 11.36 | -23.85..+12.18 | -23.85 |
| post_self_pump | 108 | 2.8 | -3.55 | -1.04 | 0.80 | -5.23..-2.14 | -31.03 |
| delay_momentum | 144 | 9.7 | -4.11 | -1.74 | 1.01 | -6.40..-2.39 | -22.88 |

## Selected configurations

- `breakout`: entry `breakout;max_single_buy_sol=30;breakout_pct=20;max_wait_ms=5000`; exit `h=60s,tp=10,sl=none`.
- `dip_rebound`: entry `dip_rebound;max_wait_ms=40000;drawdown_pct=30;rebound_pct=5`; exit `h=60s,tp=10,sl=none`.
- `post_self_pump`: entry `post_self_pump;delay_ms=400`; exit `h=60s,tp=20,sl=10`.
- `delay_momentum`: entry `delay_momentum;delay_ms=1000;min_return_pct=0;min_flow_sol=10`; exit `h=40s,tp=10,sl=none`.

## Verdict

No long-only post-migration rule survived out of sample with a positive 95% bootstrap interval. Keep migration trading shelved; retain the replay pipeline for new hypotheses.

The train-selected overall winner was `breakout;max_single_buy_sol=30;breakout_pct=20;max_wait_ms=5000` with `h=60s,tp=10,sl=none`. Its test mean was -4.20% over 4 trades.

## Dataset checks

Self-pump paths: 274; mayhem paths excluded from model selection: 169; callers: 229.

Caveats: one short regime; only observed post-transaction reserve states; no model of how other traders react to our order; slot time is approximated as 400 ms; non-WSOL and mayhem pools are excluded from the primary screen.
