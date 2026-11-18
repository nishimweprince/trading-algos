#include "app/send_audit.hpp"

#include <algorithm>

#include "core/rpc.hpp"
#include "obs/log.hpp"

namespace arb::app {

AuditVerdict classify_sim_error(const std::string& err_json) {
  if (err_json.empty() || err_json == "null") return AuditVerdict::ok;
  if (err_json.find("\"Custom\":6002") != std::string::npos) return AuditVerdict::min_out;
  return AuditVerdict::other;
}

SendAudit::SendAudit(std::string rpc_url, int timeout_ms) : url_(std::move(rpc_url)), timeout_ms_(timeout_ms) {
  th_ = std::thread([this] { run(); });
}

SendAudit::~SendAudit() {
  {
    std::lock_guard<std::mutex> g(mu_);
    stop_ = true;
  }
  cv_.notify_all();
  th_.join();
}

void SendAudit::submit(std::string tx_b64, std::string label) {
  std::lock_guard<std::mutex> g(mu_);
  if (q_.size() >= 32) {
    dropped_.fetch_add(1);
    return;
  }
  q_.emplace_back(std::move(tx_b64), std::move(label));
  cv_.notify_one();
}

void SendAudit::run() {
  Rpc rpc(url_, timeout_ms_);
  for (;;) {
    std::pair<std::string, std::string> job;
    {
      std::unique_lock<std::mutex> l(mu_);
      cv_.wait(l, [this] { return stop_ || !q_.empty(); });
      if (stop_) return;
      job = std::move(q_.front());
      q_.pop_front();
    }
    try {
      auto r = rpc.simulate_full(job.first, {}, 0, "processed");
      AuditVerdict v = r.ok ? AuditVerdict::ok : classify_sim_error(r.err);
      (v == AuditVerdict::ok ? ok_ : v == AuditVerdict::min_out ? min_out_ : other_).fetch_add(1);
      if (on_verdict_) on_verdict_(job.second, v, r.err);
      std::string tail;
      for (size_t i = r.logs.size() > 4 ? r.logs.size() - 4 : 0; i < r.logs.size(); ++i) tail += " | " + r.logs[i].substr(0, 160);
      if (v == AuditVerdict::other)
        LOG_WARN("send audit %s: FAILED %s, %llu CU%s", job.second.c_str(), r.err.c_str(), (unsigned long long)r.units, tail.c_str());
      else
        LOG_INFO("send audit %s: %s, %llu CU", job.second.c_str(), v == AuditVerdict::ok ? "would succeed now" : "MinOutNotMet (edge gone)",
                 (unsigned long long)r.units);
    } catch (const std::exception& e) {
      LOG_WARN("send audit %s: rpc: %s", job.second.c_str(), e.what());
    }
  }
}

}  // namespace arb::app
