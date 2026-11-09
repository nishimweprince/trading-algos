#include "core/usage.hpp"

#include <atomic>
#include <map>
#include <mutex>

#include "obs/log.hpp"

namespace arb::usage {

namespace {
std::mutex g_mu;
std::map<std::string, MethodStats, std::less<>> g_methods;
std::atomic<bool> g_quota{false};
std::atomic<int64_t> g_quota_since{0};
}  // namespace

bool is_quota_body(std::string_view body) {
  return body.find("max usage") != std::string_view::npos || body.find("credits") != std::string_view::npos;
}

void set_quota(bool exhausted, int64_t now_ms) {
  bool was = g_quota.exchange(exhausted);
  if (exhausted && !was) {
    g_quota_since = now_ms;
    LOG_WARN("helius quota exhausted: polling callers back off to once a minute until a call succeeds");
  } else if (!exhausted && was) {
    LOG_INFO("helius quota available again (exhausted for %lld s)", (long long)((now_ms - g_quota_since.load()) / 1000));
    g_quota_since = 0;
  }
}

bool quota_exhausted() { return g_quota.load(); }
int64_t quota_since_ms() { return g_quota_since.load(); }

void record_rpc(std::string_view method, int status, size_t bytes_out, size_t bytes_in, std::string_view body, int64_t now_ms) {
  {
    std::lock_guard<std::mutex> g(g_mu);
    auto it = g_methods.find(method);
    if (it == g_methods.end()) it = g_methods.emplace(std::string(method), MethodStats{}).first;
    MethodStats& m = it->second;
    ++m.calls;
    m.bytes_out += bytes_out;
    m.bytes_in += bytes_in;
    if (status == 429) ++m.rate_limited;
    else if (status != 200) ++m.errors;
  }
  if (status == 429 && is_quota_body(body)) set_quota(true, now_ms);
  else if (status == 200) set_quota(false, now_ms);
}

std::vector<std::pair<std::string, MethodStats>> rpc_snapshot() {
  std::lock_guard<std::mutex> g(g_mu);
  return {g_methods.begin(), g_methods.end()};
}

bool may_call(int64_t& last_ms, int64_t now_ms, int64_t hold_ms) {
  if (quota_exhausted() && now_ms - last_ms < hold_ms) return false;
  last_ms = now_ms;
  return true;
}

}  // namespace arb::usage
