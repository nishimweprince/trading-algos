#include "app/live.hpp"

#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <deque>
#include <cstring>
#include <map>
#include <atomic>
#include <cstdlib>
#include <mutex>
#include <set>
#include <memory>
#include <sstream>
#include <thread>
#include <vector>

#include "app/bundle_builder.hpp"
#include "app/arming.hpp"
#include "app/live_send.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "app/send_audit.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/rpc.hpp"
#include "core/usage.hpp"
#include "core/ring.hpp"
#include "feed/frames.hpp"
#include "feed/laserstream_client.hpp"
#include "feed/health.hpp"
#include "feed/resync.hpp"
#include "obs/alert.hpp"
#include "obs/log.hpp"
#include "obs/metrics.hpp"
#include "submit/jito.hpp"
#include "submit/leaders.hpp"
#include "submit/router.hpp"
#include "submit/sender.hpp"
#include "app/preconf_sim.hpp"
#include "core/sys.hpp"
#include "feed/preconf.hpp"
#include "risk/sender_budget.hpp"
#include "submit/tip_floor.hpp"

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
  // Live only (dry run was removed on 17 Nov 2026): the bot sends real bundles from the hot wallet. Every
  // precondition is checked here, before any stream opens; the armed set and the lookup tables are checked once
  // loaded below. Anything missing refuses the start. The recorder mode never builds or sends anything.
  if (!recorder) {
    std::vector<std::string> missing;
    if (::access(cfg.keys.hot_wallet_path.c_str(), R_OK) != 0) missing.push_back("hot wallet file " + cfg.keys.hot_wallet_path);
    if (cfg.keys.executor_program_id == "VERIFY") missing.push_back("keys.executor_program_id");
    if (cfg.mode.armed_path.empty()) missing.push_back("mode.armed_path");
    if (cfg.mode.alts_path.empty()) missing.push_back("mode.alts_path");
    if (cfg.jito.regions.empty()) missing.push_back("jito.regions");
    if (cfg.mode.funding_mode == "flash" && cfg.flash.reserves.empty()) missing.push_back("flash.reserves");
    if (!missing.empty()) {
      std::string all;
      for (const auto& m : missing) all += (all.empty() ? "" : ", ") + m;
      LOG_ERROR("live refused: missing %s", all.c_str());
      return 2;
    }
    LOG_WARN("LIVE: bundles are sent to Jito from the hot wallet (max %.3f SOL a trade, %d a slot, "
             "fee cap %.3f SOL a day, float floor %.2f SOL)",
             double(cfg.engine.max_trade_lamports) / 1e9, cfg.engine.max_bundles_per_slot, cfg.risk.max_daily_fee_spend_sol,
             cfg.risk.min_sol_float);
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

  // Signer: the hot wallet (mode 0600 enforced; required above). The recorder signs nothing and only needs an
  // address to watch, so without a wallet file it watches the fixed test key.
  Keypair wallet = test_keypair();
  if (::access(cfg.keys.hot_wallet_path.c_str(), R_OK) == 0) wallet = Keypair::load_file(cfg.keys.hot_wallet_path);
  if (!recorder && wallet.pubkey() == test_keypair().pubkey()) {
    LOG_ERROR("live refused: the signer is the fixed test key");
    return 2;
  }

  // Subscriptions: pool accounts over the pool shards; wallet and flash reserves on their own stream.
  std::vector<Pubkey> pool_keys = rt.store().subscribed_keys(), wallet_keys = pf.reserves;
  wallet_keys.push_back(wallet.pubkey());
  // The wallet's base-token accounts: own-capital balances, and where profit accrues.
  std::vector<std::pair<Pubkey, Pubkey>> wallet_base;  // (mint, the wallet's token account for it)
  for (const std::string& m : cfg.engine.base_mints) {
    Pubkey mint = parse_pubkey(m);
    wallet_base.emplace_back(mint, ata(wallet.pubkey(), mint, prog::TOKEN));
    wallet_keys.push_back(wallet_base.back().second);
  }
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
  LOG_INFO("%s: %zu pools, %zu cycles, %zu shards (%zu pool keys, %zu wallet/reserve keys)", recorder ? "recorder" : "live",
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

  // Send side: the real lookup tables (loaded below) and a recent blockhash.
  tx::AltManager alts;
  tx::BlockhashCache blockhash;
  risk::Limits limits(risk::limits_config(cfg));
  auto jito_http = [&cfg](const std::string& url) { return submit::make_rpc_http(url, cfg.helius.rpc_timeout_ms); };
  auto jito_client = [&](std::vector<std::string> regions) {
    return std::make_unique<submit::JitoClient>(std::move(regions), cfg.jito.block_engine_fmt, jito_http, cfg.jito.backoff_429_ms);
  };
  const bool live_send = !recorder;
  submit::Submitter submitter(live_send ? jito_client(cfg.jito.regions) : nullptr);
  Pubkey executor = cfg.keys.executor_program_id == "VERIFY" ? placeholder_executor() : parse_pubkey(cfg.keys.executor_program_id);
  BundleBuilder builder(cfg, rt, wallet, executor, alts, blockhash, limits, submitter);
  // Feed health gates every send and drives the alerts (the Nov 11-16 outage went unnoticed for 4.3 days).
  feed::FeedHealthConfig fhc;
  fhc.max_update_age_ms = cfg.risk.max_feed_lag_ms;
  fhc.flap_reconnects_per_min = cfg.risk.feed_flap_reconnects_per_min;
  fhc.alert_after_ms = int64_t(cfg.obs.alert_after_s) * 1000;
  fhc.recover_after_ms = int64_t(cfg.obs.alert_after_s) * 1000;
  fhc.repeat_ms = int64_t(cfg.obs.alert_repeat_s) * 1000;
  feed::FeedHealth health(fhc);
  obs::Alerter alerts(cfg.obs.alert_name);
  if (!recorder)
    alerts.send(std::string("started: LIVE (sending bundles), ") + std::to_string(pf.pools.size()) +
                " pools, " + std::to_string(shards.size()) + " streams");
  bool was_quota = false, was_kill = false;
  // Tip floor (spec §6.6): tip_min = max(jito.tip_min_lamports, Jito's landed-tip percentile), polled off-thread.
  std::unique_ptr<submit::TipFloorPoller> tip_floor;
  if (!recorder && cfg.jito.tip_floor_percentile > 0 && !cfg.jito.tip_floor_url.empty())
    tip_floor = std::make_unique<submit::TipFloorPoller>(cfg.jito.tip_floor_url, cfg.jito.tip_floor_percentile, cfg.jito.tip_floor_refresh_ms);
  // Learned tip state survives restarts (alpha and p_land per route class).
  auto save_tips = [&] {
    if (cfg.engine.tip_state_path.empty()) return;
    const std::string tmp = cfg.engine.tip_state_path + ".tmp";
    {
      std::ofstream f(tmp, std::ios::trunc);
      f << "# jito-arb tip state: class alpha_ppm attempts landed window_attempts window_landed\n" << rt.tips().save();
      if (!f) {
        LOG_WARN("tip state: cannot write %s", tmp.c_str());
        return;
      }
    }
    if (std::rename(tmp.c_str(), cfg.engine.tip_state_path.c_str()) != 0) LOG_WARN("tip state: cannot replace %s", cfg.engine.tip_state_path.c_str());
  };
  if (!cfg.engine.tip_state_path.empty()) {
    std::ifstream f(cfg.engine.tip_state_path);
    if (f) {
      std::stringstream ss;
      ss << f.rdbuf();
      if (rt.tips().load(ss.str())) LOG_INFO("tip state: %llu attempts restored from %s", (unsigned long long)rt.tips().attempts(), cfg.engine.tip_state_path.c_str());
      else LOG_WARN("tip state: %s is malformed, starting from the priors", cfg.engine.tip_state_path.c_str());
    }
  }
  uint64_t saved_tip_attempts = rt.tips().attempts();
  // Armed set: the pools whose lookup tables and token accounts exist; routes through any other pool are skipped.
  ArmedSet armed;
  if (!cfg.mode.armed_path.empty()) {
    std::ifstream af(cfg.mode.armed_path);
    std::stringstream as;
    as << af.rdbuf();
    std::string aerr;
    if (af && armed.load_json(as.str(), &aerr)) {
      builder.set_armed(&armed);
      LOG_INFO("armed set: %zu pools from %s", armed.size(), cfg.mode.armed_path.c_str());
    } else if (live_send) {
      LOG_ERROR("armed set %s unreadable (%s): refusing to trade every pool", cfg.mode.armed_path.c_str(), aerr.c_str());
      return 1;
    }
  }
  // Lookup tables (arm-accounts writes mode.alts_path), read from chain; live refuses to start without any.
  const bool real_tables = live_send && !cfg.mode.alts_path.empty();
  auto reload_tables = [&]() -> size_t {
    tx::AltManager fresh;
    size_t n = load_tables(rpc, cfg.mode.alts_path, fresh);
    if (n) alts = std::move(fresh);
    return n;
  };
  if (real_tables) {
    size_t n = 0;
    try {
      n = reload_tables();
    } catch (const std::exception& e) {
      LOG_ERROR("lookup tables: %s", e.what());
    }
    if (n) {
      LOG_INFO("lookup tables: %zu loaded from chain (%s)", n, cfg.mode.alts_path.c_str());
    } else {
      LOG_ERROR("no lookup tables in %s: run arm-accounts --send first; refusing to send", cfg.mode.alts_path.c_str());
      return 1;
    }
  } else if (live_send) {
    LOG_ERROR("mode.alts_path is not set: live needs the real lookup tables; refusing to send");
    return 1;
  }
  auto last_tables = std::chrono::steady_clock::now();
  // Landing feedback: Jito's verdict on each sent bundle, polled off the engine thread.
  std::unique_ptr<LandingWatcher> watcher;
  // Post-send check: every sent bundle is re-run from its exact bytes on a side thread (read-only), which tells a
  // lost race (MinOutNotMet) from a transaction that cannot execute and measures its compute use.
  std::unique_ptr<SendAudit> audit;
  if (live_send) audit = std::make_unique<SendAudit>(resolve_rpc_url(cfg));
  // Audit-measured compute use per route class, handed from the audit thread to the engine thread (tick).
  std::mutex cu_obs_mu;
  std::vector<std::pair<uint16_t, uint32_t>> cu_obs;
  // Relearn compute use after a restart from the audit lines already in the sent log (newest 2,000 lines).
  if (live_send && !cfg.obs.sent_log_path.empty()) {
    std::ifstream in(cfg.obs.sent_log_path);
    std::deque<std::string> lines;
    for (std::string l; std::getline(in, l);) {
      if (l.find("\"audit\":\"failed\"") != std::string::npos || l.find("\"units\":") == std::string::npos) continue;
      lines.push_back(std::move(l));
      if (lines.size() > 2000) lines.pop_front();
    }
    size_t n = 0;
    for (const std::string& l : lines) {
      auto num = [&](const char* key) -> uint64_t {
        auto p = l.find(key);
        return p == std::string::npos ? 0 : std::strtoull(l.c_str() + p + std::strlen(key), nullptr, 10);
      };
      uint64_t units = num("\"units\":"), rc = num("\"class\":");
      if (units && rc) { rt.tips().observe_cu(uint16_t(rc), uint32_t(units)); ++n; }
    }
    if (n) LOG_INFO("compute: %zu audit measurements restored from %s", n, cfg.obs.sent_log_path.c_str());
  }
  // Sent log: one JSON line per region send and per audit verdict, joined by the signature prefix.
  std::mutex sent_log_mu;
  std::ofstream sent_log;
  if (live_send && !cfg.obs.sent_log_path.empty()) sent_log.open(cfg.obs.sent_log_path, std::ios::app);
  auto log_line = [&](const std::string& line) {
    if (!sent_log.is_open()) return;
    std::lock_guard<std::mutex> g(sent_log_mu);
    sent_log << line << "\n" << std::flush;
  };
  auto json_str = [](const std::string& s) {
    std::string o = "\"";
    for (char ch : s) o += (ch == '"' || ch == '\\') ? std::string("\\") + ch : (unsigned char)ch < 0x20 ? std::string(" ") : std::string(1, ch);
    return o + "\"";
  };
  std::unique_ptr<submit::LeaderMap> leaders;
  std::unique_ptr<submit::Router> router;
  std::atomic<uint64_t> sent_kind[4] = {0, 0, 0, 0};  // by leader kind at queue time: unknown, jito, bam, not_jito
  std::mutex tracked_mu;
  std::set<std::string> tracked;  // bundle ids already handed to the watcher (every region returns the same id)
  if (live_send) {
    watcher = std::make_unique<LandingWatcher>(jito_client({cfg.jito.regions.front()}), cfg.jito.status_poll_ms, cfg.jito.status_timeout_ms);
    watcher->start();
    if (audit)
      audit->set_on_verdict([&](const std::string& label, AuditVerdict v, const std::string& err, uint64_t units, uint16_t tag) {
        log_line("{\"t\":" + std::to_string(wall_ms()) + ",\"sig\":" + json_str(label) + ",\"audit\":\"" +
                 (v == AuditVerdict::ok ? "ok" : v == AuditVerdict::min_out ? "min_out" : "failed") + "\",\"units\":" +
                 std::to_string(units) + ",\"class\":" + std::to_string(tag) + ",\"err\":" + json_str(err.substr(0, 200)) + "}");
        // A route that ran to its end (success, or the final min_out check) measured its real compute use.
        if (v != AuditVerdict::other && units) {
          std::lock_guard<std::mutex> g(cu_obs_mu);
          cu_obs.emplace_back(tag, uint32_t(units));
        }
      });
    if (cfg.jito.route_by_leader) {
      submit::RouterConfig rc;
      rc.block_engine_fmt = cfg.jito.block_engine_fmt;
      rc.regions = cfg.jito.regions;
      rc.always = cfg.jito.always_regions;
      rc.skip_not_jito = cfg.jito.skip_not_jito_leaders;
      rc.skip_bam = cfg.jito.skip_bam_leaders;
      rc.bam_fmt = cfg.jito.bam_dbi_fmt;
      if (const char* u = std::getenv(cfg.jito.bam_uuid_env.c_str())) rc.bam_uuid = u;
      rc.timeout_ms = cfg.jito.send_timeout_ms;
      rc.backoff_429_ms = cfg.jito.backoff_429_ms;
      rc.keepalive_s = cfg.jito.keepalive_s;
      leaders = std::make_unique<submit::LeaderMap>(resolve_rpc_url(cfg), cfg.jito.route_regions);
      router = std::make_unique<submit::Router>(
          rc, leaders.get(), &rt.clock().slot,
          [&](const submit::SendResult& r, const Signature& sig, uint16_t route_class, uint64_t built_slot, const submit::LeaderInfo& l,
              int64_t t) {
            if (!r.bundle_id.empty() && !r.bam) {
              std::lock_guard<std::mutex> g(tracked_mu);
              if (tracked.insert(r.bundle_id).second) watcher->track(r.bundle_id, sig, route_class, t);
              if (tracked.size() > 10'000) tracked.clear();
            }
            char nums[160];
            std::snprintf(nums, sizeof nums, ",\"status\":%d,\"rtt_ms\":%.2f,\"queued_ms\":%.2f,\"stale\":%s", r.http_status, r.rtt_us / 1000.0,
                          r.queued_us / 1000.0, r.dropped_stale ? "true" : "false");
            log_line("{\"t\":" + std::to_string(t) + ",\"slot\":" + std::to_string(built_slot) + ",\"sig\":" + json_str(b58(sig).substr(0, 12)) +
                     ",\"class\":" + std::to_string(route_class) + ",\"leader\":" + json_str(l.identity.substr(0, 8)) + ",\"kind\":\"" +
                     submit::leader_kind_name(l.kind) + "\",\"place\":" + json_str(l.place) + ",\"leader_region\":" + json_str(l.region) +
                     ",\"sent_region\":" + json_str(r.region + (r.bam ? "/bam" : "")) + nums + ",\"id\":" + json_str(r.bundle_id) +
                     ",\"err\":" + json_str(r.error.substr(0, 160)) + "}");
          });
      submitter.set_router(router.get());
      router->warm(cfg.jito.route_regions);  // every region's connection opens now, not on its first bundle
      LOG_INFO("routing: leader-aware to %zu regions (always %zu), BAM Direct Bundle Ingest %s", cfg.jito.route_regions.size(),
               cfg.jito.always_regions.size(), rc.bam_uuid.empty() ? "off (no UUID)" : "on");
      builder.set_queued_hook([&](const tx::SignedTx& t, uint16_t rc2) {
        submit::LeaderInfo l = leaders->at(rt.clock().slot.load());
        ++sent_kind[size_t(l.kind) & 3];
        // A back-run cannot be re-simulated alone: without its trigger the edge does not exist yet.
        if (audit && !rt.trigger_tx()) audit->submit(b64(t.bytes, t.len), b58(t.sig).substr(0, 12), rc2);
      });
    } else {
      builder.set_sent_hook([&](const std::string& id, const Signature& sig, uint16_t rc, int64_t now_ms) {
        watcher->track(id, sig, rc, now_ms);
        const tx::SignedTx& t = builder.last_tx();
        if (audit) audit->submit(b64(t.bytes, t.len), b58(t.sig).substr(0, 12), rc);
      });
    }
  }
  // Plain-transaction path (Helius Sender), off unless jito.sender_enabled. Each accepted send's outcome comes
  // from getSignatureStatuses: a failed one paid its fee, which counts against the daily cap and the §10 fee cap.
  risk::SenderBudgetConfig sbc;
  sbc.daily_fail_cap_lamports = cfg.jito.sender_daily_fail_cap_lamports;
  sbc.max_consecutive_fail = cfg.jito.sender_max_consecutive_fail;
  sbc.pause_s = cfg.jito.sender_pause_s;
  risk::SenderBudget sender_budget(sbc);
  std::unique_ptr<submit::SenderClient> sender, astralane;
  std::atomic<uint64_t> sender_fees_paid{0}, sender_tips_paid{0};
  // One client per plain-transaction route; all share the budget, the send log ("path" = route name) and the caps.
  auto make_plain = [&](submit::SenderClientConfig scc) {
    return std::make_unique<submit::SenderClient>(
        scc, resolve_rpc_url(cfg), &rt.clock().slot,
        [&](const submit::SenderSendResult& r, const submit::SenderJob& j) {
          // Refused or never sent: nothing can land, so nothing is at risk.
          if (!r.accepted) sender_budget.on_result(j.sig, risk::SenderBudget::Result::dropped, wall_ms());
          char nums[200];
          std::snprintf(nums, sizeof nums, ",\"status\":%d,\"rtt_ms\":%.2f,\"queued_ms\":%.2f,\"stale\":%s,\"fee\":%llu,\"cu_price\":%llu,\"cu_limit\":%u,\"tip\":%llu",
                        r.http_status, r.rtt_us / 1000.0, r.queued_us / 1000.0, r.dropped_stale ? "true" : "false",
                        (unsigned long long)j.fee_lamports, (unsigned long long)j.cu_price, j.cu_limit, (unsigned long long)j.tip_lamports);
          log_line("{\"t\":" + std::to_string(j.wall_ms) + ",\"slot\":" + std::to_string(j.built_slot) + ",\"sig\":" +
                   json_str(j.sig.substr(0, 12)) + ",\"class\":" + std::to_string(j.route_class) + ",\"path\":" + json_str(j.path) + nums +
                   ",\"accepted\":" + (r.accepted ? "true" : "false") + ",\"err\":" + json_str(r.error.substr(0, 160)) + "}");
        },
        [&](const submit::SenderJob& j, submit::SenderOutcome o, uint64_t landed_slot) {
          auto res = o == submit::SenderOutcome::landed   ? risk::SenderBudget::Result::landed
                     : o == submit::SenderOutcome::failed ? risk::SenderBudget::Result::failed
                                                          : risk::SenderBudget::Result::dropped;
          int64_t now = wall_ms();
          sender_budget.on_result(j.sig, res, now);
          if (o != submit::SenderOutcome::dropped) {
            limits.record_fee(now, j.fee_lamports);  // landed or failed, the fee was paid
            sender_fees_paid += j.fee_lamports;
          }
          if (o == submit::SenderOutcome::landed) sender_tips_paid += j.tip_lamports;
          log_line("{\"t\":" + std::to_string(now) + ",\"sig\":" + json_str(j.sig.substr(0, 12)) + ",\"path\":" + json_str(j.path) +
                   ",\"landing\":\"" + submit::sender_outcome_name(o) + "\",\"landed_slot\":" + std::to_string(landed_slot) + ",\"fee\":" +
                   std::to_string(o == submit::SenderOutcome::dropped ? 0 : j.fee_lamports) + "}");
          if (o == submit::SenderOutcome::landed)
            LOG_INFO("%s: %s LANDED in slot %llu", j.path.c_str(), j.sig.substr(0, 12).c_str(), (unsigned long long)landed_slot);
        });
  };
  risk::SenderFeeConfig sfc;
  sfc.fee_frac_ppm = cfg.jito.sender_fee_frac_ppm;
  sfc.max_cu_price = cfg.jito.sender_max_cu_price;
  sfc.min_cu_price = cfg.jito.sender_min_cu_price;
  sfc.base_fee_lamports = cfg.engine.base_fee_lamports;
  if (live_send && cfg.jito.sender_enabled) {
    submit::SenderClientConfig scc;
    scc.name = "sender";
    scc.url = resolve_sender_url(cfg);
    scc.timeout_ms = cfg.jito.send_timeout_ms;
    scc.keepalive_s = cfg.jito.sender_keepalive_s;
    scc.status_timeout_s = cfg.jito.sender_status_timeout_s;
    sender = make_plain(scc);
    risk::SenderFeeConfig f = sfc;
    f.tip_lamports = cfg.jito.sender_tip_lamports;
    builder.add_plain_path("sender", sender.get(), &sender_budget, f, risk::sender_tip_accounts(),
                           std::max<uint64_t>(cfg.jito.sender_tip_lamports, 5'000), false, cfg.helius.preconf_fallback_sender);
    LOG_INFO("plain route ON: Helius Sender (%s), tip %llu lamports, fee <= %.1f%% of gross, failed-fee cap %llu lamports/day",
             cfg.jito.sender_url.c_str(), (unsigned long long)cfg.jito.sender_tip_lamports, cfg.jito.sender_fee_frac_ppm / 1e4,
             (unsigned long long)cfg.jito.sender_daily_fail_cap_lamports);
  }
  if (live_send && cfg.jito.astralane_enabled) {
    const char* key = std::getenv(cfg.jito.astralane_key_env.c_str());
    if (!key || !*key) {
      LOG_WARN("plain route: astralane_enabled but %s is unset; Astralane stays off", cfg.jito.astralane_key_env.c_str());
    } else {
      submit::SenderClientConfig scc;
      scc.name = "astralane";
      scc.url = cfg.jito.astralane_url + (cfg.jito.astralane_url.find('?') == std::string::npos ? "?" : "&") + "api-key=" + key;
      scc.timeout_ms = cfg.jito.send_timeout_ms;
      scc.keepalive_s = cfg.jito.astralane_keepalive_s;
      scc.ping_body = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getHealth\"}";
      scc.status_timeout_s = cfg.jito.sender_status_timeout_s;
      astralane = make_plain(scc);
      risk::SenderFeeConfig f = sfc;
      f.tip_lamports = cfg.jito.astralane_tip_lamports;
      f.max_tip_share_ppm = cfg.jito.astralane_max_tip_share_ppm;
      builder.add_plain_path("astralane", astralane.get(), &sender_budget, f, risk::astralane_tip_accounts(),
                             cfg.jito.astralane_tip_lamports);
      LOG_INFO("plain route ON: Astralane (%s), tip %llu lamports when the tip is <= %.0f%% of gross", cfg.jito.astralane_url.c_str(),
               (unsigned long long)cfg.jito.astralane_tip_lamports, cfg.jito.astralane_max_tip_share_ppm / 1e4);
    }
  }
  // Preconfirmations: Helius preconfSubscribe on the armed pools -> simulation worker -> speculative pricing on
  // the engine thread (below, in the main loop) -> bundle + a Sender Max plain send.
  std::unique_ptr<submit::SenderClient> sender_max;
  std::unique_ptr<feed::CreditGuard> preconf_guard;
  std::unique_ptr<app::PreconfSim> preconf_sim;
  std::unique_ptr<feed::PreconfFeed> preconf_feed;
  uint64_t preconf_spec_pools = 0, preconf_late = 0, preconf_no_state = 0;
  if (live_send && cfg.helius.preconf_enabled) {
    const char* key = std::getenv(cfg.helius.api_key_env.c_str());
    if (!key || !*key || armed.size() == 0) {
      LOG_WARN("preconf: needs %s and an armed pool set; staying off", cfg.helius.api_key_env.c_str());
    } else {
      submit::SenderClientConfig scc;
      scc.name = "sender_max";
      scc.url = cfg.jito.sender_max_url + (cfg.jito.sender_max_url.find('?') == std::string::npos ? "?" : "&") + "api-key=" + key;
      scc.timeout_ms = cfg.jito.send_timeout_ms;
      scc.keepalive_s = cfg.jito.sender_keepalive_s;
      scc.status_timeout_s = cfg.jito.sender_status_timeout_s;
      sender_max = make_plain(scc);
      risk::SenderFeeConfig f = sfc;
      f.tip_lamports = cfg.jito.sender_max_tip_lamports;
      f.tip_frac_ppm = cfg.jito.sender_max_tip_frac_ppm;
      f.max_tip_share_ppm = cfg.jito.sender_max_max_tip_share_ppm;
      builder.add_plain_path("sender_max", sender_max.get(), &sender_budget, f, risk::sender_tip_accounts(), 50'000'000, true);
      builder.set_backrun(cfg.helius.preconf_backrun);
      preconf_guard = std::make_unique<feed::CreditGuard>(cfg.helius.preconf_daily_credit_cap);
      if (preconf_guard->load(cfg.helius.preconf_credits_path, wall_ms()))
        LOG_INFO("preconf: %llu credits already used today (from %s)", (unsigned long long)preconf_guard->today(wall_ms()),
                 cfg.helius.preconf_credits_path.c_str());
      std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_of;
      std::unordered_map<uint32_t, std::vector<Pubkey>> keys_of;
      feed::PreconfConfig pc;
      pc.host = cfg.helius.preconf_host;
      pc.api_key = key;
      pc.include_failed = cfg.helius.preconf_include_failed;
      pc.regions = cfg.helius.preconf_regions;
      for (const Pubkey& addr : armed.pools()) {
        auto id = rt.store().pool_id(addr);
        if (!id) continue;
        pool_of[addr] = *id;
        keys_of[*id] = rt.store().keys_of_pool(*id);
        pc.accounts.push_back(b58(addr));
      }
      preconf_sim = std::make_unique<app::PreconfSim>(cfg.helius.preconf_rpc_url + "/?api-key=" + key, std::move(pool_of), std::move(keys_of), preconf_guard.get());
      preconf_feed = std::make_unique<feed::PreconfFeed>(pc, preconf_guard.get(), [&](feed::PreconfTx&& t) { preconf_sim->push(std::move(t)); });
      preconf_feed->start();
      LOG_INFO("preconf: ON for %zu armed pools, Sender Max tip >= %llu lamports, credit cap %llu/day", pc.accounts.size(),
               (unsigned long long)cfg.jito.sender_max_tip_lamports, (unsigned long long)cfg.helius.preconf_daily_credit_cap);
    }
  }
  // Trigger mode: with "preconf", stream updates refresh state only and every trade comes from a preconfirmation.
  const bool preconf_only = cfg.helius.trigger == "preconf" && preconf_feed != nullptr;
  if (cfg.helius.trigger == "preconf" && !preconf_feed)
    LOG_WARN("trigger = preconf but preconfirmations are off: stream updates keep triggering trades");
  else if (preconf_only)
    LOG_INFO("trigger: preconfirmations only (the LaserStream feed keeps pool state current, it triggers nothing)");
  reg.gauge("arb_trigger_preconf_only", "1: trades come only from preconfirmations").set(preconf_only ? 1 : 0);
  // Wallet state from the wallet stream. Dry run signs with a key that holds nothing, so the values only reach
  // the float check and own-capital sizing when sending is live; the gauges show them either way.
  auto& g_wallet = reg.gauge("arb_wallet_lamports", "hot wallet SOL balance, lamports (-1 until seen)");
  g_wallet.set(-1);
  auto on_wallet = [&](uint64_t lamports, const uint8_t*, size_t, uint64_t) {
    g_wallet.set(int64_t(lamports));
    if (live_send) builder.set_wallet_lamports(lamports);
  };
  rt.feed().watch(wallet.pubkey(), on_wallet);
  std::vector<feed::FeedHandler::Watch> on_base;
  for (auto& [mint, account] : wallet_base) {
    auto& g = reg.gauge("arb_wallet_base_units_" + b58(mint).substr(0, 4), "hot wallet balance of this base token, base units (-1 until seen)");
    g.set(-1);
    on_base.push_back([&g, &rt, mint, live_send](uint64_t, const uint8_t* data, size_t len, uint64_t) {
      uint64_t amount = 0;
      if (len >= 72) std::memcpy(&amount, data + 64, 8);  // SPL token account amount
      g.set(int64_t(amount));
      if (live_send) rt.engine().set_own_balance(mint, amount);
    });
    rt.feed().watch(account, on_base.back());
  }
  if (!recorder) {
    try {
      std::vector<Pubkey> wk{wallet.pubkey()};
      for (auto& [mint, account] : wallet_base) wk.push_back(account);
      auto accs = rpc.multiple_accounts(wk, "processed");
      for (size_t i = 0; i < accs.size() && i < wk.size(); ++i) {
        if (!accs[i]) continue;
        if (i == 0) on_wallet(accs[i]->lamports, nullptr, 0, 0);
        else on_base[i - 1](accs[i]->lamports, accs[i]->data.data(), accs[i]->data.size(), 0);
      }
    } catch (const std::exception& e) {
      LOG_WARN("wallet read: %s", e.what());
    }
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
  int64_t last_blockhash_try = 0, last_resync_try = 0, last_bank_resync_try = 0, last_tables_try = 0;
  // Next to the decision logs (the data dir the dashboard reads).
  auto& g_decode = reg.gauge("arb_decode_failures", "account decode failures");
  auto& g_cand = reg.gauge("arb_engine_candidates", "candidates priced above min_ev");
  auto& g_rec = reg.gauge("arb_recorder_bytes", "bytes written by the recorder");
  auto& g_kill = reg.gauge("arb_kill_switch", "1 while the KILL file is present");
  auto& g_risk_blocked = reg.gauge("arb_bundles_risk_blocked", "candidates stopped by a limit: kill switch, fee cap, float, reject pause, feed lag");
  auto& g_armed = reg.gauge("arb_armed_pools", "pools in the armed set (0: no armed set, every pool)");
  auto& g_not_armed = reg.gauge("arb_bundles_not_armed", "routes skipped: through a pool outside the armed set");
  auto& g_tip_accounts = reg.gauge("arb_jito_tip_accounts", "tip accounts from the last getTipAccounts (0: the built-in list)");
  auto& g_landed = reg.gauge("arb_bundles_landed", "sent bundles Jito reported landed");
  auto& g_not_landed = reg.gauge("arb_bundles_not_landed", "sent bundles that did not land within the status timeout");
  auto& g_rejected = reg.gauge("arb_bundles_rejected", "sent bundles Jito reported invalid or failed");
  uint64_t n_landed = 0, n_not_landed = 0, n_rejected = 0;
  std::unique_ptr<submit::JitoClient> tip_client;
  int64_t next_tip_refresh_ms = wall_ms() + 5000;
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
  auto& g_untradable = reg.gauge("arb_engine_untradable", "evaluations skipped: a leg the program rejects in that direction");
  auto& g_implausible = reg.gauge("arb_engine_implausible_profit", "sized candidates skipped: profit above engine.max_profit_ratio");
  auto& g_no_room_memo = reg.gauge("arb_engine_no_room_memo", "evaluations skipped: x_max was below min_trade and nothing changed since");
  auto& g_signed = reg.gauge("arb_bundles_signed", "bundles built and signed");
  auto& g_sent = reg.gauge("arb_bundles_sent", "bundles accepted by at least one Jito region");
  auto& g_audit_ok = reg.gauge("arb_send_audit_ok", "sent bundles whose bytes would still execute when re-simulated");
  auto& g_audit_min = reg.gauge("arb_send_audit_min_out", "sent bundles that re-simulate to MinOutNotMet (edge gone: lost the race)");
  auto& g_audit_other = reg.gauge("arb_send_audit_failed", "sent bundles that re-simulate to any other error (the transaction is wrong)");
  auto& g_shards = reg.gauge("arb_feed_shards_up", "LaserStream streams currently up");
  auto& g_healthy = reg.gauge("arb_feed_healthy", "1 while the feed passes the health gate (streams up, not flapping, fresh)");
  auto& g_flaps = reg.gauge("arb_feed_reconnects_last_min", "stream reconnects across all shards in the last 60 s");
  auto& g_down_s = reg.gauge("arb_feed_unhealthy_seconds", "seconds the feed has failed the health gate (0 when healthy)");
  auto& g_status = reg.gauge("arb_feed_last_status_code", "gRPC status code of the latest stream end (-1: none)");
  auto& g_tip_floor = reg.gauge("arb_tip_floor_lamports", "Jito landed-tip percentile used as tip_min (0: not fetched or stale)");
  auto& g_alerts = reg.gauge("arb_alerts_sent", "alerts delivered to Telegram");
  auto& g_alerts_failed = reg.gauge("arb_alerts_failed", "alerts Telegram did not accept");
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
  LatGauges g_eval = lat("arb_eval_to_signed_us", "engine starts pricing the pool to bundle signed, us (detect minus queueing)");
  LatGauges g_pre = lat("arb_engine_prefilter_us", "per pool update with a cycle past the prefilter: prefilter stage, us");
  LatGauges g_size = lat("arb_engine_size_us", "per pool update with a cycle past the prefilter: sizing and pricing, us");
  LatGauges g_build = lat("arb_build_sign_us", "builder entry to signed bundle (instructions, guard, compile, sign), us");
  LatGauges g_step[4] = {lat("arb_build_step_setup_us", "build step over 1 h: limits and setup, us"),
                         lat("arb_build_step_ixs_us", "build step over 1 h: route instructions and sign guard, us"),
                         lat("arb_build_step_tables_us", "build step over 1 h: blockhash and lookup tables, us"),
                         lat("arb_build_step_sign_us", "build step over 1 h: compile message and sign, us")};
  auto& g_deferred = reg.gauge("arb_engine_deferred", "cycles past the prefilter not sized: below their update's top max_sized_per_update");
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

  const int frames_per_pass = std::max(1, cfg.helius.max_frames_per_pass);
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
      for (int k = 0; k < frames_per_pass && s.ring->peek(s.next, data, len, recv); ++k) {
        any = true;
        if (recorder) frames.write(data, len, recv, wall_ms());
        else rt.apply_frame(data, len, recv, wall_ms());
        s.ring->release(++s.next);
      }
    }
    if (any && !recorder) {
      if (preconf_only) rt.drain_changed();
      else rt.evaluate_changed();
    }
    if (preconf_sim) {
      app::SpecResult r;
      while (preconf_sim->pop(r)) {
        std::vector<AccountUpdate> views = r.views();
        std::vector<std::pair<uint32_t, std::shared_ptr<const PoolState>>> overlays;
        for (uint32_t id : r.pools) {
          // The stream already delivered a newer state for this pool: the normal path has priced it.
          if (uint64_t lu = rt.last_update_ns(id); lu > r.recv_ns) {
            ++preconf_late;
            rt.preconf_lead_us().add(lu, int64_t(lu - r.recv_ns) / 1000);
            continue;
          }
          rt.note_preconf(id, r.recv_ns);
          auto snap = rt.store().speculate(id, views);
          if (!snap) {
            ++preconf_no_state;
            continue;
          }
          overlays.emplace_back(id, std::move(snap));
        }
        preconf_spec_pools += overlays.size();
        rt.evaluate_speculative(overlays, std::max(r.slot, r.sim_slot), &r.tx, b58(r.sig));
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
      g_rec.set(int64_t(frames.bytes_written()));
      const bool kill_now = ::access(cfg.mode.kill_file.c_str(), F_OK) == 0;
      g_kill.set(kill_now ? 1 : 0);
      if (kill_now != was_kill) alerts.send(kill_now ? "KILL file present: sending stopped" : "KILL file removed");
      was_kill = kill_now;
      if (!recorder) {
        feed::FeedHealthSample hs;
        hs.now_ms = wall_ms();
        hs.shards_total = int(shards.size());
        int code = -1;
        for (auto& s : shards) {
          hs.shards_up += s.client->counters().up.load() ? 1 : 0;
          code = std::max(code, s.client->counters().last_status.load());
        }
        hs.reconnects_total = rc;
        uint64_t lu = rt.last_update_ns();
        hs.update_age_ms = lu ? int64_t((mono_ns() - std::min(mono_ns(), lu)) / 1'000'000) : -1;
        auto ev = health.update(hs);
        builder.set_feed_ok(health.ok());
        g_healthy.set(health.ok() ? 1 : 0);
        g_flaps.set(health.reconnects_last_min());
        g_down_s.set(health.ok() || health.down_since_ms() < 0 ? 0 : (hs.now_ms - health.down_since_ms()) / 1000);
        g_status.set(code);
        std::string why = health.reason() + (code > 0 ? " (last gRPC status " + std::to_string(code) + ")" : "");
        if (ev == feed::FeedEvent::down) alerts.send("FEED DOWN, sending paused: " + why);
        else if (ev == feed::FeedEvent::still_down)
          alerts.send("feed still down after " + std::to_string((hs.now_ms - health.down_since_ms()) / 60000) + " min: " + why);
        else if (ev == feed::FeedEvent::recovered) alerts.send("feed recovered");
        const bool quota = usage::quota_exhausted();
        if (quota != was_quota) alerts.send(quota ? "Helius RPC quota exhausted (429 max usage)" : "Helius RPC quota available again");
        was_quota = quota;
        g_alerts.set(int64_t(alerts.sent()));
        g_alerts_failed.set(int64_t(alerts.failed()));
      }
      g_risk_blocked.set(int64_t(builder.stats().risk_blocked));
      g_armed.set(int64_t(armed.size()));
      g_not_armed.set(int64_t(builder.stats().not_armed));
      // Jito's verdicts feed the tip controller (alpha and p_land per route class) and the reject pause.
      if (watcher)
        for (const auto& d : watcher->drain()) {
          const bool landed = d.result == submit::BundleTracker::Result::Landed;
          const bool rejected = d.result == submit::BundleTracker::Result::Rejected;
          rt.tips().record(d.route_class, landed);
          limits.record_submit(wall_ms(), rejected);
          ++(landed ? n_landed : rejected ? n_rejected : n_not_landed);
          if (landed) LOG_INFO("bundle %s landed in slot %llu", d.bundle_id.c_str(), (unsigned long long)d.landed_slot);
          // Persist Jito's verdict next to the send lines (the dashboard counts these; metrics reset on restart).
          log_line("{\"t\":" + std::to_string(wall_ms()) + ",\"id\":" + json_str(d.bundle_id) + ",\"landing\":\"" +
                   (landed ? "landed" : rejected ? "rejected" : "not_landed") + "\",\"landed_slot\":" + std::to_string(d.landed_slot) + "}");
        }
      g_landed.set(int64_t(n_landed));
      // Tables change when arm-accounts extends them: re-read once a minute (a handful of accounts, one RPC call).
      if (real_tables && now - last_tables >= std::chrono::seconds(60) && usage::may_call(last_tables_try, wall_ms())) {
        last_tables = now;
        try {
          reload_tables();
        } catch (const std::exception& e) {
          LOG_WARN("lookup tables: %s", e.what());
        }
      }
      {
        std::vector<std::pair<uint16_t, uint32_t>> obs_now;
        {
          std::lock_guard<std::mutex> g(cu_obs_mu);
          obs_now.swap(cu_obs);
        }
        for (auto [rc, units] : obs_now) {
          rt.tips().observe_cu(rc, units);
          LOG_INFO("compute: class %u used %u CU (%zu samples; limit %s)", unsigned(rc), units, rt.tips().cu_samples(rc),
                   rt.tips().cu_samples(rc) >= 3 ? "learned" : "still the estimate");
        }
      }
      if (rt.tips().attempts() != saved_tip_attempts) {
        saved_tip_attempts = rt.tips().attempts();
        save_tips();
      }
      if (tip_floor) {
        // A floor older than a minute is dropped (back to the config floor) rather than trusted.
        uint64_t fl = tip_floor->value(wall_ms(), 60'000);
        rt.tips().set_tip_floor_p25(fl);
        g_tip_floor.set(int64_t(fl));
      }
      g_not_landed.set(int64_t(n_not_landed));
      g_rejected.set(int64_t(n_rejected));
      // Tip accounts (read-only RPC). Jito says the list is stable; a short or failed
      // answer keeps the previous list.
      if (!recorder && cfg.jito.tip_accounts_refresh_s > 0 && !cfg.jito.regions.empty() && wall_ms() >= next_tip_refresh_ms) {
        next_tip_refresh_ms = wall_ms() + int64_t(cfg.jito.tip_accounts_refresh_s) * 1000;
        try {
          if (!tip_client) tip_client = jito_client({cfg.jito.regions.front()});
          // Only accounts already in the built-in list are accepted: an answer naming any other address (a
          // spoofed or compromised endpoint) must never redirect tips. A new real one needs a code change.
          std::vector<Pubkey> v;
          size_t unknown = 0;
          const auto& known = risk::default_tip_accounts();
          for (const Pubkey& k : tip_client->tip_accounts())
            if (std::find(known.begin(), known.end(), k) != known.end()) v.push_back(k);
            else ++unknown;
          if (unknown) LOG_WARN("getTipAccounts named %zu accounts outside the built-in list: ignored", unknown);
          if (v.size() >= 4) {
            g_tip_accounts.set(int64_t(v.size()));
            builder.set_tip_accounts(std::move(v));
          } else {
            LOG_WARN("getTipAccounts returned %zu accounts: keeping the current list", v.size());
          }
        } catch (const std::exception& e) {
          LOG_WARN("getTipAccounts: %s", e.what());
        }
      }
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
      g_implausible.set(int64_t(n.implausible));
      g_st_incomplete.set(int64_t(n.stale_incomplete));
      g_st_resync.set(int64_t(n.stale_resync));
      g_st_spread.set(int64_t(n.stale_spread));
      g_st_lag.set(int64_t(n.stale_lag));
      g_sp_edge.set(int64_t(n.spread_edge));
      g_sp_priced.set(int64_t(n.spread_priced));
      g_sp_gross.set(int64_t(n.spread_gross_lamports));
      g_signed.set(int64_t(builder.stats().signed_ok));
      g_sent.set(int64_t(builder.stats().sent));
      if (router) {
        reg.gauge("arb_router_accepted", "region sends Jito accepted (a bundle id)").set(int64_t(router->accepted()));
        reg.gauge("arb_router_failed", "region sends that failed or were refused").set(int64_t(router->failed()));
        reg.gauge("arb_router_dropped", "queued sends dropped: slot passed or queue full").set(int64_t(router->dropped()));
        reg.gauge("arb_bundles_leader_skipped", "signed bundles not sent: no Jito path to the slot's leaders").set(int64_t(builder.stats().leader_skipped));
        static const char* kinds[4] = {"unknown", "jito", "bam", "not_jito"};
        for (int k = 0; k < 4; ++k)
          reg.gauge(std::string("arb_bundles_sent_leader_") + kinds[k], "bundles queued, by the leader's kind at that slot").set(int64_t(sent_kind[k].load()));
        for (const auto& [region, st] : router->rtt_stats()) {
          std::string n = region;
          for (char& ch : n) if (ch == '/') ch = '_';
          reg.gauge("arb_jito_rtt_us_p50_" + n, "sendBundle round trip to this region, us (1 h)").set(st.p50);
          reg.gauge("arb_jito_rtt_us_p99_" + n, "sendBundle round trip to this region, us (1 h)").set(st.p99);
          reg.gauge("arb_jito_rtt_samples_" + n, "sendBundle round trips measured (1 h)").set(int64_t(st.count));
        }
        reg.gauge("arb_leader_map_ready", "1 once the leader schedule and Jito flags are loaded").set(leaders && leaders->ready() ? 1 : 0);
        for (const auto& [region, us] : router->keepalive_rtt())
          reg.gauge("arb_jito_keepalive_rtt_us_" + region, "latest keepalive round trip on the warm connection, us").set(us);
        reg.gauge("arb_jito_keepalives", "keepalive calls on idle block-engine connections").set(int64_t(router->keepalives()));
        reg.gauge("arb_jito_keepalive_failures", "keepalive calls that did not return 200").set(int64_t(router->keepalive_failures()));
      }
      if (preconf_feed) {
        const int64_t now_w = wall_ms();
        if (!preconf_guard->save(cfg.helius.preconf_credits_path, now_w)) LOG_WARN("preconf: cannot save %s", cfg.helius.preconf_credits_path.c_str());
        const uint64_t now_n = arb::now_ns();
        reg.gauge("arb_preconf_subscribed", "1 while the preconfSubscribe stream is open").set(preconf_feed->subscribed() ? 1 : 0);
        reg.gauge("arb_preconf_capped", "1 while off for the day at the credit cap").set(preconf_feed->capped() ? 1 : 0);
        reg.gauge("arb_preconf_messages", "preconfirmation messages received (billed)").set(int64_t(preconf_feed->messages()));
        reg.gauge("arb_preconf_duplicates", "messages already seen from the other source").set(int64_t(preconf_feed->duplicates()));
        reg.gauge("arb_preconf_failed_skipped", "preconfirmed transactions marked failed (skipped)").set(int64_t(preconf_feed->failed_skipped()));
        reg.gauge("arb_preconf_reconnects", "preconf stream reconnects").set(int64_t(preconf_feed->reconnects()));
        reg.gauge("arb_preconf_credits_today", "estimated Helius credits used by preconfs and their RPC calls today (UTC)").set(int64_t(preconf_guard->today(now_w)));
        reg.gauge("arb_preconf_credit_cap", "daily credit cap for preconfs").set(int64_t(preconf_guard->cap()));
        reg.gauge("arb_preconf_sims", "trigger transactions simulated").set(int64_t(preconf_sim->sims()));
        reg.gauge("arb_preconf_sim_reverted", "triggers that would fail now (skipped)").set(int64_t(preconf_sim->sim_reverted()));
        reg.gauge("arb_preconf_sim_errors", "simulation calls that errored").set(int64_t(preconf_sim->sim_failed()));
        reg.gauge("arb_preconf_no_pool", "preconfs whose pools could not be matched").set(int64_t(preconf_sim->no_pool()));
        reg.gauge("arb_preconf_dropped", "preconfs dropped: simulation queue full").set(int64_t(preconf_sim->dropped()));
        reg.gauge("arb_preconf_spec_pools", "pools priced on a simulated preconf state").set(int64_t(preconf_spec_pools));
        reg.gauge("arb_preconf_late", "preconf states that arrived after the stream's own update").set(int64_t(preconf_late));
        reg.gauge("arb_preconf_no_state", "simulated states that did not decode for the pool").set(int64_t(preconf_no_state));
        reg.gauge("arb_preconf_spec_sent", "opportunities sent from a preconf (bundle path)").set(int64_t(builder.stats().spec_sent));
        auto ss = preconf_sim->sim_stats(now_n);
        reg.gauge("arb_preconf_sim_us_p50", "preconf receipt to simulated state, us (1 h)").set(ss.count ? ss.p50 : -1);
        reg.gauge("arb_preconf_sim_us_p99", "preconf receipt to simulated state, us (1 h)").set(ss.count ? ss.p99 : -1);
        auto ls = rt.preconf_lead_us().stats(now_n);
        reg.gauge("arb_preconf_lead_us_p50", "preconf receipt to the stream's update of the same pool, us (1 h)").set(ls.count ? ls.p50 : -1);
        reg.gauge("arb_preconf_lead_us_p90", "preconf lead, 90th percentile, us").set(ls.count ? ls.p90 : -1);
        reg.gauge("arb_preconf_lead_samples", "preconf lead samples (1 h)").set(int64_t(ls.count));
      }
      if (sender_max) {
        reg.gauge("arb_sender_max_queued", "plain transactions queued on Sender Max (preconf-triggered)").set(int64_t(builder.plain_queued_by_path().count("sender_max") ? builder.plain_queued_by_path().at("sender_max") : 0));
        reg.gauge("arb_sender_max_landed", "Sender Max plain transactions that landed and succeeded").set(int64_t(sender_max->landed()));
        reg.gauge("arb_sender_max_failed", "Sender Max plain transactions that landed and failed").set(int64_t(sender_max->failed()));
        reg.gauge("arb_sender_max_dropped", "Sender Max plain transactions never seen on chain").set(int64_t(sender_max->dropped()));
      }
      if (astralane) {
        reg.gauge("arb_astralane_queued", "plain transactions queued for Astralane").set(int64_t(builder.plain_queued_by_path().count("astralane") ? builder.plain_queued_by_path().at("astralane") : 0));
        reg.gauge("arb_astralane_accepted", "plain transactions Astralane accepted").set(int64_t(astralane->accepted()));
        reg.gauge("arb_astralane_rejected", "plain transactions Astralane refused or that failed to send").set(int64_t(astralane->rejected()));
        reg.gauge("arb_astralane_landed", "Astralane plain transactions that landed and succeeded").set(int64_t(astralane->landed()));
        reg.gauge("arb_astralane_failed", "Astralane plain transactions that landed and failed (fee paid)").set(int64_t(astralane->failed()));
        reg.gauge("arb_astralane_dropped", "accepted Astralane transactions never seen on chain").set(int64_t(astralane->dropped()));
        reg.gauge("arb_astralane_ping_rtt_us", "latest Astralane keepalive round trip, us").set(astralane->ping_rtt_us());
      }
      if (sender || astralane) {
        const auto& bs = builder.stats();
        int64_t now = wall_ms();
        reg.gauge("arb_plain_queued", "plain transactions queued, every route").set(int64_t(bs.sender_queued));
        if (sender) {
          reg.gauge("arb_sender_queued", "plain transactions queued for Helius Sender").set(int64_t(builder.plain_queued_by_path().count("sender") ? builder.plain_queued_by_path().at("sender") : 0));
          reg.gauge("arb_sender_accepted", "plain transactions Sender accepted").set(int64_t(sender->accepted()));
          reg.gauge("arb_sender_rejected", "plain transactions Sender refused or that failed to send").set(int64_t(sender->rejected()));
          reg.gauge("arb_sender_stale", "plain transactions dropped before sending: slot passed").set(int64_t(sender->dropped_stale()));
          reg.gauge("arb_sender_landed", "plain transactions that landed and succeeded").set(int64_t(sender->landed()));
          reg.gauge("arb_sender_failed", "plain transactions that landed and failed (fee paid)").set(int64_t(sender->failed()));
          reg.gauge("arb_sender_dropped", "accepted plain transactions never seen on chain (no fee)").set(int64_t(sender->dropped()));
          reg.gauge("arb_sender_pending", "accepted plain transactions awaiting an outcome").set(int64_t(sender->pending()));
          reg.gauge("arb_sender_ping_rtt_us", "latest Sender /ping round trip, us").set(sender->ping_rtt_us());
        }
        reg.gauge("arb_sender_fee_skip", "opportunities whose gross cannot pay the Sender bid").set(int64_t(bs.sender_fee_skip));
        reg.gauge("arb_sender_budget_blocked", "plain sends blocked by the failed-fee cap or the pause").set(int64_t(bs.sender_budget_blocked));
        reg.gauge("arb_sender_build_failed", "plain variants that failed to build, guard or sign").set(int64_t(bs.sender_build_failed));
        reg.gauge("arb_sender_failed_fees_today", "lamports burnt by failed plain sends today (UTC)").set(int64_t(sender_budget.failed_today(now)));
        reg.gauge("arb_sender_at_risk", "lamports of fees on accepted sends without an outcome yet").set(int64_t(sender_budget.at_risk()));
        reg.gauge("arb_sender_fail_cap", "daily cap on failed-send fees, lamports").set(int64_t(cfg.jito.sender_daily_fail_cap_lamports));
        reg.gauge("arb_sender_paused", "1 while paused after a run of failures").set(sender_budget.paused(now) ? 1 : 0);
        reg.gauge("arb_sender_fees_paid", "fees paid by plain sends (landed + failed), lamports").set(int64_t(sender_fees_paid.load()));
        reg.gauge("arb_sender_tips_paid", "Sender tips paid by landed plain sends, lamports").set(int64_t(sender_tips_paid.load()));
      }
      if (audit) {
        g_audit_ok.set(int64_t(audit->ok()));
        g_audit_min.set(int64_t(audit->min_out()));
        g_audit_other.set(int64_t(audit->other()));
      }
      g_slot.set(int64_t(rt.clock().slot.load()));
      g_uptime.set(int64_t(std::chrono::duration_cast<std::chrono::seconds>(now - t0).count()));
      int up = 0;
      for (auto& s : shards) up += s.client->counters().up.load() ? 1 : 0;
      g_shards.set(up);
      int64_t q = 0;
      int64_t coverage_counts[6][5]{};
      for (uint32_t i = 0; i < rt.store().pool_count(); ++i) {
        auto p = rt.store().pool(i);
        const bool ready = p && p->quotable(uint64_t(cfg.engine.max_state_age_slots), rt.clock().slot.load(),
                                            uint64_t(std::max(0, cfg.engine.settle_slots)));
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
      put(g_eval, builder.eval_to_signed_us(), tnow);
      put(g_pre, rt.engine().prefilter_us(), tnow);
      put(g_size, rt.engine().size_us(), tnow);
      put(g_build, builder.build_sign_us(), tnow);
      for (int i = 0; i < 4; ++i) put(g_step[i], builder.build_step_us(i), tnow);
      g_deferred.set(int64_t(rt.engine().counters().deferred));
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
  if (watcher) watcher->shutdown();
  LOG_INFO("%s stopped: frames=%llu candidates=%llu sent=%llu recorder_bytes=%llu", recorder ? "recorder" : "live",
           (unsigned long long)rt.stats().frames, (unsigned long long)rt.engine().counters().candidates,
           (unsigned long long)builder.stats().sent, (unsigned long long)frames.bytes_written());
  return 0;
}

}  // namespace arb::app
