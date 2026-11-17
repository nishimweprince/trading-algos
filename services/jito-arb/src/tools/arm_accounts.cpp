// arm-accounts: create the hot wallet's token accounts and lookup tables for the armed pools (docs/LIVE-READINESS.md
// Stage 3). Without --send it is read-only: it reads chain state, prints what is missing and what it costs, and
// simulates the first transactions. With --send it signs with the hot wallet and sends them one at a time, each
// confirmed before the next, after you type "yes". Safe to re-run: the plan is always rebuilt from chain state, so
// an interrupted run continues where it stopped.
//
//   arm-accounts CONFIG [--send] [--yes] [--cu-price MICRO_LAMPORTS]
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <thread>

#include "app/arming.hpp"
#include "app/bundle_sim.hpp"
#include "app/live_plan.hpp"
#include "app/live_send.hpp"
#include "app/pools_file.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "core/sys.hpp"
#include "feed/resync.hpp"
#include "state/accounts.hpp"
#include "tx/dex_ix.hpp"
#include "tx/message_v0.hpp"

using namespace arb;

namespace {

std::string read_file(const std::string& path) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("cannot read " + path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

void write_atomic(const std::string& path, const std::string& body) {
  const std::string tmp = path + ".tmp";
  {
    std::ofstream f(tmp, std::ios::trunc);
    f << body;
    if (!f) throw std::runtime_error("cannot write " + tmp);
  }
  if (std::rename(tmp.c_str(), path.c_str()) != 0) throw std::runtime_error("cannot replace " + path);
}

double sol(uint64_t lamports) { return double(lamports) / 1e9; }

/** Sign, send (preflight on) and wait for confirmation. Throws on any failure: the run stops, a re-run resumes. */
std::string send_confirmed(Rpc& rpc, const Keypair& kp, const tx::IxList& ixs) {
  if (!app::setup_programs_allowed(ixs)) throw std::runtime_error("refusing to sign: a setup transaction calls an unexpected program");
  auto bh = rpc.latest_blockhash("confirmed");
  tx::SignedTx t;
  auto r = tx::build_tx(kp.pubkey(), &kp, ixs, bh.hash, {}, t);
  if (!r) throw std::runtime_error(std::string("build: ") + r.error().what);
  std::string sig = rpc.send_transaction(b64(t.bytes, t.len));
  for (int i = 0; i < 120; ++i) {
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    auto st = rpc.signature_status(sig);
    if (!st.found) continue;
    if (st.failed) throw std::runtime_error("transaction " + sig + " failed on chain");
    if (st.confirmation == "confirmed" || st.confirmation == "finalized") return sig;
  }
  throw std::runtime_error("transaction " + sig + " not confirmed within 60 s (re-run: the plan is rebuilt from chain state)");
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc < 2) throw std::runtime_error("usage: arm-accounts CONFIG [--send] [--yes] [--cu-price MICRO_LAMPORTS]");
    bool send = false, yes = false;
    uint64_t cu_price = 20'000;  // 200k CU limit x 20k micro-lamports = 4,000 lamports a transaction
    for (int i = 2; i < argc; ++i) {
      if (!std::strcmp(argv[i], "--send")) send = true;
      else if (!std::strcmp(argv[i], "--yes")) yes = true;
      else if (!std::strcmp(argv[i], "--cu-price") && i + 1 < argc) cu_price = std::stoull(argv[++i]);
      else throw std::runtime_error(std::string("unknown option ") + argv[i]);
    }
    crypto_init();
    Config cfg = load_config(argv[1]);
    if (cfg.mode.armed_path.empty()) throw std::runtime_error("mode.armed_path is not set (run live-plan --write first)");
    if (cfg.mode.alts_path.empty()) throw std::runtime_error("mode.alts_path is not set");
    Keypair wallet = Keypair::load_file(cfg.keys.hot_wallet_path);
    const Pubkey w = wallet.pubkey();
    std::optional<Pubkey> executor;
    if (cfg.keys.executor_program_id != "VERIFY") executor = parse_pubkey(cfg.keys.executor_program_id);
    Rpc rpc(resolve_rpc_url(cfg), 30'000);

    app::ArmedSet armed;
    std::string err;
    if (!armed.load_json(read_file(cfg.mode.armed_path), &err)) throw std::runtime_error(cfg.mode.armed_path + ": " + err);
    app::PoolsFile pf;
    if (!app::from_json(read_file(cfg.pools_path), pf, &err)) throw std::runtime_error(err);
    StateStore store;
    size_t missing_pools = 0;
    std::set<Pubkey> found;
    for (const PoolDef& d : pf.pools)
      if (armed.contains(d.address)) {
        store.add_pool(d);
        found.insert(d.address);
      }
    missing_pools = armed.size() - found.size();
    feed::resync(rpc, store, store.subscribed_keys(), true);

    // Flash reserves (shared table).
    std::vector<flash::FlashReserve> reserves;
    for (const std::string& rs : cfg.flash.reserves) {
      Pubkey rk = parse_pubkey(rs);
      auto ra = rpc.account(rk, "confirmed");
      if (!ra) throw std::runtime_error("reserve " + rs + " not found");
      auto r = state::decode_kamino_reserve(ra->data.data(), ra->data.size(), ra->owner);
      if (!r) throw std::runtime_error("reserve " + rs + ": " + r.error().what);
      reserves.push_back(flash::make_reserve(rk, *r, 0));
    }

    // Token accounts.
    std::vector<app::AtaNeed> atas = app::needed_atas(store, w), missing_atas;
    for (size_t i = 0; i < atas.size(); i += 100) {
      std::vector<Pubkey> keys;
      for (size_t j = i; j < std::min(atas.size(), i + 100); ++j) keys.push_back(atas[j].ata);
      auto accs = rpc.multiple_accounts(keys, "confirmed");
      for (size_t j = 0; j < keys.size(); ++j)
        if (!accs[j]) missing_atas.push_back(atas[i + j]);
    }
    bool need_pump = false;
    for (uint32_t i = 0; i < store.pool_count(); ++i)
      if (auto p = store.pool(i); p && p->kind == DexKind::PumpSwap) need_pump = true;
    const bool pump_missing = need_pump && !rpc.account(tx::dexix::pump_user_volume_accumulator(w), "confirmed");

    // Lookup tables.
    std::vector<Pubkey> keys = app::needed_table_keys(store, w, reserves, executor);
    auto load = [&](tx::AltManager& alts, std::vector<Pubkey>& tables) {
      tables.clear();
      app::load_tables(rpc, cfg.mode.alts_path, alts, &tables);
    };
    tx::AltManager alts;
    std::vector<Pubkey> tables;
    load(alts, tables);
    std::vector<Pubkey> missing_keys = alts.missing(keys);
    // Room left in the existing tables decides how many new ones the missing keys need (the slot only names them).
    std::vector<Pubkey> would_create;
    alts.plan_extend(missing_keys, w, w, 1'000'000, &would_create);
    const size_t new_tables = would_create.size();

    uint64_t ata_rent = 0;
    for (const auto& a : missing_atas) ata_rent += app::rent_exempt_lamports(a.token_program == prog::TOKEN_2022 ? 182 : app::TOKEN_ACCOUNT_BYTES);
    const uint64_t table_rent = missing_keys.size() * 32 * 6960 + new_tables * app::rent_exempt_lamports(app::ALT_META_BYTES);
    const size_t n_tx = (missing_atas.size() + 4) / 5 + (pump_missing ? 1 : 0) + (missing_keys.size() + tx::ALT_EXTEND_CHUNK - 1) / tx::ALT_EXTEND_CHUNK;
    const uint64_t fees = n_tx * (5000 + 200'000 * cu_price / 1'000'000);
    const uint64_t total = ata_rent + table_rent + fees;
    auto [balance, bal_slot] = rpc.balance(w);
    (void)bal_slot;

    std::printf("hot wallet           %s  balance %.6f SOL\n", b58(w).c_str(), sol(balance));
    std::printf("armed pools          %zu (%zu not in pools.json, skipped)\n", found.size(), missing_pools);
    std::printf("executor             %s\n", executor ? b58(*executor).c_str() : "not deployed (left out of the tables; re-run after the deploy)");
    std::printf("token accounts       %zu needed, %zu missing, rent %.6f SOL\n", atas.size(), missing_atas.size(), sol(ata_rent));
    if (need_pump) std::printf("pumpswap volume acc  %s\n", pump_missing ? "missing (created)" : "present");
    std::printf("lookup tables        %zu existing, %zu new; %zu keys needed, %zu missing, rent %.6f SOL\n", tables.size(), new_tables,
                keys.size(), missing_keys.size(), sol(table_rent));
    std::printf("transactions         about %zu, fees about %.6f SOL\n", n_tx, sol(fees));
    std::printf("total                %.6f SOL (rent comes back when the accounts and tables are closed)\n", sol(total));
    const uint64_t float_min = uint64_t(cfg.risk.min_sol_float * 1e9);
    if (balance < total) std::printf("NOT ENOUGH SOL: fund at least %.6f SOL more\n", sol(total - balance));
    else if (balance - total < float_min)
      std::printf("warning: %.6f SOL left after setup, below risk.min_sol_float %.2f: the bot will not send until topped up\n",
                  sol(balance - total), cfg.risk.min_sol_float);
    if (missing_atas.empty() && missing_keys.empty() && !pump_missing) {
      std::printf("nothing to do: every token account and table entry exists\n");
      return 0;
    }

    // Token-account transactions first: they do not depend on anything.
    std::vector<tx::IxList> setup = app::ata_txs(missing_atas, w);
    if (pump_missing) {
      tx::IxList l;
      tx::dexix::pump_init_user_volume(l, w);
      setup.push_back(l);
    }
    app::add_compute_budget(setup, 200'000, cu_price);

    if (!send) {
      // Read-only check: simulate the first setup transaction and the first table transaction.
      std::vector<tx::IxList> first;
      if (!setup.empty()) first.push_back(setup.front());
      std::vector<Pubkey> created;
      auto tplan = alts.plan_extend(missing_keys, w, w, rpc.slot("finalized"), &created);
      app::add_compute_budget(tplan, 200'000, cu_price);
      if (!tplan.empty()) first.push_back(tplan.front());
      for (const auto& l : first) {
        std::string res = rpc.simulate(app::unsigned_tx_b64(w, l));
        std::printf("simulation           %s\n", res.empty() ? "ok" : res.c_str());
      }
      std::printf("read-only: nothing sent. Run again with --send to create them.\n");
      return 0;
    }

    if (balance < total) throw std::runtime_error("not enough SOL in the hot wallet");
    if (!yes) {
      std::printf("\nThis signs and sends about %zu transactions from %s, spending about %.6f SOL. Type yes to continue: ", n_tx,
                  b58(w).c_str(), sol(total));
      std::fflush(stdout);
      std::string answer;
      std::getline(std::cin, answer);
      if (answer != "yes") {
        std::printf("stopped: nothing sent\n");
        return 1;
      }
    }
    size_t sent = 0;
    for (const auto& l : setup) {
      std::string sig = send_confirmed(rpc, wallet, l);
      std::printf("  token accounts: %s\n", sig.c_str());
      ++sent;
    }
    // Tables: re-plan before every new table, so each create uses a fresh recent slot (a create must name a slot
    // still in SlotHashes, the last 512 slots, about 100 s).
    for (;;) {
      tx::AltManager now;
      load(now, tables);
      std::vector<Pubkey> todo = now.missing(keys);
      if (todo.empty()) break;
      std::vector<Pubkey> created;
      auto plan = now.plan_extend(todo, w, w, rpc.slot("finalized"), &created);
      app::add_compute_budget(plan, 200'000, cu_price);
      for (size_t i = 0; i < plan.size(); ++i) {
        bool creates = false;
        for (uint8_t j = 0; j < plan[i].n; ++j)
          if (plan[i].ixs[j].program == prog::ALT && plan[i].ixs[j].data[0] == 0) creates = true;
        if (creates && i > 0) break;  // a second new table: re-plan with a fresh slot
        std::string sig = send_confirmed(rpc, wallet, plan[i]);
        ++sent;
        if (creates) {
          // Record the table the moment it exists, so a stopped run never loses track of it.
          tables.push_back(created.front());
          write_atomic(cfg.mode.alts_path, app::tables_to_json(tables));
          std::printf("  created table %s: %s\n", b58(created.front()).c_str(), sig.c_str());
        } else {
          std::printf("  extended: %s\n", sig.c_str());
        }
      }
    }
    write_atomic(cfg.mode.alts_path, app::tables_to_json(tables));
    auto [after, s2] = rpc.balance(w);
    (void)s2;
    std::printf("done: %zu transactions, %zu tables in %s, balance %.6f SOL. Restart jito-arb to load the tables.\n", sent,
                tables.size(), cfg.mode.alts_path.c_str(), sol(after));
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "arm-accounts: %s\n", e.what());
    return 1;
  }
}
