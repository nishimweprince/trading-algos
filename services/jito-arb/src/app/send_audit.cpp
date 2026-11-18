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

void SendAudit::submit(std::string tx_b64, std::string label, uint16_t tag) {
  std::lock_guard<std::mutex> g(mu_);
  if (q_.size() >= 32) {
    dropped_.fetch_add(1);
    return;
  }
  q_.push_back(Job{std::move(tx_b64), std::move(label), tag});
  cv_.notify_one();
}

void SendAudit::run() {
  Rpc rpc(url_, timeout_ms_);
  for (;;) {
    Job job;
    {
      std::unique_lock<std::mutex> l(mu_);
      cv_.wait(l, [this] { return stop_ || !q_.empty(); });
      if (stop_) return;
      job = std::move(q_.front());
      q_.pop_front();
    }
    try {
      auto r = rpc.simulate_full(job.tx_b64, {}, 0, "processed");
      AuditVerdict v = r.ok ? AuditVerdict::ok : classify_sim_error(r.err);
      (v == AuditVerdict::ok ? ok_ : v == AuditVerdict::min_out ? min_out_ : other_).fetch_add(1);
      if (on_verdict_) on_verdict_(job.label, v, r.err, r.units, job.tag);
      std::string tail;
      for (size_t i = r.logs.size() > 4 ? r.logs.size() - 4 : 0; i < r.logs.size(); ++i) tail += " | " + r.logs[i].substr(0, 160);
      if (v == AuditVerdict::other)
        LOG_WARN("post-send check %s: FAILED %s, %llu CU%s", job.label.c_str(), r.err.c_str(), (unsigned long long)r.units, tail.c_str());
      else
        LOG_INFO("post-send check %s: %s, %llu CU", job.label.c_str(), v == AuditVerdict::ok ? "would succeed now" : "MinOutNotMet (edge gone)",
                 (unsigned long long)r.units);
    } catch (const std::exception& e) {
      LOG_WARN("post-send check %s: rpc: %s", job.label.c_str(), e.what());
    }
  }
}

}  // namespace arb::app
