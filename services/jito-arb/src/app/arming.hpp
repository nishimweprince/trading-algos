#pragma once
// Arming the live universe (docs/LIVE-READINESS.md Stage 3): the token accounts and lookup tables the armed pools
// need. Pure planning here; the arm-accounts tool reads chain state, prints the plan, and sends only on --send.
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "core/rpc.hpp"
#include "flash/kamino.hpp"
#include "state/state_store.hpp"
#include "tx/alt_manager.hpp"

namespace arb::app {

struct AtaNeed {
  Pubkey ata{}, mint{}, token_program{};
};

/** Every wallet token account the pools' routes use (both mints of every pool), deduplicated. */
std::vector<AtaNeed> needed_atas(const StateStore& store, const Pubkey& wallet);
/** Every key the routes should find in a lookup table: shared keys plus each pool's, deduplicated, wallet excluded. */
std::vector<Pubkey> needed_table_keys(const StateStore& store, const Pubkey& wallet, std::span<const flash::FlashReserve> reserves,
                                      const std::optional<Pubkey>& executor);
/** CreateIdempotent instructions, `per_tx` to a transaction. */
std::vector<tx::IxList> ata_txs(const std::vector<AtaNeed>& atas, const Pubkey& wallet, size_t per_tx = 5);
/** Prefix each transaction with a CU limit and price (so the setup transactions land without a tip). */
void add_compute_budget(std::vector<tx::IxList>& txs, uint32_t cu_limit, uint64_t cu_price_micro_lamports);
/** Programs a setup transaction may call; anything else is refused before signing. */
bool setup_programs_allowed(const tx::IxList& ixs);

/** {"tables":["<base58>",..]} */
std::string tables_to_json(const std::vector<Pubkey>& tables);
bool tables_from_json(const std::string& body, std::vector<Pubkey>& out, std::string* err = nullptr);
/**
 * Read the tables listed in `path` from chain into `alts` (decoded, owner-checked). Returns how many loaded;
 * a missing file loads none. Tables that no longer exist or are deactivated are skipped with a warning.
 */
size_t load_tables(Rpc& rpc, const std::string& path, tx::AltManager& alts, std::vector<Pubkey>* loaded = nullptr);

}  // namespace arb::app
