#pragma once
// Helius Sender client: the plain-transaction path (19 Nov 2026; see risk/sender_budget.hpp for why).
// Per the Helius docs (Sender, checked 2026-11-19):
//   POST http://fra-sender.helius-rpc.com/fast?swqos_only=true&api-key=<key>
//        {"jsonrpc":"2.0","id":..,"method":"sendTransaction","params":["<b64>",{"encoding":"base64",
//         "skipPreflight":true,"maxRetries":0}]}
//   GET  /ping   keeps the connection warm (free; recommended for gaps over 5 s)
//   The transaction must carry a SetComputeUnitPrice instruction and a tip transfer to a designated Sender tip
//   account (risk::sender_tip_accounts); swqos_only accepts tips from 0.000005 SOL. 50 requests/s per key.
// One sender thread with a kept-alive connection; the engine only enqueues. A status thread then decides each
// accepted send's outcome with getSignatureStatuses: landed, failed (fee paid), or dropped (never landed).
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "core/rpc.hpp"
#include "submit/jito.hpp"

namespace arb::submit {

std::string sender_send_body(const std::string& tx_b64, uint64_t id = 1);
/** sendBundle [[prefix..., tx], {"encoding":"base64"}] (Sender: up to 4 transactions, landing tracked by signature). */
std::string sender_bundle_body(const std::vector<std::string>& prefix, const std::string& tx_b64, uint64_t id = 1);
/** The signature Sender returned, or its error message in `error`. */
bool parse_sender_reply(std::string_view body, std::string& signature, std::string& error);

struct SenderJob {
  std::string path;         // provider name ("sender" = Helius, "astralane"), for logs
  std::string tx_b64;
  std::vector<std::string> prefix_b64;  // non-empty: sent as a bundle with these ahead of ours (a back-run)
  std::string sig;          // base58, full
  uint64_t built_slot = 0;
  uint64_t fee_lamports = 0;  // base + priority: what a failure costs
  uint64_t cu_price = 0, tip_lamports = 0;
  uint32_t cu_limit = 0;
  uint16_t route_class = 0;
  int64_t wall_ms = 0;
  uint64_t enq_ns = 0;
};

struct SenderSendResult {
  int http_status = 0;  // 0: transport error or dropped before sending
  bool accepted = false;
  bool dropped_stale = false;
  std::string error;
  int64_t rtt_us = 0, queued_us = 0;
};

enum class SenderOutcome : uint8_t { landed = 1, failed = 2, dropped = 3 };
const char* sender_outcome_name(SenderOutcome o);

struct SenderClientConfig {
  std::string name = "sender";
  std::string url;               // full send URL, query included
  // Keepalive: GET ping_path (Helius /ping), or, when ping_body is set, POST it to the send URL (Astralane has no
  // documented ping; a getHealth call keeps the connection open whatever it answers).
  std::string ping_path = "/ping";
  std::string ping_body;
  int timeout_ms = 2000;
  int keepalive_s = 4;           // ping when idle this long (0 = off)
  int status_poll_ms = 1000;
  int status_timeout_s = 90;     // not seen by then: dropped
  uint64_t max_slot_lag = 1;     // drop a queued send once the slot clock is past built_slot + this
  /** Transports (tests inject mocks). Default: kept-alive TLS/TCP (make_rpc_http) and core::Rpc. */
  std::function<std::unique_ptr<HttpPost>(const std::string& url)> http;
  std::function<std::vector<RpcSigStatus>(const std::vector<std::string>& sigs)> statuses;
};

class SenderClient {
 public:
  using Sent = std::function<void(const SenderSendResult&, const SenderJob&)>;
  using Resolved = std::function<void(const SenderJob&, SenderOutcome, uint64_t slot)>;
  /** `rpc_url`: where getSignatureStatuses goes when no `statuses` hook is set. */
  SenderClient(SenderClientConfig c, const std::string& rpc_url, const std::atomic<uint64_t>* current_slot, Sent sent,
               Resolved resolved);
  ~SenderClient();
  SenderClient(const SenderClient&) = delete;
  SenderClient& operator=(const SenderClient&) = delete;
  /** Enqueue (never blocks); false when the queue is full. */
  bool send(SenderJob j);
  uint64_t accepted() const { return accepted_.load(); }
  uint64_t rejected() const { return rejected_.load(); }
  uint64_t dropped_stale() const { return stale_.load(); }
  uint64_t landed() const { return landed_.load(); }
  uint64_t failed() const { return failed_.load(); }
  uint64_t dropped() const { return dropped_.load(); }
  uint64_t pings() const { return pings_.load(); }
  uint64_t ping_failures() const { return ping_failures_.load(); }
  int64_t ping_rtt_us() const { return ping_rtt_us_.load(); }
  size_t pending() {
    std::lock_guard<std::mutex> g(pmu_);
    return pending_.size();
  }

 private:
  void run_send();
  void run_status();
  SenderClientConfig c_;
  std::string rpc_url_;
  const std::atomic<uint64_t>* slot_;
  Sent sent_;
  Resolved resolved_;
  std::unique_ptr<HttpPost> http_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<SenderJob> q_;
  struct Pending {
    SenderJob job;
    int64_t deadline_ms = 0;
  };
  std::mutex pmu_;
  std::vector<Pending> pending_;
  std::atomic<bool> stop_{false};
  std::atomic<uint64_t> accepted_{0}, rejected_{0}, stale_{0}, landed_{0}, failed_{0}, dropped_{0}, pings_{0}, ping_failures_{0};
  std::atomic<int64_t> ping_rtt_us_{-1};
  std::thread send_th_, status_th_;
};

}  // namespace arb::submit
