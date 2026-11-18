#include "submit/router.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>

#include "core/sys.hpp"

namespace arb::submit {

namespace {
std::string fmt_url(const std::string& fmt, const std::string& region) {
  char buf[256];
  std::snprintf(buf, sizeof buf, fmt.c_str(), region.c_str());
  return buf;
}
void add_unique(std::vector<std::string>& v, const std::string& s) {
  if (!s.empty() && std::find(v.begin(), v.end(), s) == v.end()) v.push_back(s);
}
}  // namespace

RoutePlan plan_route(const RouterConfig& c, const LeaderInfo& now, const LeaderInfo& next) {
  RoutePlan p;
  const bool known = now.kind != LeaderKind::unknown && next.kind != LeaderKind::unknown;
  if (known && c.skip_not_jito && now.kind == LeaderKind::not_jito && next.kind == LeaderKind::not_jito) {
    p.skip = true;
    p.skip_reason = "leaders not on Jito";
    return p;
  }
  if (known && c.skip_bam && c.bam_uuid.empty() && now.kind == LeaderKind::bam && next.kind == LeaderKind::bam) {
    p.skip = true;
    p.skip_reason = "BAM leaders without Direct Bundle Ingest";
    return p;
  }
  for (const auto& r : c.always) add_unique(p.block_engine, r);
  bool located = false;
  for (const LeaderInfo* l : {&now, &next}) {
    if (l->kind == LeaderKind::not_jito || l->region.empty()) continue;
    located = true;
    add_unique(p.block_engine, l->region);
    if (l->kind == LeaderKind::bam && !c.bam_uuid.empty()) add_unique(p.bam, l->region);
  }
  if (!located)  // no leader location yet (map loading, or unknown IPs): the configured regions as before
    for (const auto& r : c.regions) add_unique(p.block_engine, r);
  return p;
}

Router::Router(RouterConfig c, LeaderMap* leaders, const std::atomic<uint64_t>* current_slot, Done done)
    : c_(std::move(c)), leaders_(leaders), slot_(current_slot), done_(std::move(done)) {}

Router::~Router() {
  stop_ = true;
  for (auto& s : senders_) s->cv.notify_all();
  for (auto& s : senders_)
    if (s->th.joinable()) s->th.join();
}

Router::Sender* Router::sender(const std::string& region, bool bam) {
  for (auto& s : senders_)
    if (s->region == region && s->bam == bam) return s.get();
  auto s = std::make_unique<Sender>();
  s->region = region;
  s->bam = bam;
  std::string headers = bam ? "x-jito-auth: " + c_.bam_uuid + "\r\n" : std::string();
  const std::string url = fmt_url(bam ? c_.bam_fmt : c_.block_engine_fmt, region);
  s->http = c_.http ? c_.http(url, headers) : make_rpc_http(url, c_.timeout_ms, headers);
  Sender* raw = s.get();
  s->th = std::thread([this, raw] { run(*raw); });
  senders_.push_back(std::move(s));
  return raw;
}

RoutePlan Router::route(const std::string& tx_b64, const Signature& sig, uint16_t route_class, uint64_t built_slot, int64_t wall_ms) {
  LeaderInfo now = leaders_ ? leaders_->at(built_slot) : LeaderInfo{};
  LeaderInfo next = leaders_ ? leaders_->at(built_slot + 1) : LeaderInfo{};
  RoutePlan p = plan_route(c_, now, next);
  if (p.skip) {
    ++skipped_;
    return p;
  }
  std::string body = send_bundle_body(std::span<const std::string>(&tx_b64, 1));
  auto enqueue = [&](const std::string& region, bool bam) {
    Sender* s = sender(region, bam);
    Job j{body, sig, route_class, built_slot, now.identity.empty() ? next : now, wall_ms, now_ns()};
    {
      std::lock_guard<std::mutex> g(s->mu);
      if (s->q.size() >= 16) {
        s->q.pop_front();
        ++dropped_;
      }
      s->q.push_back(std::move(j));
    }
    s->cv.notify_one();
  };
  for (const auto& r : p.block_engine) enqueue(r, false);
  for (const auto& r : p.bam) enqueue(r, true);
  return p;
}

void Router::warm(const std::vector<std::string>& regions) {
  for (const auto& r : regions) sender(r, false);
}

std::map<std::string, int64_t> Router::keepalive_rtt() {
  std::lock_guard<std::mutex> g(rtt_mu_);
  return keepalive_rtt_;
}

void Router::run(Sender& s) {
  // Block-engine senders open their connection at once and keep it warm while idle. BAM DBI has no documented
  // cheap call, so its connection opens on the first bundle.
  const bool keep = !s.bam && c_.keepalive_s > 0;
  auto keepalive = [&] {
    std::string resp;
    const uint64_t t0 = now_ns();
    int st = 0;
    try {
      st = s.http->post("/api/v1/getTipAccounts", "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getTipAccounts\",\"params\":[]}", resp);
    } catch (const std::exception&) {
      st = 0;
    }
    s.last_post_ms = wall_ms();
    ++keepalives_;
    std::lock_guard<std::mutex> g(rtt_mu_);
    if (st == 200) keepalive_rtt_[s.region] = int64_t(now_ns() - t0) / 1000;
    else ++keepalive_failures_;
    if (st == 429) s.backoff_until_ms = wall_ms() + c_.backoff_429_ms;
  };
  if (keep) keepalive();
  for (;;) {
    Job j;
    {
      std::unique_lock<std::mutex> l(s.mu);
      bool got = keep ? s.cv.wait_for(l, std::chrono::seconds(c_.keepalive_s), [&] { return stop_.load() || !s.q.empty(); })
                      : (s.cv.wait(l, [&] { return stop_.load() || !s.q.empty(); }), true);
      if (stop_) return;
      if (!got) {
        l.unlock();
        if (wall_ms() - s.last_post_ms >= int64_t(c_.keepalive_s) * 1000) keepalive();
        continue;
      }
      j = std::move(s.q.front());
      s.q.pop_front();
    }
    SendResult r;
    r.region = s.region;
    r.bam = s.bam;
    const uint64_t t0 = now_ns();
    r.queued_us = int64_t(t0 - j.enq_ns) / 1000;
    // A bundle whose slot has passed is a new decision, never a late retry (spec §9).
    if (slot_ && slot_->load(std::memory_order_relaxed) > j.built_slot + 1) {
      r.dropped_stale = true;
      ++dropped_;
      done_(r, j.sig, j.route_class, j.built_slot, j.leader, j.wall_ms);
      continue;
    }
    if (wall_ms() < s.backoff_until_ms) {
      r.error = "429 backoff";
      ++failed_;
      done_(r, j.sig, j.route_class, j.built_slot, j.leader, j.wall_ms);
      continue;
    }
    std::string resp;
    s.last_post_ms = wall_ms();
    try {
      r.http_status = s.http->post("/api/v1/bundles", j.body, resp);
    } catch (const std::exception& e) {
      r.error = e.what();
    }
    r.rtt_us = int64_t(now_ns() - t0) / 1000;
    {
      std::lock_guard<std::mutex> g(rtt_mu_);
      auto it = rtt_.find(r.region + (s.bam ? "/bam" : ""));
      if (it == rtt_.end()) it = rtt_.emplace(r.region + (s.bam ? "/bam" : ""), obs::LatencyWindow(3600)).first;
      it->second.add(now_ns(), r.rtt_us);
    }
    if (r.http_status == 429) {
      s.backoff_until_ms = wall_ms() + c_.backoff_429_ms;
      r.error = "429";
    } else if (r.http_status) {
      auto id = parse_send_bundle(resp, &r.error);
      if (id) r.bundle_id = *id;
    }
    (r.bundle_id.empty() ? failed_ : accepted_).fetch_add(1);
    done_(r, j.sig, j.route_class, j.built_slot, j.leader, j.wall_ms);
  }
}

std::map<std::string, obs::LatencyStats> Router::rtt_stats() {
  std::map<std::string, obs::LatencyStats> out;
  std::lock_guard<std::mutex> g(rtt_mu_);
  for (auto& [k, w] : rtt_) out[k] = w.stats(now_ns());
  return out;
}

}  // namespace arb::submit
