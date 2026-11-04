// fixture-frames: a synthetic replay capture built from the committed mainnet fixtures.
//
//   fixture-frames FIXTURE_DIR OUT_FRAMES OUT_POOLS
//
// Every Raydium v4 fixture pool gets a synthetic twin: its account bytes with the two vault keys
// rewritten to twin vaults (copies of the real vault accounts), so the pool and its twin form a 2-hop
// cycle. All accounts are pushed at slot 1000 (day 1, 2026-11-01 UTC); then each twin gets one injected
// price gap: twin pool and vaults re-pushed at a fresh slot with one vault 3% heavier, followed a slot
// later by the original bytes. The second
// half of the injections are stamped a day later, so the report shows two days. OUT_POOLS is the
// matching pools file (discovery's format). The capture is synthetic: it exercises the replay path and
// the report, and says nothing about how many arbs mainnet offers (that is the VM replay).
#include <simdjson.h>

#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <set>
#include <sstream>

#include "app/pools_file.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/programs.hpp"
#include "feed/frames.hpp"
#include "feed/update.hpp"
#include "state/discovery.hpp"

using namespace arb;

namespace {

struct Acc {
  Pubkey owner{};
  std::vector<uint8_t> data;
  uint64_t lamports = 0;
  bool exists = false;
};

std::string slurp(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 4) {
    std::fprintf(stderr, "usage: fixture-frames FIXTURE_DIR OUT_FRAMES OUT_POOLS\n");
    return 2;
  }
  std::string dir = argv[1];
  std::map<Pubkey, Acc> accs;
  {
    std::string body = slurp(dir + "/accounts.json");
    simdjson::dom::parser p;
    simdjson::padded_string ps(body);
    simdjson::dom::object o = p.parse(ps)["accounts"];
    for (auto kv : o) {
      Pubkey k;
      if (!b58_decode32(kv.key, k)) continue;
      Acc a;
      if (!kv.value.is_null()) {
        a.exists = true;
        b58_decode32(std::string_view(kv.value["owner"]), a.owner);
        b64_decode(std::string_view(kv.value["data"]), a.data);
        uint64_t l = 0;
        if (kv.value["lamports"].get(l) == simdjson::SUCCESS) a.lamports = l;
      }
      accs[k] = std::move(a);
    }
  }
  app::PoolsFile pf;
  state::DiscoveryConfig dc;
  dc.base_mints = {mint::WSOL, mint::USDC};
  {
    std::string body = slurp(dir + "/pools.json");
    simdjson::dom::parser p;
    simdjson::padded_string ps(body);
    simdjson::dom::element root = p.parse(ps);
    for (const char* list : {"raydium_v4", "whirlpool"}) {
      simdjson::dom::array a = root[list];
      for (auto e : a) {
        Pubkey k = parse_pubkey(std::string_view(e));
        auto it = accs.find(k);
        if (it == accs.end() || !it->second.exists) continue;
        state::RawAccount raw{k, it->second.owner, it->second.data};
        auto d = state::pool_def_from_account(raw, dc);
        if (!d) continue;
        pf.pools.push_back(*d);
        pf.tvl_sol.push_back(1.0);
      }
    }
    simdjson::dom::array rs = root["kamino_reserves"];
    for (auto e : rs) pf.reserves.push_back(parse_pubkey(std::string_view(e)));
  }
  // Twins: key = original with the last byte flipped (not a real account; synthetic only).
  auto twin_key = [](Pubkey k) {
    k[31] ^= 0x5A;
    return k;
  };
  auto replace_key = [](std::vector<uint8_t>& data, const Pubkey& from, const Pubkey& to) {
    for (size_t i = 0; i + 32 <= data.size(); ++i)
      if (std::memcmp(data.data() + i, from.data(), 32) == 0) std::memcpy(data.data() + i, to.data(), 32);
  };
  std::vector<uint32_t> targets, originals;
  size_t n_orig = pf.pools.size();
  for (size_t i = 0; i < n_orig; ++i) {
    PoolDef d = pf.pools[i];
    if (d.kind != DexKind::RaydiumV4 || !accs[d.vault_a].exists || !accs[d.vault_b].exists) continue;
    PoolDef t = d;
    t.address = twin_key(d.address);
    t.vault_a = twin_key(d.vault_a);
    t.vault_b = twin_key(d.vault_b);
    Acc pool = accs[d.address];
    replace_key(pool.data, d.vault_a, t.vault_a);
    replace_key(pool.data, d.vault_b, t.vault_b);
    accs[t.address] = pool;
    accs[t.vault_a] = accs[d.vault_a];
    accs[t.vault_b] = accs[d.vault_b];
    state::RawAccount raw{t.address, pool.owner, pool.data};
    auto check = state::pool_def_from_account(raw, dc);
    if (!check || check->vault_a != t.vault_a || check->vault_b != t.vault_b) continue;  // layout surprise: skip
    targets.push_back(uint32_t(pf.pools.size()));
    originals.push_back(uint32_t(i));
    pf.pools.push_back(*check);
    pf.pools.back().address = t.address;
    pf.tvl_sol.push_back(1.0);
  }
  std::set<Pubkey> keys;
  for (auto& d : pf.pools) {
    for (const Pubkey& k : {d.address, d.vault_a, d.vault_b, d.mint_a, d.mint_b}) keys.insert(k);
    for (auto& [s, k] : d.tick_arrays) keys.insert(k);
  }
  for (auto& r : pf.reserves) keys.insert(r);

  feed::FrameWriter w("", 0);
  if (!w.open_path(argv[2])) {
    std::fprintf(stderr, "cannot write %s\n", argv[2]);
    return 1;
  }
  const int64_t day1 = 1'793'491'200'000LL;  // 2026-11-01T00:00:00Z
  uint64_t wv = 1, ns = 1;
  auto push = [&](const Pubkey& k, const std::vector<uint8_t>& data, const Acc& a, uint64_t slot, int64_t wall) {
    std::string f = feed::encode_account_frame(k, a.owner, data.data(), data.size(), a.lamports, slot, wv++);
    w.write(reinterpret_cast<const uint8_t*>(f.data()), uint32_t(f.size()), ns++, wall);
  };
  auto slot_frame = [&](uint64_t slot, int64_t wall) {
    std::string s = feed::encode_slot_frame(slot);
    w.write(reinterpret_cast<const uint8_t*>(s.data()), uint32_t(s.size()), ns++, wall);
  };
  slot_frame(1000, day1);
  for (const Pubkey& k : keys) {
    auto it = accs.find(k);
    if (it == accs.end() || !it->second.exists) continue;
    push(k, it->second.data, it->second, 1000, day1);
  }
  uint64_t slot = 1001;
  size_t injected = 0;
  for (size_t t = 0; t < targets.size(); ++t) {
    const PoolDef& d = pf.pools[targets[t]];
    int64_t wall = (t < (targets.size() + 1) / 2 ? day1 : day1 + 86'400'000LL) + int64_t(slot) * 400;
    // Heavier side alternates, so both cycle directions get a gap.
    const Pubkey& heavy = t % 2 == 0 ? d.vault_b : d.vault_a;
    slot_frame(slot, wall);
    for (const Pubkey& k : {d.address, d.vault_a, d.vault_b}) {
      const Acc& a = accs[k];
      std::vector<uint8_t> data = a.data;
      if (k == heavy && data.size() >= 72) {
        uint64_t amt;
        std::memcpy(&amt, data.data() + 64, 8);
        amt += amt / 100 * 3;
        std::memcpy(data.data() + 64, &amt, 8);
      }
      push(k, data, a, slot, wall);
    }
    // The original pool is re-pushed unchanged, as a busy pool would be, so it is never lag-stale.
    const PoolDef& o = pf.pools[originals[t]];
    for (const Pubkey& k : {o.address, o.vault_a, o.vault_b}) push(k, accs[k].data, accs[k], slot, wall);
    ++slot;
    slot_frame(slot, wall + 400);
    for (const Pubkey& k : {d.address, d.vault_a, d.vault_b}) push(k, accs[k].data, accs[k], slot, wall + 400);
    slot += 4;  // past the cooldown, so every injection is evaluated on its own
    ++injected;
  }
  w.close();
  std::ofstream(argv[3]) << app::to_json(pf);
  std::printf("fixture-frames: %zu pools, %zu accounts, %zu injected gaps, last slot %llu\n", pf.pools.size(), keys.size(), injected,
              (unsigned long long)slot);
  return 0;
}
