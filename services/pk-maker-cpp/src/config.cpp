#include "pkm/config.hpp"

#include <charconv>
#include <cstdlib>
#include <fstream>
#include <sstream>

extern char** environ;

namespace pkm {
namespace {

std::string trim(std::string s) {
  const auto start = s.find_first_not_of(" \t\r\n");
  const auto end = s.find_last_not_of(" \t\r\n");
  return start == std::string::npos ? std::string() : s.substr(start, end - start + 1);
}

std::string expand_home(std::string path) {
  if (path.starts_with("~/")) {
    if (const char* home = std::getenv("HOME")) path = std::string(home) + path.substr(1);
  }
  return path;
}

class Reader final {
 public:
  explicit Reader(const std::map<std::string, std::string>& vars) : vars_(vars) {}
  std::string text(const std::string& key, const std::string& fallback = "") const {
    const auto it = vars_.find(key);
    if (it == vars_.end()) return fallback;
    const auto v = trim(it->second);
    return v.empty() ? fallback : v;
  }
  template <typename T>
  T number(const std::string& key, T fallback) {
    const auto v = text(key);
    if (v.empty()) return fallback;
    T out{};
    const auto r = std::from_chars(v.data(), v.data() + v.size(), out);
    if (r.ec != std::errc{} || r.ptr != v.data() + v.size()) {
      if (error.empty()) error = key + "=" + v + " is not a number";
      return fallback;
    }
    return out;
  }
  std::string error;

 private:
  const std::map<std::string, std::string>& vars_;
};

}  // namespace

std::map<std::string, std::string> process_environment() {
  std::map<std::string, std::string> out;
  for (char** e = environ; e != nullptr && *e != nullptr; ++e) {
    const std::string kv(*e);
    const auto eq = kv.find('=');
    if (eq != std::string::npos) out[kv.substr(0, eq)] = kv.substr(eq + 1);
  }
  return out;
}

bool read_env_file(const std::string& path, std::map<std::string, std::string>& out, std::string& error) {
  std::ifstream in(expand_home(path));
  if (!in) {
    error = "cannot read " + path;
    return false;
  }
  std::string line;
  while (std::getline(in, line)) {
    line = trim(line);
    if (line.empty() || line[0] == '#') continue;
    if (line.starts_with("export ")) line = trim(line.substr(7));
    const auto eq = line.find('=');
    if (eq == std::string::npos) continue;
    const std::string key = trim(line.substr(0, eq));
    std::string value = trim(line.substr(eq + 1));
    if (value.size() >= 2 && (value.front() == '"' || value.front() == '\'') && value.back() == value.front()) {
      value = value.substr(1, value.size() - 2);
    } else if (const auto hash = value.find(" #"); hash != std::string::npos) {
      value = trim(value.substr(0, hash));
    }
    out.emplace(key, value);  // the process environment wins
  }
  return true;
}

bool load_config(const std::map<std::string, std::string>& vars, Config& c, std::string& error) {
  Reader r(vars);
  auto& s = c.strategy;
  s.half_spread = r.number("PK_PM_HALF_SPREAD", s.half_spread);
  s.shares = r.number("PK_PM_SHARES", s.shares);
  s.min_shares = r.number("PK_PM_MIN_SHARES", s.min_shares);
  s.max_net = r.number("PK_PM_MAX_NET", s.max_net);
  s.skew_k = r.number("PK_PM_SKEW_K", s.skew_k);
  s.pause_jump = r.number("PK_PM_PAUSE_JUMP", s.pause_jump);
  s.pause_ms = r.number("PK_PM_PAUSE_MS", s.pause_ms);
  s.max_kalshi_spread = r.number("PK_PM_MAX_KALSHI_SPREAD", s.max_kalshi_spread);
  s.kalshi_max_age_ms = r.number("PK_PM_KALSHI_MAX_AGE_MS", s.kalshi_max_age_ms);
  s.min_px = r.number("PK_PM_MIN_PX", s.min_px);
  s.max_px = r.number("PK_PM_MAX_PX", s.max_px);
  s.entry_early_s = r.number("PK_PM_ENTRY_EARLY_S", s.entry_early_s);
  s.stop_s = r.number("PK_PM_STOP_S", s.stop_s);
  s.requote_cents = r.number("PK_PM_REQUOTE_CENTS", s.requote_cents);
  s.min_requote_ms = r.number("PK_PM_MIN_REQUOTE_MS", s.min_requote_ms);
  s.adverse_ms = r.number("PK_PM_ADVERSE_MS", s.adverse_ms);
  s.reject_cooldown_ms = r.number("PK_PM_REJECT_COOLDOWN_MS", s.reject_cooldown_ms);
  s.max_orders_per_s = r.number("PK_PM_MAX_ORDERS_PER_S", s.max_orders_per_s);
  s.daily_loss_usd = r.number("PK_PM_DAILY_LOSS_USD", s.daily_loss_usd);
  s.max_open_cost_usd = r.number("PK_PM_MAX_OPEN_COST_USD", s.max_open_cost_usd);
  s.pair_margin = r.number("PK_PM_PAIR_MARGIN", s.pair_margin);
  s.open_leg_s = r.number("PK_PM_OPEN_LEG_S", s.open_leg_s);
  s.open_min_fair = r.number("PK_PM_OPEN_MIN_FAIR", s.open_min_fair);
  s.open_max_fair = r.number("PK_PM_OPEN_MAX_FAIR", s.open_max_fair);
  s.open_min_px = r.number("PK_PM_OPEN_MIN_PX", s.open_min_px);
  s.open_min_edge = r.number("PK_PM_OPEN_MIN_EDGE", s.open_min_edge);
  s.pair_half_spread = r.number("PK_PM_PAIR_HALF_SPREAD", s.pair_half_spread);

  c.assets.clear();
  std::stringstream assets(r.text("PK_PM_ASSETS", "BTC,ETH,SOL,XRP,BNB,HYPE,ZEC"));
  for (std::string a; std::getline(assets, a, ',');) {
    a = trim(a);
    for (auto& ch : a) ch = static_cast<char>(ch >= 'a' && ch <= 'z' ? ch - 'a' + 'A' : ch);
    if (!a.empty()) c.assets.push_back(a);
  }

  c.private_key = r.text("POLY_PRIVATE_KEY");
  if (const auto file = r.text("POLY_PRIVATE_KEY_FILE"); !file.empty()) {
    std::ifstream in(expand_home(file));
    std::getline(in, c.private_key);
    c.private_key = trim(c.private_key);
  }
  c.funder = r.text("POLY_FUNDER");
  c.signature_type = r.number("POLY_SIGNATURE_TYPE", 3);
  c.clob_api_key = r.text("POLY_CLOB_API_KEY");
  c.clob_api_secret = r.text("POLY_CLOB_API_SECRET");
  c.clob_api_passphrase = r.text("POLY_CLOB_API_PASSPHRASE");
  c.relayer_api_key = r.text("POLY_RELAYER_API_KEY");
  c.relayer_api_key_address = r.text("POLY_RELAYER_API_KEY_ADDRESS");
  c.collateral = r.text("POLY_REDEEM_COLLATERAL", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB");
  c.kalshi_key_id = r.text("KALSHI_API_KEY_ID");
  c.kalshi_key_path = expand_home(r.text("KALSHI_PRIVATE_KEY_PATH"));

  c.clob_url = r.text("POLY_CLOB_URL", c.clob_url);
  c.poly_ws_url = r.text("POLY_WS_URL", c.poly_ws_url);
  c.poly_user_ws_url = r.text("POLY_USER_WS_URL", c.poly_user_ws_url);
  c.gamma_url = r.text("POLY_GAMMA_URL", c.gamma_url);
  c.relayer_url = r.text("POLY_RELAYER_URL", c.relayer_url);
  c.kalshi_ws_url = r.text("KALSHI_WS_URL", c.kalshi_ws_url);
  c.kalshi_rest_url = r.text("KALSHI_REST_URL", c.kalshi_rest_url);

  c.state_dir = expand_home(r.text("PKM_STATE_DIR", c.state_dir));
  c.kill_file = expand_home(r.text("PKM_KILL_FILE", c.kill_file));
  c.live_confirm = r.text("PKM_LIVE_CONFIRM") == "yes";
  c.hot_core = r.number("PKM_HOT_CORE", c.hot_core);
  c.signer_core = r.number("PKM_SIGNER_CORE", c.signer_core);
  c.order_channels = r.number("PKM_ORDER_CHANNELS", c.order_channels);
  c.feed_stale_ms = r.number("PK_PM_FEED_STALE_MS", c.feed_stale_ms);
  c.poly_silence_ms = r.number("PKM_POLY_SILENCE_MS", c.poly_silence_ms);
  c.fills_poll_ms = r.number("PKM_FILLS_POLL_MS", c.fills_poll_ms);
  c.reconcile_s = r.number("PK_PM_RECONCILE_S", c.reconcile_s);
  c.presign_ttl_ms = r.number("PKM_PRESIGN_TTL_MS", c.presign_ttl_ms);

  if (!r.error.empty()) {
    error = r.error;
    return false;
  }
  auto fail = [&error](const std::string& why) {
    error = why;
    return false;
  };
  if (c.assets.empty()) return fail("PK_PM_ASSETS is empty");
  if (c.assets.size() > 16) return fail("PK_PM_ASSETS lists more than 16 assets");
  if (s.shares < s.min_shares) return fail("PK_PM_SHARES is below the venue minimum PK_PM_MIN_SHARES");
  if (s.max_net < s.shares) return fail("PK_PM_MAX_NET must be at least PK_PM_SHARES");
  if (s.shares != static_cast<double>(static_cast<long>(s.shares))) return fail("PK_PM_SHARES must be whole shares");
  if (s.half_spread < 0.005 || s.half_spread > 0.2) return fail("PK_PM_HALF_SPREAD must be within 0.005..=0.2");
  if (s.daily_loss_usd <= 0 || s.max_open_cost_usd <= 0) return fail("PK_PM risk caps must be positive");
  if (s.open_min_px < 0 || s.open_min_px > 0.95) return fail("PK_PM_OPEN_MIN_PX must be within 0..=0.95");
  if (s.open_min_edge < 0 || s.open_min_edge > 0.2) return fail("PK_PM_OPEN_MIN_EDGE must be within 0..=0.2");
  if (s.pair_half_spread < 0 || s.pair_half_spread > 0.2) return fail("PK_PM_PAIR_HALF_SPREAD must be within 0..=0.2");
  if (s.max_orders_per_s < 1) return fail("PK_PM_MAX_ORDERS_PER_S must be at least 1");
  if (c.order_channels < 2 || c.order_channels > 16) return fail("PKM_ORDER_CHANNELS must be 2..16");
  if (c.signature_type != 3) return fail("pkm trades a deposit wallet only: POLY_SIGNATURE_TYPE must be 3");
  return true;
}

std::string describe(const Config& c) {
  const auto& s = c.strategy;
  std::ostringstream o;
  o << "assets=";
  for (std::size_t i = 0; i < c.assets.size(); ++i) o << (i ? "," : "") << c.assets[i];
  o << " h=" << s.half_spread << " shares=" << s.shares << " max_net=" << s.max_net << " skew_k=" << s.skew_k
    << " pause=" << s.pause_jump << "/" << s.pause_ms << "ms adverse=" << s.adverse_ms << "ms raise=" << s.min_requote_ms
    << "ms open_leg_s=" << s.open_leg_s << " fair_band=" << s.open_min_fair << ".." << s.open_max_fair
    << " pair_margin=" << s.pair_margin << " open_min_px=" << s.open_min_px << " open_min_edge=" << s.open_min_edge
    << " pair_h=" << s.pair_half_spread << " max_open_cost=$" << s.max_open_cost_usd << " daily_loss=$"
    << s.daily_loss_usd << " orders/s=" << s.max_orders_per_s;
  return o.str();
}

}  // namespace pkm
