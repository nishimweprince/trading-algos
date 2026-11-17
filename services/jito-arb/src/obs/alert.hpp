#pragma once
// Telegram alerts (spec §11): feed down/recovered, quota, kill switch, start/stop. A background thread posts to
// the Bot API so the live loop never waits on the network. Without TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in
// the environment it only logs. Messages queue (bounded) and are sent at most one every 2 s.
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace arb::obs {

/** Telegram sendMessage JSON body for `text` (escaped). Exposed for tests. */
std::string telegram_body(const std::string& chat_id, const std::string& text);

class Alerter {
 public:
  /** Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; `host` prefixes every message (e.g. "jito-arb fra"). */
  explicit Alerter(std::string host);
  ~Alerter();
  Alerter(const Alerter&) = delete;
  Alerter& operator=(const Alerter&) = delete;
  bool enabled() const { return !token_.empty() && !chat_.empty(); }
  /** Log at WARN and, when enabled, queue for Telegram. Never blocks on the network. */
  void send(const std::string& text);
  uint64_t sent() const { return sent_.load(); }
  uint64_t failed() const { return failed_.load(); }

 private:
  void run();
  bool post(const std::string& text);
  std::string host_, token_, chat_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<std::string> q_;
  bool stop_ = false;
  std::atomic<uint64_t> sent_{0}, failed_{0};
  std::thread th_;
};

}  // namespace arb::obs
