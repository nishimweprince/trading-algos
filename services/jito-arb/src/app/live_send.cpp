#include "app/live_send.hpp"

#include <chrono>

#include <simdjson.h>

#include "core/base58.hpp"
#include "core/sys.hpp"
#include "obs/log.hpp"

namespace arb::app {

bool ArmedSet::load_json(const std::string& body, std::string* err) {
  auto bad = [&](const char* m) {
    if (err) *err = m;
    return false;
  };
  try {
    simdjson::dom::parser p;
    simdjson::padded_string ps(body);
    simdjson::dom::array arr;
    if (p.parse(ps)["pools"].get(arr) != simdjson::SUCCESS) return bad("armed set: no \"pools\" array");
    std::set<Pubkey> pools;
    for (auto e : arr) {
      std::string_view s;
      Pubkey k;
      if (e.get(s) != simdjson::SUCCESS || !b58_decode32(s, k)) return bad("armed set: bad pool address");
      pools.insert(k);
    }
    pools_ = std::move(pools);
    return true;
  } catch (const std::exception&) {
    return bad("armed set: not JSON");
  }
}

std::string armed_to_json(const std::vector<Pubkey>& pools) {
  std::string s = "{\"pools\":[";
  for (size_t i = 0; i < pools.size(); ++i) s += std::string(i ? "," : "") + "\"" + b58(pools[i]) + "\"";
  return s + "]}\n";
}

LandingWatcher::LandingWatcher(std::unique_ptr<submit::JitoClient> client, int poll_ms, int timeout_ms)
    : client_(std::move(client)),
      tracker_(
          [this](const std::string& id, uint16_t rc, submit::BundleTracker::Result r, uint64_t slot) {
            std::lock_guard<std::mutex> g(mu_);
            done_.push_back(Done{id, rc, r, slot});
          },
          poll_ms, timeout_ms) {}

LandingWatcher::~LandingWatcher() { shutdown(); }

void LandingWatcher::start() {
  if (thread_.joinable()) return;
  thread_ = std::thread([this] {
    while (!quit_) {
      try {
        poll_once(wall_ms());
      } catch (const std::exception& e) {
        LOG_WARN("landing watcher: %s", e.what());
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });
}

void LandingWatcher::shutdown() {
  quit_ = true;
  if (thread_.joinable()) thread_.join();
}

void LandingWatcher::track(const std::string& bundle_id, const Signature& sig, uint16_t route_class, int64_t now_ms) {
  std::lock_guard<std::mutex> g(mu_);
  incoming_.push_back(Track{bundle_id, sig, route_class, now_ms});
  ++pending_;
}

void LandingWatcher::signature_seen(const Signature& sig, uint64_t slot) {
  std::lock_guard<std::mutex> g(mu_);
  seen_.emplace_back(sig, slot);
}

std::vector<LandingWatcher::Done> LandingWatcher::drain() {
  std::lock_guard<std::mutex> g(mu_);
  std::vector<Done> out;
  out.swap(done_);
  return out;
}

size_t LandingWatcher::pending() { return pending_.load(); }

void LandingWatcher::poll_once(int64_t now_ms) {
  std::vector<Track> in;
  std::vector<std::pair<Signature, uint64_t>> seen;
  {
    std::lock_guard<std::mutex> g(mu_);
    in.swap(incoming_);
    seen.swap(seen_);
  }
  for (const Track& t : in) tracker_.track(t.bundle_id, t.sig, t.route_class, t.now_ms);
  for (auto& [sig, slot] : seen) tracker_.on_signature_seen(sig, slot);
  // The status call is outside the lock: it is the slow part, and track() must never wait on it.
  std::vector<std::string> ids = tracker_.due(now_ms);
  if (!ids.empty() && client_) tracker_.on_statuses(client_->inflight(ids), now_ms);
  tracker_.expire(now_ms);
  pending_ = tracker_.pending();
}

}  // namespace arb::app
