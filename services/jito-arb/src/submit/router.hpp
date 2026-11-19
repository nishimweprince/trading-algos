#pragma once
// Leader-aware, asynchronous bundle routing (18 Nov 2026). Until now the engine thread posted each bundle to every
// configured region one after another, blocking on each HTTP round trip. Now each region has its own sender thread
// and kept-alive connection; the engine only enqueues. A bundle goes to the regions nearest the leaders of its
// built slot and the next one, plus `always` regions; slots whose leaders run no Jito are skipped. With a BAM
// Direct Bundle Ingest UUID (Jito allowlist), BAM-led slots are also sent to the nearest BAM node, skipping the
// block engine's hop.
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "core/types.hpp"
#include "obs/latency.hpp"
#include "submit/jito.hpp"
#include "submit/leaders.hpp"

namespace arb::submit {

struct RouterConfig {
  std::string block_engine_fmt = "https://%s.mainnet.block-engine.jito.wtf";
  std::vector<std::string> regions;          // regions routing may use
  std::vector<std::string> always;           // sent to every time (e.g. the co-located region)
  bool skip_not_jito = true;                 // both target leaders run no Jito: no path can land it
  bool skip_bam = false;                     // both target leaders run BAM (only if they prove unreachable)
  std::string bam_fmt = "http://%s.mainnet.bam.jito.wtf:9090";
  std::string bam_uuid;                      // empty: no BAM Direct Bundle Ingest
  int timeout_ms = 2000;
  int backoff_429_ms = 1000;
  // Block-engine connections are opened when a sender starts and kept warm with a getTipAccounts call after this
  // many idle seconds (0 = off): the first Tokyo send on 18 Nov took 646 ms, mostly the TLS handshake.
  int keepalive_s = 45;
  /** Transport per (url, extra headers); default: kept-alive TLS (make_rpc_http). Tests inject a mock. */
  std::function<std::unique_ptr<HttpPost>(const std::string& url, const std::string& headers)> http;
};

/** Which regions a bundle goes to, given the leaders of its slot and the next. Pure, for tests. */
struct RoutePlan {
  bool skip = false;
  std::string skip_reason;
  std::vector<std::string> block_engine;  // region names
  std::vector<std::string> bam;           // BAM DBI regions (only with a UUID)
};
RoutePlan plan_route(const RouterConfig& c, const LeaderInfo& now, const LeaderInfo& next);

struct SendResult {
  std::string region;
  bool bam = false;
  int http_status = 0;  // 0: transport error or dropped
  std::string bundle_id, error;
  int64_t rtt_us = 0, queued_us = 0;
  bool dropped_stale = false;
};

class Router {
 public:
  using Done = std::function<void(const SendResult&, const Signature& sig, uint16_t route_class, uint64_t built_slot,
                                  const LeaderInfo& leader, int64_t wall_ms)>;
  /** `current_slot`: the live slot clock, read by senders to drop bundles whose slot has passed. */
  Router(RouterConfig c, LeaderMap* leaders, const std::atomic<uint64_t>* current_slot, Done done);
  ~Router();
  Router(const Router&) = delete;
  Router& operator=(const Router&) = delete;
  /** Enqueue (never blocks). Skipped when the plan says so; returns the plan. */
  /** `prefix`: transactions placed ahead of ours in the same bundle (a preconfirmed trigger we back-run). */
  RoutePlan route(const std::string& tx_b64, const Signature& sig, uint16_t route_class, uint64_t built_slot, int64_t wall_ms,
                  const std::vector<std::string>* prefix = nullptr);
  /** Open the block-engine connection of every region now (and keep it warm), instead of on its first bundle. */
  void warm(const std::vector<std::string>& regions);
  /** Latest keepalive round trip per region, us (-1 until one succeeded). */
  std::map<std::string, int64_t> keepalive_rtt();
  uint64_t keepalives() const { return keepalives_.load(); }
  uint64_t keepalive_failures() const { return keepalive_failures_.load(); }
  /** Round-trip times per region over the last hour (p50, p99, samples), for the metrics. */
  std::map<std::string, obs::LatencyStats> rtt_stats();
  uint64_t accepted() const { return accepted_.load(); }
  uint64_t failed() const { return failed_.load(); }
  uint64_t dropped() const { return dropped_.load(); }
  uint64_t skipped() const { return skipped_.load(); }

 private:
  struct Job {
    std::string body;
    Signature sig{};
    uint16_t route_class = 0;
    uint64_t built_slot = 0;
    LeaderInfo leader;
    int64_t wall_ms = 0;
    uint64_t enq_ns = 0;
  };
  struct Sender {
    std::string region;
    bool bam = false;
    std::unique_ptr<HttpPost> http;
    std::mutex mu;
    std::condition_variable cv;
    std::deque<Job> q;
    int64_t backoff_until_ms = 0;
    int64_t last_post_ms = 0;
    std::thread th;
  };
  void run(Sender& s);
  Sender* sender(const std::string& region, bool bam);
  RouterConfig c_;
  LeaderMap* leaders_;
  const std::atomic<uint64_t>* slot_;
  Done done_;
  std::vector<std::unique_ptr<Sender>> senders_;
  std::atomic<bool> stop_{false};
  std::atomic<uint64_t> accepted_{0}, failed_{0}, dropped_{0}, skipped_{0}, keepalives_{0}, keepalive_failures_{0};
  std::map<std::string, int64_t> keepalive_rtt_;  // guarded by rtt_mu_
  std::mutex rtt_mu_;
  std::map<std::string, obs::LatencyWindow> rtt_;
};

}  // namespace arb::submit
