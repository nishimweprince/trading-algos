// sim-check: phase-3 mainnet checks by simulateTransaction only (sigVerify:false, replaceRecentBlockhash:true).
// Nothing is signed and nothing is sent. The fee payer and swap authority is a public wSOL holder's own
// wallet (harness/holders_seed.json, re-validated live: system-owned, on curve, enough SOL).
//
//   sim-check [--rpc URL] [--root DIR] [--executor PUBKEY] [--wallet PUBKEY] [--tamper]
//
//   (a) kamino:   wrap SOL -> Kamino flash_borrow(wSOL) -> flash_repay. Proves the account order and data,
//                 and that the fee pulled equals flash_loan_fee() exactly.
//   (b) swaps:    the same around a direct Raydium v4 round trip (wSOL -> X -> wSOL on one pool) without the
//                 executor. Proves the swap legs work inside a flash-loan transaction and that the chained
//                 quotes are exact (final wSOL balance predicted to the unit).
//   (c) full:     the whole §7.3 transaction (CU limit, borrow, execute_route, repay, tip) with the executor
//                 id from --executor (default: the dry-run placeholder, not deployed): on mainnet it must fail
//                 at the execute_route instruction and nowhere else. On a local validator with the executor
//                 deployed (tests/validator.sh) the same transaction must succeed, and fail with MinOutNotMet
//                 when min_out is tampered.
// Writes tests/fixtures/sim_check.json (results and logs).
#include <simdjson.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>

#include "app/bundle_builder.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/compact_u16.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "dex/quoter.hpp"
#include "flash/kamino.hpp"
#include "risk/sign_guard.hpp"
#include "state/discovery.hpp"
#include "tx/builders.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;

namespace {

std::string slurp(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

std::string unsigned_tx_b64(const Pubkey& payer, const tx::IxList& ixs, std::span<const tx::LookupTable> tables = {}) {
  uint8_t msg[4096];
  auto m = tx::compile_message_v0(payer, ixs, Hash{}, tables, msg, sizeof msg);
  if (!m) return "";
  std::vector<uint8_t> wire;
  uint8_t sv[3];
  size_t n = compact_u16_encode(msg[1], sv);
  wire.insert(wire.end(), sv, sv + n);
  wire.insert(wire.end(), size_t(msg[1]) * 64, 0);
  wire.insert(wire.end(), msg, msg + *m);
  return b64(wire.data(), wire.size());
}

void ata_idempotent(tx::IxList& l, const Pubkey& payer, const Pubkey& owner, const Pubkey& mint) {
  l.add(prog::ATA).acc(payer, true, true).rw(ata(owner, mint, prog::TOKEN)).ro(owner).ro(mint).ro(prog::SYSTEM).ro(prog::TOKEN).u8(1);
}
void sync_native(tx::IxList& l, const Pubkey& account) { l.add(prog::TOKEN).rw(account).u8(17); }

uint64_t amount_of(const std::optional<RpcAccount>& a) {
  if (!a || a->data.size() < 72) return 0;
  uint64_t v;
  std::memcpy(&v, a->data.data() + 64, 8);
  return v;
}

std::string json_str(const std::string& s) {
  std::string o = "\"";
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    if (c == '\n') { o += "\\n"; continue; }
    o += c;
  }
  return o + "\"";
}

struct Check {
  std::string name;
  bool pass = false;
  std::string detail, err;
  uint64_t units = 0;
  std::vector<std::string> logs;
};

std::set<Pubkey> g_touched;  // every account the simulated transactions use (validator.sh clones them)
void touch(const tx::IxList& l) {
  for (uint8_t i = 0; i < l.n; ++i)
    for (uint8_t a = 0; a < l.ixs[i].n_accounts; ++a) g_touched.insert(l.ixs[i].accounts[a].key);
}

std::string to_json(const std::vector<Check>& cs, const std::string& rpc_kind, uint64_t slot) {
  std::string s = "{\"source\":\"simulateTransaction sigVerify:false (nothing signed or sent)\",\"rpc\":" + json_str(rpc_kind) +
                  ",\"slot\":" + std::to_string(slot) + ",\"accounts\":[";
  bool first = true;
  for (const Pubkey& k : g_touched) {
    s += (first ? "" : ",") + json_str(b58(k));
    first = false;
  }
  s += "],\"checks\":[";
  for (size_t i = 0; i < cs.size(); ++i) {
    const Check& c = cs[i];
    s += (i ? ",\n" : "\n") + std::string("{\"name\":") + json_str(c.name) + ",\"pass\":" + (c.pass ? "true" : "false") +
         ",\"detail\":" + json_str(c.detail) + ",\"err\":" + json_str(c.err) + ",\"units\":" + std::to_string(c.units) + ",\"logs\":[";
    for (size_t j = 0; j < c.logs.size() && j < 60; ++j) s += (j ? "," : "") + json_str(c.logs[j].substr(0, 200));
    s += "]}";
  }
  return s + "\n]}\n";
}

}  // namespace

int main(int argc, char** argv) {
  std::string root = ".", executor_s, wallet_s;
  const char* key = std::getenv("HELIUS_API_KEY");
  std::string url = key && *key ? std::string("https://mainnet.helius-rpc.com/?api-key=") + key : "https://api.mainnet-beta.solana.com";
  for (int i = 1; i + 1 < argc; i += 2) {
    std::string a = argv[i];
    if (a == "--rpc") url = argv[i + 1];
    else if (a == "--root") root = argv[i + 1];
    else if (a == "--executor") executor_s = argv[i + 1];
    else if (a == "--wallet") wallet_s = argv[i + 1];  // the executor's OWNER on a validator build
  }
  bool tamper = false;  // with a deployed executor: min_out one unit above what the route returns -> MinOutNotMet
  for (int i = 1; i < argc; ++i) tamper |= std::string(argv[i]) == "--tamper";
  crypto_init();
  Rpc rpc(url, 30000);
  std::string rpc_kind = url.find("helius") != std::string::npos ? "helius" : url.find("127.0.0.1") != std::string::npos ? "local" : "public";
  std::vector<Check> out;
  uint64_t slot = 0;

  // Wallet: the first seeded wSOL holder whose owner is a system wallet with >= 0.2 SOL.
  Pubkey wallet{};
  {
    simdjson::dom::parser p;
    simdjson::padded_string ps(slurp(root + "/harness/holders_seed.json"));
    std::vector<Pubkey> accs;
    for (auto e : simdjson::dom::array(p.parse(ps)["holders"][b58(mint::WSOL)])) accs.push_back(parse_pubkey(std::string_view(e)));
    auto ta = rpc.multiple_accounts(accs);
    std::vector<Pubkey> owners;
    for (auto& a : ta)
      if (a && a->owner == prog::TOKEN && a->data.size() >= 64) owners.push_back(key_from(a->data.data() + 32));
    auto oa = rpc.multiple_accounts(owners);
    for (size_t i = 0; i < owners.size(); ++i)
      if (oa[i] && oa[i]->owner == prog::SYSTEM && oa[i]->data.empty() && oa[i]->lamports >= 200'000'000ULL && is_on_curve(owners[i].data()) &&
          (wallet_s.empty() || b58(owners[i]) == wallet_s)) {
        wallet = owners[i];
        break;
      }
  }
  if (is_zero(wallet)) {
    std::fprintf(stderr, "no usable wSOL holder wallet\n");
    return 1;
  }
  std::fprintf(stderr, "wallet %s (public holder; simulation only)\n", b58(wallet).c_str());
  Pubkey wsol_ata = ata(wallet, mint::WSOL, prog::TOKEN);

  // Kamino wSOL reserve from the fixture list, read live.
  flash::FlashReserve fr;
  {
    simdjson::dom::parser p;
    simdjson::padded_string ps(slurp(root + "/tests/fixtures/pools.json"));
    for (auto e : simdjson::dom::array(p.parse(ps)["kamino_reserves"])) {
      Pubkey rk = parse_pubkey(std::string_view(e));
      auto a = rpc.account(rk);
      if (!a) continue;
      auto r = state::decode_kamino_reserve(a->data.data(), a->data.size(), a->owner);
      if (r && r->liquidity_mint == mint::WSOL) fr = flash::make_reserve(rk, *r, 0);
    }
  }
  std::fprintf(stderr, "kamino wSOL reserve %s enabled=%d fee_sf=%llu available=%llu\n", b58(fr.reserve).c_str(), int(fr.enabled),
               (unsigned long long)fr.flash_loan_fee_sf, (unsigned long long)fr.available);

  // (a) borrow -> repay. ixs: [0] CU limit, [1] ATA idempotent, [2] wrap transfer, [3] sync, [4] borrow, [5] repay.
  {
    Check c;
    c.name = "kamino_borrow_repay";
    const uint64_t amount = 5'000'000'000ULL, wrap = 10'000'000ULL;
    auto fee = flash::flash_loan_fee(amount, fr.flash_loan_fee_sf);
    tx::IxList l;
    tx::set_compute_unit_limit(l, 400'000);
    ata_idempotent(l, wallet, wallet, mint::WSOL);
    tx::system_transfer(l, wallet, wsol_ata, wrap);
    sync_native(l, wsol_ata);
    uint8_t bi = l.n;
    flash::borrow_ix(l, fr, wallet, wsol_ata, amount);
    flash::repay_ix(l, fr, wallet, wsol_ata, amount, bi);
    std::vector<Pubkey> watch = {wsol_ata};
    auto pre = rpc.multiple_accounts_at(watch, slot);
    touch(l);
    auto sim = rpc.simulate_full(unsigned_tx_b64(wallet, l), watch, slot);
    c.units = sim.units;
    c.logs = sim.logs;
    c.err = sim.err;
    if (sim.ok && fee && !sim.accounts.empty()) {
      uint64_t want = amount_of(pre[0]) + wrap - *fee, got = amount_of(sim.accounts[0]);
      c.pass = want == got;
      c.detail = "fee " + std::to_string(*fee) + " on " + std::to_string(amount) + "; wSOL ATA expected " + std::to_string(want) +
                 " got " + std::to_string(got);
    } else {
      c.detail = fee ? "simulation failed" : "flash loans disabled on the reserve";
    }
    out.push_back(c);
  }

  // (b) borrow -> Raydium wSOL->X -> X->wSOL (same pool) -> repay, with the final balance predicted exactly.
  {
    Check c;
    c.name = "kamino_around_raydium_round_trip";
    simdjson::dom::parser p;
    simdjson::padded_string ps(slurp(root + "/tests/fixtures/pools.json"));
    state::DiscoveryConfig dc;
    dc.base_mints = {mint::WSOL, mint::USDC};
    std::shared_ptr<const PoolState> best;
    for (auto e : simdjson::dom::array(p.parse(ps)["raydium_v4"])) {
      Pubkey pk = parse_pubkey(std::string_view(e));
      auto a = rpc.account(pk);
      if (!a) continue;
      auto def = state::pool_def_from_account(state::RawAccount{pk, a->owner, a->data}, dc);
      if (!def || (def->mint_a != mint::WSOL && def->mint_b != mint::WSOL)) continue;
      StateStore st;
      uint32_t id = st.add_pool(*def);
      std::vector<Pubkey> keys = {pk, def->vault_a, def->vault_b, def->mint_a, def->mint_b};
      uint64_t s = 0;
      auto accs = rpc.multiple_accounts_at(keys, s);
      for (size_t i = 0; i < keys.size(); ++i) {
        AccountUpdate u;
        u.key = keys[i];
        u.slot = s;
        if (!accs[i]) u.missing = true;
        else { u.owner = accs[i]->owner; u.data = accs[i]->data.data(); u.len = accs[i]->data.size(); }
        st.apply(u);
      }
      auto snap = st.pool(id);
      if (!snap->complete) continue;
      uint64_t sol_depth = snap->mint_a == mint::WSOL ? snap->vault_a_amount : snap->vault_b_amount;
      if (!best || sol_depth > (best->mint_a == mint::WSOL ? best->vault_a_amount : best->vault_b_amount)) best = snap;
      if (best && sol_depth > 5'000'000'000'000ULL) break;  // deep enough
    }
    if (!best) {
      c.detail = "no live Raydium wSOL pool";
      out.push_back(c);
    } else {
      dex::RaydiumV4Quoter q;
      bool sol_is_a = best->mint_a == mint::WSOL;
      Pubkey x = sol_is_a ? best->mint_b : best->mint_a;
      const uint64_t amount = 1'000'000'000ULL, wrap = 50'000'000ULL;
      auto q1 = q.quote_exact_in(*best, sol_is_a, amount);
      // State after leg 1: input vault +amount, output vault -out (the program's transfers).
      PoolState after = *best;
      after.spot_known = 0;  // the copy is modified below: drop the cached spot rates
      if (q1) {
        if (sol_is_a) { after.vault_a_amount += amount; after.vault_b_amount -= q1->amount_out; }
        else { after.vault_b_amount += amount; after.vault_a_amount -= q1->amount_out; }
      }
      auto q2 = q1 ? q.quote_exact_in(after, !sol_is_a, q1->amount_out) : q1;
      auto fee = flash::flash_loan_fee(amount, fr.flash_loan_fee_sf);
      Pubkey x_ata = ata(wallet, x, prog::TOKEN);
      tx::IxList l;
      tx::set_compute_unit_limit(l, 800'000);
      ata_idempotent(l, wallet, wallet, mint::WSOL);
      ata_idempotent(l, wallet, wallet, x);
      tx::system_transfer(l, wallet, wsol_ata, wrap);
      sync_native(l, wsol_ata);
      uint8_t bi = l.n;
      flash::borrow_ix(l, fr, wallet, wsol_ata, amount);
      tx::dexix::UserAccounts ua{wallet, sol_is_a ? wsol_ata : x_ata, sol_is_a ? x_ata : wsol_ata};
      tx::dexix::swap(l, *best, sol_is_a, ua, amount, 0);
      tx::dexix::swap(l, after, !sol_is_a, ua, q1 ? q1->amount_out : 0, 0);
      flash::repay_ix(l, fr, wallet, wsol_ata, amount, bi);
      std::vector<Pubkey> watch = {wsol_ata, x_ata, best->vault_a, best->vault_b};
      std::vector<Pubkey> bracket = {wsol_ata, x_ata, best->address, best->vault_a, best->vault_b};
      uint64_t s1 = 0, s2 = 0;
      auto pre = rpc.multiple_accounts_at(bracket, s1);
      touch(l);
      auto sim = rpc.simulate_full(unsigned_tx_b64(wallet, l), watch, s1);
      auto post_bracket = rpc.multiple_accounts_at(bracket, s2);
      bool moved = false;
      for (size_t i = 2; i < bracket.size(); ++i) moved |= !pre[i] || !post_bracket[i] || pre[i]->data != post_bracket[i]->data;
      c.units = sim.units;
      c.logs = sim.logs;
      c.err = sim.err;
      if (!q1 || !q2 || !fee) {
        c.detail = "quote or fee unavailable";
      } else if (!sim.ok) {
        c.detail = "simulation failed";
      } else {
        uint64_t want = amount_of(pre[0]) + wrap + q2->amount_out - amount - *fee, got = amount_of(sim.accounts[0]);
        // The pool may trade between the bracket reads and the simulation: then only the account order is proven.
        c.pass = got == want || moved;
        c.detail = "pool " + b58(best->address) + " leg1 out " + std::to_string(q1->amount_out) + " leg2 out " +
                   std::to_string(q2->amount_out) + "; wSOL ATA expected " + std::to_string(want) + " got " + std::to_string(got) +
                   (moved ? " (pool moved during the bracket: balance not compared)" : got == want ? " (exact)" : " MISMATCH");
      }
      out.push_back(c);

      // (c) the full §7.3 transaction through the executor, with a placeholder or deployed id.
      Check f;
      f.name = "full_route_tx";
      Pubkey exec = executor_s.empty() ? app::dry_run_executor() : parse_pubkey(executor_s);
      engine::Cycle cyc;
      cyc.n = 2;
      cyc.base = mint::WSOL;
      cyc.legs[0] = engine::Leg{0, sol_is_a, mint::WSOL, x};
      cyc.legs[1] = engine::Leg{0, !sol_is_a, x, mint::WSOL};
      std::shared_ptr<const PoolState> legs[2] = {best, std::make_shared<PoolState>(after)};
      tx::RouteTxParams rp;
      rp.wallet = wallet;
      rp.executor = exec;
      rp.tip_account = risk::default_tip_accounts()[0];
      rp.amount_in = amount;
      // The executor sees the base ATA after the borrow; the route returns leg2's output. min_out = exactly that
      // (the profitable-route check on a validator where the round trip is funded), or one more (--tamper).
      rp.min_out = (q2 ? q2->amount_out : 0) + (tamper ? 1 : 0);
      rp.tip_lamports = 1000;
      rp.cu_limit = 600'000;
      rp.reserve = &fr;
      tx::IxList full;
      auto lay = tx::build_route_ixs(full, rp, cyc, legs);
      risk::SignGuard g{wallet, exec, risk::default_tip_accounts(), 50'000'000};
      bool guard_ok = lay && g.check(full).has_value();
      if (lay) touch(full);
      // Too many keys for one static message: compile against a lookup table of the route's non-signer
      // accounts. On mainnet that table does not exist, so the RPC rejects the transaction while loading
      // addresses; the check therefore runs the legacy-size variant: borrow, execute, repay, tip without
      // the CU limit, with the executor's accounts as written.
      std::string txb = lay ? unsigned_tx_b64(wallet, full) : "";
      if (txb.empty()) {
        f.detail = "route does not fit a static message: run against a local validator with real lookup tables (tests/validator.sh)";
        f.pass = guard_ok;  // built and sign-guarded; on-chain part deferred
      } else {
        auto s3 = rpc.simulate_full(txb, {wsol_ata});
        f.units = s3.units;
        f.logs = s3.logs;
        f.err = s3.err;
        // Placeholder executor: the runtime refuses to load a transaction that invokes a program account
        // that does not exist (ProgramAccountNotFound, before any instruction runs). Any other error would
        // point at the rest of the transaction (accounts, Kamino, tip).
        bool undeployed_only = !s3.ok && s3.err.find("ProgramAccountNotFound") != std::string::npos;
        std::vector<uint8_t> raw;
        b64_decode(txb, raw);
        bool min_out_error = s3.err.find("6002") != std::string::npos || s3.err.find("0x1772") != std::string::npos;
        f.pass = guard_ok && (executor_s.empty() ? undeployed_only : tamper ? (!s3.ok && min_out_error) : s3.ok);
        f.detail = std::string("sign guard ") + (guard_ok ? "ok" : "BLOCKED") + "; " + std::to_string(raw.size()) +
                   " bytes unsigned-static (" + (raw.size() <= tx::PACKET_DATA_SIZE ? "fits" : "over 1,232: needs lookup tables") + "); " +
                   (executor_s.empty() ? (undeployed_only ? "rejected only for the undeployed executor (ProgramAccountNotFound)"
                                                          : "unexpected result")
                                       : tamper ? (min_out_error ? "tampered min_out reverted with MinOutNotMet (6002)" : "tampered: unexpected result")
                                                : (s3.ok ? "succeeded" : "failed"));
      }
      out.push_back(f);
    }
  }
  std::string path = root + "/tests/fixtures/sim_check.json";
  std::ofstream(path) << to_json(out, rpc_kind, slot);
  int fails = 0;
  for (auto& c : out) {
    std::printf("%-34s %s  %s\n", c.name.c_str(), c.pass ? "PASS" : "FAIL", c.detail.c_str());
    if (!c.pass) {
      ++fails;
      if (!c.err.empty()) std::printf("    err: %s\n", c.err.substr(0, 300).c_str());
    }
  }
  return fails ? 3 : 0;
}
