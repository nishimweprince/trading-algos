#include "aux/leaders.hpp"

#include <simdjson.h>
#include <sys/stat.h>

#include <chrono>
#include <deque>
#include <fstream>
#include <sstream>
#include <thread>

#include "aux/rpc.hpp"
#include "aux/writer.hpp"
#include "core/base58.hpp"
#include "core/sys.hpp"

namespace pd {

namespace {

using simdjson::dom::element;

int64_t mtime_of(const std::string& path) {
  struct stat st;
  if (path.empty() || ::stat(path.c_str(), &st) != 0) return -1;
  return int64_t(st.st_mtime);
}

std::string read_file(const std::string& path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

void add_bam_entry(element v, std::unordered_set<std::string>& out) {
  std::string_view s;
  if (v.get(s) == simdjson::SUCCESS) { out.emplace(s); return; }
  for (const char* k : {"identity", "validator", "identity_pubkey", "pubkey"})
    if (v[k].get(s) == simdjson::SUCCESS) { out.emplace(s); return; }
}

}  // namespace

Schedule parse_leader_schedule(std::string_view body) {
  simdjson::dom::parser p;
  simdjson::dom::object r = p.parse(body.data(), body.size())["result"];
  Schedule out;
  for (auto [identity, slots] : r) {
    std::vector<uint32_t> v;
    for (element s : slots.get_array()) v.push_back(uint32_t(uint64_t(s)));
    out.emplace_back(std::string(identity), std::move(v));
  }
  return out;
}

std::unordered_map<std::string, std::string> parse_cluster_versions(std::string_view body) {
  simdjson::dom::parser p;
  simdjson::dom::array a = p.parse(body.data(), body.size())["result"];
  std::unordered_map<std::string, std::string> out;
  for (element n : a) {
    std::string_view id, ver;
    if (n["pubkey"].get(id) != simdjson::SUCCESS) continue;
    if (n["version"].get(ver) != simdjson::SUCCESS) ver = {};
    out.emplace(std::string(id), std::string(ver));
  }
  return out;
}

std::unordered_set<std::string> parse_bam_list(std::string_view json) {
  simdjson::dom::parser p;
  element root = p.parse(json.data(), json.size());
  std::unordered_set<std::string> out;
  simdjson::dom::array a;
  if (root.get(a) != simdjson::SUCCESS && root["validators"].get(a) != simdjson::SUCCESS)
    throw std::runtime_error("BAM list: expected an array or {\"validators\": [...]}");
  for (element v : a) add_bam_entry(v, out);
  return out;
}

LeaderClass classify_version(std::string_view version) {
  return version.substr(0, 2) == "0." ? LeaderClass::Firedancer : LeaderClass::Agave;
}

LeaderClass classify_leader(const std::string& identity, const std::unordered_map<std::string, std::string>& versions,
                            const std::unordered_set<std::string>& bam) {
  if (bam.count(identity)) return LeaderClass::Bam;
  auto it = versions.find(identity);
  return it == versions.end() ? LeaderClass::Agave : classify_version(it->second);
}

std::unique_ptr<LeaderTable> build_leader_table(uint64_t first_slot, uint64_t slots_per_epoch, const std::vector<Schedule>& epochs,
                                                const std::unordered_map<std::string, std::string>& versions,
                                                const std::unordered_set<std::string>& bam) {
  auto t = std::make_unique<LeaderTable>();
  t->first_slot = first_slot;
  t->cls.assign(slots_per_epoch * epochs.size(), uint8_t(LeaderClass::Agave));
  t->leader.assign(t->cls.size(), 0);
  t->identities.push_back(Pubkey{});  // index 0: unknown leader
  std::unordered_map<std::string, uint16_t> index;
  for (size_t e = 0; e < epochs.size(); ++e) {
    for (auto& [identity, slots] : epochs[e]) {
      auto [it, created] = index.emplace(identity, uint16_t(t->identities.size()));
      if (created) {
        Pubkey k{};
        if (!b58_decode32(identity, k) || t->identities.size() >= 0xffff) {
          index.erase(it);
          continue;
        }
        t->identities.push_back(k);
      }
      uint8_t c = uint8_t(classify_leader(identity, versions, bam));
      for (uint32_t s : slots) {
        if (s >= slots_per_epoch) continue;
        uint64_t off = e * slots_per_epoch + s;
        t->cls[off] = c;
        t->leader[off] = it->second;
      }
    }
  }
  return t;
}

void run_aux_leaders(const Config& cfg, Shared& sh, Logger& log) {
  name_thread("pd-aux-leaders");
  pin_thread(cfg.threads.aux_cpu);
  if (!cfg.routing.enabled) return;
  Rpc rpc(cfg.rpc.http_url, std::max(cfg.rpc.timeout_ms, 20000));  // a schedule is a few MB of JSON
  std::unordered_map<std::string, std::string> versions;
  std::unordered_set<std::string> bam;
  std::vector<Schedule> epochs;
  uint64_t first_slot = 0, slots_per_epoch = 0;
  int64_t bam_mtime = -2;
  uint64_t next_nodes = 0, next_epoch = 0;
  std::deque<std::unique_ptr<LeaderTable>> alive;  // logic may still read the previous tables
  int failures = 0;

  while (!sh.stop.load(std::memory_order_relaxed)) {
    uint64_t now = now_ns();
    bool dirty = false;
    try {
      int64_t m = mtime_of(cfg.routing.bam_validators_path);
      if (m != bam_mtime) {
        bam_mtime = m;
        bam = m < 0 ? std::unordered_set<std::string>{} : parse_bam_list(read_file(cfg.routing.bam_validators_path));
        log.info("BAM list: " + std::to_string(bam.size()) + " validators" + (m < 0 ? " (no " + cfg.routing.bam_validators_path + ")" : ""));
        dirty = true;
      }
      if (now >= next_nodes) {
        versions = parse_cluster_versions(rpc.call("getClusterNodes", "[]"));
        next_nodes = now + uint64_t(cfg.routing.cluster_nodes_refresh_s) * 1'000'000'000ULL;
        dirty = true;
      }
      if (now >= next_epoch) {
        next_epoch = now + 30'000'000'000ULL;
        Rpc::EpochInfo ei = rpc.epoch_info();
        if (ei.first_slot() != first_slot || epochs.size() < 2) {
          std::vector<Schedule> next;
          next.push_back(parse_leader_schedule(rpc.call("getLeaderSchedule", "[" + std::to_string(ei.absolute_slot) + "]")));
          try {
            // Known one epoch ahead; covers the boundary for lookahead.
            next.push_back(parse_leader_schedule(rpc.call("getLeaderSchedule", "[" + std::to_string(ei.first_slot() + ei.slots_in_epoch) + "]")));
          } catch (const std::exception&) {
          }
          epochs = std::move(next);
          first_slot = ei.first_slot();
          slots_per_epoch = ei.slots_in_epoch;
          dirty = true;
        }
      }
      if (dirty && !epochs.empty()) {
        auto t = build_leader_table(first_slot, slots_per_epoch, epochs, versions, bam);
        size_t n[N_LEADER_CLASSES] = {0};
        for (uint8_t c : t->cls) ++n[c < N_LEADER_CLASSES ? c : 0];
        sh.leaders.store(t.get(), std::memory_order_release);
        alive.push_back(std::move(t));
        while (alive.size() > 3) alive.pop_front();
        log.info("leader table: epoch from slot " + std::to_string(first_slot) + ", " + std::to_string(epochs.size()) +
                 " epoch(s): agave " + std::to_string(n[0]) + " / bam " + std::to_string(n[1]) + " / firedancer " +
                 std::to_string(n[2]) + " slots");
      }
      failures = 0;
    } catch (const std::exception& e) {
      if (++failures % 10 == 1) log.warn(std::string("aux-leaders: ") + e.what());
      next_epoch = std::min<uint64_t>(next_epoch, now + 5'000'000'000ULL);
    }
    for (int i = 0; i < 10 && !sh.stop.load(std::memory_order_relaxed); ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  // main joins the logic thread before this one, so nothing reads the tables after this.
  sh.leaders.store(nullptr, std::memory_order_release);
}

}  // namespace pd
