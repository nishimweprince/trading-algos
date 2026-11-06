// sim-parity: the laptop's quoter-parity oracle (the official DEX SDKs are not installed here).
//
// For each fixture pool, at several sizes in both directions, build the direct DEX swap the executor
// would CPI, send it to simulateTransaction (sigVerify:false, replaceRecentBlockhash:true, nothing is
// ever sent to the cluster) from accounts that already hold the input token, and read the output token
// account's post-simulation balance. getMultipleAccounts brackets each batch; a batch is kept only when
// the pool, both vaults and the tick arrays are byte-identical before and after, so the simulation ran
// on exactly the state the quoter saw. Kept batches (account bytes + simulated amounts) are written to
// tests/fixtures/parity_<dex>.json; the unit test replays them offline and requires exact equality.
//
//   sim-parity [--rpc URL] [--dex raydium_v4|whirlpool|cpmm|all] [--min-samples N] [--max-pools N]
//              [--seed-holders harness/holders_seed.json] [--pools-file pools.json]
// --pools-file takes the pools of that kind from a discovery output (deepest first) instead of
// tests/fixtures/pools.json, for DEXes the fixture list does not cover yet.
//
// Funding: holders' own wallets (see holders()). Rate: one request every 280 ms (~3.5 req/s), exponential backoff on 429.
#include <simdjson.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <algorithm>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/compact_u16.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "dex/quoter.hpp"
#include "state/discovery.hpp"
#include "state/state_store.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;

namespace {

std::string g_rpc_url;
std::unique_ptr<Rpc> g_rpc;
auto g_last = std::chrono::steady_clock::now();
uint64_t g_calls = 0;

template <class F>
auto rpc_call(F&& f) -> decltype(f(*g_rpc)) {
  for (int attempt = 0;; ++attempt) {
    auto wait = g_last + std::chrono::milliseconds(280) - std::chrono::steady_clock::now();
    if (wait.count() > 0) std::this_thread::sleep_for(wait);
    g_last = std::chrono::steady_clock::now();
    ++g_calls;
    try {
      return f(*g_rpc);
    } catch (const std::exception& e) {
      std::string m = e.what();
      // JSON-RPC errors (invalid request, a method the node refuses) and auth failures won't change on retry.
      if (attempt >= 6 || m.rfind("rpc error:", 0) == 0 || m.find("http 401") != std::string::npos ||
          m.find("http 400") != std::string::npos)
        throw;
      bool limited = m.find("429") != std::string::npos;
      std::fprintf(stderr, "  rpc retry (%s)\n", m.substr(0, 120).c_str());
      std::this_thread::sleep_for(std::chrono::milliseconds(limited ? 2000 << attempt : 500 << attempt));
      g_rpc = std::make_unique<Rpc>(g_rpc_url, 30000);
    }
  }
}

struct Holder {
  Pubkey account{}, owner{};
  uint64_t amount = 0;
};

// Funding for simulations (plan: "a funded mainnet wallet that already holds both tokens"): the
// largest holders of each mint from getTokenLargestAccounts, kept only when the token account's owner is
// an ordinary system-owned wallet with SOL for fees. That wallet is the fee payer and swap authority of
// its own simulated swap, paying into its own ATA (created idempotently inside the simulation).
// sigVerify:false means no signature is produced; nothing is ever sent. Program-owned accounts (pool
// vaults, PDAs) are never used as funding or as signers.
std::map<Pubkey, std::vector<Holder>> g_holders;  // mint -> wallet-owned token accounts, largest first
std::map<Pubkey, std::vector<Pubkey>> g_seed;     // --seed-holders: known token accounts per mint (re-validated live)

const std::vector<Holder>& holders(const Pubkey& mint) {
  auto it = g_holders.find(mint);
  if (it != g_holders.end()) return it->second;
  std::vector<Holder> out;
  uint64_t slot = 0;
  std::vector<std::pair<Pubkey, uint64_t>> largest;
  if (auto sd = g_seed.find(mint); sd != g_seed.end()) {
    for (const Pubkey& k : sd->second) largest.emplace_back(k, 0);
  } else try {
    largest = rpc_call([&](Rpc& r) { return r.token_largest_accounts(mint, slot); });
  } catch (const std::exception&) {
    // Very large mints (wSOL, USDC): getTokenLargestAccounts is refused. Helius DAS getTokenAccounts
    // (one page of 1000 accounts for the mint) is a filtered read; keep its 20 largest. The public RPC
    // has no DAS: that direction then goes unfunded and is skipped.
    if (g_rpc_url.find("helius") == std::string::npos) {
      std::fprintf(stderr, "  no holders for %s on this RPC (needs Helius DAS); its swaps are skipped\n", b58(mint).c_str());
      return g_holders[mint] = {};
    }
    std::string body = rpc_call([&](Rpc& r) {
      return r.call("getTokenAccounts", "{\"mint\":\"" + b58(mint) + "\",\"limit\":1000}");
    });
    simdjson::dom::parser p;
    simdjson::padded_string ps(body);
    simdjson::dom::array arr;
    if (p.parse(ps)["result"]["token_accounts"].get(arr) == simdjson::SUCCESS) {
      for (auto e : arr) {
        std::string_view a;
        uint64_t amt = 0;
        Pubkey k;
        if (e["address"].get(a) == simdjson::SUCCESS && b58_decode32(a, k) && e["amount"].get(amt) == simdjson::SUCCESS)
          largest.emplace_back(k, amt);
      }
    }
    std::sort(largest.begin(), largest.end(), [](auto& x, auto& y) { return x.second > y.second; });
    if (largest.size() > 20) largest.resize(20);
  }
  std::vector<Pubkey> keys;
  for (auto& [k, amt] : largest) keys.push_back(k);
  std::vector<Holder> cands;
  if (!keys.empty()) {
    auto accs = rpc_call([&](Rpc& r) { return r.multiple_accounts(keys); });
    for (size_t i = 0; i < keys.size() && i < accs.size(); ++i) {
      if (!accs[i] || accs[i]->owner != prog::TOKEN) continue;
      auto t = state::decode_token_account(accs[i]->data.data(), accs[i]->data.size(), accs[i]->owner);
      if (!t || t->state != 1 || t->amount == 0 || t->has_delegate) continue;
      cands.push_back(Holder{keys[i], t->owner, t->amount});
    }
  }
  if (!cands.empty()) {
    std::vector<Pubkey> owners;
    for (auto& h : cands) owners.push_back(h.owner);
    auto oa = rpc_call([&](Rpc& r) { return r.multiple_accounts(owners); });
    for (size_t i = 0; i < cands.size() && i < oa.size(); ++i)
      if (oa[i] && oa[i]->owner == prog::SYSTEM && oa[i]->data.empty() && oa[i]->lamports >= 50'000'000ULL &&
          is_on_curve(cands[i].owner.data()))
        out.push_back(cands[i]);
  }
  return g_holders[mint] = out;
}

/** Unsigned v0 transaction: shortvec(n) + n zero signatures + message (sigVerify:false). */
std::string unsigned_tx_b64(const Pubkey& payer, const tx::IxList& ixs) {
  uint8_t msg[4096];
  Hash bh{};
  auto m = tx::compile_message_v0(payer, ixs, bh, {}, msg, sizeof msg);
  if (!m) return "";
  std::vector<uint8_t> wire;
  uint8_t sv[3];
  size_t n = compact_u16_encode(msg[1], sv);
  wire.insert(wire.end(), sv, sv + n);
  wire.insert(wire.end(), size_t(msg[1]) * 64, 0);
  wire.insert(wire.end(), msg, msg + *m);
  return b64(wire.data(), wire.size());
}

struct Sample {
  bool a_to_b;
  uint64_t amount_in;
  bool sim_ok;
  uint64_t sim_out, sim_in, units;
  std::string err;
  // live comparison
  bool verified = false;  // the simulation's vault post-states imply exactly the bracket's pre-state
  uint64_t sim_slot = 0;
  bool quote_ok;
  bool quote_boundary;
  uint64_t quote_out, quote_in;
};

struct Batch {
  DexKind kind;
  Pubkey pool;
  uint64_t slot_before = 0, slot_after = 0;
  std::vector<std::pair<Pubkey, std::optional<RpcAccount>>> accounts;  // pool state as quoted
  std::vector<Sample> samples;
};

std::string json_escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    if (c == '\n') { o += "\\n"; continue; }
    o += c;
  }
  return o;
}

void write_batches(const std::string& path, const std::vector<Batch>& bs, const char* dex) {
  std::ofstream f(path);
  f << "{\"dex\":\"" << dex << "\",\"source\":\"simulateTransaction sigVerify:false, bracketed by getMultipleAccounts\","
    << "\"batches\":[";
  for (size_t i = 0; i < bs.size(); ++i) {
    const Batch& b = bs[i];
    f << (i ? ",\n" : "\n") << "{\"pool\":\"" << b58(b.pool) << "\",\"slot_before\":" << b.slot_before << ",\"slot_after\":" << b.slot_after
      << ",\"accounts\":{";
    for (size_t j = 0; j < b.accounts.size(); ++j) {
      f << (j ? "," : "") << "\"" << b58(b.accounts[j].first) << "\":";
      if (!b.accounts[j].second) f << "null";
      else f << "{\"owner\":\"" << b58(b.accounts[j].second->owner) << "\",\"data\":\""
             << b64(b.accounts[j].second->data.data(), b.accounts[j].second->data.size()) << "\"}";
    }
    f << "},\"samples\":[";
    for (size_t j = 0; j < b.samples.size(); ++j) {
      const Sample& s = b.samples[j];
      f << (j ? "," : "") << "{\"a_to_b\":" << (s.a_to_b ? "true" : "false") << ",\"amount_in\":" << s.amount_in
        << ",\"sim_ok\":" << (s.sim_ok ? "true" : "false") << ",\"sim_out\":" << s.sim_out << ",\"sim_in\":" << s.sim_in
        << ",\"units\":" << s.units << ",\"sim_slot\":" << s.sim_slot << ",\"verified\":" << (s.verified ? "true" : "false") << ",\"err\":\"" << json_escape(s.err) << "\"}";
    }
    f << "]}";
  }
  f << "\n]}\n";
}

std::string g_pools_file;

std::vector<std::string> fixture_pools(const std::string& root, const char* list) {
  if (!g_pools_file.empty()) {
    std::ifstream f(g_pools_file);
    std::stringstream ss;
    ss << f.rdbuf();
    simdjson::dom::parser p;
    simdjson::padded_string ps(ss.str());
    std::vector<std::pair<double, std::string>> v;
    for (auto e : simdjson::dom::array(p.parse(ps)["pools"])) {
      std::string_view kind, addr;
      double tvl = 0;
      if (e["kind"].get(kind) || kind != list || e["address"].get(addr)) continue;
      if (e["tvl_sol"].get(tvl)) tvl = 0;
      v.emplace_back(tvl, std::string(addr));
    }
    std::sort(v.begin(), v.end(), [](auto& a, auto& b) { return a.first > b.first; });
    std::vector<std::string> out;
    for (auto& [t, a] : v) out.push_back(a);
    return out;
  }
  std::ifstream f(root + "/tests/fixtures/pools.json");
  std::stringstream ss;
  ss << f.rdbuf();
  simdjson::dom::parser p;
  simdjson::padded_string ps(ss.str());
  std::vector<std::string> out;
  simdjson::dom::array a = p.parse(ps)[list];
  for (auto e : a) out.emplace_back(std::string_view(e));
  return out;
}

struct Stats {
  size_t unverified = 0;
  size_t kept = 0, discarded_batches = 0, kept_batches = 0, mismatches = 0, both_fail = 0, boundary = 0;
  uint64_t units_sum = 0, units_n = 0, units_max = 0;
};

/** One bracketed batch for one pool. Returns true when kept. */
bool run_pool(DexKind kind, const Pubkey& pool, std::vector<Batch>& out, Stats& st) {
  // Read the pool to learn its vaults, mints and tick arrays.
  auto pa = rpc_call([&](Rpc& r) { return r.account(pool); });
  if (!pa) return false;
  state::RawAccount raw{pool, pa->owner, pa->data};
  state::DiscoveryConfig dc;
  auto def = state::pool_def_from_account(raw, dc);
  if (!def) return false;
  if (kind == DexKind::Whirlpool) {
    auto w = state::decode_whirlpool(pa->data.data(), pa->data.size(), pa->owner);
    if (w->adaptive_fee()) { std::fprintf(stderr, "  skip %s: adaptive fee\n", b58(pool).c_str()); return false; }
  }
  auto pick = [&](const Pubkey& m) -> const Holder* {
    for (auto& h : holders(m))
      if (h.account != def->vault_a && h.account != def->vault_b) return &h;
    return nullptr;
  };
  const Holder* hA = pick(def->mint_a);
  const Holder* hB = pick(def->mint_b);
  // Destinations: each funding wallet's own ATA for the output mint (a->b pays into hA's mint-b ATA).
  Pubkey dB = hA ? ata(hA->owner, def->mint_b, prog::TOKEN) : def->vault_a;
  Pubkey dA = hB ? ata(hB->owner, def->mint_a, prog::TOKEN) : def->vault_b;
  std::vector<Pubkey> keys = {pool, def->vault_a, def->vault_b, def->mint_a, def->mint_b, dA, dB,
                              hA ? hA->account : dA, hB ? hB->account : dB};
  for (auto& [start, k] : def->tick_arrays) keys.push_back(k);
  if (!is_zero(def->config)) keys.push_back(def->config);  // CPMM fee rates, PumpSwap global config
  if (!is_zero(def->fee_config)) keys.push_back(def->fee_config);  // PumpSwap fee tiers
  uint64_t s1 = 0, s2 = 0;
  auto before = rpc_call([&](Rpc& r) { return r.multiple_accounts_at(keys, s1); });
  if (before.size() != keys.size() || !before[0] || !before[1] || !before[2]) return false;

  StateStore store;
  uint32_t id = store.add_pool(*def);
  for (size_t i = 0; i < keys.size(); ++i) {
    if (i >= 5 && i <= 8) continue;  // holders/destinations are not pool state
    AccountUpdate u;
    u.key = keys[i];
    u.slot = s1;
    if (!before[i]) u.missing = true;
    else { u.owner = before[i]->owner; u.data = before[i]->data.data(); u.len = before[i]->data.size(); }
    store.apply(u);
  }
  auto ps = store.pool(id);
  if (!ps->complete) { std::fprintf(stderr, "  skip %s: incomplete\n", b58(pool).c_str()); return false; }
  auto amount_at = [&](size_t idx) -> uint64_t {
    if (!before[idx]) return 0;
    auto t = state::decode_token_account(before[idx]->data.data(), before[idx]->data.size(), before[idx]->owner);
    return t ? t->amount : 0;
  };
  uint64_t pre_dA = amount_at(5), pre_dB = amount_at(6), bal_A = hA ? amount_at(7) : 0, bal_B = hB ? amount_at(8) : 0;

  dex::RaydiumV4Quoter rq;
  dex::WhirlpoolQuoter wq;
  dex::CpmmQuoter cq;
  dex::PumpSwapQuoter pq;
  const dex::IQuoter* qs[] = {&rq, &wq, &cq, &pq};
  const dex::IQuoter* qp = nullptr;
  for (const dex::IQuoter* c : qs)
    if (c->kind() == kind) qp = c;
  if (!qp) return false;
  const dex::IQuoter& q = *qp;
  if (!q.supports(*ps)) { std::fprintf(stderr, "  skip %s: not supported by the quoter\n", b58(pool).c_str()); return false; }

  Batch b;
  b.kind = kind;
  b.pool = pool;
  b.slot_before = s1;
  for (bool a_to_b : {true, false}) {
    uint64_t depth = a_to_b ? ps->vault_a_amount : ps->vault_b_amount;
    uint64_t bal = a_to_b ? bal_A : bal_B;
    // 90% of the holder's balance: the wallet is someone else's and may spend between bracket and simulation.
    double lo = std::max(1.0, double(depth) * 1e-7), hi = std::min(double(bal) * 0.9, double(depth) * 0.05);
    if (!(a_to_b ? hA : hB) || hi < lo * 2) continue;
    for (int i = 0; i < 10; ++i) {
      uint64_t amount = uint64_t(lo * std::pow(hi / lo, double(i) / 9.0));
      if (amount == 0) continue;
      Sample s{a_to_b, amount, false, 0, 0, 0, "", false, 0, false, false, 0, 0};
      auto qq = q.quote_exact_in(*ps, a_to_b, amount);
      s.quote_ok = qq.has_value() && !qq->crosses_boundary;
      s.quote_boundary = qq.has_value() && qq->crosses_boundary;
      if (qq) { s.quote_out = qq->amount_out; s.quote_in = qq->amount_in; }
      const Holder* src_h = a_to_b ? hA : hB;
      const Pubkey& dst = a_to_b ? dB : dA;
      tx::IxList ixs;
      // create_associated_token_account_idempotent(payer, ata, owner, mint, system, token)
      const Pubkey& wallet = src_h->owner;
      ixs.add(prog::ATA).acc(wallet, true, true).rw(dst).ro(wallet).ro(a_to_b ? def->mint_b : def->mint_a)
          .ro(prog::SYSTEM).ro(prog::TOKEN).u8(1);
      tx::dexix::UserAccounts ua{src_h->owner, a_to_b ? src_h->account : dA, a_to_b ? dB : src_h->account};
      tx::dexix::swap(ixs, *ps, a_to_b, ua, amount, 0);
      std::string txb = unsigned_tx_b64(wallet, ixs);
      const Pubkey& src = src_h->account;
      auto sim = rpc_call([&](Rpc& r) { return r.simulate_full(txb, {dst, src, def->vault_a, def->vault_b}, s1); });
      s.sim_slot = sim.slot;
      s.sim_ok = sim.ok;
      s.units = sim.units;
      if (sim.ok && sim.accounts.size() == 4 && sim.accounts[0] && sim.accounts[1] && sim.accounts[2] && sim.accounts[3]) {
        auto d = state::decode_token_account(sim.accounts[0]->data.data(), sim.accounts[0]->data.size(), sim.accounts[0]->owner);
        auto srt = state::decode_token_account(sim.accounts[1]->data.data(), sim.accounts[1]->data.size(), sim.accounts[1]->owner);
        uint64_t pre_d = a_to_b ? pre_dB : pre_dA, pre_s = a_to_b ? bal_A : bal_B;
        auto va = state::decode_token_account(sim.accounts[2]->data.data(), sim.accounts[2]->data.size(), sim.accounts[2]->owner);
        auto vb = state::decode_token_account(sim.accounts[3]->data.data(), sim.accounts[3]->data.size(), sim.accounts[3]->owner);
        if (d && srt && va && vb) {
          s.sim_out = d->amount - pre_d;
          s.sim_in = pre_s - srt->amount;
          // Vault conservation: input vault grew by what the source lost, output vault shrank by what the
          // destination gained, relative to the bracket's vault balances.
          uint64_t vin_pre = a_to_b ? ps->vault_a_amount : ps->vault_b_amount;
          uint64_t vout_pre = a_to_b ? ps->vault_b_amount : ps->vault_a_amount;
          uint64_t vin_post = a_to_b ? va->amount : vb->amount, vout_post = a_to_b ? vb->amount : va->amount;
          s.verified = vin_post - vin_pre == s.sim_in && vout_pre - vout_post == s.sim_out;
        }
      } else if (!sim.ok) {
        s.err = sim.err;
        for (auto& l : sim.logs)
          if (l.find("Error") != std::string::npos || l.find("failed") != std::string::npos) s.err += " | " + l;
        if (s.err.size() > 400) s.err.resize(400);
      }
      b.samples.push_back(s);
    }
  }
  auto after = rpc_call([&](Rpc& r) { return r.multiple_accounts_at(keys, s2); });
  b.slot_after = s2;
  bool same = after.size() == before.size();
  bool src_same[2] = {true, true};
  for (size_t i = 0; same && i < keys.size(); ++i) {
    bool eb = before[i].has_value(), ea = after[i].has_value();
    bool eq = eb == ea && (!eb || before[i]->data == after[i]->data);
    if (i == 7 || i == 8) { src_same[i - 7] = eq; continue; }  // funding accounts may move
    if (!eq) same = false;
  }
  // A moved funding account does not touch a successful sample (vault conservation already proves the
  // state it ran on), but it does void a failed one: the program also rejects a swap when the user's
  // source balance is short (processor.rs: user_source.amount < amount_in -> InsufficientFunds), which is
  // the holder's wallet, not pool state the quoter models.
  auto funding_moved = [&](const Sample& s) { return !s.sim_ok && !src_same[s.a_to_b ? 0 : 1]; };
  // The simulated transaction is [create the destination ATA, swap]: a failure at any instruction but the swap
  // (index 1) is the harness's (e.g. a classic-SPL ATA for a Token-2022 mint), not the DEX rejecting the swap.
  auto pre_swap_failure = [&](const Sample& s) {
    return !s.sim_ok && s.err.find("\"InstructionError\":[1,") == std::string::npos;
  };
  size_t mism = 0;
  for (auto& s : b.samples) {
    if (s.sim_ok && !s.verified) continue;  // simulation ran on a different state than the bracket
    if (funding_moved(s) || pre_swap_failure(s)) continue;
    bool bad = (s.sim_ok && s.quote_ok && (s.sim_out != s.quote_out || (s.sim_in && s.sim_in != s.quote_in))) ||
               (s.sim_ok != s.quote_ok && !s.quote_boundary);
    if (bad) {
      ++mism;
      std::fprintf(stderr, "    MISMATCH a_to_b=%d in=%llu sim_ok=%d sim_out=%llu sim_in=%llu quote_ok=%d quote_out=%llu quote_in=%llu err=%s\n",
                   int(s.a_to_b), (unsigned long long)s.amount_in, int(s.sim_ok), (unsigned long long)s.sim_out,
                   (unsigned long long)s.sim_in, int(s.quote_ok), (unsigned long long)s.quote_out,
                   (unsigned long long)s.quote_in, s.err.substr(0, 200).c_str());
    }
  }
  std::fprintf(stderr, "  %s %s: %zu samples, bracket %s (slots %llu..%llu), live mismatches %zu\n", dex_name(kind), b58(pool).c_str(),
               b.samples.size(), same ? "same" : "MOVED", (unsigned long long)s1, (unsigned long long)s2, mism);
  if (!same || b.samples.empty()) {
    ++st.discarded_batches;
    return false;
  }
  size_t unverified = 0;
  std::vector<Sample> kept;
  for (auto& s : b.samples) {
    if ((s.sim_ok && !s.verified) || funding_moved(s) || pre_swap_failure(s)) { ++unverified; continue; }
    kept.push_back(s);
  }
  b.samples = std::move(kept);
  st.unverified += unverified;
  if (b.samples.empty()) { ++st.discarded_batches; return false; }
  for (size_t i = 0; i < keys.size(); ++i) b.accounts.emplace_back(keys[i], before[i]);
  for (auto& s : b.samples) {
    ++st.kept;
    if (s.sim_ok) { st.units_sum += s.units; ++st.units_n; st.units_max = std::max(st.units_max, s.units); }
    if (!s.sim_ok && !s.quote_ok) ++st.both_fail;
    if (s.quote_boundary) ++st.boundary;
  }
  st.mismatches += mism;
  ++st.kept_batches;
  out.push_back(std::move(b));
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  std::string dex = "all", root = ".";
  size_t min_samples = 300, max_pools = 60;
  const char* key = std::getenv("HELIUS_API_KEY");
  g_rpc_url = key && *key ? std::string("https://mainnet.helius-rpc.com/?api-key=") + key : "https://api.mainnet-beta.solana.com";
  for (int i = 1; i + 1 < argc; i += 2) {
    std::string a = argv[i];
    if (a == "--rpc") g_rpc_url = argv[i + 1];
    else if (a == "--dex") dex = argv[i + 1];
    else if (a == "--min-samples") min_samples = std::stoul(argv[i + 1]);
    else if (a == "--max-pools") max_pools = std::stoul(argv[i + 1]);
    else if (a == "--root") root = argv[i + 1];
    else if (a == "--pools-file") g_pools_file = argv[i + 1];
    else if (a == "--seed-holders") {
      std::ifstream f(argv[i + 1]);
      std::stringstream ss;
      ss << f.rdbuf();
      simdjson::dom::parser p;
      simdjson::padded_string ps(ss.str());
      simdjson::dom::object o = p.parse(ps)["holders"];
      for (auto kv : o) {
        Pubkey m;
        if (!b58_decode32(kv.key, m)) continue;
        for (auto e : simdjson::dom::array(kv.value)) g_seed[m].push_back(parse_pubkey(std::string_view(e)));
      }
      std::fprintf(stderr, "seeded holders for %zu mints\n", g_seed.size());
    }
  }
  crypto_init();
  g_rpc = std::make_unique<Rpc>(g_rpc_url, 30000);
  std::fprintf(stderr, "funding: largest wallet-owned holders per mint (simulation only)\n");
  int rc = 0;
  for (auto [kind, list, name] : {std::tuple{DexKind::RaydiumV4, "raydium_v4", "raydium_v4"},
                                  std::tuple{DexKind::Whirlpool, "whirlpool", "whirlpool"},
                                  std::tuple{DexKind::Cpmm, "cpmm", "cpmm"},
                                  std::tuple{DexKind::PumpSwap, "pumpswap", "pumpswap"}}) {
    if (dex != "all" && dex != name) continue;
    std::vector<Batch> batches;
    Stats st;
    size_t tried = 0;
    // Two passes: busy pools often move inside a bracket; a second try may land in a quiet window.
    for (int pass = 0; pass < 2 && st.kept < min_samples; ++pass) {
      for (auto& addr : fixture_pools(root, list)) {
        if (st.kept >= min_samples || tried >= max_pools * 2) break;
        Pubkey pk = parse_pubkey(addr);
        bool done = false;
        for (auto& b : batches) done |= b.pool == pk;
        if (done) continue;
        ++tried;
        try {
          // Written after every kept batch, so an interrupted run keeps what it collected.
          if (run_pool(kind, pk, batches, st))
            write_batches(root + "/tests/fixtures/parity_" + std::string(name) + ".json", batches, name);
        } catch (const std::exception& e) {
          std::fprintf(stderr, "  %s error: %s\n", addr.c_str(), e.what());
        }
      }
    }
    write_batches(root + "/tests/fixtures/parity_" + std::string(name) + ".json", batches, name);
    std::printf("%s: kept %zu samples in %zu batches (discarded %zu moved batches); live mismatches %zu; "
                "unverified (dropped) %zu; both-failed %zu; boundary %zu; sim CU mean %llu max %llu; rpc calls %llu\n",
                name, st.kept, st.kept_batches, st.discarded_batches, st.mismatches, st.unverified, st.both_fail, st.boundary,
                (unsigned long long)(st.units_n ? st.units_sum / st.units_n : 0), (unsigned long long)st.units_max,
                (unsigned long long)g_calls);
    if (st.mismatches) rc = 3;
  }
  return rc;
}
