#include "obs/log.hpp"
#include "submit/jito.hpp"

#include <simdjson.h>

#include <cmath>
#include <cstdio>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/rpc.hpp"

namespace arb::submit {

std::string send_bundle_body(std::span<const std::string> txs, uint64_t id) {
  std::string s = "{\"jsonrpc\":\"2.0\",\"id\":" + std::to_string(id) + ",\"method\":\"sendBundle\",\"params\":[[";
  for (size_t i = 0; i < txs.size(); ++i) s += (i ? ",\"" : "\"") + txs[i] + "\"";
  return s + "],{\"encoding\":\"base64\"}]}";
}

std::string statuses_body(const char* method, std::span<const std::string> ids, uint64_t id) {
  std::string s = "{\"jsonrpc\":\"2.0\",\"id\":" + std::to_string(id) + ",\"method\":\"" + method + "\",\"params\":[[";
  for (size_t i = 0; i < ids.size() && i < MAX_STATUS_IDS; ++i) s += (i ? ",\"" : "\"") + ids[i] + "\"";
  return s + "]]}";
}

Result<std::string> parse_send_bundle(std::string_view body, std::string* msg) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root;
  if (p.parse(ps).get(root) != simdjson::SUCCESS) return fail(Errc::rpc, "bad JSON");
  std::string_view id;
  if (root["result"].get(id) == simdjson::SUCCESS) return std::string(id);
  std::string_view m;
  if (msg && root["error"]["message"].get(m) == simdjson::SUCCESS) *msg = std::string(m);
  return fail(Errc::rpc, "sendBundle error");
}

std::vector<InflightStatus> parse_inflight(std::string_view body) {
  std::vector<InflightStatus> out;
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::array arr;
  if (p.parse(ps)["result"]["value"].get(arr) != simdjson::SUCCESS) return out;
  for (auto e : arr) {
    InflightStatus s;
    std::string_view v;
    if (e["bundle_id"].get(v) == simdjson::SUCCESS) s.bundle_id = std::string(v);
    if (e["status"].get(v) == simdjson::SUCCESS) {
      s.status = v == "Landed" ? Inflight::Landed : v == "Pending" ? Inflight::Pending : v == "Failed" ? Inflight::Failed
               : v == "Invalid" ? Inflight::Invalid : Inflight::Unknown;
    }
    uint64_t slot = 0;
    if (e["landed_slot"].get(slot) == simdjson::SUCCESS) s.landed_slot = slot;
    out.push_back(std::move(s));
  }
  return out;
}

std::vector<Pubkey> parse_tip_accounts(std::string_view body) {
  std::vector<Pubkey> out;
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::array arr;
  if (p.parse(ps)["result"].get(arr) != simdjson::SUCCESS) return out;
  for (auto e : arr) {
    std::string_view s;
    Pubkey k;
    if (e.get(s) == simdjson::SUCCESS && b58_decode32(s, k)) out.push_back(k);
  }
  return out;
}

uint64_t parse_tip_floor_p25(std::string_view body) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::array arr;
  if (p.parse(ps).get(arr) != simdjson::SUCCESS) return 0;
  double sol = 0;
  for (auto e : arr) {  // newest entry is the last one
    double v;
    if (e["landed_tips_25th_percentile"].get(v) == simdjson::SUCCESS) sol = v;
  }
  // SOL as a float at the API edge; converted once, rounded up, to integer lamports.
  return sol > 0 ? uint64_t(std::ceil(sol * double(LAMPORTS_PER_SOL) - 1e-6)) : 0;
}

namespace {

class RpcHttp final : public HttpPost {
 public:
  RpcHttp(const std::string& url, int timeout_ms) : rpc_(url, timeout_ms), path_(parse_url(url).path) {}
  int post(std::string_view path, std::string_view body, std::string& out) override {
    int status = 0;
    std::string full = path_ == "/" ? std::string(path) : path_ + std::string(path);
    out = rpc_.http("POST", full, body, "Content-Type: application/json\r\n", status, "jito");
    return status;
  }

 private:
  Rpc rpc_;
  std::string path_;
};

}  // namespace

std::unique_ptr<HttpPost> make_rpc_http(const std::string& base_url, int timeout_ms) {
  return std::make_unique<RpcHttp>(base_url, timeout_ms);
}

JitoClient::JitoClient(std::vector<std::string> regions, const std::string& url_fmt, HttpFactory factory, int backoff_429_ms)
    : backoff_ms_(backoff_429_ms) {
  for (auto& r : regions) {
    char url[256];
    std::snprintf(url, sizeof url, url_fmt.c_str(), r.c_str());
    regions_.push_back(Region{r, factory(url), 0, 0});
  }
}

std::vector<RegionResult> JitoClient::send_bundle(std::span<const std::string> txs, int64_t now_ms) {
  std::vector<RegionResult> out;
  std::string body = send_bundle_body(txs);
  for (Region& r : regions_) {
    RegionResult rr;
    rr.region = r.name;
    if (now_ms < r.backoff_until_ms) {
      rr.backed_off = true;
      out.push_back(rr);
      continue;
    }
    std::string resp;
    try {
      ++sends_;
      rr.http_status = r.http->post("/api/v1/bundles", body, resp);
    } catch (const std::exception& e) {
      rr.error = e.what();
      out.push_back(rr);
      continue;
    }
    if (rr.http_status == 429) {
      r.backoff_until_ms = now_ms + backoff_ms_;
      ++r.status_429;
      rr.error = "429";
    } else {
      auto id = parse_send_bundle(resp, &rr.error);
      if (id) rr.bundle_id = *id;
    }
    out.push_back(rr);
  }
  return out;
}

std::vector<InflightStatus> JitoClient::inflight(std::span<const std::string> ids) {
  if (regions_.empty() || ids.empty()) return {};
  std::string resp;
  int st = regions_[0].http->post("/api/v1/getInflightBundleStatuses", statuses_body("getInflightBundleStatuses", ids), resp);
  return st == 200 ? parse_inflight(resp) : std::vector<InflightStatus>{};
}

std::vector<Pubkey> JitoClient::tip_accounts() {
  if (regions_.empty()) return {};
  std::string resp;
  int st = regions_[0].http->post("/api/v1/getTipAccounts", "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getTipAccounts\",\"params\":[]}", resp);
  if (st != 200) {
    LOG_WARN("getTipAccounts: http %d from %s: %s", st, regions_[0].name.c_str(), resp.substr(0, 160).c_str());
    return {};
  }
  auto v = parse_tip_accounts(resp);
  if (v.empty()) LOG_WARN("getTipAccounts: no accounts parsed from %s: %s", regions_[0].name.c_str(), resp.substr(0, 160).c_str());
  return v;
}

Submitter::Submitter(bool dry_run, std::unique_ptr<JitoClient> client) {
  if (!dry_run) client_ = std::move(client);
}

SubmitOutcome Submitter::submit(const tx::SignedTx& t, uint64_t built_slot, uint64_t current_slot, int64_t now_ms,
                                std::vector<RegionResult>* results) {
  if (!client_) return SubmitOutcome::DryRun;
  if (current_slot > built_slot) return SubmitOutcome::Stale;
  std::string b64tx = b64(t.bytes, t.len);
  auto rr = client_->send_bundle(std::span<const std::string>(&b64tx, 1), now_ms);
  bool any = false;
  for (auto& r : rr) any |= !r.bundle_id.empty();
  if (results) *results = std::move(rr);
  return any ? SubmitOutcome::Sent : SubmitOutcome::NoRegion;
}

void BundleTracker::track(const std::string& id, const Signature& sig, uint16_t rc, int64_t now_ms) {
  open_[id] = Open{sig, rc, now_ms, 0};
}

std::vector<std::string> BundleTracker::due(int64_t now_ms) {
  std::vector<std::string> out;
  for (auto& [id, o] : open_) {
    if (out.size() >= MAX_STATUS_IDS) break;
    if (now_ms - o.last_poll_ms >= poll_ms_) {
      o.last_poll_ms = now_ms;
      out.push_back(id);
    }
  }
  return out;
}

void BundleTracker::finish(const std::string& id, Result r, uint64_t slot) {
  auto it = open_.find(id);
  if (it == open_.end()) return;
  uint16_t rc = it->second.route_class;
  open_.erase(it);
  if (done_) done_(id, rc, r, slot);
}

void BundleTracker::on_statuses(const std::vector<InflightStatus>& st, int64_t now_ms) {
  for (const InflightStatus& s : st) {
    if (s.status == Inflight::Landed) finish(s.bundle_id, Result::Landed, s.landed_slot);
    else if (s.status == Inflight::Failed) finish(s.bundle_id, Result::Rejected, 0);
    else if (s.status == Inflight::Invalid) {
      // Not in the block engine's 5-minute window: after the first poll this means it was dropped.
      auto it = open_.find(s.bundle_id);
      if (it != open_.end() && now_ms - it->second.sent_ms > poll_ms_) finish(s.bundle_id, Result::NotLanded, 0);
    }
  }
}

void BundleTracker::on_signature_seen(const Signature& sig, uint64_t slot) {
  for (auto& [id, o] : open_)
    if (o.sig == sig) {
      finish(std::string(id), Result::Landed, slot);
      return;
    }
}

void BundleTracker::expire(int64_t now_ms) {
  std::vector<std::string> gone;
  for (auto& [id, o] : open_)
    if (now_ms - o.sent_ms >= timeout_ms_) gone.push_back(id);
  for (auto& id : gone) finish(id, Result::NotLanded, 0);
}

}  // namespace arb::submit
