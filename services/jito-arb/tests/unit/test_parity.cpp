// Phase 2: quoter parity, replayed offline from tests/fixtures/parity_<dex>.json (written by harness/sim-parity).
// Every kept sample's simulation ran on exactly the stored state (bracket unchanged and vault conservation
// verified). Requirement: 100% exact amount_out (and amount_in), and a failed simulation must not quote as
// a valid, in-window swap. Nothing here is relaxed to make a DEX pass; a failing DEX stays disabled.
#include "core/config.hpp"
#include "core/programs.hpp"
#include "dex/quoter.hpp"
#include "fixture.hpp"
#include "state/discovery.hpp"
#include "test.hpp"

using namespace arb;
using namespace arbtest;

namespace {

struct ParityResult {
  size_t batches = 0, ok_samples = 0, failed_sims = 0, mismatches = 0;
};

ParityResult replay(const char* file, const dex::IQuoter& q) {
  ParityResult r;
  std::string body = read_file(fixture_path(file));
  if (body.empty()) return r;
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::array batches = p.parse(ps)["batches"];
  for (auto b : batches) {
    std::map<std::string, state::RawAccount> accs;
    simdjson::dom::object ao = b["accounts"];
    std::vector<std::pair<Pubkey, std::optional<state::RawAccount>>> list;
    for (auto kv : ao) {
      Pubkey k;
      b58_decode32(kv.key, k);
      if (kv.value.is_null()) { list.emplace_back(k, std::nullopt); continue; }
      state::RawAccount a;
      a.key = k;
      b58_decode32(std::string_view(kv.value["owner"]), a.owner);
      b64_decode(std::string_view(kv.value["data"]), a.data);
      list.emplace_back(k, a);
    }
    Pubkey pool;
    b58_decode32(std::string_view(b["pool"]), pool);
    const state::RawAccount* pa = nullptr;
    for (auto& [k, a] : list)
      if (k == pool && a) pa = &*a;
    CHECK(pa != nullptr);
    if (!pa) continue;
    state::DiscoveryConfig dc;
    auto def = state::pool_def_from_account(*pa, dc);
    CHECK(def.has_value());
    if (!def) continue;
    StateStore store;
    uint32_t id = store.add_pool(*def);
    for (auto& [k, a] : list) {
      AccountUpdate u;
      u.key = k;
      u.slot = 1;
      if (!a) u.missing = true;
      else { u.owner = a->owner; u.data = a->data.data(); u.len = a->data.size(); }
      store.apply(u);
    }
    auto st = store.pool(id);
    CHECK(st->complete);
    ++r.batches;
    simdjson::dom::array samples = b["samples"];
    for (auto s : samples) {
      bool a_to_b = bool(s["a_to_b"]);
      uint64_t amount = uint64_t(s["amount_in"]);
      bool sim_ok = bool(s["sim_ok"]);
      auto quote = q.quote_exact_in(*st, a_to_b, amount);
      if (sim_ok) {
        ++r.ok_samples;
        uint64_t sim_out = uint64_t(s["sim_out"]), sim_in = uint64_t(s["sim_in"]);
        bool good = quote && !quote->crosses_boundary && quote->amount_out == sim_out && quote->amount_in == sim_in;
        if (!good) {
          ++r.mismatches;
          std::fprintf(stderr, "    parity %s pool=%s a_to_b=%d in=%llu sim_out=%llu quote_out=%llu\n", file,
                       b58(pool).c_str(), int(a_to_b), (unsigned long long)amount, (unsigned long long)sim_out,
                       (unsigned long long)(quote ? quote->amount_out : 0));
        }
      } else {
        ++r.failed_sims;
        // The program rejected it: the quoter must not report a valid in-window swap.
        bool consistent = !quote || quote->crosses_boundary;
        if (!consistent) ++r.mismatches;
      }
    }
  }
  return r;
}

// A DEX may be enabled only with >= 200 exact samples (plan: "if any parity mismatch remains, keep the DEX
// disabled"; an unproven DEX is treated the same). Mismatches in kept samples always fail.
void require_parity_or_disabled(const char* dex, const ParityResult& r, bool enabled_default, const char* config_key) {
  CHECK_EQ(r.mismatches, size_t(0));
  if (r.ok_samples >= 200) return;
  std::printf("    %s parity UNPROVEN (%zu samples < 200): must be disabled\n", dex, r.ok_samples);
  CHECK(!enabled_default);
  std::string example = read_file(std::string(ARB_SOURCE_DIR) + "/config/bot.example.toml");
  CHECK(example.find(std::string(config_key) + " = false") != std::string::npos);
}

}  // namespace

TEST(parity_raydium_v4_exact) {
  dex::RaydiumV4Quoter q;
  auto r = replay("parity_raydium_v4.json", q);
  std::printf("    raydium_v4 parity: %zu batches, %zu exact-compare samples, %zu failed sims, %zu mismatches\n", r.batches,
              r.ok_samples, r.failed_sims, r.mismatches);
  require_parity_or_disabled("raydium_v4", r, Config{}.dex.raydium_v4_enabled, "raydium_v4_enabled");
}

TEST(parity_whirlpool_exact) {
  dex::WhirlpoolQuoter q;
  auto r = replay("parity_whirlpool.json", q);
  std::printf("    whirlpool parity: %zu batches, %zu exact-compare samples, %zu failed sims, %zu mismatches\n", r.batches,
              r.ok_samples, r.failed_sims, r.mismatches);
  require_parity_or_disabled("whirlpool", r, Config{}.dex.whirlpool_enabled, "whirlpool_enabled");
}

TEST(parity_cpmm_exact) {
  dex::CpmmQuoter q;
  auto r = replay("parity_cpmm.json", q);
  std::printf("    cpmm parity: %zu batches, %zu exact-compare samples, %zu failed sims, %zu mismatches\n", r.batches,
              r.ok_samples, r.failed_sims, r.mismatches);
  require_parity_or_disabled("cpmm", r, Config{}.dex.cpmm_enabled, "cpmm_enabled");
}

TEST(parity_pumpswap_exact) {
  dex::PumpSwapQuoter q;
  auto r = replay("parity_pumpswap.json", q);
  std::printf("    pumpswap parity: %zu batches, %zu exact-compare samples, %zu failed sims, %zu mismatches\n", r.batches,
              r.ok_samples, r.failed_sims, r.mismatches);
  require_parity_or_disabled("pumpswap", r, Config{}.dex.pumpswap_enabled, "pumpswap_enabled");
}

TEST(parity_clmm_exact) {
  dex::ClmmQuoter q;
  auto r = replay("parity_clmm.json", q);
  std::printf("    clmm parity: %zu batches, %zu exact-compare samples, %zu failed sims, %zu mismatches\n", r.batches,
              r.ok_samples, r.failed_sims, r.mismatches);
  require_parity_or_disabled("clmm", r, Config{}.dex.clmm_enabled, "clmm_enabled");
}
