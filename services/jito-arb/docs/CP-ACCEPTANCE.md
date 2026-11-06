# CPMM / PumpSwap acceptance

Base: `feat/jito-arb-coverage` at `01f61484`. Existing parity captures are unchanged:
CPMM 260 exact samples and PumpSwap 530 exact samples, with zero mismatches.

## Offline checks

From `services/jito-arb`:

```sh
cmake --preset release
cmake --build --preset release -j 6
ctest --preset release --output-on-failure
cargo test --manifest-path program/executor/Cargo.toml
```

The Rust executor tests capture CPI instructions on the host; they do not execute
DEX programs or prove validator behavior. They check both directions, the program
allowlist, instruction discriminators/amounts/minimums, account order/privileges,
and account-range rejection. C++ tests also check mixed CPMM/Pump executor route
ranges, both route orders, and SPL/Token-2022 ATA selection.

Decoder and instruction oracles use Anchor 0.31.1, web3.js 1.99.0 and spl-token
0.4.15 (the existing pump-desk fixture dependencies). With those dependencies
available through `NODE_PATH`:

```sh
node tools/fetch_fixtures.mjs --cp-from-parity
node tests/golden_tx/gen_cp.mjs
```

Both commands are offline and deterministic. `cp_decode.json` holds 57 real
accounts from the existing parity RPC captures, independently decoded by Anchor's
BorshAccountsCoder. No C++ offsets or decoder outputs generate expectations.
`config/dex_programs.toml` pins full upstream source and IDL commits; the IDLs are
committed verbatim. Golden instruction data uses BorshInstructionCoder, account
order and privileges come from the IDLs, and PDA/ATA keys use web3.js/spl-token.

The closed form is a continuous, fee-aware search seed only. It models CPMM's
input/output creator-fee direction and Pump's sell-output/buy-input fees, selecting
Pump's current snapshot fee tier. It falls back to geometric search when the
model is unsupported or invalid. All accepted final amounts and profit use exact
integer quote_chain results. A transfer-fee extension always rejects a new CP DEX
pool, even if both epoch rates are zero.

## Local-validator setup still required

No validator run or mainnet executor deployment is claimed here. The existing
`program/executor/tests/validator.sh` remains unchanged. It expects Rust/Cargo,
Agave/Solana CLI (`cargo build-sbf`, `solana-test-validator`, `solana-keygen`,
`solana`), a C++ `sim-check` build, a public funded token holder address, an RPC
with account reads, and a route-specific `tests/fixtures/sim_check.json` account
list. Its current route is Raydium; running it alone does not validate CPMM/Pump.
The script resets its local ledger and loads the locally built executor, so it
must be run separately in an explicitly prepared local-validator environment.
No private holder key is needed for `sigVerify:false` simulation.

The added helper only prints clone arguments; optional preflight uses
`getMultipleAccounts` and never starts/resets a validator or deploys anything:

```sh
node tools/cp_validator_preflight.mjs cpmm
node tools/cp_validator_preflight.mjs pumpswap --rpc "$RPC_URL"
```

For each chosen CP route, add its public holder/ATAs and any Kamino dependencies
to the clone set. Clone the selected DEX and fee program as upgradeable, plus
SPL/Token-2022/ATA programs. Confirm buyback recipient ATAs exist for Pump's quote
mint. A prepared validator must test a complete executor route at the measured
minimum and rejection above that minimum, with both swap directions covered.
Mainnet reads and unsigned simulation remain separate from executor deployment.
Archived signed-candidate replay tooling can be used independently by the parent
branch; it is not modified here.
