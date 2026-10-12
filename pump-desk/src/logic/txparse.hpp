#pragma once
// Migrate / create transaction parsing over TxView (pump-fun/src/racer/migrateTx.ts).
// Pure functions over string_views into the frame; no allocation.
#include <cstdint>
#include <string_view>

#include "core/types.hpp"
#include "stream/update.hpp"

namespace pd {

/** base58 strings of the programs and mints the parsers compare against (computed once). */
struct KnownB58 {
  char pump_fun[48], pump_swap[48], token_2022[48], wsol[48];
  uint8_t pump_fun_len, pump_swap_len, token_2022_len, wsol_len;
  std::string_view pump_fun_sv() const { return {pump_fun, pump_fun_len}; }
  std::string_view pump_swap_sv() const { return {pump_swap, pump_swap_len}; }
  std::string_view token_2022_sv() const { return {token_2022, token_2022_len}; }
  std::string_view wsol_sv() const { return {wsol, wsol_len}; }
};
const KnownB58& known_b58();

struct TraceEntry {
  std::string_view program;  // base58
  std::string_view name;     // instruction name from "Program log: Instruction: <Name>"
};

/** (program, instruction) pairs attributed through the CPI invoke stack. Returns count. */
size_t instruction_trace(const TxView& tx, TraceEntry* out, size_t cap);

/** "Migrate" or "MigrateV<digits>" (not MigrateBondingCurveCreator). */
bool is_migrate_name(std::string_view name);

struct MigrateInfo {
  bool is_migrate = false;
  bool landed = false;           // ok, not "already migrated", balances moved
  bool curve_buy_before = false; // pump.fun Buy before Migrate (completion bundled)
  bool pool_buy_after = false;   // PumpSwap Buy after Migrate (first position bundled)
  std::string_view caller;       // account key 0 (raw 32 bytes)
  std::string_view mints[4];     // non-WSOL mints seen in token balances (base58)
  uint8_t n_mints = 0;
};

MigrateInfo parse_migrate(const TxView& tx);

/** True when any (mint, owner) token balance differs between pre and post. */
bool balances_moved(const TxView& tx);

/** Sum of post (or pre) token balances matching mint and owner (base58 views). */
uint64_t sum_tokens(const TokenBalanceView* tb, uint16_t n, std::string_view mint, std::string_view owner);

/** The owner holding the most of `mint` after the tx: the new pool's base vault authority. */
std::string_view largest_holder(const TxView& tx, std::string_view mint);

/** Token program (base58) of `mint` from the balance metadata, or empty. */
std::string_view token_program_of(const TxView& tx, std::string_view mint);

struct LaunchInfo {
  std::string_view mint;           // base58, the single new non-WSOL mint
  std::string_view curve;          // base58, its largest holder (the bonding curve)
  std::string_view token_program;  // base58
  bool no_transfer_fee = false;    // createLogsProveNoTransferFee
  bool ok = false;
};

/** A successful pump.fun create tx -> its mint, curve and fee proof. */
LaunchInfo parse_launch(const TxView& tx);

/** Logs complete, no "transfer fee", exactly one InitializeMint{,2} and it ran under Token-2022. */
bool create_logs_prove_no_transfer_fee(const TxView& tx);

}  // namespace pd
