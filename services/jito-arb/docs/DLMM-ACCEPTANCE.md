# DLMM acceptance

Source/IDL pin: Meteora DLMM SDK `1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1`. `harness/dlmm_fixtures.mjs` decodes official binary account artifacts with Anchor and generates independent SDK math/instruction cases. `tests/fixtures/dlmm_sdk.json` covers signed bin indices, bitmap extensions, volatility and fee modes.

`tests/fixtures/parity_dlmm.json` contains 246 verified exact mainnet swap simulations across 13 unchanged-state brackets, zero mismatches: 116 X-to-Y and 130 Y-to-X. All 246 use nonzero variable-fee control. Independent Anchor inspection of the captured active bins found 127 outputs exceeding that active bin's liquidity, establishing traversal across bins. Five moving brackets and 14 unverified samples were discarded. Pool selection used Meteora's public SOL/USDC TVL ranking, followed by chain account reads; no API price entered the quoter.

Cloned-validator executor acceptance passed: two opposing legs, measured-output chaining, output equal to min_out succeeds, output plus one fails with custom 6002. See EXECUTOR-VALIDATION.md.

Configuration stays disabled by default. Explicit enablement registers the validated quoter. Initial supported coverage is classic SPL tokens only; Token-2022 DLMM pools are rejected, including transfer-fee extensions. Discovery fixes the bin-array window and extension-selected sparse arrays. Missing arrays and boundaries fail closed and use the existing counters.

Quotes use the subscribed Clock timestamp to decay volatility. A clock older than the pool is refused; advancing Clock updates do not invalidate an unchanged pool. Production pool discovery, subscription coverage and representative live route simulation remain deployment acceptance gates.
