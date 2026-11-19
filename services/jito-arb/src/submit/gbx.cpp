#include "submit/gbx.hpp"

#include <algorithm>
#include <chrono>

#include "core/base58.hpp"
#include "core/pb.hpp"
#include "core/sys.hpp"
#include "obs/log.hpp"

namespace arb::submit {

namespace {
const std::string AUTH_CHALLENGE = "/auth.AuthService/GenerateAuthChallenge";
const std::string AUTH_TOKENS = "/auth.AuthService/GenerateAuthTokens";
const std::string AUTH_REFRESH = "/auth.AuthService/RefreshAccessToken";
const std::string SEND_BUNDLE = "/searcher.SearcherService/SendBundle";
const std::string CONNECTED_LEADERS = "/searcher.SearcherService/GetConnectedLeaders";
constexpr int GRPC_UNAUTHENTICATED = 16;

std::string_view key_view(const Pubkey& k) { return {reinterpret_cast<const char*>(k.data()), 32}; }

bool parse_token(std::string_view m, GbxToken& t) {
  pb::Reader r(m);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) t.value = std::string(r.bytes());
    else if (f == 2 && wt == pb::LEN) {  // google.protobuf.Timestamp {seconds = 1, nanos = 2}
      pb::Reader ts(r.bytes());
      uint32_t g, w;
      while (ts.next(g, w)) {
        if (g == 1 && w == pb::VARINT) t.expires_s = int64_t(ts.varint());
        else ts.skip(w);
      }
    } else r.skip(wt);
  }
  return r.ok && !t.value.empty();
}
}  // namespace

std::string gbx_challenge_request(const Pubkey& pubkey) {
  pb::Writer w;
  w.u64(1, 1);  // Role::SEARCHER
  w.bytes(2, key_view(pubkey));
  return w.out;
}

bool gbx_parse_challenge(std::string_view resp, std::string& challenge) {
  pb::Reader r(resp);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) challenge = std::string(r.bytes());
    else r.skip(wt);
  }
  return r.ok && !challenge.empty();
}

std::string gbx_challenge_message(const Pubkey& pubkey, const std::string& challenge) { return b58(pubkey) + "-" + challenge; }

std::string gbx_tokens_request(const std::string& challenge, const Pubkey& pubkey, const Signature& signed_challenge) {
  pb::Writer w;
  w.bytes(1, challenge);
  w.bytes(2, key_view(pubkey));
  w.bytes(3, std::string_view(reinterpret_cast<const char*>(signed_challenge.data()), 64));
  return w.out;
}

bool gbx_parse_tokens(std::string_view resp, GbxToken& access, GbxToken& refresh) {
  pb::Reader r(resp);
  uint32_t f, wt;
  bool a = false, b = false;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) a = parse_token(r.bytes(), access);
    else if (f == 2 && wt == pb::LEN) b = parse_token(r.bytes(), refresh);
    else r.skip(wt);
  }
  return r.ok && a && b;
}

std::string gbx_refresh_request(const std::string& refresh_token) {
  pb::Writer w;
  w.bytes(1, refresh_token);
  return w.out;
}

bool gbx_parse_refresh(std::string_view resp, GbxToken& access) {
  pb::Reader r(resp);
  uint32_t f, wt;
  bool a = false;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) a = parse_token(r.bytes(), access);
    else r.skip(wt);
  }
  return r.ok && a;
}

std::string gbx_bundle_request(const std::vector<std::string>& txs, const std::vector<uint32_t>& dropping,
                               const std::vector<Pubkey>& targets, uint64_t slot) {
  pb::Writer bundle;
  for (const auto& t : txs) {
    pb::Writer packet;
    packet.bytes(1, t);  // Packet.data; meta omitted (the broadcaster fills what it needs)
    bundle.message(3, packet);
  }
  if (!dropping.empty()) {  // packed repeated uint32
    pb::Writer packed;
    for (uint32_t i : dropping) packed.varint(i);
    bundle.bytes(4, packed.out);
  }
  if (slot) bundle.u64(5, slot);
  for (const auto& k : targets) bundle.bytes(6, key_view(k));
  pb::Writer req;
  req.message(1, bundle);
  return req.out;
}

bool gbx_parse_bundle_uuid(std::string_view resp, std::string& uuid) {
  pb::Reader r(resp);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) uuid = std::string(r.bytes());
    else r.skip(wt);
  }
  return r.ok;
}

bool gbx_parse_connected_leaders(std::string_view resp, std::map<std::string, std::vector<uint64_t>>& out) {
  out.clear();
  pb::Reader r(resp);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f != 1 || wt != pb::LEN) {
      r.skip(wt);
      continue;
    }
    pb::Reader e(r.bytes());  // map entry: key = 1 (string), value = 2 (SlotList)
    std::string key;
    std::vector<uint64_t> slots;
    uint32_t g, w;
    while (e.next(g, w)) {
      if (g == 1 && w == pb::LEN) key = std::string(e.bytes());
      else if (g == 2 && w == pb::LEN) {
        pb::Reader sl(e.bytes());
        uint32_t h, x;
        while (sl.next(h, x)) {
          if (h == 1) pb::each_varint(sl, x, [&](uint64_t s) { slots.push_back(s); });
          else sl.skip(x);
        }
      } else e.skip(w);
    }
    if (!e.ok) return false;
    if (!key.empty()) out[key] = std::move(slots);
  }
  return r.ok;
}

const char* gbx_outcome_name(GbxOutcome o) {
  switch (o) {
    case GbxOutcome::landed: return "landed";
    case GbxOutcome::failed: return "failed";
    case GbxOutcome::dropped: return "dropped";
  }
  return "?";
}

GbxClient::GbxClient(GbxConfig c, Keypair identity, const std::string& rpc_url, const std::atomic<uint64_t>* slot, Sent sent,
                     Resolved resolved)
    : c_(std::move(c)), id_(identity), rpc_url_(rpc_url), slot_(slot), sent_(std::move(sent)), resolved_(std::move(resolved)) {
  // The caller supplies the transport (live: make_gbx_transport, linked only into the bot; tests: a mock).
  if (!c_.statuses) {
    auto rpc = std::make_shared<Rpc>(rpc_url_, 5000);
    c_.statuses = [rpc](const std::vector<std::string>& sigs) { return rpc->signature_statuses(sigs); };
  }
  th_ = std::thread([this] { run(); });
  status_th_ = std::thread([this] { run_status(); });
}

GbxClient::~GbxClient() {
  stop_ = true;
  cv_.notify_all();
  if (th_.joinable()) th_.join();
  if (status_th_.joinable()) status_th_.join();
}

bool GbxClient::send(GbxJob j) {
  j.enq_ns = now_ns();
  {
    std::lock_guard<std::mutex> g(mu_);
    if (q_.size() >= 32) return false;
    q_.push_back(std::move(j));
  }
  cv_.notify_one();
  return true;
}

bool GbxClient::gbx_slot(uint64_t slot) const {
  std::lock_guard<std::mutex> g(info_mu_);
  return slot_leader_.count(slot) != 0;
}

size_t GbxClient::connected_validators() const {
  std::lock_guard<std::mutex> g(info_mu_);
  return validators_;
}

uint32_t GbxClient::coverage_ppm(uint64_t from, uint64_t window) const {
  std::lock_guard<std::mutex> g(info_mu_);
  if (!window) return 0;
  uint64_t n = 0;
  for (auto it = slot_leader_.lower_bound(from); it != slot_leader_.end() && it->first < from + window; ++it) ++n;
  return uint32_t(n * 1'000'000 / window);
}

bool GbxClient::ensure_auth(GbxTransport& t) {
  const int64_t now = wall_ms() / 1000;
  if (!access_.empty() && (access_exp_ == 0 || now < access_exp_ - 30)) return true;
  std::string resp, err;
  if (!refresh_.empty() && (refresh_exp_ == 0 || now < refresh_exp_ - 30)) {
    GbxToken a;
    if (t.call(AUTH_REFRESH, gbx_refresh_request(refresh_), resp, "", err) == 0 && gbx_parse_refresh(resp, a)) {
      access_ = a.value;
      access_exp_ = a.expires_s;
      return true;
    }
  }
  std::string challenge;
  int code = t.call(AUTH_CHALLENGE, gbx_challenge_request(id_.pubkey()), resp, "", err);
  if (code != 0 || !gbx_parse_challenge(resp, challenge)) {
    ++auth_fail_;
    std::lock_guard<std::mutex> g(info_mu_);
    last_err_ = "auth challenge: code " + std::to_string(code) + " " + err;
    return false;
  }
  const std::string msg = gbx_challenge_message(id_.pubkey(), challenge);
  Signature sig{};
  id_.sign(reinterpret_cast<const uint8_t*>(msg.data()), msg.size(), sig.data());
  GbxToken a, r;
  code = t.call(AUTH_TOKENS, gbx_tokens_request(challenge, id_.pubkey(), sig), resp, "", err);
  if (code != 0 || !gbx_parse_tokens(resp, a, r)) {
    ++auth_fail_;
    std::lock_guard<std::mutex> g(info_mu_);
    last_err_ = "auth tokens: code " + std::to_string(code) + " " + err;
    return false;
  }
  access_ = a.value;
  access_exp_ = a.expires_s;
  refresh_ = r.value;
  refresh_exp_ = r.expires_s;
  if (!authed_.exchange(true)) LOG_INFO("gbx: authenticated as %s", b58(id_.pubkey()).c_str());
  return true;
}

void GbxClient::refresh_leaders(GbxTransport& t) {
  std::string resp, err;
  int code = t.call(CONNECTED_LEADERS, "", resp, access_, err);
  std::map<std::string, std::vector<uint64_t>> m;
  if (code != 0 || !gbx_parse_connected_leaders(resp, m)) {
    std::lock_guard<std::mutex> g(info_mu_);
    last_err_ = "connected leaders: code " + std::to_string(code) + " " + err;
    return;
  }
  std::map<uint64_t, std::string> sl;
  for (const auto& [v, slots] : m)
    for (uint64_t s : slots) sl[s] = v;
  std::lock_guard<std::mutex> g(info_mu_);
  const bool first = validators_ == 0 && !m.empty();
  slot_leader_ = std::move(sl);
  validators_ = m.size();
  if (first) LOG_INFO("gbx: %zu connected validators, %zu upcoming GBX slots", validators_, slot_leader_.size());
}

void GbxClient::run() {
  std::unique_ptr<GbxTransport> t;
  auto last_warn = std::chrono::steady_clock::now() - std::chrono::hours(1);
  auto last_leaders = std::chrono::steady_clock::now() - std::chrono::hours(1);
  while (!stop_) {
    if (!t) t = c_.transport ? c_.transport() : nullptr;
    if (!t) {
      std::this_thread::sleep_for(std::chrono::seconds(1));
      continue;
    }
    const bool have_auth = ensure_auth(*t);
    if (!have_auth && std::chrono::steady_clock::now() - last_warn > std::chrono::seconds(60)) {
      last_warn = std::chrono::steady_clock::now();
      LOG_WARN("gbx: not authenticated (%llu failures): %s", (unsigned long long)auth_fail_.load(), last_error().c_str());
    }
    if (have_auth && std::chrono::steady_clock::now() - last_leaders > std::chrono::seconds(c_.leaders_refresh_s)) {
      refresh_leaders(*t);
      last_leaders = std::chrono::steady_clock::now();
    }
    GbxJob j;
    {
      std::unique_lock<std::mutex> l(mu_);
      cv_.wait_for(l, std::chrono::seconds(1), [&] { return stop_.load() || !q_.empty(); });
      if (stop_) break;
      if (q_.empty()) continue;
      j = std::move(q_.front());
      q_.pop_front();
    }
    if (slot_ && slot_->load(std::memory_order_relaxed) > j.built_slot + c_.max_slot_lag) {
      ++stale_;
      if (sent_) sent_(j, -1, "", "slot passed", 0);
      continue;
    }
    if (!have_auth && !ensure_auth(*t)) {
      ++rejected_;
      if (sent_) sent_(j, GRPC_UNAUTHENTICATED, "", last_error(), 0);
      continue;
    }
    std::string resp, err, uuid;
    const uint64_t t0 = now_ns();
    int code = t->call(SEND_BUNDLE, gbx_bundle_request(j.txs, j.dropping, j.targets), resp, access_, err);
    if (code == GRPC_UNAUTHENTICATED) {  // token expired early: authenticate again, once
      access_.clear();
      if (ensure_auth(*t)) code = t->call(SEND_BUNDLE, gbx_bundle_request(j.txs, j.dropping, j.targets), resp, access_, err);
    }
    const int64_t rtt = int64_t(now_ns() - t0) / 1000;
    if (code == 0 && gbx_parse_bundle_uuid(resp, uuid)) {
      ++ok_;
      std::lock_guard<std::mutex> g(pmu_);
      pending_.push_back(Pending{j, wall_ms() + int64_t(c_.status_timeout_s) * 1000});
    } else {
      ++rejected_;
      std::lock_guard<std::mutex> g(info_mu_);
      last_err_ = "send: code " + std::to_string(code) + " " + err;
    }
    if (sent_) sent_(j, code, uuid, err, rtt);
  }
}

void GbxClient::run_status() {
  while (!stop_) {
    std::this_thread::sleep_for(std::chrono::milliseconds(c_.status_poll_ms));
    std::vector<Pending> batch;
    {
      std::lock_guard<std::mutex> g(pmu_);
      batch.assign(pending_.begin(), pending_.begin() + long(std::min<size_t>(pending_.size(), 256)));
    }
    if (batch.empty()) continue;
    std::vector<std::string> sigs;
    for (auto& p : batch) sigs.push_back(p.job.sig);
    std::vector<RpcSigStatus> st;
    try {
      st = c_.statuses(sigs);
    } catch (const std::exception& e) {
      LOG_WARN("gbx: getSignatureStatuses failed: %s", e.what());
      continue;
    }
    const int64_t now = wall_ms();
    std::vector<std::pair<GbxJob, std::pair<GbxOutcome, uint64_t>>> done;
    {
      std::lock_guard<std::mutex> g(pmu_);
      for (size_t i = 0; i < batch.size(); ++i) {
        GbxOutcome o;
        uint64_t slot = 0;
        if (i < st.size() && st[i].found) {
          o = st[i].failed ? GbxOutcome::failed : GbxOutcome::landed;
          slot = st[i].slot;
        } else if (now >= batch[i].deadline_ms) {
          o = GbxOutcome::dropped;
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
        case GbxOutcome::landed: ++landed_; break;
        case GbxOutcome::failed: ++failed_; break;
        case GbxOutcome::dropped: ++dropped_; break;
      }
      if (resolved_) resolved_(job, os.first, os.second);
    }
  }
}

}  // namespace arb::submit
