#pragma once
// PumpSwap buy / sell transactions, mirroring @pump-fun/pump-swap-sdk 1.20.0
// OfflinePumpAmm.buyInstructionsNoPool / sellInstructionsNoPool:
//   buy : [CU limit, CU price] ata(WSOL) transfer(max_quote) sync_native ata(base) buy close(WSOL) [tips]
//   sell: [CU limit, CU price] ata(WSOL) sell close(WSOL) [close(base) on a full exit] [tips]
// Every key except the amounts is derived before the trigger (SwapKeys), so
// building at trigger time is a ~1 µs compile plus one ed25519 signature.
#include <cstdint>

#include "core/types.hpp"
#include "sol/amm.hpp"
#include "sol/tx.hpp"

namespace pd {

/** Per-wallet constants. */
struct UserKeys {
  Pubkey user{};
  Pubkey wsol_ata{};                 // ata(user, WSOL, TOKEN)
  Pubkey volume_acc{};               // PumpSwap ["user_volume_accumulator", user]
  Pubkey volume_acc_wsol_ata{};      // ata(volume_acc, WSOL, TOKEN) — cashback coins
};

void derive_user_keys(const Pubkey& user, UserKeys& out);

/** Per-mint keys of the canonical pool a migration creates. */
struct SwapKeys {
  Pubkey mint{}, base_token_program{};
  Pubkey pool_authority{}, pool{}, pool_base_vault{}, pool_quote_vault{};
  Pubkey user_base_ata{};
  Pubkey coin_creator{}, coin_creator_vault_auth{}, coin_creator_vault_ata{};
  Pubkey pool_v2{};
  bool cashback = false, mayhem = false;
};

/** Canonical pool PDA chain from pump.fun mint + coin creator (SDK canonicalPumpPoolPda et al.). */
void derive_swap_keys(const UserKeys& u, const Pubkey& mint, const Pubkey& base_token_program,
                      const Pubkey& coin_creator, bool cashback, bool mayhem, SwapKeys& out);

/** Re-derive only the coin-creator half (the pool push disagreed with the curve's creator). */
void set_coin_creator(SwapKeys& k, const Pubkey& coin_creator);

struct Tip {
  Pubkey account{};
  uint64_t lamports = 0;
};

struct TxParams {
  uint32_t cu_limit = 0;      // 0 = omit
  uint64_t cu_price = 0;      // micro-lamports per CU; 0 = omit
  Tip tips[2];                // Sender tip, optional Jito tip
  uint8_t n_tips = 0;
  uint8_t fee_recipient_index = 0;  // rotates the protocol fee recipient (write-lock spread)
  uint8_t buyback_index = 0;
};

/** Append the PumpSwap `buy` instruction (+ remaining accounts). */
void pump_buy_ix(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
                 uint64_t base_out, uint64_t max_quote);
/** Append the PumpSwap `sell` instruction (+ remaining accounts). */
void pump_sell_ix(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
                  uint64_t base_in, uint64_t min_quote);

/** Full buy instruction list (see header). */
void buy_ixs(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
             uint64_t base_out, uint64_t max_quote);
/** Full sell instruction list; `close_base` also closes the emptied base ATA (rent back). */
void sell_ixs(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
              uint64_t base_in, uint64_t min_quote, bool close_base);

}  // namespace pd
