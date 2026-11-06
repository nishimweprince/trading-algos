#include "app/bundle_sim.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/compact_u16.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "core/sys.hpp"
#include "obs/log.hpp"
#include "tx/builders.hpp"
#include "tx/dex_ix.hpp"

namespace arb::app {

namespace {

constexpr size_t MAX_QUEUE = 64;
constexpr size_t PACKET_DATA_SIZE = 1232;

const Pubkey& token_program_of(const Pubkey& mint, const PoolState& p) {
  const auto& info = mint == p.mint_a ? p.mint_a_info : p.mint_b_info;
  return info && info->token_program == prog::TOKEN_2022 ? prog::TOKEN_2022 : prog::TOKEN;
}

}  // namespace

SimLayout sim_accounts(const engine::Cycle& c, std::span<const std::shared_ptr<const PoolState>> legs, const Pubkey& holder) {
  SimLayout s;
  s.base_account = tx::wallet_ata(holder, c.base, *legs[0]);
  for (int i = 0; i + 1 < c.n; ++i) s.intermediates.push_back(tx::wallet_ata(holder, c.legs[i].mint_out, *legs[size_t(i)]));
  return s;
}

Result<SimLayout> build_sim_ixs(tx::IxList& l, const engine::Cycle& c, std::span<const std::shared_ptr<const PoolState>> legs,
                                const engine::Opportunity& o, const Pubkey& holder, const std::vector<bool>& need_ata) {
  if (c.n < 2 || c.n > engine::MAX_LEGS || legs.size() < c.n) return fail(Errc::unsupported, "route must have 2 or 3 legs");
  for (int i = 0; i < c.n; ++i)
    if (!legs[size_t(i)]) return fail(Errc::bad_data, "missing leg snapshot");
  SimLayout s = sim_accounts(c, legs, holder);
  l.clear();
  for (int i = 0; i + 1 < c.n; ++i) {
    if (size_t(i) >= need_ata.size() || !need_ata[size_t(i)]) continue;
    const Pubkey& mint = c.legs[i].mint_out;
    const Pubkey& tp = token_program_of(mint, *legs[size_t(i)]);
    // ATA program CreateIdempotent (1): payer, ata, owner, mint, system, token program.
    l.add(prog::ATA).acc(holder, true, true).rw(s.intermediates[size_t(i)]).ro(holder).ro(mint).ro(prog::SYSTEM).ro(tp).u8(1);
  }
  s.first_swap = l.n;
  for (int i = 0; i < c.n; ++i) {
    const PoolState& p = *legs[size_t(i)];
    const engine::Leg& leg = c.legs[i];
    tx::dexix::UserAccounts ua{holder, tx::wallet_ata(holder, p.mint_a, p), tx::wallet_ata(holder, p.mint_b, p)};
    uint64_t in = i == 0 ? o.x : o.leg_out[i - 1];
    try {
      tx::dexix::swap(l, p, leg.a_to_b, ua, in, 0);
    } catch (const std::exception&) {
      return fail(Errc::unsupported, "swap builder refused the leg");
    }
  }
  return s;
}

obs::SimFail classify_sim_error(const std::string& err_json, uint8_t first_swap, int n_legs) {
  // {"InstructionError":[2,{"Custom":6001}]} or [2,"InvalidAccountData"]
  auto k = err_json.find("InstructionError");
  if (k == std::string::npos) return obs::SimFail::other;
  auto b = err_json.find('[', k);
  if (b == std::string::npos) return obs::SimFail::other;
  int idx = std::atoi(err_json.c_str() + b + 1);
  if (idx < first_swap) return obs::SimFail::setup;
  int leg = idx - first_swap;
  if (leg >= 0 && leg < n_legs) return obs::SimFail(uint8_t(leg + 1));
  return obs::SimFail::other;
}

obs::DecisionRecord sim_outcome(const SimJob& j, bool ok, obs::SimFail why, uint64_t sim_slot, uint64_t sim_final_out,
                                uint64_t units, int64_t wall_ms) {
  obs::DecisionRecord r = j.cand;  // cycle, hops, dex, pools, base, signature, tip, fee
  r.kind = uint8_t(obs::RecordKind::Outcome);
  r.action = uint8_t(obs::Action::DryRun);
  r.outcome = uint8_t(ok ? obs::Outcome::SimulatedOk : obs::Outcome::SimulationFailed);
  r.reason = uint8_t(why);
  r.wall_ms = wall_ms;
  r.slot = sim_slot;
  r.state_slot[0] = j.cand.slot;
  r.min_out = j.o.final_out;  // quoted
  r.cu_limit = uint32_t(std::min<uint64_t>(units, UINT32_MAX));
  r.ev_lamports = 0;
  r.p_land_ppm = 0;
  if (!ok) {
    r.final_out = 0;
    r.profit_base = 0;
    r.gross_lamports = 0;
    r.net_lamports = 0;
    return r;
  }
  r.final_out = sim_final_out;
  __int128 profit = __int128(sim_final_out) - __int128(j.o.x) - __int128(j.o.flash_fee);
  r.profit_base = int64_t(profit);
  // The candidate's own base -> lamports rate: gross / profit_base (both after the flash fee).
  __int128 gross = 0;
  if (j.o.profit_base > 0) gross = profit * __int128(j.cand.gross_lamports) / __int128(j.o.profit_base);
  r.gross_lamports = gross > 0 ? uint64_t(gross) : 0;
  r.net_lamports = int64_t(gross - __int128(j.cand.tip_lamports) - __int128(j.cand.fee_lamports));
  return r;
}

std::string unsigned_tx_b64(const Pubkey& payer, const tx::IxList& ixs) {
  uint8_t msg[4096];
  auto m = tx::compile_message_v0(payer, ixs, Hash{}, {}, msg, sizeof msg);
  if (!m) return "";
  std::vector<uint8_t> wire;
  uint8_t sv[3];
  size_t n = compact_u16_encode(msg[1], sv);
  wire.insert(wire.end(), sv, sv + n);
  wire.insert(wire.end(), size_t(msg[1]) * 64, 0);
  wire.insert(wire.end(), msg, msg + *m);
  if (wire.size() > PACKET_DATA_SIZE) return "";
  return b64(wire.data(), wire.size());
}

BundleSim::BundleSim(const Config& cfg, std::atomic<bool>& stop) : cfg_(cfg), stop_(stop) {}

BundleSim::~BundleSim() {
  {
    std::lock_guard<std::mutex> g(mu_);
    quit_ = true;
  }
  cv_.notify_all();
  if (thread_.joinable()) thread_.join();
}

void BundleSim::start() {
  if (!cfg_.sim.enabled || thread_.joinable()) return;
  thread_ = std::thread([this] { run(); });
}

bool BundleSim::submit(SimJob j) {
  if (!cfg_.sim.enabled) return false;
  std::lock_guard<std::mutex> g(mu_);
  ++n_.submitted;
  int64_t now = wall_ms();
  double rate = std::max(0.01, cfg_.sim.max_per_s);
  if (tokens_ms_ == 0) tokens_ = rate;
  tokens_ = std::min(rate, tokens_ + double(now - tokens_ms_) * rate / 1000.0);
  tokens_ms_ = now;
  if (tokens_ < 1.0 || queue_.size() >= MAX_QUEUE) {
    ++n_.dropped;
    return false;
  }
  tokens_ -= 1.0;
  queue_.push_back(std::move(j));
  cv_.notify_one();
  return true;
}

std::vector<obs::DecisionRecord> BundleSim::drain() {
  std::lock_guard<std::mutex> g(mu_);
  std::vector<obs::DecisionRecord> out;
  out.swap(done_);
  return out;
}

void BundleSim::run() {
  Rpc rpc(resolve_rpc_url(cfg_), cfg_.helius.rpc_timeout_ms);
  while (!stop_ && !quit_) {
    SimJob j;
    {
      std::unique_lock<std::mutex> lk(mu_);
      cv_.wait_for(lk, std::chrono::milliseconds(200), [&] { return stop_ || quit_ || !queue_.empty(); });
      if (stop_ || quit_) break;
      if (queue_.empty()) continue;
      j = std::move(queue_.front());
      queue_.pop_front();
    }
    process(j, rpc);
  }
}

void BundleSim::refresh_holders(const Pubkey& mint, Rpc& rpc) {
  Holders& h = holders_[mint];
  h.refreshed_ms = wall_ms();
  h.list.clear();
  uint64_t slot = 0;
  auto largest = rpc.token_largest_accounts(mint, slot, "processed");
  std::vector<Pubkey> accs;
  for (auto& [k, amt] : largest) accs.push_back(k);
  if (accs.empty()) return;
  auto ta = rpc.multiple_accounts(accs, "processed");
  std::vector<SimHolder> cand;
  for (size_t i = 0; i < accs.size(); ++i) {
    if (!ta[i] || ta[i]->owner != prog::TOKEN || ta[i]->data.size() < 72) continue;
    SimHolder s;
    s.owner = key_from(ta[i]->data.data() + 32);
    s.token_account = accs[i];
    std::memcpy(&s.balance, ta[i]->data.data() + 64, 8);
    // Only the owner's canonical ATA: the route's base account is derived the same way.
    if (ata(s.owner, mint, prog::TOKEN) != s.token_account) continue;
    cand.push_back(s);
  }
  std::vector<Pubkey> owners;
  for (auto& s : cand) owners.push_back(s.owner);
  if (owners.empty()) return;
  auto oa = rpc.multiple_accounts(owners, "processed");
  for (size_t i = 0; i < cand.size(); ++i)
    if (oa[i] && oa[i]->owner == prog::SYSTEM && oa[i]->data.empty() && oa[i]->lamports >= cfg_.sim.min_payer_lamports)
      h.list.push_back(cand[i]);
  std::sort(h.list.begin(), h.list.end(), [](const SimHolder& a, const SimHolder& b) { return a.balance > b.balance; });
  LOG_INFO("bundle sim: %zu usable holders of %s (largest %llu)", h.list.size(), b58(mint).substr(0, 4).c_str(),
           (unsigned long long)(h.list.empty() ? 0 : h.list.front().balance));
}

const SimHolder* BundleSim::holder_for(const Pubkey& mint, uint64_t amount, Rpc& rpc) {
  auto it = holders_.find(mint);
  if (it == holders_.end() || wall_ms() - it->second.refreshed_ms > int64_t(cfg_.sim.holder_refresh_s) * 1000) {
    refresh_holders(mint, rpc);
    it = holders_.find(mint);
  }
  // Largest first; the balance is re-checked before each simulation (the pre read).
  for (const SimHolder& h : it->second.list)
    if (h.balance >= amount) return &h;
  return nullptr;
}

void BundleSim::process(SimJob& j, Rpc& rpc) {
  auto finish = [&](obs::DecisionRecord r) {
    std::lock_guard<std::mutex> g(mu_);
    done_.push_back(r);
  };
  auto failed = [&](obs::SimFail why, uint64_t slot, uint64_t units) {
    finish(sim_outcome(j, false, why, slot, 0, units, wall_ms()));
  };
  std::span<const std::shared_ptr<const PoolState>> legs(j.legs.data(), j.c.n);
  try {
    const SimHolder* h = holder_for(j.c.base, j.o.x, rpc);
    if (!h) {
      ++n_.no_holder;
      return failed(obs::SimFail::no_holder, 0, 0);
    }
    SimLayout acc = sim_accounts(j.c, legs, h->owner);
    std::vector<Pubkey> pre_keys = {acc.base_account};
    for (const Pubkey& k : acc.intermediates) pre_keys.push_back(k);
    uint64_t pre_slot = 0;
    auto pre = rpc.multiple_accounts_at(pre_keys, pre_slot, "processed");
    uint64_t base_pre = 0;
    if (pre[0] && pre[0]->data.size() >= 72) std::memcpy(&base_pre, pre[0]->data.data() + 64, 8);
    if (base_pre < j.o.x) {
      ++n_.no_holder;  // the holder spent it since the last refresh
      holders_.erase(j.c.base);
      return failed(obs::SimFail::no_holder, pre_slot, 0);
    }
    std::vector<bool> need(acc.intermediates.size());
    for (size_t i = 0; i < acc.intermediates.size(); ++i) need[i] = !pre[i + 1];
    tx::IxList l;
    auto lay = build_sim_ixs(l, j.c, legs, j.o, h->owner, need);
    if (!lay) {
      ++n_.failed;
      return failed(obs::SimFail::other, pre_slot, 0);
    }
    std::string txb = unsigned_tx_b64(h->owner, l);
    if (txb.empty()) {
      ++n_.too_large;
      return failed(obs::SimFail::too_large, pre_slot, 0);
    }
    auto sim = rpc.simulate_full(txb, {acc.base_account}, j.cand.slot, "processed");
    if (!sim.ok) {
      ++n_.failed;
      obs::SimFail why = classify_sim_error(sim.err, lay->first_swap, j.c.n);
      std::string tail;
      for (size_t i = sim.logs.size() > 3 ? sim.logs.size() - 3 : 0; i < sim.logs.size(); ++i) tail += " | " + sim.logs[i].substr(0, 120);
      LOG_INFO("bundle sim failed: cycle %u leg %u err %s%s", j.c.id, unsigned(why), sim.err.substr(0, 80).c_str(), tail.c_str());
      return failed(why, sim.slot, sim.units);
    }
    uint64_t base_post = 0;
    if (!sim.accounts.empty() && sim.accounts[0] && sim.accounts[0]->data.size() >= 72)
      std::memcpy(&base_post, sim.accounts[0]->data.data() + 64, 8);
    // The holder spent x and got the route's output back into the same account.
    uint64_t final_out = base_post + j.o.x - std::min(base_post + j.o.x, base_pre);
    obs::DecisionRecord r = sim_outcome(j, true, obs::SimFail::none, sim.slot, final_out, sim.units, wall_ms());
    ++n_.ok;
    if (r.net_lamports > 0) ++n_.profitable;
    finish(r);
  } catch (const std::exception& e) {
    ++n_.rpc_error;
    LOG_WARN("bundle sim: %s", e.what());
    failed(obs::SimFail::rpc, 0, 0);
  }
}

}  // namespace arb::app
