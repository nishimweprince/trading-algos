// pump-desk: live curve-in, migrate-out trader on pump.fun.
//
//   pump-desk [--config pump-desk.toml] [--preflight]
//
// There is no dry-run. --preflight runs the boot checks (RPC, PumpSwap and
// pump.fun configs, a simulated PumpSwap buy on a known canonical pool, a
// simulated curve buy that must reach the program's curve-complete check,
// Sender connectivity) and exits without subscribing or trading.
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>

#include "aux/aux.hpp"
#include "aux/jupiter.hpp"
#include "aux/leaders.hpp"
#include "aux/rpc.hpp"
#include "aux/writer.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/config.hpp"
#include "core/ring.hpp"
#include "core/sys.hpp"
#include "exec/exec.hpp"
#include "logic/engine.hpp"
#include "logic/shared.hpp"
#include "sol/constants.hpp"
#include "sol/crypto.hpp"
#include "sol/layouts.hpp"
#include "sol/curve.hpp"
#include "sol/swap.hpp"
#include "stream/ingest.hpp"

using namespace pd;

namespace {

Shared* g_shared = nullptr;

void on_signal(int) {
  if (g_shared) g_shared->stop.store(true);
}

// pump-fun/src/racer/sendReady.ts BOOT_SEED_POOL: a graduated canonical pool to simulate against.
constexpr Pubkey SEED_MINT = PK("74xrbNsxV8uPT6Kcedk2T3FNPtw8jzyVNWmaVkzxpump");
constexpr Pubkey SEED_POOL = PK("28jtYEpTMpiTmgMUXjS7mgkoqpoqxnrHtay99xQoKHZn");

struct PoolRead {
  Pool pool;
  Pubkey token_program{};
  uint64_t base = 0, quote = 0, supply = 0;
};

PoolRead read_pool(Rpc& rpc, const Pubkey& pool_key, const Pubkey& mint) {
  auto pa = rpc.multiple_accounts({pool_key, mint});
  if (pa.size() != 2 || !pa[0] || !pa[1]) throw std::runtime_error("pool or mint not found: " + b58(pool_key));
  PoolRead r;
  if (!decode_pool(pa[0]->data.data(), pa[0]->data.size(), r.pool)) throw std::runtime_error("pool did not decode");
  r.token_program = pa[1]->owner;
  MintInfo mi;
  decode_mint(pa[1]->data.data(), pa[1]->data.size(), r.token_program == prog::TOKEN_2022, mi);
  r.supply = mi.supply;
  auto va = rpc.multiple_accounts({r.pool.base_vault, r.pool.quote_vault});
  if (va.size() != 2 || !va[0] || !va[1]) throw std::runtime_error("pool vaults not found");
  decode_token_amount(va[0]->data.data(), va[0]->data.size(), r.base);
  decode_token_amount(va[1]->data.data(), va[1]->data.size(), r.quote);
  return r;
}

struct CurveRead {
  bool exists = false;
  Curve curve;
};

CurveRead read_curve(Rpc& rpc, const Pubkey& mint) {
  CurveRead r;
  auto acct = rpc.account(pda({std::string_view("bonding-curve"), mint}, prog::PUMP_FUN));
  if (acct && decode_curve(acct->data.data(), acct->data.size(), r.curve)) r.exists = true;
  return r;
}

/**
 * Two simulations with the exact builders the engine uses; any unexpected program
 * error means the hand-rolled bytes no longer match the deployed programs: refuse to start.
 *  1. A small PumpSwap buy on the seed pool (the pool-side keys every exit uses).
 *  2. A curve buy on the seed mint's (migrated) bonding curve. It must fail with pump's
 *     BondingCurveComplete / AlreadyMigrated: every account passed the program's constraints.
 */
void self_check(Rpc& rpc, Shared& sh, const Keypair& kp, const Config& cfg, Logger& log) {
  PoolRead pr = read_pool(rpc, SEED_POOL, SEED_MINT);
  UserKeys u;
  derive_user_keys(kp.pubkey(), u);
  SwapKeys k;
  derive_swap_keys(u, SEED_MINT, pr.token_program, pr.pool.coin_creator, pr.pool.cashback, pr.pool.mayhem, k);
  if (k.pool != SEED_POOL || k.pool_base_vault != pr.pool.base_vault || k.pool_quote_vault != pr.pool.quote_vault)
    throw std::runtime_error("self-check: derived pool keys do not match the chain");
  AmmConfig amm;
  sh.amm.load(amm);
  if (!amm.valid || !amm.curve_valid) throw std::runtime_error("self-check: no PumpSwap / pump.fun config");
  FeeBps fees = compute_fees_bps(amm, true, pr.supply, pr.base, pr.quote + pr.pool.virtual_quote, pr.pool.mayhem,
                                 pr.pool.creator_fee_bps);
  BuyQuote q = buy_quote_input(1'000'000, cfg.entry.slippage_pct, pr.base, pr.quote, pr.pool.virtual_quote, fees,
                               is_zero(pr.pool.coin_creator));
  if (!q.ok) throw std::runtime_error("self-check: quote failed");
  TxParams p;
  p.cu_limit = cfg.exits.cu_limit;
  p.cu_price = cfg.fees.cu_price_floor;
  p.tips[0] = Tip{parse_pubkey(cfg.sender.tip_accounts.at(0)), cfg.sender.tip_lamports};
  p.n_tips = 1;
  IxList ixs;
  buy_ixs(ixs, u, k, amm, p, q.base_out, q.max_quote);
  BlockhashSnap bh;
  sh.blockhash.load(bh);
  SignedTx tx;
  if (!build_signed_tx(kp, ixs, bh.hash, tx)) throw std::runtime_error("self-check: tx too large");
  std::string err = rpc.simulate(b64(tx.bytes, tx.len));
  if (!err.empty()) throw std::runtime_error("self-check: simulated PumpSwap buy failed: " + err);
  log.info("self-check ok: simulated PumpSwap buy on " + b58(SEED_POOL) + " (" + std::to_string(tx.len) + " bytes)");

  CurveRead cr = read_curve(rpc, SEED_MINT);
  if (!cr.exists) throw std::runtime_error("self-check: seed bonding curve not found");
  CurveKeys ck;
  derive_curve_keys(kp.pubkey(), SEED_MINT, pr.token_program, cr.curve.creator, cr.curve.mayhem, cr.curve.cashback, ck);
  p.cu_limit = cfg.entry.cu_limit;
  curve_buy_ixs(ixs, kp.pubkey(), ck, amm, p, 1'000'000, 1'000'000, cfg.curve.partial_fill);
  if (!build_signed_tx(kp, ixs, bh.hash, tx)) throw std::runtime_error("self-check: curve tx too large");
  err = rpc.simulate(b64(tx.bytes, tx.len));
  // {"InstructionError":[n,{"Custom":6005}]}: BondingCurveComplete (6081: BondingCurveAlreadyMigrated).
  if (err.find("6005") == std::string::npos && err.find("6081") == std::string::npos)
    throw std::runtime_error("self-check: a curve buy on a migrated curve did not stop at the completion check: " +
                             (err.empty() ? std::string("it passed") : err));
  log.info("self-check ok: curve buy accounts pass pump.fun's constraints (" + std::to_string(tx.len) + " bytes)");
}

void restore_positions(Rpc& rpc, Writer& writer, Engine& eng, const Keypair& kp, Logger& log) {
  for (RestoredPosition r : writer.open_positions()) {
    try {
      Pubkey user_ata = ata(kp.pubkey(), r.mint, r.token_program);
      auto acct = rpc.account(user_ata);
      uint64_t held = 0;
      if (acct) decode_token_amount(acct->data.data(), acct->data.size(), held);
      if (held == 0) {
        writer.close_orphan(r.mint, "restored_empty");
        log.warn("restored position " + b58(r.mint) + " holds no tokens: closed in the database");
        continue;
      }
      if (is_zero(r.curve)) r.curve = pda({std::string_view("bonding-curve"), r.mint}, prog::PUMP_FUN);
      r.base_amount = held;
      // Where the position stands now: still on the curve, waiting for the migrate, or in the pool.
      CurveRead cr = read_curve(rpc, r.mint);
      double mark = 0;
      if (cr.exists && !cr.curve.complete) {
        r.phase = Phase::Curve;
        r.curve_state = cr.curve;
        if (!is_zero(cr.curve.creator)) r.creator = cr.curve.creator;
        mark = curve_price_sol(cr.curve);
      } else {
        UserKeys u;
        derive_user_keys(kp.pubkey(), u);
        SwapKeys k;
        derive_swap_keys(u, r.mint, r.token_program, r.creator, r.cashback, r.mayhem, k);
        try {
          PoolRead pr = read_pool(rpc, k.pool, r.mint);
          r.phase = Phase::Pool;
          r.base_res = pr.base;
          r.quote_res = pr.quote;
          r.virtual_quote = pr.pool.virtual_quote;
          if (!is_zero(pr.pool.coin_creator)) r.creator = pr.pool.coin_creator;
          mark = pool_price_sol(pr.base, pr.quote, pr.pool.virtual_quote);
        } catch (const std::exception&) {
          r.phase = Phase::AwaitMigrate;  // complete, no pool yet
          if (cr.exists) mark = curve_price_sol(cr.curve);
        }
      }
      if (r.entry_price <= 0) {
        // The buy was in flight at shutdown and landed: adopt it at the current mark.
        r.entry_price = mark;
        r.base_bought = held;
        log.warn("adopting " + b58(r.mint) + ": buy landed after the last run stopped; entry = current mark, cost unknown");
      }
      if (!eng.restore_position(r)) log.error("no free slot to restore " + b58(r.mint));
      else log.info("restored open position " + b58(r.mint) + " (" + phase_name(r.phase) + ") base=" + std::to_string(held));
    } catch (const std::exception& e) {
      log.error("restore " + b58(r.mint) + " failed: " + e.what() + " — sell it manually");
    }
  }
}

}  // namespace

int main(int argc, char** argv) {
  std::string config_path = "pump-desk.toml";
  bool preflight = false;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--config") && i + 1 < argc) config_path = argv[++i];
    else if (!std::strcmp(argv[i], "--preflight")) preflight = true;
    else {
      std::fprintf(stderr, "usage: %s [--config path] [--preflight]\n", argv[0]);
      return 2;
    }
  }
  Logger log;
  try {
    Config cfg = load_config(config_path);
    crypto_init();
    init_derived();
    if (cfg.stream.endpoint.empty()) throw std::runtime_error("HELIUS_GRPC_URL (stream.endpoint) is not set");
    if (cfg.rpc.http_url.empty()) throw std::runtime_error("HELIUS_HTTP_URL (rpc.http_url) is not set");
    const char* secret = std::getenv(cfg.wallet_env.c_str());
    if (!secret || !*secret) throw std::runtime_error(cfg.wallet_env + " is not set");
    Keypair kp = Keypair::parse(secret);
    log.info("pump-desk starting LIVE, wallet " + b58(kp.pubkey()) + (preflight ? " (preflight)" : ""));

    std::unique_ptr<Shared> sh(new Shared);
    g_shared = sh.get();
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    Rpc rpc(cfg.rpc.http_url, cfg.rpc.timeout_ms);
    refresh_amm_config(rpc, *sh);
    {
      Rpc::Blockhash b = rpc.latest_blockhash();
      BlockhashSnap s;
      s.hash = b.hash;
      s.last_valid_height = b.last_valid_height;
      s.fetched_ns = now_ns();
      sh->blockhash.store(s);
    }
    AmmConfig amm;
    sh->amm.load(amm);
    if (amm.disable_flags & (1u << 4)) throw std::runtime_error("PumpSwap sells are disabled on chain");
    auto [lamports, bal_slot] = rpc.balance(kp.pubkey());
    log.info("wallet balance " + std::to_string(double(lamports) / 1e9) + " SOL");
    if (lamports < 10'000'000) log.warn("wallet holds < 0.01 SOL: every entry will be vetoed");
    self_check(rpc, *sh, kp, cfg, log);
    if (cfg.jupiter.enabled) {
      // The exit of last resort must answer before we need it. Not fatal: it is only a fallback.
      try {
        Jupiter jup(cfg);
        JupiterSwap js = jup.sell_to_sol(SEED_MINT, 1'000'000'000, kp.pubkey());
        log.info("jupiter exit fallback ready: " + js.route + " quote for the seed pool, " + std::to_string(js.out_lamports) +
                 " lamports for 1000 tokens");
      } catch (const std::exception& e) {
        log.warn(std::string("jupiter exit fallback NOT reachable (stuck exits go to the operator): ") + e.what());
      }
    } else {
      log.info("jupiter exit fallback off (set JUPITER_API_KEY to enable)");
    }

    Exec exec(cfg, *sh);
    std::string exec_err;
    if (!exec.connect_all(&exec_err)) throw std::runtime_error("Sender unreachable: " + exec_err);
    if (!exec_err.empty()) log.warn(exec_err);
    log.info("sender connected: " + cfg.sender.url + (cfg.jito.enabled ? " + jito " + cfg.jito.url : ""));
    if (preflight) {
      log.info("preflight passed");
      return 0;
    }

    Writer writer(cfg, *sh, log);
    writer.open();
    FrameRing ring(size_t(cfg.ring.slots), size_t(cfg.ring.slot_bytes));
    Engine engine(cfg, *sh, kp, &ring);
    engine.set_wallet_lamports(lamports, bal_slot);
    restore_positions(rpc, writer, engine, kp, log);
    if (cfg.threads.lock_memory && !lock_memory()) log.warn("mlockall failed (needs CAP_IPC_LOCK / ulimit -l)");

    Ingest ingest(cfg, *sh, ring, kp.pubkey(), log);
    auto hot = [&](const char* name, int cpu, auto&& fn) {
      return std::thread([&, name, cpu, fn]() mutable {
        name_thread(name);
        if (cpu >= 0 && !pin_thread(cpu)) log.warn(std::string(name) + ": could not pin to cpu " + std::to_string(cpu));
        if (!set_fifo(cfg.threads.fifo_priority)) log.warn(std::string(name) + ": SCHED_FIFO refused");
        fn();
      });
    };
    std::thread t_writer([&] { writer.run(); });
    std::thread t_fast([&] { run_aux_fast(cfg, *sh, kp.pubkey(), log); });
    std::thread t_req([&] { run_aux_req(cfg, *sh, kp, log); });
    std::thread t_leaders([&] { run_aux_leaders(cfg, *sh, log); });
    std::thread t_capture;
    if (!cfg.storage.capture_path.empty()) {
      t_capture = std::thread([&] {
        name_thread("pd-capture");
        FILE* f = std::fopen(cfg.storage.capture_path.c_str(), "ab");
        if (!f) { log.error("cannot open capture file"); return; }
        std::unique_ptr<uint8_t[]> buf(new uint8_t[size_t(cfg.ring.slot_bytes)]);
        uint64_t seq = ring.cursor(), lost = 0;
        while (!sh->stop.load()) {
          uint32_t len;
          uint64_t recv;
          int r = ring.copy_out(seq, buf.get(), len, recv);
          if (r == 0) { std::this_thread::sleep_for(std::chrono::milliseconds(1)); continue; }
          if (r < 0) { ++lost; seq = ring.cursor(); continue; }
          std::fwrite(&recv, 8, 1, f);
          std::fwrite(&len, 4, 1, f);
          std::fwrite(buf.get(), 1, len, f);
          ++seq;
        }
        std::fclose(f);
        if (lost) log.warn("capture lost " + std::to_string(lost) + " gaps");
      });
    }
    std::thread t_exec = hot("pd-exec", cfg.threads.exec_cpu, [&] { exec.run(); });
    std::thread t_logic = hot("pd-logic", cfg.threads.logic_cpu, [&] { engine.run(); });
    std::thread t_ingest = hot("pd-ingest", cfg.threads.ingest_cpu, [&] { ingest.run(); });
    log.info("running: ingest cpu " + std::to_string(cfg.threads.ingest_cpu) + ", logic cpu " +
             std::to_string(cfg.threads.logic_cpu) + ", exec cpu " + std::to_string(cfg.threads.exec_cpu));

    t_ingest.join();
    t_logic.join();
    t_exec.join();
    t_fast.join();
    t_req.join();
    t_leaders.join();  // after logic: it frees the leader tables
    if (t_capture.joinable()) t_capture.join();
    t_writer.join();
    for (size_t i = 0; i < MAX_POSITIONS; ++i) {
      const Position& p = engine.position(i);
      if (p.state != PosState::Empty)
        log.warn("shutdown with an open position in " + b58(p.mint) + " (state " + std::to_string(int(p.state)) +
                 "): it is restored on the next start");
    }
    log.info("stopped");
    return 0;
  } catch (const std::exception& e) {
    log.error(e.what());
    return 1;
  }
}
