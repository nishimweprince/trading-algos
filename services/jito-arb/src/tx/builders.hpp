#pragma once
// Instruction builders for the arb transaction (spec §7.3, §8, §9):
//   ComputeBudget SetComputeUnitLimit (tag 2, u32) / SetComputeUnitPrice (tag 3, u64 micro-lamports)
//   System Transfer (u32 index 2, u64 lamports) for the Jito tip
//   executor execute_route (tag 0 + ExecuteRouteArgs, packed little-endian; program/executor/src/args.rs)
//   Address Lookup Table Create (u32 0, u64 recent_slot, u8 bump) / Extend (u32 2, u64 n, n x Pubkey)
#include <cstdint>
#include <span>
#include <vector>

#include "core/error.hpp"
#include "engine/route_graph.hpp"
#include "flash/kamino.hpp"
#include "state/pool_state.hpp"
#include "tx/message_v0.hpp"

namespace arb::tx {

void set_compute_unit_limit(IxList& l, uint32_t units);
void set_compute_unit_price(IxList& l, uint64_t micro_lamports);
void system_transfer(IxList& l, const Pubkey& from, const Pubkey& to, uint64_t lamports);

// ---- executor (spec §8) ----

/** One leg as the executor sees it: DEX enum, direction, and its slice of the remaining accounts. */
struct LegSpec {
  uint8_t dex = 0, a_to_b = 0, acct_start = 0, acct_len = 0;
};
struct ExecuteRouteArgs {
  uint64_t amount_in = 0, min_out = 0;
  uint8_t num_legs = 0;
  LegSpec legs[3]{};
};
constexpr uint8_t EXECUTE_ROUTE_TAG = 0;
constexpr size_t EXECUTE_ROUTE_DATA_LEN = 1 + 8 + 8 + 1 + 3 * 4;  // 30

/** Packed instruction data, identical to the Rust parser's layout (shared fixture tests/fixtures/execute_route.json). */
size_t encode_execute_route(const ExecuteRouteArgs& a, uint8_t out[EXECUTE_ROUTE_DATA_LEN]);

/**
 * The executor instruction for a cycle. Accounts: [signer, base ATA, intermediate ATAs (n-1)] then, per leg,
 * [DEX program, the swap's accounts in the program's order]; LegSpec.acct_start counts from the first leg
 * account (after the fixed ones). The wallet's ATAs are used for every token on the route.
 */
Result<ExecuteRouteArgs> execute_route_ix(IxList& l, const Pubkey& executor, const Pubkey& wallet, const engine::Cycle& c,
                                          std::span<const std::shared_ptr<const PoolState>> legs, uint64_t amount_in,
                                          uint64_t min_out);

/** ATA of `wallet` for `mint` under the mint's token program (classic SPL unless the snapshot says Token-2022). */
Pubkey wallet_ata(const Pubkey& wallet, const Pubkey& mint, const PoolState& p);

// ---- the arb transaction (spec §7.3) ----

struct RouteTxParams {
  Pubkey wallet{}, executor{}, tip_account{};
  uint64_t amount_in = 0, min_out = 0, tip_lamports = 0;
  uint32_t cu_limit = 0;
  uint64_t cu_price_micro_lamports = 0;  // 0 omits SetComputeUnitPrice (bundle path)
  const flash::FlashReserve* reserve = nullptr;  // null = own-capital mode (no borrow/repay)
};

struct RouteTxLayout {
  uint8_t borrow_index = 0xff, execute_index = 0xff, repay_index = 0xff, tip_index = 0xff;
};

/**
 * [CU limit][CU price?][flash borrow?][execute_route][flash repay?][tip transfer]. The repay's
 * borrow_instruction_index is the borrow's absolute position, computed here from the final layout.
 */
Result<RouteTxLayout> build_route_ixs(IxList& l, const RouteTxParams& p, const engine::Cycle& c,
                                      std::span<const std::shared_ptr<const PoolState>> legs);

// ---- address lookup tables (spec §8) ----

/** Lookup-table address: PDA [authority, recent_slot (u64 LE)] under the ALT program. */
Pubkey lookup_table_address(const Pubkey& authority, uint64_t recent_slot, uint8_t* bump = nullptr);
void create_lookup_table(IxList& l, const Pubkey& authority, const Pubkey& payer, uint64_t recent_slot, Pubkey* table_out);
void extend_lookup_table(IxList& l, const Pubkey& table, const Pubkey& authority, const Pubkey& payer,
                         std::span<const Pubkey> addresses);

/** A decoded lookup-table account (LookupTableMeta, 56 bytes, then the addresses). */
struct AltAccount {
  uint64_t deactivation_slot = UINT64_MAX;
  uint64_t last_extended_slot = 0;
  uint8_t last_extended_start = 0;
  bool has_authority = false;
  Pubkey authority{};
  std::vector<Pubkey> addresses;
};
Result<AltAccount> decode_lookup_table(const uint8_t* d, size_t n, const Pubkey& owner);

}  // namespace arb::tx
