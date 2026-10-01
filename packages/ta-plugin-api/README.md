# ta-plugin-api

The contract between services and the broker/exchange plugins under `plugins/`.

A plugin is an ordinary workspace distribution (`ta-plugin-<broker>`) that
publishes a `ProviderFactory` in one or both entry-point groups:

| Group | Used by |
|---|---|
| `ta.execution` | execution-service |
| `ta.market_data` | market-data-service |

```toml
[project.entry-points."ta.execution"]
ctrader = "ta_plugin_ctrader:FACTORY"
```

Services find providers with `load_providers(group, names)` and never import a
plugin to decide which broker to run. Unlike `ta.strategies`, discovery **fails
closed**: a configured provider that is missing, published twice, fails to
import, or is not a `ProviderFactory` raises `PluginError` and the service does
not start.

Also here, because more than one plugin and service needs them:

- `MarketDataHub`: non-blocking fan-out from one broker connection to N stream
  subscribers.
- `SymbolResolutionError`: a symbol with no unambiguous broker mapping.
- `ta_plugin_api.testing`: the conformance kit. `MarketDataConformance` and
  `ExecutionConformance` are pytest mixins a plugin subclasses in
  `tests/test_conformance.py` with fixtures built from its own fakes; see
  ARCHITECTURE.md "Adding a plugin". The module imports pytest, so it is for
  tests only.
