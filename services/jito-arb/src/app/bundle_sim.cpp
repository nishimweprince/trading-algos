#include "app/bundle_sim.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <fstream>
#include <sstream>
#include <limits>
#include <filesystem>
#include <simdjson.h>

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
                                const engine::Opportunity& o, const Pubkey& holder, const std::vector<bool>& need_ata, bool need_pump_volume) {
  if (c.n < 2 || c.n > engine::MAX_LEGS || legs.size() < c.n) return fail(Errc::unsupported, "route must have 2 or 3 legs");
  for (int i = 0; i < c.n; ++i)
    if (!legs[size_t(i)]) return fail(Errc::bad_data, "missing leg snapshot");
  SimLayout s = sim_accounts(c, legs, holder);
  l.clear();
  tx::set_compute_unit_limit(l, 1'400'000);
  for (int i = 0; i + 1 < c.n; ++i) {
    if (size_t(i) >= need_ata.size() || !need_ata[size_t(i)]) continue;
    const Pubkey& mint = c.legs[i].mint_out;
    const Pubkey& tp = token_program_of(mint, *legs[size_t(i)]);
    // ATA program CreateIdempotent (1): payer, ata, owner, mint, system, token program.
    l.add(prog::ATA).acc(holder, true, true).rw(s.intermediates[size_t(i)]).ro(holder).ro(mint).ro(prog::SYSTEM).ro(tp).u8(1);
  }
  if (need_pump_volume) tx::dexix::pump_init_user_volume(l, holder);
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
  r.profit_base = int64_t(std::clamp<__int128>(profit, INT64_MIN, INT64_MAX));
  // The candidate's own base -> lamports rate: gross / profit_base (both after the flash fee).
  __int128 gross = 0;
  if (j.o.profit_base > 0) gross = profit * __int128(j.cand.gross_lamports) / __int128(j.o.profit_base);
  r.gross_lamports = uint64_t(std::clamp<__int128>(gross, 0, UINT64_MAX));
  r.net_lamports = int64_t(std::clamp<__int128>(gross - __int128(j.cand.tip_lamports) - __int128(j.cand.fee_lamports), INT64_MIN, INT64_MAX));
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

BundleSim::BundleSim(const Config& cfg, std::atomic<bool>& stop) : cfg_(cfg), stop_(stop) {
  std::ifstream f(cfg.sim.holder_seed_path);
  if (!f) {
    LOG_WARN("bundle sim: holder seeds unavailable at %s", cfg.sim.holder_seed_path.c_str());
    return;
  }
  try {
    std::stringstream ss;
    ss << f.rdbuf();
    simdjson::dom::parser p;
    simdjson::padded_string ps(ss.str());
    simdjson::dom::object obj = p.parse(ps)["holders"];
    for (auto kv : obj) {
      Pubkey mint;
      if (!b58_decode32(kv.key, mint)) throw std::runtime_error("invalid seed mint");
      for (auto e : simdjson::dom::array(kv.value)) {
        Pubkey k;
        if (!b58_decode32(std::string_view(e), k)) throw std::runtime_error("invalid seed account");
        seeds_[mint].push_back(k);
      }
    }
  } catch (const std::exception& e) {
    seeds_.clear();
    LOG_WARN("bundle sim: invalid holder seeds: %s", e.what());
  }
}

BundleSim::~BundleSim() { shutdown(); }

void BundleSim::shutdown() {
  quit_ = true;
  cv_.notify_all();
  if (thread_.joinable()) thread_.join();
  std::lock_guard<std::mutex> g(mu_);
  while (!queue_.empty()) {
    const auto& j = queue_.front();
    done_.push_back(sim_outcome(j, false, obs::SimFail::shutdown, 0, 0, 0, wall_ms()));
    queue_.pop_front();
    ++n_.terminal;
  }
  n_.queued = 0;
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
  if (stop_ || quit_ || tokens_ < 1.0 || queue_.size() + done_.size() + n_.in_flight.load() >= MAX_QUEUE) {
    ++n_.dropped;
    return false;
  }
  tokens_ -= 1.0;
  queue_.push_back(std::move(j));
  n_.queued = queue_.size();
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
      n_.queued = queue_.size();
      n_.in_flight = 1;
    }
    process(j, rpc);
  }
}

void BundleSim::refresh_holders(const Pubkey& mint, Rpc& rpc) {
  Holders& h = holders_[mint];
  h.refreshed_ms = wall_ms();
  h.list.clear();
  auto validate = [&](std::vector<Pubkey> keys) {
    // Bound every getMultipleAccounts request; public seeds are hints, never trusted balances/authorities.
    if (keys.size() > 100) keys.resize(100);
    if (keys.empty()) return;
    auto ta = rpc.multiple_accounts(keys, "processed");
    std::vector<SimHolder> candidates;
    for (size_t i = 0; i < ta.size(); ++i) {
      if (!ta[i]) continue;
      auto token = state::decode_token_account(ta[i]->data.data(), ta[i]->data.size(), ta[i]->owner);
      if (!token || token->mint != mint || token->state != 1 || token->has_delegate || !token->amount) continue;
      // Resolve the owner's canonical account even when the seed names another token account it owns.
      Pubkey canonical = ata(token->owner, mint, ta[i]->owner);
      candidates.push_back({token->owner, canonical, 0});
    }
    if (candidates.empty()) return;
    std::vector<Pubkey> owners, canonical;
    for (auto& c : candidates) { owners.push_back(c.owner); canonical.push_back(c.token_account); }
    auto oa = rpc.multiple_accounts(owners, "processed");
    auto ca = rpc.multiple_accounts(canonical, "processed");
    for (size_t i = 0; i < candidates.size(); ++i) {
      if (!oa[i] || oa[i]->owner != prog::SYSTEM || !oa[i]->data.empty() ||
          oa[i]->lamports < cfg_.sim.min_payer_lamports || !is_on_curve(candidates[i].owner.data()) || !ca[i]) continue;
      auto t = state::decode_token_account(ca[i]->data.data(), ca[i]->data.size(), ca[i]->owner);
      if (!t || t->mint != mint || t->owner != candidates[i].owner || t->state != 1 || t->has_delegate || !t->amount) continue;
      candidates[i].balance = t->amount;
      if (std::none_of(h.list.begin(), h.list.end(), [&](const auto& x) { return x.owner == candidates[i].owner; }))
        h.list.push_back(candidates[i]);
    }
  };
  if (auto seed = seeds_.find(mint); seed != seeds_.end()) validate(seed->second);
  if (h.list.empty()) {
    std::vector<Pubkey> keys;
    try {
      uint64_t slot = 0;
      for (auto& [k, amount] : rpc.token_largest_accounts(mint, slot, "processed")) keys.push_back(k);
    } catch (const std::exception&) {
      // Helius rejects getTokenLargestAccounts for very large mints. DAS is a bounded, filtered fallback.
      if (cfg_.helius.rpc_url.find("helius") == std::string::npos) return;
      std::string body = rpc.call("getTokenAccounts", "{\"mint\":\"" + b58(mint) + "\",\"limit\":1000}");
      simdjson::dom::parser p;
      simdjson::padded_string ps(body);
      std::vector<std::pair<uint64_t, Pubkey>> amounts;
      for (auto e : simdjson::dom::array(p.parse(ps)["result"]["token_accounts"])) {
        Pubkey key;
        uint64_t amount;
        std::string_view address;
        if (e["address"].get(address) == simdjson::SUCCESS && b58_decode32(address, key) &&
            e["amount"].get(amount) == simdjson::SUCCESS) amounts.emplace_back(amount, key);
      }
      std::sort(amounts.rbegin(), amounts.rend());
      for (size_t i = 0; i < std::min<size_t>(100, amounts.size()); ++i) keys.push_back(amounts[i].second);
    }
    validate(keys);
  }
  std::sort(h.list.begin(), h.list.end(), [](const auto& a, const auto& b) { return a.balance > b.balance; });
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

std::array<uint64_t, 3> sim_leg_units(const std::vector<std::string>& logs,
                                     std::span<const std::shared_ptr<const PoolState>> legs) {
  std::array<uint64_t, 3> units{};
  size_t leg = 0;
  std::string root;
  for (const auto& line : logs) {
    if (line.rfind("Program ", 0) != 0) continue;
    size_t space = line.find(' ', 8);
    if (space == std::string::npos) continue;
    const std::string program = line.substr(8, space - 8);
    if (line.find("invoke [1]", space) != std::string::npos) root = program;
    if (root != program || leg >= legs.size()) continue;
    const Pubkey* expected = dex_program(legs[leg]->kind);
    if (!expected || program != b58(*expected)) continue;
    if (auto pos = line.find(" consumed ", space); pos != std::string::npos)
      units[leg] = std::strtoull(line.c_str() + pos + 10, nullptr, 10);
    if (line.find(" success", space) != std::string::npos || line.find(" failed:", space) != std::string::npos) {
      ++leg;
      root.clear();
    }
  }
  return units;
}

namespace {
std::string json_string(const std::string& value) {
  std::string s = "\"";
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') { s += '\\'; s += char(c); }
    else if (c == '\n') s += "\\n";
    else if (c == '\r') s += "\\r";
    else if (c == '\t') s += "\\t";
    else if (c >= 32) s += char(c);
  }
  return s + "\"";
}
uint64_t account_amount(const std::optional<RpcAccount>& a) {
  uint64_t v = 0;
  if (a && a->data.size() >= 72) std::memcpy(&v, a->data.data() + 64, 8);
  return v;
}
bool account_same(const std::optional<RpcAccount>& a, const std::optional<RpcAccount>& b) {
  return a.has_value() == b.has_value() && (!a || (a->owner == b->owner && a->lamports == b->lamports && a->data == b->data));
}
}  // namespace

void BundleSim::process(SimJob& j, Rpc& rpc) {
  std::vector<std::string> logs;
  std::string error;
  std::vector<int64_t> residuals;
  std::span<const std::shared_ptr<const PoolState>> legs(j.legs.data(), j.c.n);
  auto finish = [&](obs::DecisionRecord r) {
    auto units = sim_leg_units(logs, legs);
    for (size_t i = 0; i < units.size(); ++i) r.leg_out[i] = units[i];
    try {
      std::filesystem::create_directories(cfg_.obs.decision_log_dir);
      time_t t = time_t(r.wall_ms / 1000);
      tm utc;
      gmtime_r(&t, &utc);
      char name[64];
      std::strftime(name, sizeof name, "/sim-details-%Y%m%d-%H.jsonl", &utc);
      std::ofstream f(cfg_.obs.decision_log_dir + name, std::ios::app);
      f << "{\"signature\":" << json_string(b58(j.cand.signature, 64)) << ",\"wall_ms\":" << r.wall_ms
        << ",\"candidate_slot\":" << j.cand.slot << ",\"sim_slot\":" << r.slot
        << ",\"reason\":" << unsigned(r.reason) << ",\"error\":" << json_string(error)
        << ",\"diagnostic\":\"fixed quoted inputs; not executor measured-output chaining\",\"residuals\":[";
      for (size_t i = 0; i < residuals.size(); ++i) f << (i ? "," : "") << residuals[i];
      f << "],\"logs\":[";
      for (size_t i = 0; i < logs.size(); ++i) f << (i ? "," : "") << json_string(logs[i]);
      f << "]}\n";
      if (!f) LOG_WARN("bundle sim: diagnostic write failed");
    } catch (const std::exception& e) { LOG_WARN("bundle sim diagnostics: %s", e.what()); }
    std::lock_guard<std::mutex> g(mu_);
    done_.push_back(r);  // total outstanding (including completed) is bounded at submit
    ++n_.terminal;
    n_.in_flight = 0;
  };
  auto failed = [&](obs::SimFail why, uint64_t slot, uint64_t units) {
    finish(sim_outcome(j, false, why, slot, 0, units, wall_ms()));
  };
  try {
    std::optional<SimHolder> holder;
    std::vector<Pubkey> pre_keys;
    std::vector<std::optional<RpcAccount>> pre;
    uint64_t pre_slot = 0;
    // Re-read funds immediately, try another cached holder if one spent its balance.
    for (int attempt = 0; attempt < 3; ++attempt) {
      const SimHolder* h = holder_for(j.c.base, j.o.x, rpc);
      if (!h) { holder.reset(); break; }
      holder = *h;
      SimLayout acc = sim_accounts(j.c, legs, h->owner);
      pre_keys = {acc.base_account};
      pre_keys.insert(pre_keys.end(), acc.intermediates.begin(), acc.intermediates.end());
      auto read_keys = pre_keys;
      read_keys.push_back(h->owner);
      pre = rpc.multiple_accounts_at(read_keys, pre_slot, "processed");
      auto& wallet = pre.back();
      if (account_amount(pre[0]) >= j.o.x && wallet && wallet->owner == prog::SYSTEM && wallet->data.empty() &&
          wallet->lamports >= cfg_.sim.min_payer_lamports) { pre.pop_back(); break; }
      auto& list = holders_[j.c.base].list;
      list.erase(std::remove_if(list.begin(), list.end(), [&](const auto& x) { return x.owner == holder->owner; }), list.end());
      holder.reset();
    }
    if (!holder) {
      ++n_.no_holder;
      return failed(obs::SimFail::no_holder, pre_slot, 0);
    }
    uint64_t base_pre = account_amount(pre[0]);
    std::vector<bool> need(pre_keys.size() - 1);
    for (size_t i = 0; i < need.size(); ++i) need[i] = !pre[i + 1];
    tx::IxList l;
    bool uses_pump = false;
    for (const auto& p : legs) uses_pump |= p && p->kind == DexKind::PumpSwap;
    bool need_volume = uses_pump && !rpc.account(tx::dexix::pump_user_volume_accumulator(holder->owner), "processed");
    auto lay = build_sim_ixs(l, j.c, legs, j.o, holder->owner, need, need_volume);
    if (!lay) {
      ++n_.failed;
      return failed(obs::SimFail::other, pre_slot, 0);
    }
    std::string txb = unsigned_tx_b64(holder->owner, l);
    if (txb.empty()) {
      ++n_.too_large;
      return failed(obs::SimFail::too_large, pre_slot, 0);
    }
    auto sim = rpc.simulate_full(txb, pre_keys, std::max(j.cand.slot, pre_slot), "processed");
    logs = sim.logs;
    error = sim.err;
    // These transactions do not alter mainnet. An unchanged bracket establishes that the holder's baseline
    // was stable across the simulation; otherwise balance deltas cannot measure the route's output.
    uint64_t after_slot = 0;
    auto after = rpc.multiple_accounts_at(pre_keys, after_slot, "processed");
    bool stable = after_slot >= sim.slot && after.size() == pre.size();
    for (size_t i = 0; stable && i < pre.size(); ++i) stable = account_same(pre[i], after[i]);
    if (!stable) {
      ++n_.funding_changed;
      holders_.erase(j.c.base);
      return failed(obs::SimFail::funding_changed, sim.slot, sim.units);
    }
    if (!sim.ok) {
      ++n_.failed;
      auto why = classify_sim_error(sim.err, lay->first_swap, j.c.n);
      LOG_INFO("bundle sim failed: cycle %u leg %u err %s", j.c.id, unsigned(why), sim.err.substr(0, 120).c_str());
      return failed(why, sim.slot, sim.units);
    }
    if (sim.accounts.size() != pre.size() || !sim.accounts[0]) {
      ++n_.rpc_error;
      error = "missing post-simulation account state";
      return failed(obs::SimFail::rpc, sim.slot, sim.units);
    }
    bool consumed_inventory = false;
    for (size_t i = 1; i < pre.size(); ++i) {
      i128 delta = i128(account_amount(sim.accounts[i])) - i128(account_amount(pre[i]));
      residuals.push_back(int64_t(std::clamp<i128>(delta, INT64_MIN, INT64_MAX)));
      consumed_inventory |= delta < 0;
    }
    if (consumed_inventory) {
      ++n_.intermediate_balance;
      return failed(obs::SimFail::intermediate_balance, sim.slot, sim.units);
    }
    i128 output = i128(account_amount(sim.accounts[0])) + i128(j.o.x) - i128(base_pre);
    if (output < 0 || output > UINT64_MAX) {
      ++n_.funding_changed;
      return failed(obs::SimFail::funding_changed, sim.slot, sim.units);
    }
    auto r = sim_outcome(j, true, obs::SimFail::none, sim.slot, uint64_t(output), sim.units, wall_ms());
    ++n_.ok;
    if (r.net_lamports > 0) ++n_.profitable;
    LOG_INFO("bundle sim ok: cycle %u final_out %llu quoted %llu net %lld units %llu", j.c.id,
             (unsigned long long)r.final_out, (unsigned long long)r.min_out, (long long)r.net_lamports, (unsigned long long)sim.units);
    finish(r);
  } catch (const std::exception& e) {
    ++n_.rpc_error;
    error = e.what();
    LOG_WARN("bundle sim: %s", e.what());
    failed(obs::SimFail::rpc, 0, 0);
  }
}

}  // namespace arb::app
