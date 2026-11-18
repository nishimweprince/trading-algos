#include "submit/leaders.hpp"

#include <simdjson.h>

#include <chrono>
#include <cmath>

#include "core/rpc.hpp"
#include "obs/log.hpp"
#include "submit/conn.hpp"

namespace arb::submit {

const char* leader_kind_name(LeaderKind k) {
  switch (k) {
    case LeaderKind::jito: return "jito";
    case LeaderKind::bam: return "bam";
    case LeaderKind::not_jito: return "not_jito";
    default: return "unknown";
  }
}

const std::vector<RegionPoint>& jito_regions() {
  static const std::vector<RegionPoint> v{{"frankfurt", 50.11, 8.68}, {"amsterdam", 52.37, 4.90}, {"london", 51.51, -0.13},
                                          {"dublin", 53.35, -6.26},   {"ny", 40.71, -74.01},       {"slc", 40.76, -111.89},
                                          {"tokyo", 35.68, 139.69},   {"singapore", 1.35, 103.82}};
  return v;
}

std::string nearest_region(double lat, double lon, const std::vector<std::string>& allowed) {
  constexpr double kRad = 3.14159265358979323846 / 180.0;
  std::string best;
  double best_d = 1e18;
  for (const RegionPoint& r : jito_regions()) {
    if (std::find(allowed.begin(), allowed.end(), r.name) == allowed.end()) continue;
    double dlat = (r.lat - lat) * kRad, dlon = (r.lon - lon) * kRad;
    double a = std::sin(dlat / 2) * std::sin(dlat / 2) + std::cos(lat * kRad) * std::cos(r.lat * kRad) * std::sin(dlon / 2) * std::sin(dlon / 2);
    double d = 2 * std::atan2(std::sqrt(a), std::sqrt(1 - a));
    if (d < best_d) { best_d = d; best = r.name; }
  }
  return best;
}

LeaderMap::LeaderMap(std::string rpc_url, std::vector<std::string> regions, int refresh_s)
    : url_(std::move(rpc_url)), regions_(std::move(regions)), refresh_s_(refresh_s) {
  th_ = std::thread([this] { run(); });
}

LeaderMap::~LeaderMap() {
  stop_ = true;
  cv_.notify_all();
  th_.join();
}

LeaderInfo LeaderMap::at(uint64_t slot) const {
  std::shared_ptr<const Snapshot> s;
  {
    std::lock_guard<std::mutex> g(mu_);
    s = snap_;
  }
  LeaderInfo out;
  if (!s) return out;
  for (const auto& [first, idx] : s->epochs) {
    if (slot < first || slot - first >= idx.size()) continue;
    uint32_t v = idx[size_t(slot - first)];
    if (v >= s->identities.size()) return out;
    out.identity = s->identities[v];
    out.kind = s->validators[v].kind;
    out.region = s->validators[v].region;
    out.place = s->validators[v].place;
    return out;
  }
  return out;
}

void LeaderMap::run() {
  while (!stop_) {
    try {
      refresh();
    } catch (const std::exception& e) {
      LOG_WARN("leader map: %s", e.what());
    }
    std::unique_lock<std::mutex> l(cv_mu_);
    cv_.wait_for(l, std::chrono::seconds(ready_ ? refresh_s_ : 30), [this] { return stop_.load(); });
  }
}

void LeaderMap::refresh() {
  Rpc rpc(url_, 30'000);
  auto ep = rpc.epoch_info("confirmed");
  auto snap = std::make_shared<Snapshot>();
  std::unordered_map<std::string, uint32_t> index;
  auto id_of = [&](std::string_view s) {
    auto [it, inserted] = index.try_emplace(std::string(s), uint32_t(snap->identities.size()));
    if (inserted) snap->identities.emplace_back(s);
    return it->second;
  };
  simdjson::dom::parser p;
  // This epoch, and the next once its schedule is known (always, for a leader schedule computed an epoch ahead).
  for (uint64_t first : {ep.first_slot(), ep.first_slot() + ep.slots_in_epoch}) {
    std::string body;
    try {
      body = rpc.call("getLeaderSchedule", "[" + std::to_string(first) + ",{\"commitment\":\"confirmed\"}]");
    } catch (const std::exception&) {
      continue;
    }
    simdjson::dom::object m;
    if (p.parse(body)["result"].get(m) != simdjson::SUCCESS) continue;
    std::vector<uint32_t> per_slot(ep.slots_in_epoch, UINT32_MAX);
    for (auto [k, v] : m) {
      uint32_t id = id_of(k);
      for (auto x : v.get_array()) {
        uint64_t off = uint64_t(int64_t(x));
        if (off < per_slot.size()) per_slot[off] = id;
      }
    }
    snap->epochs.emplace_back(first, std::move(per_slot));
  }
  if (snap->epochs.empty()) throw std::runtime_error("no leader schedule");
  // Jito's validator list: running_jito / running_bam per identity. Absent = not running Jito.
  std::unordered_map<std::string, LeaderKind> kinds;
  {
    std::string body, err;
    int st = http_once("GET", "https://kobe.mainnet.jito.network/api/v1/validators", {}, 15'000, body, &err);
    if (st == 200) {
      simdjson::dom::parser kp;
      simdjson::dom::array arr;
      if (kp.parse(body)["validators"].get(arr) == simdjson::SUCCESS)
        for (auto v : arr) {
          std::string_view id;
          bool jito = false, bam = false;
          if (v["identity_account"].get(id) != simdjson::SUCCESS) continue;
          if (v["running_jito"].get(jito) != simdjson::SUCCESS) jito = false;
          if (v["running_bam"].get(bam) != simdjson::SUCCESS) bam = false;
          kinds[std::string(id)] = !jito ? LeaderKind::not_jito : bam ? LeaderKind::bam : LeaderKind::jito;
        }
    } else {
      LOG_WARN("leader map: Kobe validators: %s", st ? ("http " + std::to_string(st)).c_str() : err.c_str());
    }
  }
  // Gossip IPs, then one batch geolocation for IPs not seen before.
  std::unordered_map<std::string, std::string> ip_of;
  {
    std::string body = rpc.call("getClusterNodes", "[]");
    simdjson::dom::parser cp;
    simdjson::dom::array arr;
    if (cp.parse(body)["result"].get(arr) == simdjson::SUCCESS)
      for (auto n : arr) {
        std::string_view id, gossip;
        if (n["pubkey"].get(id) != simdjson::SUCCESS || n["gossip"].get(gossip) != simdjson::SUCCESS) continue;
        if (index.count(std::string(id))) ip_of[std::string(id)] = std::string(gossip.substr(0, gossip.rfind(':')));
      }
  }
  std::vector<std::string> todo;
  for (const auto& [id, ip] : ip_of)
    if (!geo_.count(ip)) todo.push_back(ip);
  for (size_t i = 0; i < todo.size(); i += 100) {
    std::string req = "[";
    for (size_t j = i; j < std::min(todo.size(), i + 100); ++j) req += (j > i ? ",\"" : "\"") + todo[j] + "\"";
    req += "]";
    std::string body, err;
    if (http_once("POST", "http://ip-api.com/batch?fields=query,countryCode,city,lat,lon", req, 15'000, body, &err) != 200) break;
    simdjson::dom::parser gp;
    simdjson::dom::array arr;
    if (gp.parse(body).get(arr) != simdjson::SUCCESS) break;
    for (auto g : arr) {
      std::string_view q, cc, city;
      double lat = 0, lon = 0;
      if (g["query"].get(q) != simdjson::SUCCESS || g["lat"].get(lat) != simdjson::SUCCESS || g["lon"].get(lon) != simdjson::SUCCESS) continue;
      if (g["countryCode"].get(cc) != simdjson::SUCCESS) cc = "?";
      if (g["city"].get(city) != simdjson::SUCCESS) city = "";
      geo_[std::string(q)] = {std::string(cc) + " " + std::string(city), {lat, lon}};
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));  // ip-api: 15 batch requests a minute
  }
  snap->validators.resize(snap->identities.size());
  for (size_t i = 0; i < snap->identities.size(); ++i) {
    Validator& v = snap->validators[i];
    auto k = kinds.find(snap->identities[i]);
    v.kind = kinds.empty() ? LeaderKind::unknown : k == kinds.end() ? LeaderKind::not_jito : k->second;
    auto ip = ip_of.find(snap->identities[i]);
    if (ip != ip_of.end())
      if (auto g = geo_.find(ip->second); g != geo_.end()) {
        v.place = g->second.first;
        v.region = nearest_region(g->second.second.first, g->second.second.second, regions_);
      }
  }
  {
    std::lock_guard<std::mutex> g(mu_);
    snap_ = std::move(snap);
  }
  if (!ready_) LOG_INFO("leader map: %zu validators, %zu with a region, %zu Jito flags", ip_of.size(), geo_.size(), kinds.size());
  ready_ = true;
}

}  // namespace arb::submit
