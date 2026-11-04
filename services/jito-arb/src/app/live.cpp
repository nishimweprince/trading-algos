#include "app/live.hpp"

#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <fstream>
#include <memory>
#include <sstream>
#include <thread>
#include <vector>

#include "app/bundle_builder.hpp"
#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "core/base58.hpp"
#include "core/rpc.hpp"
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
    for (const Pubkey& k : pool_keys)  // reserves and anything else subscribed
      if (std::find(keys.begin(), keys.end(), k) == keys.end()) keys.push_back(k);
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
  auto& g_decode = reg.gauge("arb_decode_failures", "account decode failures");
  auto& g_cand = reg.gauge("arb_engine_candidates", "candidates priced above min_ev");
  auto& g_dry = reg.gauge("arb_bundles_dry_run", "bundles built and signed in dry run");
  auto& g_rec = reg.gauge("arb_recorder_bytes", "bytes written by the recorder");
  auto& g_kill = reg.gauge("arb_kill_switch", "1 while the KILL file is present");
  auto& g_pools = reg.gauge("arb_pools", "tracked pools");
  auto& g_cycles = reg.gauge("arb_cycles", "indexed cycles");
  auto& g_quotable = reg.gauge("arb_pools_quotable", "pools whose snapshot is quotable now");
  auto& g_slot = reg.gauge("arb_slot", "slot clock (newest slot seen)");
  auto& g_uptime = reg.gauge("arb_uptime_seconds", "seconds since start");
  auto& g_updates = reg.gauge("arb_engine_pool_updates", "pool updates evaluated");
  auto& g_seen = reg.gauge("arb_engine_cycles_seen", "cycle evaluations");
  auto& g_stale = reg.gauge("arb_engine_stale", "evaluations skipped: a leg not fresh");
  auto& g_unsup = reg.gauge("arb_engine_unsupported", "evaluations skipped: a leg's DEX not quoted");
  auto& g_pass = reg.gauge("arb_engine_prefilter_pass", "cycles past the spot-edge prefilter");
  auto& g_sized = reg.gauge("arb_engine_sized", "cycles sized");
  auto& g_skipped = reg.gauge("arb_engine_skipped", "sized candidates not dispatched");
  auto& g_untradable = reg.gauge("arb_engine_untradable", "evaluations skipped: a leg the program rejects in that direction");
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
      {"arb_sizer_fail_other", &engine::SizerDiag::other, nullptr},
      {"arb_sizer_zero_liquidity_leg", &engine::SizerDiag::zero_liquidity_leg, nullptr},
  };
  for (auto& d : diag_gauges) d.g = &reg.gauge(d.name, "sized cycles that failed, by reason (since start)");
  auto last_example = std::chrono::steady_clock::now();
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
      while (s.ring->peek(s.next, data, len, recv)) {
        any = true;
        if (recorder) frames.write(data, len, recv, wall_ms());
        else rt.on_frame(data, len, recv, wall_ms());
        s.ring->release(++s.next);
      }
    }
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
      g_signed.set(int64_t(builder.stats().signed_ok));
      g_slot.set(int64_t(rt.clock().slot.load()));
      g_uptime.set(int64_t(std::chrono::duration_cast<std::chrono::seconds>(now - t0).count()));
      int up = 0;
      for (auto& s : shards) up += s.client->counters().up.load() ? 1 : 0;
      g_shards.set(up);
      int64_t q = 0;
      for (uint32_t i = 0; i < rt.store().pool_count(); ++i) {
        auto p = rt.store().pool(i);
        q += p && p->quotable(uint64_t(cfg.engine.max_state_age_slots)) ? 1 : 0;
      }
      g_quotable.set(q);
      uint64_t tnow = mono_ns();
      put(g_lag, rt.feed().feed_lag_us(), tnow);
      put(g_off, rt.feed().slot_offset_us(), tnow);
      put(g_det, builder.detect_to_signed_us(), tnow);
      put(g_edge, rt.engine().sized_edge_ppm(), tnow);
      put(g_fprof, rt.engine().failed_profit_ppm(), tnow);
      const auto& dg = rt.engine().sizer_diag();
      for (auto& d : diag_gauges) d.g->set(int64_t(dg.*(d.field)));
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
      }
      uint64_t last = rt.last_update_ns();
      g_age.set(last ? int64_t((mono_ns() - std::min(mono_ns(), last)) / 1'000'000) : -1);
      if (!recorder && blockhash.needs_refresh(wall_ms())) {
        try {
          auto bh = rpc.latest_blockhash("processed");
          blockhash.set(bh.hash, bh.slot, wall_ms());
        } catch (const std::exception& e) {
          LOG_WARN("blockhash: %s", e.what());
        }
      }
    }
    // Resync (spec §4): pools whose snapshot lags the slot clock by more than resync_lag_slots, every 10 s,
    // on this thread (the StateStore has a single writer).
    if (!recorder && now - last_resync >= std::chrono::seconds(10)) {
      last_resync = now;
      uint64_t clock = rt.clock().slot.load();
      std::vector<uint32_t> lagging;
      for (uint32_t i = 0; i < rt.store().pool_count() && clock; ++i) {
        auto p = rt.store().pool(i);
        if (p && clock > p->slot + uint64_t(cfg.helius.resync_lag_slots)) lagging.push_back(i);
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
  LOG_INFO("%s stopped: frames=%llu candidates=%llu dry_run_bundles=%llu recorder_bytes=%llu", recorder ? "recorder" : "live",
           (unsigned long long)rt.stats().frames, (unsigned long long)rt.engine().counters().candidates,
           (unsigned long long)builder.stats().dry_run, (unsigned long long)frames.bytes_written());
  return 0;
}

}  // namespace arb::app
