# Curve-in, migrate-out replay

Input: 202 admitted curves → 202 usable paths; standard non-mayhem 139 (93 train / 46 chronological test), mayhem 63, non-standard 0.
Size 0.05 SOL; buy fee 1000000 / sell fee 1450000 lamports; curve fee from TradeEvents; pool fee 125 bps; buy lands +2, sell +2 slots, after every tx of the landing slot.

## Selected on train

- rule: `T=2 X=20 N=25 H=30s abort=off`
- train: n=66 mean=-11.15% med=-2.00% win=8% se=3.03 ci95=[-17.20,-5.38] p05=-71.3% {'failed': 42, 'pool_timeout': 8, 'stall': 14, 'pool_tp': 2}
- **test: n=38 mean=-12.87% med=-2.00% win=3% se=3.79 ci95=[-20.86,-5.93] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1}**
- test, fees 0.5M/0.5M: n=38 mean=-11.17% med=-1.00% win=3% se=3.70 ci95=[-19.00,-4.35] p05=-77.6% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1}
- test, landing +1/+1: n=38 mean=-12.87% med=-2.00% win=5% se=3.68 ci95=[-20.62,-6.17] p05=-63.9% {'pool_timeout': 2, 'failed': 23, 'stall': 12, 'pool_tp': 1}
- mayhem (all, same rule): n=41 mean=-15.51% med=-14.62% win=15% se=7.16 ci95=[-28.15,-0.44] p05=-65.0% {'pool_timeout': 14, 'stall': 18, 'pool_tp': 9}

## Gate: FAIL

Requires train mean > 0, test mean > 0 and test n ≥ 20.

## Top 10 train rules and their test result

| rule | train | test |
|---|---|---|
| `T=2 X=20 N=25 H=30s abort=off` | n=66 mean=-11.15% med=-2.00% win=8% se=3.03 ci95=[-17.20,-5.38] p05=-71.3% {'failed': 42, 'pool_timeout': 8, 'stall': 14, 'pool_tp': 2} | n=38 mean=-12.87% med=-2.00% win=3% se=3.79 ci95=[-20.86,-5.93] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=2 X=10 N=25 H=30s abort=off` | n=66 mean=-11.21% med=-2.00% win=8% se=3.02 ci95=[-17.22,-5.52] p05=-71.3% {'failed': 42, 'pool_timeout': 7, 'stall': 14, 'pool_tp': 3} | n=38 mean=-12.87% med=-2.00% win=3% se=3.79 ci95=[-20.86,-5.93] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=3 X=20 N=25 H=30s abort=off` | n=66 mean=-11.60% med=-2.00% win=15% se=3.16 ci95=[-18.11,-5.49] p05=-71.3% {'failed': 29, 'stall': 20, 'pool_timeout': 14, 'pool_tp': 3} | n=38 mean=-13.10% med=-2.00% win=11% se=3.83 ci95=[-20.98,-5.85] p05=-80.5% {'pool_timeout': 4, 'failed': 18, 'pool_tp': 2, 'stall': 14} |
| `T=2 X=20 N=5 H=30s abort=off` | n=66 mean=-11.61% med=-2.00% win=5% se=2.98 ci95=[-17.42,-6.01] p05=-71.3% {'failed': 42, 'pool_timeout': 9, 'stall': 14, 'pool_tp': 1} | n=38 mean=-12.17% med=-2.00% win=3% se=3.76 ci95=[-20.15,-5.44] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=2 X=10 N=5 H=30s abort=off` | n=66 mean=-11.62% med=-2.00% win=5% se=2.98 ci95=[-17.42,-6.03] p05=-71.3% {'failed': 42, 'pool_timeout': 8, 'stall': 14, 'pool_tp': 2} | n=38 mean=-12.17% med=-2.00% win=3% se=3.76 ci95=[-20.15,-5.44] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=2 X=20 N=10 H=30s abort=off` | n=66 mean=-11.72% med=-2.00% win=6% se=3.00 ci95=[-17.58,-6.17] p05=-71.3% {'failed': 42, 'pool_timeout': 9, 'stall': 14, 'pool_tp': 1} | n=38 mean=-12.79% med=-2.00% win=3% se=3.78 ci95=[-20.78,-5.86] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=3 X=20 N=5 H=30s abort=off` | n=66 mean=-11.74% med=-2.00% win=14% se=3.09 ci95=[-18.07,-5.88] p05=-71.3% {'failed': 29, 'stall': 20, 'pool_timeout': 16, 'pool_tp': 1} | n=38 mean=-11.97% med=-2.00% win=11% se=3.84 ci95=[-19.76,-4.78] p05=-80.5% {'pool_timeout': 5, 'failed': 18, 'stall': 14, 'pool_tp': 1} |
| `T=2 X=20 N=3 H=30s abort=off` | n=66 mean=-11.75% med=-2.00% win=3% se=2.99 ci95=[-17.53,-6.15] p05=-71.3% {'failed': 42, 'pool_timeout': 10, 'stall': 14} | n=38 mean=-12.19% med=-2.00% win=3% se=3.76 ci95=[-20.17,-5.48] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=2 X=10 N=10 H=30s abort=off` | n=66 mean=-11.75% med=-2.00% win=6% se=2.99 ci95=[-17.64,-6.20] p05=-71.3% {'failed': 42, 'pool_timeout': 8, 'stall': 14, 'pool_tp': 2} | n=38 mean=-12.79% med=-2.00% win=3% se=3.78 ci95=[-20.78,-5.86] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |
| `T=2 X=10 N=3 H=30s abort=off` | n=66 mean=-11.75% med=-2.00% win=3% se=2.99 ci95=[-17.54,-6.16] p05=-71.3% {'failed': 42, 'pool_timeout': 9, 'stall': 14, 'pool_tp': 1} | n=38 mean=-12.19% med=-2.00% win=3% se=3.76 ci95=[-20.17,-5.48] p05=-80.5% {'failed': 24, 'stall': 12, 'pool_tp': 1, 'pool_timeout': 1} |

## Entry threshold sensitivity (all standard paths, X=5 N=3 H=120s abort off)

- T=0.5: n=53 mean=-23.98% med=-2.00% win=2% se=4.47 ci95=[-33.53,-16.02] p05=-96.7% {'failed': 26, 'pool_timeout': 5, 'stall': 21, 'pool_tp': 1}
- T=1: n=66 mean=-20.37% med=-2.00% win=2% se=3.71 ci95=[-27.74,-13.45] p05=-95.6% {'failed': 35, 'pool_timeout': 8, 'stall': 22, 'pool_tp': 1}
- T=2: n=104 mean=-14.61% med=-2.00% win=3% se=2.63 ci95=[-19.94,-9.78] p05=-92.9% {'failed': 66, 'pool_timeout': 10, 'stall': 25, 'pool_tp': 3}
- T=3: n=104 mean=-14.97% med=-2.00% win=14% se=2.78 ci95=[-20.86,-9.56] p05=-95.1% {'failed': 47, 'pool_timeout': 19, 'stall': 29, 'pool_tp': 9}
- T=4: n=109 mean=-15.24% med=-2.00% win=22% se=2.86 ci95=[-21.15,-9.76] p05=-95.6% {'pool_timeout': 20, 'failed': 36, 'stall': 35, 'pool_tp': 18}
