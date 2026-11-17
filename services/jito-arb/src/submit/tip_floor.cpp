#include "submit/tip_floor.hpp"

#include <chrono>

#include "core/sys.hpp"
#include "obs/log.hpp"
#include "submit/conn.hpp"
#include "submit/jito.hpp"

namespace arb::submit {

TipFloorPoller::TipFloorPoller(std::string url, int pct, int refresh_ms) : url_(std::move(url)), pct_(pct), refresh_ms_(refresh_ms) {
  th_ = std::thread([this] { run(); });
}

TipFloorPoller::~TipFloorPoller() {
  {
    std::lock_guard<std::mutex> g(mu_);
    stop_ = true;
  }
  cv_.notify_all();
  th_.join();
}

uint64_t TipFloorPoller::value(int64_t now_ms, int64_t max_age_ms) const {
  int64_t ok = ok_ms_.load();
  return ok && now_ms - ok <= max_age_ms ? lamports_.load() : 0;
}

void TipFloorPoller::run() {
  bool warned = false;
  for (;;) {
    std::string body, err;
    int status = http_once("GET", url_, {}, 5000, body, &err);
    polls_.fetch_add(1);
    uint64_t v = status == 200 ? parse_tip_floor(body, pct_) : 0;
    if (v) {
      lamports_.store(v);
      ok_ms_.store(wall_ms());
      warned = false;
    } else {
      failures_.fetch_add(1);
      if (!warned) LOG_WARN("tip floor: %s", status ? ("http " + std::to_string(status)).c_str() : err.c_str());
      warned = true;
    }
    std::unique_lock<std::mutex> l(mu_);
    if (cv_.wait_for(l, std::chrono::milliseconds(refresh_ms_), [this] { return stop_; })) return;
  }
}

}  // namespace arb::submit
