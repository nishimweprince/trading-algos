# ta-plugin-ctrader

cTrader Open API provider: the protobuf wire stack (`framing`, `protocol`,
`proto`, generated `_generated/`), OAuth token rotation (`tokens`), the account
registry (`accounts`), symbol catalogs, spot/trendbar decoding, the
single-account `CTraderSession` and the multi-account `CTraderGateway`.

Published as `ctrader` in the `ta.execution` entry-point group. Services load it
with `ta_plugin_api.load_providers` and never import it to choose a broker.

- **Settings** are structural (`settings.CTraderSettings`): any object with the
  listed attributes works, so execution-service and market-data-service each
  bind them from their own environment.
- **Token caches are per process.** cTrader rotates the refresh token on every
  refresh and invalidates the previous one, so two processes sharing a cache
  file (or a grant) lock each other out. Give each its own grant and
  `token_cache_path`.
- **`ta_plugin_ctrader.testing`** ships `FakeCTraderServer`, a wire-level fake
  that consumers use in their own tests.
- **One-shot CLI helpers** (`discover`): `--discover-accounts`,
  `--discover-symbols`, `--refresh-token`, wired by the service's `main`.

Regenerate the protobuf modules with `./scripts/generate_protos.sh` (needs the
`dev` extra); see `proto/README.md`.
