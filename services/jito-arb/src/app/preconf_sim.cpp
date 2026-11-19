#include "app/preconf_sim.hpp"

#include <algorithm>
#include <chrono>
#include <set>

#include "core/base64.hpp"
#include "core/sys.hpp"
#include "obs/log.hpp"
#include "tx/builders.hpp"

namespace arb::app {

std::vector<AccountUpdate> SpecResult::views() const {
  std::vector<AccountUpdate> v;
  v.reserve(accounts.size());
  for (const OwnedUpdate& a : accounts) {
    AccountUpdate u;
    u.key = a.key;
    u.owner = a.owner;
    u.data = a.data.data();
    u.len = a.data.size();
    u.slot = a.slot;
    u.missing = a.missing;
    v.push_back(u);
  }
  return v;
}

std::vector<uint32_t> touched_pools(const feed::TxKeys& k, const std::unordered_map<Pubkey, uint32_t, PubkeyHash>& pool_of,
                                    const std::unordered_map<Pubkey, std::vector<Pubkey>, PubkeyHash>& tables,
                                    std::vector<Pubkey>* missing) {
  std::vector<uint32_t> out;
  auto add = [&](const Pubkey& key) {
    auto it = pool_of.find(key);
    if (it != pool_of.end() && std::find(out.begin(), out.end(), it->second) == out.end()) out.push_back(it->second);
  };
  for (const Pubkey& key : k.statics) add(key);
  for (const auto& l : k.lookups) {
    auto t = tables.find(l.table);
    if (t == tables.end()) {
      if (missing) missing->push_back(l.table);
      continue;
    }
    bool short_table = false;
    for (uint8_t i : l.writable) {
      if (i < t->second.size()) add(t->second[i]);
      else short_table = true;
    }
    for (uint8_t i : l.readonly) {
      if (i < t->second.size()) add(t->second[i]);
      else short_table = true;
    }
    if (short_table && missing) missing->push_back(l.table);  // extended since we read it
  }
  return out;
}

PreconfSim::PreconfSim(std::string rpc_url, std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_of,
                       std::unordered_map<uint32_t, std::vector<Pubkey>> keys_of, feed::CreditGuard* guard, size_t max_queue)
    : rpc_url_(std::move(rpc_url)), pool_of_(std::move(pool_of)), keys_of_(std::move(keys_of)), guard_(guard), max_queue_(max_queue) {
  th_ = std::thread([this] { run(); });
}

PreconfSim::~PreconfSim() {
  stop_ = true;
  cv_.notify_all();
  if (th_.joinable()) th_.join();
}

void PreconfSim::push(feed::PreconfTx&& t) {
  ++in_;
  {
    std::lock_guard<std::mutex> g(mu_);
    if (q_.size() >= max_queue_) {
      q_.pop_front();  // stale anyway: the newest preconfirmations are the ones still worth acting on
      ++dropped_;
    }
    q_.push_back(std::move(t));
  }
  cv_.notify_one();
}

bool PreconfSim::pop(SpecResult& out) {
  std::lock_guard<std::mutex> g(out_mu_);
  if (out_.empty()) return false;
  out = std::move(out_.front());
  out_.pop_front();
  return true;
}

void PreconfSim::run() {
  std::unique_ptr<Rpc> rpc;
  while (!stop_) {
    feed::PreconfTx t;
    {
      std::unique_lock<std::mutex> l(mu_);
      cv_.wait_for(l, std::chrono::milliseconds(500), [&] { return stop_.load() || !q_.empty(); });
      if (stop_) break;
      if (q_.empty()) continue;
      t = std::move(q_.front());
      q_.pop_front();
    }
    feed::TxKeys keys;
    if (!feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(t.tx.data()), t.tx.size(), keys)) continue;
    try {
      if (!rpc) rpc = std::make_unique<Rpc>(rpc_url_, 3000);
      std::vector<Pubkey> missing;
      std::vector<uint32_t> pools = touched_pools(keys, pool_of_, tables_, &missing);
      if (!missing.empty()) {
        // Lookup tables are read once and cached; one more read when a table has been extended since.
        std::sort(missing.begin(), missing.end());
        missing.erase(std::unique(missing.begin(), missing.end()), missing.end());
        auto accts = rpc->multiple_accounts(missing, "processed");
        ++table_reads_;
        if (guard_) guard_->add(1, wall_ms());
        for (size_t i = 0; i < missing.size() && i < accts.size(); ++i) {
          if (!accts[i]) continue;
          auto alt = tx::decode_lookup_table(accts[i]->data.data(), accts[i]->data.size(), accts[i]->owner);
          if (alt) tables_[missing[i]] = alt->addresses;
        }
        pools = touched_pools(keys, pool_of_, tables_, nullptr);
      }
      if (pools.empty()) {
        ++no_pool_;
        continue;
      }
      std::vector<Pubkey> read;
      for (uint32_t id : pools) {
        auto it = keys_of_.find(id);
        if (it != keys_of_.end()) read.insert(read.end(), it->second.begin(), it->second.end());
      }
      std::sort(read.begin(), read.end());
      read.erase(std::unique(read.begin(), read.end()), read.end());
      Rpc::SimResult sim = rpc->simulate_full(b64(reinterpret_cast<const uint8_t*>(t.tx.data()), t.tx.size()), read, 0, "processed");
      ++sims_;
      if (guard_) guard_->add(1, wall_ms());
      if (!sim.ok) {
        ++sim_reverted_;  // it would fail now (or already ran): nothing to act on
        continue;
      }
      SpecResult r;
      r.pools = std::move(pools);
      r.slot = t.slot;
      r.sim_slot = sim.slot;
      r.recv_ns = t.recv_ns;
      r.sig = keys.sig;
      for (size_t i = 0; i < read.size() && i < sim.accounts.size(); ++i) {
        OwnedUpdate u;
        u.key = read[i];
        u.slot = std::max(sim.slot, t.slot);
        if (sim.accounts[i]) {
          u.owner = sim.accounts[i]->owner;
          u.data = std::move(sim.accounts[i]->data);
        } else {
          u.missing = true;
        }
        r.accounts.push_back(std::move(u));
      }
      r.sim_done_ns = now_ns();
      {
        std::lock_guard<std::mutex> g(lat_mu_);
        sim_us_.add(r.sim_done_ns, int64_t(r.sim_done_ns - t.recv_ns) / 1000);
      }
      std::lock_guard<std::mutex> g(out_mu_);
      if (out_.size() >= max_queue_) out_.pop_front();
      out_.push_back(std::move(r));
    } catch (const std::exception& e) {
      ++sim_failed_;
      rpc.reset();
      LOG_WARN("preconf sim: %s", e.what());
    }
  }
}

}  // namespace arb::app
