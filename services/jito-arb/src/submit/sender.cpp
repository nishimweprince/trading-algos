#include "submit/sender.hpp"

#include <simdjson.h>

#include <algorithm>
#include <chrono>

#include "core/sys.hpp"
#include "obs/log.hpp"

namespace arb::submit {

std::string sender_send_body(const std::string& tx_b64, uint64_t id) {
  return "{\"jsonrpc\":\"2.0\",\"id\":" + std::to_string(id) + ",\"method\":\"sendTransaction\",\"params\":[\"" + tx_b64 +
         "\",{\"encoding\":\"base64\",\"skipPreflight\":true,\"maxRetries\":0}]}";
}

bool parse_sender_reply(std::string_view body, std::string& signature, std::string& error) {
  signature.clear();
  error.clear();
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root;
  if (p.parse(ps).get(root) != simdjson::SUCCESS) {
    error = "unparsable reply: " + std::string(body.substr(0, 120));
    return false;
  }
  std::string_view s;
  if (root["result"].get(s) == simdjson::SUCCESS) {
    signature = s;
    return true;
  }
  if (root["error"]["message"].get(s) == simdjson::SUCCESS) error = s;
  else error = "no result: " + std::string(body.substr(0, 120));
  return false;
}

const char* sender_outcome_name(SenderOutcome o) {
  switch (o) {
    case SenderOutcome::landed: return "landed";
    case SenderOutcome::failed: return "failed";
    case SenderOutcome::dropped: return "dropped";
  }
  return "?";
}

SenderClient::SenderClient(SenderClientConfig c, const std::string& rpc_url, const std::atomic<uint64_t>* current_slot, Sent sent,
                           Resolved resolved)
    : c_(std::move(c)), rpc_url_(rpc_url), slot_(current_slot), sent_(std::move(sent)), resolved_(std::move(resolved)) {
  if (!c_.http) {
    int t = c_.timeout_ms;
    c_.http = [t](const std::string& url) { return make_rpc_http(url, t); };
  }
  if (!c_.statuses) {
    auto rpc = std::make_shared<Rpc>(rpc_url_, 5000);
    c_.statuses = [rpc](const std::vector<std::string>& sigs) { return rpc->signature_statuses(sigs); };
  }
  send_th_ = std::thread([this] { run_send(); });
  status_th_ = std::thread([this] { run_status(); });
}

SenderClient::~SenderClient() {
  stop_ = true;
  cv_.notify_all();
  if (send_th_.joinable()) send_th_.join();
  if (status_th_.joinable()) status_th_.join();
}

bool SenderClient::send(SenderJob j) {
  j.enq_ns = now_ns();
  {
    std::lock_guard<std::mutex> g(mu_);
    if (q_.size() >= 64) return false;
    q_.push_back(std::move(j));
  }
  cv_.notify_one();
  return true;
}

void SenderClient::run_send() {
  int64_t last_post_ms = 0;
  auto ensure = [&]() -> bool {
    if (http_) return true;
    try {
      http_ = c_.http(c_.url);
    } catch (const std::exception& e) {
      LOG_WARN("sender: connect failed: %s", e.what());
    }
    return bool(http_);
  };
  auto ping = [&] {
    if (!ensure()) return;
    std::string out;
    uint64_t t0 = now_ns();
    int st = 0;
    try {
      st = http_->get(ping_path_, out);
    } catch (const std::exception&) {
      http_.reset();
    }
    ++pings_;
    last_post_ms = wall_ms();
    if (st == 200) ping_rtt_us_ = int64_t(now_ns() - t0) / 1000;
    else ++ping_failures_;
  };
  if (c_.keepalive_s > 0) ping();
  while (!stop_) {
    SenderJob j;
    {
      std::unique_lock<std::mutex> l(mu_);
      bool got = c_.keepalive_s > 0
                     ? cv_.wait_for(l, std::chrono::seconds(c_.keepalive_s), [&] { return stop_.load() || !q_.empty(); })
                     : (cv_.wait(l, [&] { return stop_.load() || !q_.empty(); }), true);
      if (stop_) break;
      if (!got || q_.empty()) {
        l.unlock();
        if (c_.keepalive_s > 0 && wall_ms() - last_post_ms >= int64_t(c_.keepalive_s) * 1000) ping();
        continue;
      }
      j = std::move(q_.front());
      q_.pop_front();
    }
    SenderSendResult r;
    r.queued_us = int64_t(now_ns() - j.enq_ns) / 1000;
    if (slot_ && slot_->load(std::memory_order_relaxed) > j.built_slot + c_.max_slot_lag) {
      r.dropped_stale = true;
      ++stale_;
      if (sent_) sent_(r, j);
      continue;
    }
    std::string out, sig, err;
    uint64_t t0 = now_ns();
    for (int attempt = 0; attempt < 2; ++attempt) {  // one reconnect when a kept-alive socket was closed
      if (!ensure()) break;
      try {
        r.http_status = http_->post("", sender_send_body(j.tx_b64), out);
        break;
      } catch (const std::exception& e) {
        http_.reset();
        r.error = e.what();
      }
    }
    r.rtt_us = int64_t(now_ns() - t0) / 1000;
    last_post_ms = wall_ms();
    if (r.http_status == 200 && parse_sender_reply(out, sig, err)) {
      r.accepted = true;
      r.error.clear();
      ++accepted_;
      std::lock_guard<std::mutex> g(pmu_);
      pending_.push_back(Pending{j, wall_ms() + int64_t(c_.status_timeout_s) * 1000});
    } else {
      if (r.error.empty()) r.error = err.empty() ? ("http " + std::to_string(r.http_status) + " " + out.substr(0, 120)) : err;
      ++rejected_;
    }
    if (sent_) sent_(r, j);
  }
}

void SenderClient::run_status() {
  while (!stop_) {
    std::this_thread::sleep_for(std::chrono::milliseconds(c_.status_poll_ms));
    std::vector<Pending> batch;
    {
      std::lock_guard<std::mutex> g(pmu_);
      size_t n = std::min<size_t>(pending_.size(), 256);
      batch.assign(pending_.begin(), pending_.begin() + long(n));
    }
    if (batch.empty()) continue;
    std::vector<std::string> sigs;
    sigs.reserve(batch.size());
    for (auto& p : batch) sigs.push_back(p.job.sig);
    std::vector<RpcSigStatus> st;
    try {
      st = c_.statuses(sigs);
    } catch (const std::exception& e) {
      LOG_WARN("sender: getSignatureStatuses failed: %s", e.what());
      continue;
    }
    const int64_t now = wall_ms();
    std::vector<std::pair<SenderJob, std::pair<SenderOutcome, uint64_t>>> done;
    {
      std::lock_guard<std::mutex> g(pmu_);
      for (size_t i = 0; i < batch.size(); ++i) {
        SenderOutcome o;
        uint64_t slot = 0;
        if (i < st.size() && st[i].found) {
          o = st[i].failed ? SenderOutcome::failed : SenderOutcome::landed;
          slot = st[i].slot;
        } else if (now >= batch[i].deadline_ms) {
          o = SenderOutcome::dropped;
        } else {
          continue;
        }
        auto it = std::find_if(pending_.begin(), pending_.end(), [&](const Pending& p) { return p.job.sig == batch[i].job.sig; });
        if (it != pending_.end()) pending_.erase(it);
        done.push_back({batch[i].job, {o, slot}});
      }
    }
    for (auto& [job, os] : done) {
      switch (os.first) {
        case SenderOutcome::landed: ++landed_; break;
        case SenderOutcome::failed: ++failed_; break;
        case SenderOutcome::dropped: ++dropped_; break;
      }
      if (resolved_) resolved_(job, os.first, os.second);
    }
  }
}

}  // namespace arb::submit
