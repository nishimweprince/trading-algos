#include "app/live.hpp"

#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <map>
#include <set>
#include <memory>
#include <sstream>
#include <thread>
#include <vector>

#include "app/bundle_builder.hpp"
#include "app/bundle_sim.hpp"
#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "core/base58.hpp"
#include "core/rpc.hpp"
#include "core/usage.hpp"
#include "core/ring.hpp"
#include "feed/frames.hpp"
#include "feed/laserstream_client.hpp"
#include "feed/resync.hpp"
#include "obs/log.hpp"
#include "obs/metrics.hpp"
#include "submit/jito.hpp"

namespace arb::app {

namespace {

uint64_t mono_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
}
int64_t wall_ms() {
  return int64_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count());
}

struct Shard {
  std::unique_ptr<FrameRing> ring;
  std::unique_ptr<feed::LaserstreamClient> client;
  std::thread thread;
  uint64_t next = 0;
};

constexpr size_t RING_SLOTS = 1024, RING_SLOT_BYTES = 16 << 10;  // whirlpool tick arrays are ~10 KB
constexpr int MAX_FRAMES_PER_PASS = 256;  // per shard, before the engine prices the batch's changed pools

}  // namespace

int run_live(const Config& cfg, bool recorder, std::atomic<bool>& stop, int run_seconds) {
  if (!cfg.mode.dry_run) {
    LOG_ERROR("dry_run = false is refused in this build: mainnet sends start at phase 4, after the owner reviews the "
              "phase-2 replay gate and the phase-3 validator tests (RUNBOOK-VM.md)");
    return 2;
  }
  std::ifstream f(cfg.pools_path);
  if (!f) {
    LOG_ERROR("pools file %s not found: run `discover` first", cfg.pools_path.c_str());
    return 1;
  }
  std::stringstream ss;
  ss << f.rdbuf();
  PoolsFile pf;
  std::string err;
  if (!from_json(ss.str(), pf, &err)) {
    LOG_ERROR("%s", err.c_str());
    return 1;
  }
  Runtime rt(cfg, pf);
  auto& reg = obs::Registry::get();

  // Signer: the hot wallet when its file exists (mode 0600 enforced), else the fixed dry-run key.
  Keypair wallet = dry_run_keypair();
  if (::access(cfg.keys.hot_wallet_path.c_str(), R_OK) == 0) wallet = Keypair::load_file(cfg.keys.hot_wallet_path);
  else LOG_WARN("no hot wallet at %s: dry run signs with the fixed test key", cfg.keys.hot_wallet_path.c_str());

  // Subscriptions: pool accounts over the pool shards; wallet and flash reserves on their own stream.
  std::vector<Pubkey> pool_keys = rt.store().subscribed_keys(), wallet_keys = pf.reserves;
  wallet_keys.push_back(wallet.pubkey());
  std::vector<Pubkey> only_pools;
  for (const Pubkey& k : pool_keys)
    if (std::find(wallet_keys.begin(), wallet_keys.end(), k) == wallet_keys.end()) only_pools.push_back(k);
  auto plan = feed::plan_subscriptions(only_pools, wallet_keys, cfg.helius.max_grpc_connections, cfg.helius.shard_size);
  if (plan.overflow) LOG_WARN("subscription overflow: %zu keys do not fit %d x %d", plan.overflow, cfg.helius.max_grpc_connections, cfg.helius.shard_size);
  std::vector<feed::ShardSpec> specs = plan.pool_shards;
  specs.push_back(plan.wallet_shard);
  std::vector<Shard> shards(specs.size());
  for (size_t i = 0; i < specs.size(); ++i) {
    shards[i].ring = std::make_unique<FrameRing>(RING_SLOTS, RING_SLOT_BYTES);
    shards[i].client = std::make_unique<feed::LaserstreamClient>(cfg, "shard" + std::to_string(i), *shards[i].ring, stop);
    shards[i].client->set_spec(specs[i]);
  }
  LOG_INFO("%s: %zu pools, %zu cycles, %zu shards (%zu pool keys, %zu wallet/reserve keys)", recorder ? "recorder" : "live dry run",
           pf.pools.size(), rt.cycles().size(), shards.size(), only_pools.size(), wallet_keys.size());

  // Streams first, then the initial state by RPC: once every stream is subscribed, no change after the read
  // can be missed, so the read is usable at once (quiet pools are quotable without waiting for a trade).
  // Frames that arrive meanwhile queue in the rings; older ones lose to the read by (slot, write_version).
  for (auto& s : shards) s.thread = std::thread([&s] { s.client->run(); });
  auto all_up = [&] {
    for (auto& s : shards)
      if (!s.client->counters().up.load()) return false;
    return true;
  };
  Rpc rpc(resolve_rpc_url(cfg), cfg.helius.rpc_timeout_ms);
  if (!recorder) {
    auto wait_until = std::chrono::steady_clock::now() + std::chrono::seconds(15);
    while (!all_up() && !stop && std::chrono::steady_clock::now() < wait_until)
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    // `up` is set when the stream opens, a moment before the server applies the subscription request.
    if (all_up()) std::this_thread::sleep_for(std::chrono::seconds(1));
    std::vector<uint32_t> ids(rt.store().pool_count());
    for (uint32_t i = 0; i < ids.size(); ++i) ids[i] = i;
    std::vector<Pubkey> keys = feed::pool_ordered_keys(rt.store(), ids);
    std::set<Pubkey> have(keys.begin(), keys.end());  // a linear find here was quadratic: ~40 s at 46k keys
    for (const Pubkey& k : pool_keys)  // reserves and anything else subscribed
      if (have.insert(k).second) keys.push_back(k);
    bool live = all_up();
    try {
      auto st = feed::resync(rpc, rt.store(), keys, live);
      LOG_INFO("initial resync: %zu applied, %zu missing (%s)", st.applied, st.missing,
               live ? "streams up: usable at once" : "streams not all up: waiting for pushes");
    } catch (const std::exception& e) {
      LOG_WARN("initial resync failed: %s", e.what());
    }
  }

  // Send side (dry run): virtual lookup tables until discovery's real ones exist; real recent blockhash.
  tx::AltManager alts;
  tx::BlockhashCache blockhash;
  risk::Limits limits(risk::limits_config(cfg));
  submit::Submitter submitter(true, nullptr);
  Pubkey executor = cfg.keys.executor_program_id == "VERIFY" ? dry_run_executor() : parse_pubkey(cfg.keys.executor_program_id);
  BundleBuilder builder(cfg, rt, wallet, executor, alts, blockhash, limits, submitter);
  builder.set_virtual_tables(true);
  BundleSim sim(cfg, stop);
  if (!recorder && cfg.sim.enabled) {
    builder.set_simulator(&sim);
    sim.start();
  }
  obs::DecisionLog dlog(cfg.obs.decision_log_dir, cfg.obs.decision_log_rotate_s);
  feed::FrameWriter frames(cfg.recorder.dir, cfg.recorder.rotate_s);
  if (!recorder) {
    rt.set_decision_log(&dlog);
    rt.set_send_hook([&](const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& r) { builder(o, c, r); });
  }

  auto& g_frames = reg.gauge("arb_feed_frames", "frames received (all shards)");
  auto& g_bytes = reg.gauge("arb_feed_bytes", "bytes received (all shards)");
  auto& g_reconnects = reg.gauge("arb_feed_reconnects", "stream reconnects (all shards)");
  auto& g_age = reg.gauge("arb_feed_update_age_ms", "age of the newest account update");
  auto& g_quota = reg.gauge("arb_helius_quota_exhausted", "1 while Helius answers 429 max usage (polling backs off to 1/min)");
  auto& g_quota_s = reg.gauge("arb_helius_quota_exhausted_seconds", "seconds since the quota ran out (0 when available)");
  // Usage per source, for the plan budget: RPC calls/bytes by method, stream updates/bytes by account kind.
  std::map<std::string, obs::Gauge*> usage_gauges;
  auto usage_gauge = [&](const std::string& name, const char* help) -> obs::Gauge& {
    auto it = usage_gauges.find(name);
    if (it == usage_gauges.end()) it = usage_gauges.emplace(name, &reg.gauge(name, help)).first;
    return *it->second;
  };
  int64_t last_blockhash_try = 0, last_resync_try = 0, last_bank_resync_try = 0;
  // Next to the decision logs (the data dir the dashboard reads).
  std::string quarantine_path = cfg.obs.decision_log_dir + "/../quarantine.json";
  auto& g_decode = reg.gauge("arb_decode_failures", "account decode failures");
  auto& g_cand = reg.gauge("arb_engine_candidates", "candidates priced above min_ev");
  auto& g_dry = reg.gauge("arb_bundles_dry_run", "bundles built and signed in dry run");
  auto& g_rec = reg.gauge("arb_recorder_bytes", "bytes written by the recorder");
  auto& g_kill = reg.gauge("arb_kill_switch", "1 while the KILL file is present");
  auto& g_skip_logs = reg.gauge("arb_decision_skip_logs_dropped", "skipped decision records sampled out; signed candidates and outcomes are never sampled here");
  auto& g_pools = reg.gauge("arb_pools", "tracked pools");
  auto& g_cycles = reg.gauge("arb_cycles", "indexed cycles");
  auto& g_quotable = reg.gauge("arb_pools_quotable", "pools whose snapshot is quotable now");
  dex::RaydiumV4Quoter coverage_ray; dex::WhirlpoolQuoter coverage_wp; dex::CpmmQuoter coverage_cp;
  dex::DlmmQuoter coverage_dl; dex::ClmmQuoter coverage_cl; dex::PumpSwapQuoter coverage_pump;
  const dex::IQuoter* coverage_quoters[] = {&coverage_ray,&coverage_wp,&coverage_cp,&coverage_dl,&coverage_cl,&coverage_pump};
  const bool coverage_enabled[] = {cfg.dex.raydium_v4_enabled,cfg.dex.whirlpool_enabled,cfg.dex.cpmm_enabled,
      cfg.dex.dlmm_enabled,cfg.dex.clmm_enabled,cfg.dex.pumpswap_enabled};
  obs::Gauge* coverage_gauges[6][5]{};
  const char* coverage_fields[] = {"tracked","complete","ready","supported","unsupported"};
  for (uint8_t d=0; d<6; ++d) {
    for (size_t f=0; f<5; ++f)
      coverage_gauges[d][f]=&reg.gauge(std::string("arb_pools_")+coverage_fields[f]+"_"+dex_name(DexKind(d)),
          "per-DEX coverage; support checks require complete snapshots and are separate from readiness/enablement");
    reg.gauge(std::string("arb_dex_enabled_")+dex_name(DexKind(d)),"DEX quoting explicitly enabled").set(coverage_enabled[d]);
  }

  auto& g_slot = reg.gauge("arb_slot", "slot clock (newest slot seen)");
  auto& g_uptime = reg.gauge("arb_uptime_seconds", "seconds since start");
  auto& g_updates = reg.gauge("arb_engine_pool_updates", "pool updates evaluated");
  auto& g_seen = reg.gauge("arb_engine_cycles_seen", "cycle evaluations");
  auto& g_stale = reg.gauge("arb_engine_stale", "evaluations skipped: a leg not fresh");
  auto& g_unsup = reg.gauge("arb_engine_unsupported", "evaluations skipped: a leg's DEX not quoted");
  auto& g_pass = reg.gauge("arb_engine_prefilter_pass", "cycles past the spot-edge prefilter");
  auto& g_sized = reg.gauge("arb_engine_sized", "cycles sized");
  auto& g_skipped = reg.gauge("arb_engine_skipped", "sized candidates not dispatched");
  auto& g_st_incomplete = reg.gauge("arb_engine_stale_incomplete", "stale: a pool component not yet seen");
  auto& g_st_resync = reg.gauge("arb_engine_stale_resync", "stale: a resync read awaiting its stream push");
  auto& g_st_spread = reg.gauge("arb_engine_stale_spread", "stale: pool account and vaults written over max_state_age_slots apart");
  auto& g_st_lag = reg.gauge("arb_engine_stale_lag", "stale: snapshot behind the slot clock by over max_slot_lag");
  auto& g_sp_edge = reg.gauge("arb_engine_spread_edge", "spread-stale cycles past the spot-edge prefilter (shadow, never sent)");
  auto& g_sp_priced = reg.gauge("arb_engine_spread_priced", "spread-stale cycles that sized and priced as a candidate (shadow)");
  auto& g_sp_gross = reg.gauge("arb_engine_spread_gross_lamports", "gross of spread-stale priced cycles (shadow), lamports");
  auto& g_sim_submitted = reg.gauge("arb_sim_submitted", "dry-run simulation jobs submitted since start");
  auto& g_sim_queued = reg.gauge("arb_sim_queued", "simulation jobs waiting for RPC");
  auto& g_sim_inflight = reg.gauge("arb_sim_in_flight", "simulation jobs doing RPC");
  auto& g_sim_terminal = reg.gauge("arb_sim_terminal", "simulation terminal outcomes (all categories)");
  auto& g_sim_per_leg = reg.gauge("arb_sim_per_leg", "routes too large for one message, simulated leg by leg");
  auto& g_sim_matched = reg.gauge("arb_sim_state_matched", "simulations whose legs' vaults equalled the engine's snapshot");
  auto& g_sim_drifted = reg.gauge("arb_sim_state_drifted", "simulations whose legs' vaults had moved since the snapshot (state drift)");
  auto& g_sim_suspect = reg.gauge("arb_sim_quoter_suspect", "failed or short simulations on matched state (quoter suspect)");
  auto& g_sim_funding = reg.gauge("arb_sim_funding_changed", "holder account movement invalidated diagnostic");
  auto& g_sim_inventory = reg.gauge("arb_sim_intermediate_balance", "fixed-input route consumed holder inventory");
  auto& g_sim_ok = reg.gauge("arb_sim_ok", "dry-run bundles whose legs simulated without error");
  auto& g_sim_failed = reg.gauge("arb_sim_failed", "dry-run bundles whose legs failed in simulation");
  auto& g_sim_profitable = reg.gauge("arb_sim_profitable", "simulated bundles still profitable after tip and fee");
  auto& g_sim_dropped = reg.gauge("arb_sim_dropped", "dry-run bundles not simulated (rate cap, queue full or stale)");
  auto& g_sim_stale = reg.gauge("arb_sim_stale", "dry-run bundles dropped at dequeue: queued longer than sim.max_wait_ms");
  auto& g_sim_no_holder = reg.gauge("arb_sim_no_holder", "dry-run bundles with no public holder of the trade size");
  auto& g_sim_too_large = reg.gauge("arb_sim_too_large", "dry-run bundles whose direct-swap message exceeds 1,232 bytes");
  auto& g_sim_context_retries = reg.gauge("arb_sim_context_retries", "bounded retries while the simulation bank catches up to the unchanged minimum context slot");
  auto& g_sim_setup = reg.gauge("arb_sim_setup_error", "simulation setup or unclassified message errors; excludes failed swap legs");
  auto& g_sim_rpc = reg.gauge("arb_sim_rpc_error", "simulation RPC errors");
  auto& g_untradable = reg.gauge("arb_engine_untradable", "evaluations skipped: a leg the program rejects in that direction");
  auto& g_quarantined = reg.gauge("arb_engine_quarantined", "evaluations skipped: a leg quarantined by failed or short simulations");
  auto& g_implausible = reg.gauge("arb_engine_implausible_profit", "sized candidates skipped: profit above engine.max_profit_ratio");
  auto& g_quarantine_pools = reg.gauge("arb_quarantined_pools", "pool directions quarantined now");
  auto& g_no_room_memo = reg.gauge("arb_engine_no_room_memo", "evaluations skipped: x_max was below min_trade and nothing changed since");
  auto& g_signed = reg.gauge("arb_bundles_signed", "bundles built and signed");
  auto& g_shards = reg.gauge("arb_feed_shards_up", "LaserStream streams currently up");
  // Latency windows (last 60 s), microseconds: p50 / p99 / max and the sample count.
  struct LatGauges {
    obs::Gauge *p50, *p99, *max, *n;
  };
  auto lat = [&](const std::string& name, const std::string& help) {
    return LatGauges{&reg.gauge(name + "_p50", help + " (p50, last 60 s)"), &reg.gauge(name + "_p99", help + " (p99, last 60 s)"),
                     &reg.gauge(name + "_max", help + " (max, last 60 s)"), &reg.gauge(name + "_samples", help + " (samples, last 60 s)")};
  };
  LatGauges g_lag = lat("arb_feed_lag_us", "account update received minus the server's created_at, us");
  LatGauges g_off = lat("arb_slot_offset_us", "account update received minus its slot's start, us");
  LatGauges g_det = lat("arb_detect_to_signed_us", "frame received to bundle signed, us");
  LatGauges g_sim_mis = lat("arb_sim_mismatch_ppm", "simulated vs quoted final out, ppm (signed; 1 h window)");
  LatGauges g_sim_lag = lat("arb_sim_lag_slots", "slots from the candidate to its simulation (1 h window)");
  obs::LatencyWindow sim_mismatch(3600), sim_lag(3600);
  risk::Limits sim_limits(risk::limits_config(cfg));
  uint8_t sim_tripped = 0;
  LatGauges g_edge = lat("arb_sized_edge_ppm", "spot edge the prefilter saw on sized cycles, ppm");
  LatGauges g_fprof = lat("arb_failed_profit_ppm", "best realized profit / size on cycles that sized to no profit, ppm");
  struct DiagGauge {
    const char* name;
    uint64_t engine::SizerDiag::*field;
    obs::Gauge* g;
  };
  DiagGauge diag_gauges[] = {
      {"arb_sizer_fail_no_room", &engine::SizerDiag::no_room, nullptr},
      {"arb_sizer_fail_no_profit", &engine::SizerDiag::no_profit, nullptr},
      {"arb_sizer_fail_partial_fill", &engine::SizerDiag::partial_fill, nullptr},
      {"arb_sizer_fail_quote_error", &engine::SizerDiag::quote_error, nullptr},
      {"arb_sizer_fail_zero_out", &engine::SizerDiag::zero_out, nullptr},
      {"arb_sizer_fail_boundary", &engine::SizerDiag::boundary, nullptr},
      {"arb_sizer_fail_boundary_cache", &engine::SizerDiag::boundary_cache, nullptr},
      {"arb_sizer_fail_other", &engine::SizerDiag::other, nullptr},
      {"arb_sizer_zero_liquidity_leg", &engine::SizerDiag::zero_liquidity_leg, nullptr},
  };
  for (auto& d : diag_gauges) d.g = &reg.gauge(d.name, "sized cycles that failed, by reason (since start)");
  auto last_example = std::chrono::steady_clock::now();
  auto& g_bank_updates = reg.gauge("arb_feed_updates_with_bank", "account updates carrying a bank_id (Alpenglow, Agave 4.3)");
  auto& g_created_banks = reg.gauge("arb_feed_created_banks", "CreatedBank slot statuses seen");
  auto& g_contested = reg.gauge("arb_feed_contested_slots", "slots with updates from two or more banks");
  auto& g_resolved = reg.gauge("arb_feed_resolved_slots", "contested slots resolved when their bank was confirmed");
  auto& g_bank_resyncs = reg.gauge("arb_feed_bank_resyncs", "accounts re-read because a losing bank had written them");
  auto last_bank_resync = std::chrono::steady_clock::now();
  auto& g_slot_ms = reg.gauge("arb_slot_time_ms", "measured slot duration over the last minute (config chain.slot_ms is the assumed one)");
  auto slot_mark_t = std::chrono::steady_clock::now();
  uint64_t slot_mark = 0;
  auto put = [](LatGauges& g, obs::LatencyWindow& w, uint64_t now) {
    auto st = w.stats(now);
    g.p50->set(st.p50);
    g.p99->set(st.p99);
    g.max->set(st.max);
    g.n->set(int64_t(st.count));
  };
  g_pools.set(int64_t(pf.pools.size()));
  g_cycles.set(int64_t(rt.cycles().size()));

  auto t0 = std::chrono::steady_clock::now();
  auto last_tick = t0, last_resync = t0;
  std::vector<uint8_t> buf(RING_SLOT_BYTES);
  while (!stop) {
    bool any = false;
    for (auto& s : shards) {
      const uint8_t* data;
      uint32_t len;
      uint64_t recv;
      // Bounded per shard per pass: shards take turns, and the once-a-second tick runs under any backlog.
      for (int k = 0; k < MAX_FRAMES_PER_PASS && s.ring->peek(s.next, data, len, recv); ++k) {
        any = true;
        if (recorder) frames.write(data, len, recv, wall_ms());
        else rt.apply_frame(data, len, recv, wall_ms());
        s.ring->release(++s.next);
      }
    }
    if (any && !recorder) rt.evaluate_changed();
    auto now = std::chrono::steady_clock::now();
    if (now - last_tick >= std::chrono::seconds(1)) {
      last_tick = now;
      uint64_t fr = 0, by = 0, rc = 0;
      for (auto& s : shards) {
        fr += s.client->counters().frames.load();
        by += s.client->counters().bytes.load();
        rc += s.client->counters().reconnects.load();
      }
      g_frames.set(int64_t(fr));
      g_bytes.set(int64_t(by));
      g_reconnects.set(int64_t(rc));
      g_decode.set(int64_t(rt.store().decode_failures()));
      g_cand.set(int64_t(rt.engine().counters().candidates));
      g_dry.set(int64_t(builder.stats().dry_run));
      g_rec.set(int64_t(frames.bytes_written()));
      g_kill.set(::access(cfg.mode.kill_file.c_str(), F_OK) == 0 ? 1 : 0);
      const auto& n = rt.engine().counters();
      g_updates.set(int64_t(n.updates));
      g_seen.set(int64_t(n.cycles_seen));
      g_stale.set(int64_t(n.stale));
      g_unsup.set(int64_t(n.unsupported));
      g_pass.set(int64_t(n.prefilter_pass));
      g_sized.set(int64_t(n.sized));
      g_skipped.set(int64_t(n.skipped));
      g_untradable.set(int64_t(n.untradable));
      g_no_room_memo.set(int64_t(n.no_room_memo));
      g_quarantined.set(int64_t(n.quarantined));
      g_implausible.set(int64_t(n.implausible));
      // Simulation outcomes: into the decision log, the quote-mismatch feed (record-only in dry run) and gauges.
      for (const obs::DecisionRecord& r : sim.drain()) {
        if (!dlog.write(r)) LOG_ERROR("cannot persist simulation outcome");
        if (r.outcome == uint8_t(obs::Outcome::SimulatedOk) && r.min_out) {
          int64_t mis = int64_t((__int128(r.final_out) - __int128(r.min_out)) * 1'000'000 / __int128(r.min_out));
          sim_mismatch.add(mono_ns(), mis);
          // Exact parity uses coherent snapshots. These delayed diagnostics include state drift, so record
          // per-DEX mismatch policy only on same-slot single-DEX routes; never attribute a route delta to all legs.
          bool same_dex = true;
          for (int i = 1; i < r.hops; ++i) same_dex &= r.dex[i] == r.dex[0];
          if (r.slot == r.state_slot[0] && same_dex) sim_limits.record_sim(r.dex[0], std::abs(mis) > 1000);
        }
        if (r.slot && r.state_slot[0]) sim_lag.add(mono_ns(), int64_t(r.slot) - int64_t(r.state_slot[0]));
        // Feed the verdict back to dispatch: a route that failed at leg k marks legs 1..k (a short output from an
        // earlier leg makes a later one fail), a short success marks every leg, a clean success clears them.
        // Not-simulated outcomes (too large, no holder, RPC, setup) say nothing about the pools, and neither does
        // a simulation that ran more than strike_max_lag_slots after the candidate.
        if (app::sim_verdict_usable(r, cfg.sim.strike_max_lag_slots)) {
          int bad_upto = -1;  // legs [0, bad_upto) are bad; 0 = all good; -1 = no verdict
          if (r.outcome == uint8_t(obs::Outcome::SimulatedOk) && r.min_out) {
            int64_t short_bps = int64_t((__int128(r.min_out) - __int128(r.final_out)) * 10'000 / __int128(r.min_out));
            bad_upto = short_bps > cfg.sim.short_bps ? r.hops : 0;
          } else if (r.outcome == uint8_t(obs::Outcome::SimulationFailed) && r.reason >= 1 && r.reason <= 3) {
            bad_upto = r.reason;
          }
          bool per_leg = r.pad0[0] == 1;  // legs simulated alone: each verdict is that leg's own
          if (bad_upto >= 0)
            for (int i = 0; i < r.hops; ++i) {
              bool bad = i < bad_upto;
              if (per_leg)
                bad = r.outcome == uint8_t(obs::Outcome::SimulationFailed) ? i == r.reason - 1
                                                                          : int64_t(r.state_seq[i]) > cfg.sim.short_bps;
              if (auto id = rt.store().pool_id(key_from(r.pools[i])))
                rt.engine().note_sim(*id, (r.a_to_b_mask >> i) & 1, bad, wall_ms());
            }
        }
        // Runtime errors are classified in the outcome/sidecar; they are not proof of an amount-out mismatch.
      }
      // Record-only: a separate Limits instance, so a mismatch rate (which here includes state drift between
      // the candidate and its simulation) never blocks dry-run bundles; logged once when a DEX would trip.
      for (uint8_t d = 0; d < 8; ++d)
        if (sim_limits.dex_disabled(d) && !(sim_tripped & (1u << d))) {
          sim_tripped |= uint8_t(1u << d);
          LOG_WARN("bundle sim: %s would be disabled by the quote-mismatch rate (record-only in dry run)", dex_name(DexKind(d)));
        }
      const auto& sc = sim.counters();
      g_sim_submitted.set(int64_t(sc.submitted.load()));
      g_sim_queued.set(int64_t(sc.queued.load()));
      g_sim_inflight.set(int64_t(sc.in_flight.load()));
      g_sim_terminal.set(int64_t(sc.terminal.load()));
      g_sim_per_leg.set(int64_t(sc.per_leg.load()));
      g_sim_matched.set(int64_t(sc.state_matched.load()));
      g_sim_drifted.set(int64_t(sc.state_drifted.load()));
      g_sim_suspect.set(int64_t(sc.quoter_suspect.load()));
      g_sim_funding.set(int64_t(sc.funding_changed.load()));
      g_sim_inventory.set(int64_t(sc.intermediate_balance.load()));
      g_sim_ok.set(int64_t(sc.ok.load()));
      g_sim_failed.set(int64_t(sc.failed.load()));
      g_sim_profitable.set(int64_t(sc.profitable.load()));
      g_sim_dropped.set(int64_t(sc.dropped.load()));
      g_sim_stale.set(int64_t(sc.stale.load()));
      g_sim_no_holder.set(int64_t(sc.no_holder.load()));
      g_sim_too_large.set(int64_t(sc.too_large.load()));
      g_sim_context_retries.set(int64_t(sc.context_retries.load()));
      g_sim_setup.set(int64_t(sc.setup_error.load()));
      g_sim_rpc.set(int64_t(sc.rpc_error.load()));
      g_st_incomplete.set(int64_t(n.stale_incomplete));
      g_st_resync.set(int64_t(n.stale_resync));
      g_st_spread.set(int64_t(n.stale_spread));
      g_st_lag.set(int64_t(n.stale_lag));
      g_sp_edge.set(int64_t(n.spread_edge));
      g_sp_priced.set(int64_t(n.spread_priced));
      g_sp_gross.set(int64_t(n.spread_gross_lamports));
      g_signed.set(int64_t(builder.stats().signed_ok));
      g_slot.set(int64_t(rt.clock().slot.load()));
      g_uptime.set(int64_t(std::chrono::duration_cast<std::chrono::seconds>(now - t0).count()));
      int up = 0;
      for (auto& s : shards) up += s.client->counters().up.load() ? 1 : 0;
      g_shards.set(up);
      int64_t q = 0;
      int64_t coverage_counts[6][5]{};
      for (uint32_t i = 0; i < rt.store().pool_count(); ++i) {
        auto p = rt.store().pool(i);
        const bool ready = p && p->quotable(uint64_t(cfg.engine.max_state_age_slots));
        q += ready ? 1 : 0;
        if (p && uint8_t(p->kind)<6) {
          const uint8_t d=uint8_t(p->kind);
          ++coverage_counts[d][0]; coverage_counts[d][1]+=p->complete; coverage_counts[d][2]+=ready;
          if (p->complete) ++coverage_counts[d][coverage_quoters[d]->supports(*p)?3:4];
        }
      }
      g_skip_logs.set(int64_t(rt.stats().skip_logs_dropped));
      g_quotable.set(q);
      for (uint8_t d=0; d<6; ++d) for (size_t f=0; f<5; ++f) coverage_gauges[d][f]->set(coverage_counts[d][f]);
      uint64_t tnow = mono_ns();
      put(g_lag, rt.feed().feed_lag_us(), tnow);
      put(g_off, rt.feed().slot_offset_us(), tnow);
      put(g_det, builder.detect_to_signed_us(), tnow);
      put(g_sim_mis, sim_mismatch, tnow);
      put(g_sim_lag, sim_lag, tnow);
      // Measured slot time, once a minute: chain.slot_ms should match it (slots went 400 -> 200 ms in Oct 2026).
      if (uint64_t sl = rt.clock().slot.load(); slot_mark == 0) {
        slot_mark = sl;
        slot_mark_t = now;
      } else if (now - slot_mark_t >= std::chrono::seconds(60) && sl > slot_mark) {
        double ms = std::chrono::duration<double, std::milli>(now - slot_mark_t).count() / double(sl - slot_mark);
        g_slot_ms.set(int64_t(ms + 0.5));
        if (std::abs(ms - cfg.chain.slot_ms) > 0.25 * cfg.chain.slot_ms)
          LOG_WARN("measured slot time %.0f ms differs from chain.slot_ms %d by over 25%%", ms, cfg.chain.slot_ms);
        slot_mark = sl;
        slot_mark_t = now;
      }
      const auto& bk = rt.feed().banks();
      g_bank_updates.set(int64_t(bk.updates_with_bank));
      g_created_banks.set(int64_t(bk.created_banks));
      g_contested.set(int64_t(bk.contested_slots));
      g_resolved.set(int64_t(bk.resolved_slots));
      g_bank_resyncs.set(int64_t(bk.accounts_resynced));
      put(g_edge, rt.engine().sized_edge_ppm(), tnow);
      put(g_fprof, rt.engine().failed_profit_ppm(), tnow);
      const auto& dg = rt.engine().sizer_diag();
      for (auto& d : diag_gauges) d.g->set(int64_t(dg.*(d.field)));
      {
        // Quarantine list for the dashboard: <data dir>/quarantine.json, rewritten each second.
        auto q = rt.engine().quarantined(wall_ms());
        g_quarantine_pools.set(int64_t(q.size()));
        std::string js = "{\"at_ms\":" + std::to_string(wall_ms()) + ",\"pools\":[";
        for (size_t i = 0; i < q.size(); ++i) {
          const PoolDef& d = rt.store().def(q[i].pool);
          js += (i ? "," : "") + std::string("{\"pool\":\"") + b58(d.address) + "\",\"dex\":\"" + dex_name(d.kind) +
                "\",\"dir\":\"" + (q[i].a_to_b ? "a>b" : "b>a") + "\",\"until_ms\":" + std::to_string(q[i].until_ms) +
                ",\"strikes\":" + std::to_string(q[i].strikes) + "}";
        }
        js += "]}\n";
        std::string qp = quarantine_path + ".tmp";
        if (FILE* f = std::fopen(qp.c_str(), "w")) {
          std::fwrite(js.data(), 1, js.size(), f);
          std::fclose(f);
          std::rename(qp.c_str(), quarantine_path.c_str());
        }
      }
      if (now - last_example >= std::chrono::seconds(30)) {
        last_example = now;
        // The largest predicted edge that sized to no profit: what the prefilter believed vs what quoting found.
        for (const engine::SizerExample& ex : rt.engine().take_examples()) {
          const engine::Cycle& c = rt.cycles().cycle(ex.cycle);
          std::string legs;
          for (int i = 0; i < c.n; ++i) {
            auto p = rt.store().pool(c.legs[i].pool);
            char buf[320];
            if (p->kind == DexKind::Whirlpool)
              std::snprintf(buf, sizeof buf, " | %swp %s %s liq=%llu tick=%d sqrt=%llu.. spacing=%u fee=%u arrays=%u", i == ex.fail_leg ? "*" : "",
                            b58(p->address).substr(0, 8).c_str(), c.legs[i].a_to_b ? "a>b" : "b>a", (unsigned long long)uint64_t(p->wp.liquidity),
                            p->wp.tick_current_index, (unsigned long long)uint64_t(p->wp.sqrt_price >> 64), unsigned(p->wp.tick_spacing),
                            unsigned(p->wp.fee_rate), unsigned(p->n_ticks));
            else if (p->kind != DexKind::RaydiumV4)
              std::snprintf(buf, sizeof buf, " | %s%s %s %s a=%llu b=%llu", i == ex.fail_leg ? "*" : "", dex_name(p->kind),
                            b58(p->address).substr(0, 8).c_str(), c.legs[i].a_to_b ? "a>b" : "b>a", (unsigned long long)p->vault_a_amount,
                            (unsigned long long)p->vault_b_amount);
            else
              std::snprintf(buf, sizeof buf, " | %sray %s %s a=%llu b=%llu pnl=%llu/%llu status=%llu fee=%llu/%llu", i == ex.fail_leg ? "*" : "",
                            b58(p->address).substr(0, 8).c_str(), c.legs[i].a_to_b ? "a>b" : "b>a", (unsigned long long)p->vault_a_amount,
                            (unsigned long long)p->vault_b_amount, (unsigned long long)p->ray.need_take_pnl_coin,
                            (unsigned long long)p->ray.need_take_pnl_pc, (unsigned long long)p->ray.status,
                            (unsigned long long)p->ray.swap_fee_numerator, (unsigned long long)p->ray.swap_fee_denominator);
            legs += buf;
          }
          LOG_INFO("sizer example [%s]: cycle %u base %s predicted edge %.2f bps, x_max %llu, min_trade %llu, best x %llu profit %lld, err %s%s",
                   engine::reason_name(ex.reason), ex.cycle, b58(c.base).substr(0, 4).c_str(), double(ex.edge_ppm) / 100.0,
                   (unsigned long long)ex.x_max, (unsigned long long)ex.min_trade, (unsigned long long)ex.best_x, (long long)ex.best_profit,
                   ex.reason == engine::Reason::sizer_quote_error ? errc_name(ex.err) : "-", legs.c_str());
        }
        // Pools whose pool/vault slot spread kept edges out of the engine (shadow-priced): is the rule too strict?
        for (const engine::SpreadExample& e : rt.engine().take_spread_examples(5)) {
          auto p = rt.store().pool(e.pool);
          if (!p) continue;
          LOG_INFO("spread stale: %s %s pool_slot %llu vaults %llu/%llu, %llu past prefilter, %llu priced, best net %lld (cycle %u)",
                   dex_name(p->kind), b58(p->address).substr(0, 8).c_str(), (unsigned long long)p->pool_slot,
                   (unsigned long long)p->vault_a_slot, (unsigned long long)p->vault_b_slot, (unsigned long long)e.edge_hits,
                   (unsigned long long)e.priced, (long long)e.best_net, e.best_cycle);
        }
      }
      uint64_t last = rt.last_update_ns();
      g_age.set(last ? int64_t((mono_ns() - std::min(mono_ns(), last)) / 1'000'000) : -1);
      g_quota.set(usage::quota_exhausted() ? 1 : 0);
      g_quota_s.set(usage::quota_exhausted() ? (wall_ms() - usage::quota_since_ms()) / 1000 : 0);
      for (const auto& [method, m] : usage::rpc_snapshot()) {
        usage_gauge("arb_rpc_calls_" + method, "RPC calls of this method since start").set(int64_t(m.calls));
        usage_gauge("arb_rpc_bytes_in_" + method, "RPC response bytes of this method since start").set(int64_t(m.bytes_in));
        usage_gauge("arb_rpc_rate_limited_" + method, "429 answers to this method since start").set(int64_t(m.rate_limited));
      }
      {
        const auto& tr = rt.feed().traffic();
        for (size_t k = 0; k < tr.size(); ++k) {
          if (!tr[k].updates) continue;
          std::string name = k < ACCOUNT_KIND_COUNT ? account_kind_name(AccountKind(k)) : "unknown";
          usage_gauge("arb_feed_updates_" + name, "stream account updates of this kind since start").set(int64_t(tr[k].updates));
          usage_gauge("arb_feed_bytes_" + name, "stream account data bytes of this kind since start").set(int64_t(tr[k].bytes));
        }
      }
      if (!recorder && blockhash.needs_refresh(wall_ms()) && usage::may_call(last_blockhash_try, wall_ms())) {
        try {
          auto bh = rpc.latest_blockhash("processed");
          blockhash.set(bh.hash, bh.slot, wall_ms());
        } catch (const std::exception& e) {
          LOG_WARN("blockhash: %s", e.what());
        }
      }
    }
    // Accounts a losing bank wrote: re-read them as soon as the slot's bank is confirmed (rare: equivocation,
    // or fast leader handoff from Agave 4.4), at most every 250 ms.
    if (!recorder && now - last_bank_resync >= std::chrono::milliseconds(250) && usage::may_call(last_bank_resync_try, wall_ms())) {
      std::vector<Pubkey> keys = rt.feed().take_resync();
      if (!keys.empty()) {
        last_bank_resync = now;
        std::sort(keys.begin(), keys.end());
        keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
        try {
          feed::resync(rpc, rt.store(), keys, all_up());
          LOG_INFO("bank resync: %zu accounts written by a losing bank re-read", keys.size());
        } catch (const std::exception& e) {
          LOG_WARN("bank resync: %s", e.what());
        }
      }
    }
    // Resync (spec §4): pools whose snapshot lags the slot clock by more than resync_lag_ms, every 10 s,
    // on this thread (the StateStore has a single writer).
    if (!recorder && now - last_resync >= std::chrono::seconds(10) && usage::may_call(last_resync_try, wall_ms())) {
      last_resync = now;
      uint64_t clock = rt.clock().slot.load();
      std::vector<uint32_t> lagging;
      for (uint32_t i = 0; i < rt.store().pool_count() && clock; ++i) {
        auto p = rt.store().pool(i);
        if (p && clock > p->slot + ms_to_slots(cfg, cfg.helius.resync_lag_ms)) lagging.push_back(i);
      }
      if (!lagging.empty()) {
        try {
          // Quiet pools are refreshed here about once a minute; with every stream up the read is usable at once.
          feed::resync(rpc, rt.store(), feed::pool_ordered_keys(rt.store(), lagging), all_up());
        } catch (const std::exception& e) {
          LOG_WARN("resync: %s", e.what());
        }
      }
    }
    if (run_seconds > 0 && now - t0 >= std::chrono::seconds(run_seconds)) stop = true;
    if (!any) std::this_thread::sleep_for(std::chrono::microseconds(50));
  }
  for (auto& s : shards) s.thread.join();
  sim.shutdown();
  for (const auto& r : sim.drain())
    if (!dlog.write(r)) LOG_ERROR("cannot persist final simulation outcome");
  LOG_INFO("%s stopped: frames=%llu candidates=%llu dry_run_bundles=%llu recorder_bytes=%llu", recorder ? "recorder" : "live",
           (unsigned long long)rt.stats().frames, (unsigned long long)rt.engine().counters().candidates,
           (unsigned long long)builder.stats().dry_run, (unsigned long long)frames.bytes_written());
  return 0;
}

}  // namespace arb::app
