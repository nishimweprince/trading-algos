#include "core/config.hpp"

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "sol/swap.hpp"

namespace pd {

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
  void get(const std::string& k, double& v) { if (auto r = raw(k)) v = std::stod(*r); }
  void get(const std::string& k, int& v) { if (auto r = raw(k)) v = std::stoi(*r); }
  void get(const std::string& k, uint32_t& v) { if (auto r = raw(k)) v = static_cast<uint32_t>(std::stoul(*r)); }
  void get(const std::string& k, uint64_t& v) {
    if (auto r = raw(k)) {
      std::string s;
      for (char c : *r) if (c != '_') s.push_back(c);
      v = std::stoull(s);
    }
  }
  void get(const std::string& k, std::vector<std::string>& v) { if (auto r = raw(k)) v = split_array(*r); }
  void get(const std::string& k, std::vector<double>& v) {
    if (auto r = raw(k)) {
      v.clear();
      for (auto& s : split_array(*r)) v.push_back(std::stod(s));
    }
  }
};

void env_default(std::string& v, const char* name) {
  if (!v.empty()) return;
  if (const char* e = std::getenv(name)) v = e;
}

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
  G(stream, endpoint); G(stream, token); G(stream, band_values); G(stream, ping_interval_ms);
  G(stream, reconnect_min_ms); G(stream, reconnect_max_ms); G(stream, gap_silence_ms); G(stream, gap_slot_jump);
  G(stream, silence_reconnect_ms);
  G(rpc, http_url); G(rpc, blockhash_refresh_ms); G(rpc, amm_config_refresh_ms); G(rpc, balance_refresh_ms);
  G(rpc, timeout_ms);
  G(racer, admit_cost_sol); G(racer, max_completion_cost_sol); G(racer, curve_fee_bps);
  G(racer, track_window_minutes); G(racer, complete_no_pool_sec); G(racer, pool_push_wait_ms);
  G(racer, default_virtual_quote_lamports);
  G(racer, max_curves);
  G(screen, min_pool_sol); G(screen, max_pool_sol); G(screen, max_buy_impact_pct); G(screen, require_pump_suffix);
  G(screen, max_migrate_age_slots);
  G(screen, blacklist_mints); G(screen, blacklist_creators);
  G(entry, size_wallet_pct); G(entry, min_size_sol); G(entry, max_size_sol); G(entry, slippage_pct); G(entry, cu_limit);
  G(entry, priority_multiplier);
  G(exits, tp0_enabled); G(exits, tp0_pct); G(exits, tp0_sell_fraction); G(exits, tp0_move_stop_to_pct);
  G(exits, tp1_pct); G(exits, tp1_sell_fraction); G(exits, tp2_pct); G(exits, trailing_arm_pct);
  G(exits, trailing_gap_pct); G(exits, trailing_gap_high_vol_pct); G(exits, hard_stop_pct);
  G(exits, tp1_move_stop_to_pct); G(exits, time_stop_minutes); G(exits, dead_money_enabled);
  G(exits, dead_money_minutes); G(exits, dead_money_max_mfe_pct); G(exits, liquidity_dump_pct);
  G(exits, stall_enabled); G(exits, stall_arm_sec); G(exits, stall_sec);
  G(exits, protective_slippage_tiers);
  G(exits, take_profit_slippage_tiers); G(exits, exit_retry_ms); G(exits, max_exit_attempts);
  G(exits, exit_priority_multiplier); G(exits, cu_limit);
  G(risk, max_concurrent_positions); G(risk, gas_headroom_sol); G(risk, wallet_floor_sol); G(risk, kill_file);
  G(risk, buy_confirm_timeout_ms); G(risk, sent_resolve_ms);
  G(fees, cu_price_default); G(fees, cu_price_floor); G(fees, cu_price_cap); G(fees, refresh_ms);
  G(fees, priority_level);
  G(sender, url); G(sender, swqos_only); G(sender, tip_lamports); G(sender, exit_tip_lamports);
  G(sender, connections); G(sender, ping_ms); G(sender, tip_accounts);
  G(sender, retry_429_ms); G(sender, max_429_retries);
  G(jito, enabled); G(jito, url); G(jito, tip_lamports); G(jito, tip_accounts);
  G(routing, enabled); G(routing, lookahead_slots); G(routing, bam_validators_path);
  G(routing, cluster_nodes_refresh_s); G(routing, rivals_path); G(routing, rival_window_slots);
  G(routing, cu_limit_margin_pct); G(routing, cu_samples_min);
  for (auto [name, rc] : {std::pair{"agave", &c.routing.agave}, {"bam", &c.routing.bam}, {"firedancer", &c.routing.firedancer}}) {
    std::string sec = std::string("routing.") + name + ".";
    r.get(sec + "cu_price_mode", rc->cu_price_mode);
    r.get(sec + "cu_price_multiplier", rc->cu_price_multiplier);
    r.get(sec + "exit_cu_price_mode", rc->exit_cu_price_mode);
    r.get(sec + "cu_limit", rc->cu_limit);
    r.get(sec + "sender_tip_lamports", rc->sender_tip_lamports);
    r.get(sec + "jito", rc->jito);
  }
  G(lighthouse, enabled); G(lighthouse, vault_guard); G(lighthouse, vault_tolerance_bps); G(lighthouse, rival_guard);
  G(lighthouse, max_rivals);
  G(jupiter, enabled); G(jupiter, url); G(jupiter, slippage_bps); G(jupiter, max_attempts); G(jupiter, confirm_ms);
  G(jupiter, max_priority_lamports);
  G(threads, ingest_cpu); G(threads, logic_cpu); G(threads, exec_cpu); G(threads, aux_cpu);
  G(threads, fifo_priority); G(threads, lock_memory);
  G(ring, slots); G(ring, slot_bytes);
  G(storage, db_path); G(storage, jsonl_path); G(storage, capture_path);
#undef G
  r.get("wallet_env", c.wallet_env);
  for (auto& [k, v] : kv)
    if (!r.used.count(k)) throw std::runtime_error("config: unknown key " + k);

  env_default(c.stream.endpoint, "HELIUS_GRPC_URL");
  env_default(c.stream.token, "HELIUS_GRPC_TOKEN");
  env_default(c.rpc.http_url, "HELIUS_HTTP_URL");
  if (const char* s = std::getenv("SENDER_URL")) c.sender.url = s;
  if (const char* k = std::getenv("JUPITER_API_KEY")) c.jupiter.api_key = k;
  if (c.jupiter.api_key.empty()) c.jupiter.enabled = false;
  if (const char* j = std::getenv("JITO_URL")) c.jito.url = j;

  if (c.sender.tip_accounts.empty())
    // pump-fun/src/executor/heliusSender.ts HELIUS_SENDER_TIP_ACCOUNTS
    c.sender.tip_accounts = {"4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
                             "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
                             "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD", "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
                             "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF", "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
                             "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey", "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or"};
  if (c.jito.tip_accounts.empty())
    c.jito.tip_accounts = {"96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
                           "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
                           "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
                           "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"};

  if (c.sender.swqos_only && c.sender.tip_lamports < 5000)
    throw std::runtime_error("config: sender.tip_lamports must be >= 5000 with swqos_only");
  if (!c.sender.swqos_only && c.sender.tip_lamports < 1'000'000)
    throw std::runtime_error("config: sender.tip_lamports must be >= 1000000 without swqos_only");
  if (c.entry.priority_multiplier <= 0) throw std::runtime_error("config: entry.priority_multiplier must be > 0");
  if (c.sender.retry_429_ms < 0 || c.sender.max_429_retries < 0 || c.sender.max_429_retries > 255)
    throw std::runtime_error("config: sender.retry_429_ms / max_429_retries");
  {
    const std::string& l = c.fees.priority_level;
    if (l != "Min" && l != "Low" && l != "Medium" && l != "High" && l != "VeryHigh" && l != "UnsafeMax")
      throw std::runtime_error("config: fees.priority_level must be Min|Low|Medium|High|VeryHigh|UnsafeMax");
  }
  if (c.entry.size_wallet_pct <= 0 || c.entry.size_wallet_pct > 100) throw std::runtime_error("config: entry.size_wallet_pct");
  if (c.exits.protective_slippage_tiers.empty() || c.exits.take_profit_slippage_tiers.empty())
    throw std::runtime_error("config: exit slippage tiers must not be empty");
  if (c.exits.liquidity_dump_pct < 0 || c.exits.liquidity_dump_pct > 100)
    throw std::runtime_error("config: exits.liquidity_dump_pct must be 0..100");
  if (c.exits.stall_arm_sec < 0 || c.exits.stall_sec < 0)
    throw std::runtime_error("config: exits.stall_arm_sec / stall_sec must be >= 0");
  if (c.risk.max_concurrent_positions < 1 || c.risk.max_concurrent_positions > 8)
    throw std::runtime_error("config: risk.max_concurrent_positions must be 1..8");
  if (c.screen.max_migrate_age_slots < 0) throw std::runtime_error("config: screen.max_migrate_age_slots must be >= 0");
  if (c.jupiter.slippage_bps < 1 || c.jupiter.slippage_bps > 10000 || c.jupiter.max_attempts < 0 || c.jupiter.max_attempts > 20 ||
      c.jupiter.confirm_ms < 1000)
    throw std::runtime_error("config: jupiter.slippage_bps 1..10000, max_attempts 0..20, confirm_ms >= 1000");
  if (c.routing.lookahead_slots < 0 || c.routing.lookahead_slots > 8) throw std::runtime_error("config: routing.lookahead_slots must be 0..8");
  if (c.routing.rival_window_slots < 0 || c.routing.rival_window_slots > 64)
    throw std::runtime_error("config: routing.rival_window_slots must be 0..64");
  if (c.routing.cu_limit_margin_pct < 0 || c.routing.cu_samples_min < 1) throw std::runtime_error("config: routing.cu_limit_margin_pct / cu_samples_min");
  for (auto [name, rc] : {std::pair{"agave", &c.routing.agave}, {"bam", &c.routing.bam}, {"firedancer", &c.routing.firedancer}}) {
    std::string sec = std::string("config: routing.") + name;
    for (const std::string* m : {&rc->cu_price_mode, &rc->exit_cu_price_mode})
      if (*m != "estimate" && *m != "floor" && *m != "beat_rivals")
        throw std::runtime_error(sec + (m == &rc->cu_price_mode ? ".cu_price_mode" : ".exit_cu_price_mode") +
                                 " must be estimate|floor|beat_rivals");
    if (rc->cu_price_multiplier <= 0) throw std::runtime_error(sec + ".cu_price_multiplier must be > 0");
    if (rc->jito && !c.jito.enabled) throw std::runtime_error(sec + ".jito needs jito.enabled");
    if (rc->sender_tip_lamports && c.sender.swqos_only && rc->sender_tip_lamports < 5000)
      throw std::runtime_error(sec + ".sender_tip_lamports must be >= 5000 with swqos_only");
    if (rc->sender_tip_lamports && !c.sender.swqos_only && rc->sender_tip_lamports < 1'000'000)
      throw std::runtime_error(sec + ".sender_tip_lamports must be >= 1000000 without swqos_only");
  }
  if (c.lighthouse.max_rivals < 0 || c.lighthouse.max_rivals > int(MAX_RIVALS))
    throw std::runtime_error("config: lighthouse.max_rivals must be 0.." + std::to_string(MAX_RIVALS));
  if (c.lighthouse.vault_tolerance_bps < 0) throw std::runtime_error("config: lighthouse.vault_tolerance_bps must be >= 0");
  return c;
}

}  // namespace pd
