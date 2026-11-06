#include "core/config.hpp"

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace arb {

namespace {

std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t\r\n");
  if (a == std::string::npos) return "";
  size_t b = s.find_last_not_of(" \t\r\n");
  return s.substr(a, b - a + 1);
}

std::string strip_comment(const std::string& line) {
  bool in_str = false;
  for (size_t i = 0; i < line.size(); ++i) {
    if (line[i] == '"') in_str = !in_str;
    if (line[i] == '#' && !in_str) return line.substr(0, i);
  }
  return line;
}

std::string unquote(const std::string& v) {
  if (v.size() >= 2 && v.front() == '"' && v.back() == '"') return v.substr(1, v.size() - 2);
  return v;
}

std::vector<std::string> split_array(const std::string& v) {
  std::vector<std::string> out;
  if (v.size() < 2 || v.front() != '[' || v.back() != ']') throw std::runtime_error("config: expected array, got " + v);
  std::string body = v.substr(1, v.size() - 2);
  std::string cur;
  bool in_str = false;
  for (char c : body) {
    if (c == '"') in_str = !in_str;
    if (c == ',' && !in_str) {
      if (!trim(cur).empty()) out.push_back(unquote(trim(cur)));
      cur.clear();
    } else {
      cur.push_back(c);
    }
  }
  if (!trim(cur).empty()) out.push_back(unquote(trim(cur)));
  return out;
}

std::string digits(const std::string& r) {
  std::string s;
  for (char c : r)
    if (c != '_') s.push_back(c);
  return s;
}

struct Reader {
  const std::map<std::string, std::string>& kv;
  std::map<std::string, bool> used;

  const std::string* raw(const std::string& key) {
    auto it = kv.find(key);
    if (it == kv.end()) return nullptr;
    used[key] = true;
    return &it->second;
  }
  void get(const std::string& k, std::string& v) { if (auto r = raw(k)) v = unquote(*r); }
  void get(const std::string& k, bool& v) {
    if (auto r = raw(k)) {
      if (*r == "true") v = true;
      else if (*r == "false") v = false;
      else throw std::runtime_error("config: " + k + " must be true/false");
    }
  }
  void get(const std::string& k, double& v) { if (auto r = raw(k)) v = std::stod(digits(*r)); }
  void get(const std::string& k, int& v) { if (auto r = raw(k)) v = std::stoi(digits(*r)); }
  void get(const std::string& k, uint32_t& v) { if (auto r = raw(k)) v = static_cast<uint32_t>(std::stoul(digits(*r))); }
  void get(const std::string& k, uint64_t& v) { if (auto r = raw(k)) v = std::stoull(digits(*r)); }
  void get(const std::string& k, std::vector<std::string>& v) { if (auto r = raw(k)) v = split_array(*r); }
};

}  // namespace

std::map<std::string, std::string> parse_toml(const std::string& text) {
  std::map<std::string, std::string> kv;
  std::istringstream in(text);
  std::string line, section;
  int n = 0;
  while (std::getline(in, line)) {
    ++n;
    line = trim(strip_comment(line));
    if (line.empty()) continue;
    if (line.front() == '[' && line.back() == ']' && line.find('=') == std::string::npos) {
      section = trim(line.substr(1, line.size() - 2));
      continue;
    }
    size_t eq = line.find('=');
    if (eq == std::string::npos) throw std::runtime_error("config line " + std::to_string(n) + ": expected key = value");
    std::string key = trim(line.substr(0, eq));
    std::string val = trim(line.substr(eq + 1));
    kv[section.empty() ? key : section + "." + key] = val;
  }
  return kv;
}

Config load_config(const std::string& path) {
  Config c;
  std::map<std::string, std::string> kv;
  if (!path.empty()) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("config: cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    kv = parse_toml(ss.str());
  }
  Reader r{kv, {}};
#define G(sec, field) r.get(#sec "." #field, c.sec.field)
  G(mode, dry_run); G(mode, funding_mode); G(mode, kill_file); G(mode, simulate_before_send);
  G(helius, laserstream_endpoint); G(helius, rpc_url); G(helius, api_key_env); G(helius, max_grpc_connections);
  G(helius, shard_size); G(helius, resync_lag_ms); G(helius, reconnect_min_ms); G(helius, reconnect_max_ms);
  G(helius, ping_interval_ms); G(helius, replay_from_slot); G(helius, rpc_timeout_ms);
  G(jito, regions); G(jito, block_engine_fmt); G(jito, tip_floor_url); G(jito, tip_min_lamports);
  G(jito, tip_max_lamports); G(jito, tip_floor_refresh_ms); G(jito, tip_accounts_refresh_s); G(jito, backoff_429_ms);
  G(jito, status_poll_ms); G(jito, status_timeout_ms); G(jito, sender_enabled); G(jito, sender_url);
  G(engine, base_mints); G(engine, max_hops); G(engine, min_spot_edge); G(engine, min_ev_lamports);
  G(engine, min_net_lamports); G(engine, max_trade_lamports); G(engine, min_trade_lamports);
  G(engine, max_input_fraction); G(engine, target_land_rate); G(engine, max_bundles_per_slot);
  G(engine, max_cycles_per_pool); G(engine, max_send_lag_ms); G(engine, max_state_age_slots);
  G(engine, cooldown_ms); G(chain, slot_ms); G(engine, sizer_max_evals); G(engine, alpha_init); G(engine, alpha_min);
  G(engine, alpha_max); G(engine, alpha_step); G(engine, alpha_window); G(engine, p_land_init);
  G(engine, cu_overhead); G(engine, max_route_cu); G(engine, cu_headroom); G(engine, base_fee_lamports);
  G(engine, cu_price_micro_lamports);
  G(dex, raydium_v4_enabled); G(dex, whirlpool_enabled); G(dex, raydium_v4_cu); G(dex, whirlpool_cu);
  G(dex, cpmm_enabled); G(dex, cpmm_cu);
  G(discovery, min_pool_tvl_sol); G(discovery, max_tracked_pools); G(discovery, interval_s);
  G(discovery, track_whirlpool); G(discovery, raydium_fetch_cap);
  G(discovery, track_cpmm); G(discovery, cpmm_fetch_cap);
  G(flash, provider); G(flash, lending_market); G(flash, reserves);
  G(risk, max_daily_fee_spend_sol); G(risk, min_sol_float); G(risk, max_sol_float);
  G(risk, max_consecutive_rejects); G(risk, reject_pause_s); G(risk, max_feed_lag_ms);
  G(risk, quote_mismatch_rate); G(risk, quote_mismatch_min_samples); G(risk, transfer_fee_max_bps);
  G(risk, freeze_allowlist); G(risk, denylist_path); G(risk, denylist_ttl_s);
  G(keys, hot_wallet_path); G(keys, cold_wallet_pubkey); G(keys, executor_program_id);
  G(obs, metrics_addr); G(obs, metrics_port); G(obs, log_level); G(obs, log_path); G(obs, decision_log_dir);
  G(obs, decision_log_rotate_s);
  G(recorder, dir); G(recorder, rotate_s);
  G(sim, enabled); G(sim, max_per_s); G(sim, holder_refresh_s); G(sim, min_payer_lamports);
#undef G
  r.get("pools_path", c.pools_path);
  for (auto& [k, v] : kv)
    if (!r.used.count(k)) throw std::runtime_error("config: unknown key " + k);
  if (c.mode.funding_mode != "flash" && c.mode.funding_mode != "own")
    throw std::runtime_error("config: mode.funding_mode must be flash or own");
  if (c.engine.max_hops < 2 || c.engine.max_hops > 3) throw std::runtime_error("config: engine.max_hops must be 2 or 3");
  // The LaserStream endpoint comes with the Helius key, so it may live in the environment file instead.
  if (const char* ep = std::getenv("LASERSTREAM_ENDPOINT"); ep && *ep) c.helius.laserstream_endpoint = ep;
  return c;
}

std::string resolve_rpc_url(const Config& c) {
  const char* key = std::getenv(c.helius.api_key_env.c_str());
  if (key && *key) return c.helius.rpc_url + "/?api-key=" + key;
  return "https://api.mainnet-beta.solana.com";
}

}  // namespace arb
