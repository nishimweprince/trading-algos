#pragma once
// Send audit: every bundle the bot sends is simulated afterwards (read-only, sigVerify off, recent blockhash
// replaced) from the exact signed bytes, on its own thread. 18 Nov 2026: 80 bundles sent, 0 landed, and the full
// §7.3 transaction (Kamino borrow, executor, repay, tip) had never been simulated on mainnet. A MinOutNotMet
// (custom 6002) means the edge was gone by then (lost the race); any other error means the transaction itself
// is wrong.
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace arb::app {

enum class AuditVerdict : uint8_t { ok, min_out, other };
/** Classify a simulation's value.err JSON ("" = success). */
AuditVerdict classify_sim_error(const std::string& err_json);

class SendAudit {
 public:
  explicit SendAudit(std::string rpc_url, int timeout_ms = 10'000);
  ~SendAudit();
  SendAudit(const SendAudit&) = delete;
  SendAudit& operator=(const SendAudit&) = delete;
  /** Queue one sent transaction (base64); dropped when 32 are already waiting. Never blocks. */
  void submit(std::string tx_b64, std::string label);
  uint64_t ok() const { return ok_.load(); }
  uint64_t min_out() const { return min_out_.load(); }
  uint64_t other() const { return other_.load(); }
  uint64_t dropped() const { return dropped_.load(); }

 private:
  void run();
  std::string url_;
  int timeout_ms_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<std::pair<std::string, std::string>> q_;
  bool stop_ = false;
  std::atomic<uint64_t> ok_{0}, min_out_{0}, other_{0}, dropped_{0};
  std::thread th_;
};

}  // namespace arb::app
