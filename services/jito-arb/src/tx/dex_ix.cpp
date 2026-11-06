#include "tx/dex_ix.hpp"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <string>

#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "dex/clmm_math.hpp"
#include "state/discovery.hpp"

namespace arb::tx::dexix {

const Pubkey& raydium_authority() {
  static const Pubkey k = pda({std::string_view("amm authority")}, prog::RAYDIUM_V4);
  return k;
}

const Pubkey& cpmm_authority() {
  static const Pubkey k = pda({std::string_view("vault_and_lp_mint_auth_seed")}, prog::RAYDIUM_CPMM);
  return k;
}

const Pubkey& pump_global_config() {
  static const Pubkey k = pda({std::string_view("global_config")}, prog::PUMPSWAP);
  return k;
}
const Pubkey& pump_fee_config() {
  static const Pubkey k = pda({std::string_view("fee_config"), prog::PUMPSWAP}, prog::PUMP_FEES);
  return k;
}
const Pubkey& pump_event_authority() {
  static const Pubkey k = pda({std::string_view("__event_authority")}, prog::PUMPSWAP);
  return k;
}
Pubkey pump_user_volume_accumulator(const Pubkey& user) {
  return pda({std::string_view("user_volume_accumulator"), user}, prog::PUMPSWAP);
}

Pubkey whirlpool_oracle(const Pubkey& whirlpool) { return pda({std::string_view("oracle"), whirlpool}, prog::WHIRLPOOL); }

std::array<Pubkey, 3> whirlpool_tick_arrays(const state::Whirlpool& w, const Pubkey& pool, bool a_to_b) {
  int32_t span = state::TICK_ARRAY_SIZE * int32_t(w.tick_spacing);
  int32_t base = state::tick_array_start(w.tick_current_index, w.tick_spacing);
  int offs[3] = {0, -1, -2};
  if (!a_to_b) {
    bool shifted = w.tick_current_index + int32_t(w.tick_spacing) >= base + span;
    for (int i = 0; i < 3; ++i) offs[i] = shifted ? i + 1 : i;
  }
  std::array<Pubkey, 3> out{};
  int n = 0;
  for (int o : offs) {
    int32_t start = base + o * span;
    if (start > state::MAX_TICK_INDEX || start + span <= state::MIN_TICK_INDEX - span) continue;
    out[size_t(n++)] = state::tick_array_pda(pool, start);
  }
  for (int i = n; i < 3; ++i) out[size_t(i)] = out[size_t(n > 0 ? n - 1 : 0)];  // pad with the last valid array
  return out;
}

const Pubkey& program_of(DexKind kind) {
  const Pubkey* p = dex_program(kind);
  if (!p) throw std::invalid_argument(std::string("swap: unsupported dex ") + dex_name(kind));
  return *p;
}

std::vector<AccountMeta> swap_accounts(const PoolState& p, bool a_to_b, const UserAccounts& u) {
  std::vector<AccountMeta> v;
  if (p.kind != DexKind::RaydiumV4 && p.kind != DexKind::Whirlpool && p.kind != DexKind::Cpmm && p.kind != DexKind::PumpSwap &&
      p.kind != DexKind::Clmm)
    throw std::invalid_argument(std::string("swap accounts: unsupported dex ") + dex_name(p.kind));
  if (p.kind == DexKind::Clmm) {
    // swap_v2: payer (signer), amm_config, pool_state (w), input token account (w), output token account (w),
    // input vault (w), output vault (w), observation_state (w), token_program, token_program_2022, memo_program,
    // input vault mint, output vault mint; then the tick arrays the swap walks (remaining accounts), starting at
    // the first initialized array in the swap's direction. Token "a" is token_0.
    v.push_back({u.authority, true, false});
    v.push_back({p.clmm.amm_config, false, false});
    v.push_back({p.address, false, true});
    v.push_back({a_to_b ? u.token_a : u.token_b, false, true});
    v.push_back({a_to_b ? u.token_b : u.token_a, false, true});
    v.push_back({a_to_b ? p.vault_a : p.vault_b, false, true});
    v.push_back({a_to_b ? p.vault_b : p.vault_a, false, true});
    v.push_back({p.clmm.observation_key, false, true});
    v.push_back({prog::TOKEN, false, false});
    v.push_back({prog::TOKEN_2022, false, false});
    v.push_back({prog::MEMO, false, false});
    v.push_back({a_to_b ? p.mint_a : p.mint_b, false, false});
    v.push_back({a_to_b ? p.mint_b : p.mint_a, false, false});
    if (!p.clmm.tick_spacing || !p.mint_a_info || !p.mint_b_info || !state::clmm_mint_supported(*p.mint_a_info) ||
        !state::clmm_mint_supported(*p.mint_b_info) || p.clmm.dynamic_fee)
      throw std::invalid_argument("CLMM unsupported mint extensions or dynamic fee");
    if (p.clmm_bitmap) {
      if (p.clmm_bitmap->pool != p.address) throw std::invalid_argument("CLMM bitmap pool mismatch");
      v.push_back({state::clmm_bitmap_extension_pda(p.address), false, true});
    }
    auto start = dex::clmm::initialized_array(p.clmm, p.clmm_bitmap.get(),
        dex::clmm::array_start_index(p.clmm.tick_current, p.clmm.tick_spacing), a_to_b, true);
    if (!start) throw std::invalid_argument("CLMM first initialized array unavailable");
    for (int k = 0; k < MAX_TICK_ARRAYS; ++k) {
      const TickArraySlot* slot = p.tick_slot(*start);
      if (!slot || slot->state != TickArraySlot::Loaded || !slot->clmm || slot->clmm->pool != p.address ||
          slot->key != state::clmm_tick_array_pda(p.address, *start)) {
        if (!k) throw std::invalid_argument("CLMM first array not loaded");
        break;
      }
      v.push_back({slot->key, false, true});
      start = dex::clmm::initialized_array(p.clmm, p.clmm_bitmap.get(), *start, a_to_b);
      if (!start) break;
    }
    return v;
  }
  if (p.kind == DexKind::PumpSwap) {
    // sell_v2 / buy_exact_quote_in_v2 (PUMP_SWAP_TRADE_V2.md): 17 accounts, the same for both. Token "a" is base.
    auto tp = [](const std::shared_ptr<const state::Mint>& m) {
      return m && m->token_program == prog::TOKEN_2022 ? prog::TOKEN_2022 : prog::TOKEN;
    };
    const Pubkey base_tp = tp(p.mint_a_info), quote_tp = tp(p.mint_b_info);
    v.push_back({p.address, false, true});
    v.push_back({u.authority, true, true});
    v.push_back({pump_global_config(), false, false});
    v.push_back({p.mint_a, false, false});
    v.push_back({p.mint_b, false, false});
    v.push_back({u.token_a, false, true});
    v.push_back({u.token_b, false, true});
    v.push_back({p.vault_a, false, true});
    v.push_back({p.vault_b, false, true});
    v.push_back({base_tp, false, false});
    v.push_back({quote_tp, false, false});
    v.push_back({prog::SYSTEM, false, false});
    v.push_back({pump_user_volume_accumulator(u.authority), false, true});
    v.push_back({pump_fee_config(), false, false});
    // One of the 8 buyback fee recipients' quote-mint ATAs (must exist; it does for wSOL and USDC).
    v.push_back({ata(p.pump_cfg.buyback_fee_recipients[0], p.mint_b, quote_tp), false, true});
    v.push_back({pump_event_authority(), false, false});
    v.push_back({prog::PUMPSWAP, false, false});
    (void)a_to_b;  // same accounts both ways; the instruction (sell vs buy) carries the direction
    return v;
  }
  if (p.kind == DexKind::Cpmm) {
    // payer (signer), authority, amm_config, pool_state (w), input token account (w), output token account (w),
    // input vault (w), output vault (w), input token program, output token program, input mint, output mint,
    // observation_state (w). Token "a" is token_0.
    const Pubkey& in_vault = a_to_b ? p.vault_a : p.vault_b;
    const Pubkey& out_vault = a_to_b ? p.vault_b : p.vault_a;
    const Pubkey& in_prog = a_to_b ? p.cpmm.token_0_program : p.cpmm.token_1_program;
    const Pubkey& out_prog = a_to_b ? p.cpmm.token_1_program : p.cpmm.token_0_program;
    v.push_back({u.authority, true, false});
    v.push_back({cpmm_authority(), false, false});
    v.push_back({p.cpmm.amm_config, false, false});
    v.push_back({p.address, false, true});
    v.push_back({a_to_b ? u.token_a : u.token_b, false, true});
    v.push_back({a_to_b ? u.token_b : u.token_a, false, true});
    v.push_back({in_vault, false, true});
    v.push_back({out_vault, false, true});
    v.push_back({in_prog, false, false});
    v.push_back({out_prog, false, false});
    v.push_back({a_to_b ? p.mint_a : p.mint_b, false, false});
    v.push_back({a_to_b ? p.mint_b : p.mint_a, false, false});
    v.push_back({p.cpmm.observation_key, false, true});
    return v;
  }
  if (p.kind == DexKind::RaydiumV4) {
    // 0 token program, 1 amm (w), 2 authority, 3 coin vault (w), 4 pc vault (w), 5 user source (w),
    // 6 user destination (w), 7 user owner (signer)
    v.push_back({prog::TOKEN, false, false});
    v.push_back({p.address, false, true});
    v.push_back({raydium_authority(), false, false});
    v.push_back({p.vault_a, false, true});
    v.push_back({p.vault_b, false, true});
    v.push_back({a_to_b ? u.token_a : u.token_b, false, true});
    v.push_back({a_to_b ? u.token_b : u.token_a, false, true});
    v.push_back({u.authority, true, false});
    return v;
  }
  // New kinds must never fall through to Whirlpool.
  if (p.kind != DexKind::Whirlpool)
    throw std::invalid_argument(std::string("swap accounts: unsupported dex ") + dex_name(p.kind));
  // token_program, token_authority (signer), whirlpool (w), token_owner_account_a (w), token_vault_a (w),
  // token_owner_account_b (w), token_vault_b (w), tick_array_0..2 (w), oracle
  auto ta = whirlpool_tick_arrays(p.wp, p.address, a_to_b);
  v.push_back({prog::TOKEN, false, false});
  v.push_back({u.authority, true, false});
  v.push_back({p.address, false, true});
  v.push_back({u.token_a, false, true});
  v.push_back({p.vault_a, false, true});
  v.push_back({u.token_b, false, true});
  v.push_back({p.vault_b, false, true});
  for (auto& k : ta) v.push_back({k, false, true});
  v.push_back({whirlpool_oracle(p.address), false, false});
  return v;
}

std::vector<uint8_t> swap_data(DexKind kind, bool a_to_b, uint64_t amount_in, uint64_t min_out) {
  // Filled in a fixed buffer (GCC 14 reports a false -Wstringop-overflow on vector::insert after push_back).
  uint8_t d[42];
  size_t n = 0;
  auto put = [&](const void* p, size_t len) {
    std::memcpy(d + n, p, len);
    n += len;
  };
  if (kind != DexKind::RaydiumV4 && kind != DexKind::Whirlpool && kind != DexKind::Cpmm && kind != DexKind::PumpSwap &&
      kind != DexKind::Clmm)
    throw std::invalid_argument(std::string("swap data: unsupported dex ") + dex_name(kind));
  if (kind == DexKind::Clmm) {  // swap_v2(amount, other_amount_threshold, sqrt_price_limit_x64 = 0, is_base_input = true)
    put(CLMM_SWAP_V2_DISC, 8);
    put(&amount_in, 8);
    put(&min_out, 8);
    unsigned __int128 no_limit = 0;
    put(&no_limit, 16);
    d[n++] = 1;
    return std::vector<uint8_t>(d, d + n);
  }
  if (kind == DexKind::PumpSwap) {
    // a_to_b: sell_v2(base_amount_in, min_quote_amount_out); b_to_a: buy_exact_quote_in_v2(spendable_quote_in,
    // min_base_amount_out), whose minimum must be > 0.
    put(a_to_b ? PUMP_SELL_V2_DISC : PUMP_BUY_EXACT_QUOTE_IN_V2_DISC, 8);
    uint64_t floor_out = a_to_b ? min_out : std::max<uint64_t>(min_out, 1);
    put(&amount_in, 8);
    put(&floor_out, 8);
    return std::vector<uint8_t>(d, d + n);
  }
  if (kind == DexKind::Cpmm) {  // swap_base_input(amount_in, minimum_amount_out)
    put(CPMM_SWAP_BASE_INPUT_DISC, 8);
    put(&amount_in, 8);
    put(&min_out, 8);
    return std::vector<uint8_t>(d, d + n);
  }
  if (kind == DexKind::RaydiumV4) {
    d[n++] = RAYDIUM_SWAP_BASE_IN_V2;
    put(&amount_in, 8);
    put(&min_out, 8);
    return std::vector<uint8_t>(d, d + n);
  }
  if (kind != DexKind::Whirlpool)
    throw std::invalid_argument(std::string("swap data: unsupported dex ") + dex_name(kind));
  // swap(amount, other_amount_threshold, sqrt_price_limit, amount_specified_is_input, a_to_b)
  put(WHIRLPOOL_SWAP_DISC, 8);
  put(&amount_in, 8);
  put(&min_out, 8);
  unsigned __int128 no_limit = 0;  // NO_EXPLICIT_SQRT_PRICE_LIMIT
  put(&no_limit, 16);
  d[n++] = 1;
  d[n++] = a_to_b ? 1 : 0;
  return std::vector<uint8_t>(d, d + n);
}

void swap(IxList& l, const PoolState& p, bool a_to_b, const UserAccounts& u, uint64_t amount_in, uint64_t min_out) {
  Ix& ix = l.add(program_of(p.kind));
  for (auto& m : swap_accounts(p, a_to_b, u)) ix.acc(m.key, m.signer, m.writable);
  auto d = swap_data(p.kind, a_to_b, amount_in, min_out);
  ix.bytes(d.data(), d.size());
}

}  // namespace arb::tx::dexix
