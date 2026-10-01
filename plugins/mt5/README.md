# ta-plugin-mt5

MetaTrader 5 provider: `terminal.MT5Adapter` (the synchronous seam),
`RealMT5Adapter` (wraps the Windows-only `MetaTrader5` package, imported
lazily), the strategy-compatible symbol manifest (`symbols.load_mt5_symbols`),
and `testing.FakeMT5Adapter` for consumers' tests.

Published as `mt5` in the `ta.execution` entry-point group. Services load it
with `ta_plugin_api.load_providers` and never import it to choose a broker.

- **Install on the Windows host** with the `terminal` extra, which pulls in
  `MetaTrader5`. Everywhere else the package imports fine without it.
- **One terminal per process.** `MetaTrader5` keeps module-global state, so a
  host with two terminals (for example hfm and a Deriv account) runs two
  processes, each with its own `--profile`.
- **Symbols are case-sensitive** and kept verbatim (`Volatility 75 Index`).
