// quote-probe: read one pool live by RPC (reads only) and quote a ladder of sizes in both directions,
// printing each result or the quoter's exact error. For diagnosing a pool the engine cannot quote.
//
//   quote-probe POOL [--rpc URL]
#include <cstdio>
#include <cstdlib>
#include <string>

#include "core/base58.hpp"
#include "core/crypto.hpp"
#include "core/rpc.hpp"
#include "dex/quoter.hpp"
#include "state/discovery.hpp"
#include "state/state_store.hpp"

using namespace arb;

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: quote-probe POOL [--rpc URL]\n");
    return 2;
  }
  const char* key = std::getenv("HELIUS_API_KEY");
  std::string url = key && *key ? std::string("https://mainnet.helius-rpc.com/?api-key=") + key : "https://api.mainnet-beta.solana.com";
  for (int i = 2; i + 1 < argc; i += 2)
    if (std::string(argv[i]) == "--rpc") url = argv[i + 1];
  crypto_init();
  Rpc rpc(url, 30000);
  Pubkey pk = parse_pubkey(argv[1]);
  auto a = rpc.account(pk);
  if (!a) {
    std::fprintf(stderr, "no account\n");
    return 1;
  }
  state::DiscoveryConfig dc;
  auto def = state::pool_def_from_account(state::RawAccount{pk, a->owner, a->data}, dc);
  if (!def) {
    std::fprintf(stderr, "not a supported pool\n");
    return 1;
  }
  StateStore store;
  uint32_t id = store.add_pool(*def);
  std::vector<Pubkey> keys = {def->address, def->vault_a, def->vault_b, def->mint_a, def->mint_b};
  for (auto& [s, k] : def->tick_arrays) keys.push_back(k);
  if (!is_zero(def->config)) keys.push_back(def->config);
  if (!is_zero(def->fee_config)) keys.push_back(def->fee_config);
  uint64_t slot = 0;
  auto accs = rpc.multiple_accounts_at(keys, slot);
  for (size_t i = 0; i < keys.size(); ++i) {
    AccountUpdate u;
    u.key = keys[i];
    u.slot = slot;
    if (!accs[i]) u.missing = true;
    else { u.owner = accs[i]->owner; u.data = accs[i]->data.data(); u.len = accs[i]->data.size(); }
    store.apply(u);
  }
  auto p = store.pool(id);
  std::printf("%s %s slot %llu complete=%d vaults a=%llu b=%llu\n", dex_name(p->kind), argv[1], (unsigned long long)slot, int(p->complete),
              (unsigned long long)p->vault_a_amount, (unsigned long long)p->vault_b_amount);
  if (p->kind == DexKind::Cpmm)
    std::printf("  cpmm status=%u fees owed %llu/%llu trade=%llu creator=%llu (%s, on %u) protocol=%llu fund=%llu\n", unsigned(p->cpmm.status),
                (unsigned long long)p->cpmm.fees_owed_0(), (unsigned long long)p->cpmm.fees_owed_1(),
                (unsigned long long)p->cpmm_cfg.trade_fee_rate, (unsigned long long)p->cpmm_cfg.creator_fee_rate,
                p->cpmm.enable_creator_fee ? "enabled" : "off", unsigned(p->cpmm.creator_fee_on),
                (unsigned long long)p->cpmm_cfg.protocol_fee_rate, (unsigned long long)p->cpmm_cfg.fund_fee_rate);
  if (p->kind == DexKind::Whirlpool) {
    std::printf("  liquidity=%s tick=%d spacing=%u fee=%u adaptive=%d\n", std::to_string(uint64_t(p->wp.liquidity)).c_str(),
                p->wp.tick_current_index, unsigned(p->wp.tick_spacing), unsigned(p->wp.fee_rate), int(p->wp.adaptive_fee()));
    for (uint8_t t = 0; t < p->n_ticks; ++t) {
      const TickArraySlot& ts = p->ticks[t];
      int inited = 0;
      if (ts.array)
        for (auto& tk : ts.array->ticks) inited += tk.initialized ? 1 : 0;
      std::printf("  tick array start %d state %s initialized ticks %d\n", ts.start,
                  ts.state == TickArraySlot::Loaded ? "loaded" : ts.state == TickArraySlot::Missing ? "missing" : "unknown", inited);
    }
  }
  dex::RaydiumV4Quoter rq;
  dex::WhirlpoolQuoter wq;
  dex::CpmmQuoter cq;
  dex::PumpSwapQuoter pq;
  const dex::IQuoter* quoters[] = {&rq, &wq, &cq, &pq};
  const dex::IQuoter* qp = nullptr;
  for (const dex::IQuoter* c : quoters)
    if (c->kind() == p->kind) qp = c;
  if (!qp) {
    std::fprintf(stderr, "no quoter for %s\n", dex_name(p->kind));
    return 1;
  }
  const dex::IQuoter& q = *qp;
  std::printf("  depth a>b %llu  b>a %llu\n", (unsigned long long)dex::input_depth(*p, true), (unsigned long long)dex::input_depth(*p, false));
  for (bool d : {true, false})
    for (uint64_t x = 1000; x <= 100'000'000'000'000ULL; x *= 10) {
      auto r = q.quote_exact_in(*p, d, x);
      if (r)
        std::printf("  %s in %llu -> out %llu (consumed %llu, boundary %d)\n", d ? "a>b" : "b>a", (unsigned long long)x,
                    (unsigned long long)r->amount_out, (unsigned long long)r->amount_in, int(r->crosses_boundary));
      else
        std::printf("  %s in %llu -> error %s: %s\n", d ? "a>b" : "b>a", (unsigned long long)x, errc_name(r.error().code), r.error().what);
    }
  return 0;
}
