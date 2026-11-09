#include "tx/dex_ix.hpp"

#include <cstring>
#include <stdexcept>
#include <string>

#include "core/programs.hpp"
#include "state/discovery.hpp"

namespace arb::tx::dexix {

const Pubkey& raydium_authority() {
  static const Pubkey k = pda({std::string_view("amm authority")}, prog::RAYDIUM_V4);
  return k;
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
