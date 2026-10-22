#include "hla/config.hpp"

#include <cstdlib>
#include <stdexcept>

namespace hla {
namespace {

std::string_view trim(std::string_view text) {
  while (!text.empty() && (text.front() == ' ' || text.front() == '\t')) text.remove_prefix(1);
  while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\n' || text.back() == '\r'))
    text.remove_suffix(1);
  return text;
}

std::vector<std::string_view> split(std::string_view text, char separator) {
  std::vector<std::string_view> parts;
  while (!text.empty()) {
    const auto pos = text.find(separator);
    const auto part = trim(text.substr(0, pos));
    if (!part.empty()) parts.push_back(part);
    if (pos == std::string_view::npos) break;
    text.remove_prefix(pos + 1);
  }
  return parts;
}

const char* raw(const char* name) { return std::getenv(name); }

std::string text(const char* name, std::string fallback) {
  const auto* value = raw(name);
  return value == nullptr || *value == '\0' ? fallback : std::string(value);
}

template <typename T>
T integer(const char* name, T fallback) {
  const auto* value = raw(name);
  if (value == nullptr || *value == '\0') return fallback;
  try {
    std::size_t used = 0;
    const auto parsed = std::stoll(value, &used);
    if (used != std::string_view(value).size()) throw std::invalid_argument("trailing");
    return static_cast<T>(parsed);
  } catch (...) {
    throw std::invalid_argument(std::string(name) + " is not an integer");
  }
}

double number(const char* name, double fallback) {
  const auto* value = raw(name);
  if (value == nullptr || *value == '\0') return fallback;
  try {
    return std::stod(value);
  } catch (...) {
    throw std::invalid_argument(std::string(name) + " is not a number");
  }
}

int minutes(std::string_view hhmm) {
  if (hhmm.size() != 5 || hhmm[2] != ':') throw std::invalid_argument("window time must be HH:MM");
  const int h = std::stoi(std::string(hhmm.substr(0, 2)));
  const int m = std::stoi(std::string(hhmm.substr(3, 2)));
  if (h < 0 || h > 24 || m < 0 || m > 59) throw std::invalid_argument("window time out of range");
  return h * 60 + m;
}

}  // namespace

std::vector<PairConfig> parse_pairs(std::string_view value) {
  std::vector<PairConfig> pairs;
  for (const auto item : split(value, ',')) {
    const auto eq = item.find('=');
    if (eq == std::string_view::npos) throw std::invalid_argument("pair needs coin=MT5SYMBOL: " + std::string(item));
    PairConfig pair;
    pair.coin = std::string(trim(item.substr(0, eq)));
    auto rest = trim(item.substr(eq + 1));
    const auto star = rest.find('*');
    pair.mt5_symbol = std::string(trim(rest.substr(0, star)));
    if (star != std::string_view::npos) pair.ratio = std::stod(std::string(rest.substr(star + 1)));
    if (pair.coin.empty() || pair.mt5_symbol.empty() || !(pair.ratio > 0))
      throw std::invalid_argument("bad pair: " + std::string(item));
    for (const auto& existing : pairs)
      if (existing.coin == pair.coin || existing.mt5_symbol == pair.mt5_symbol)
        throw std::invalid_argument("duplicate pair: " + std::string(item));
    pairs.push_back(std::move(pair));
  }
  if (pairs.empty()) throw std::invalid_argument("HLA_PAIRS is empty");
  if (pairs.size() > 32) throw std::invalid_argument("HLA_PAIRS exceeds 32 pairs");
  return pairs;
}

std::vector<std::pair<int, int>> parse_windows(std::string_view value) {
  std::vector<std::pair<int, int>> windows;
  for (const auto item : split(value, ',')) {
    const auto dash = item.find('-');
    if (dash == std::string_view::npos) throw std::invalid_argument("window needs HH:MM-HH:MM");
    const auto start = minutes(trim(item.substr(0, dash)));
    const auto end = minutes(trim(item.substr(dash + 1)));
    if (end <= start) throw std::invalid_argument("window end must follow start");
    windows.emplace_back(start, end);
  }
  return windows;
}

Config load_config(bool live) {
  Config c;
  c.live = live;
  c.pairs = parse_pairs(text("HLA_PAIRS", "xyz:JPY=USDJPY,xyz:EUR=EURUSD,xyz:SILVER=XAGUSD,xyz:SP500=US500,"
                                           "xyz:XYZ100=NAS100,xyz:CL=USOIL,BTC=BTCUSD,ETH=ETHUSD"));
  c.hl_account = text("HLA_HL_ACCOUNT", "");
  c.agent_key_file = text("HLA_AGENT_KEY_FILE", "");
  for (const auto login : split(text("HLA_MT5_LOGINS", ""), ',')) c.mt5_logins.push_back(std::stoll(std::string(login)));
  c.bridge_name = text("HLA_BRIDGE_NAME", c.bridge_name);
  c.kill_file = text("HLA_KILL_FILE", c.kill_file);
  c.journal = text("HLA_JOURNAL", c.journal);
  c.status_file = text("HLA_STATUS_FILE", c.status_file);
  c.status_port = static_cast<std::uint16_t>(integer("HLA_STATUS_PORT", c.status_port));

  c.quote_lots = integer("HLA_QUOTE_LOTS", c.quote_lots);
  c.max_inventory_lots = integer("HLA_MAX_INVENTORY_LOTS", c.max_inventory_lots);
  c.edge_ppm = integer("HLA_EDGE_PPM", c.edge_ppm);
  c.adverse_ppm = integer("HLA_ADVERSE_PPM", c.adverse_ppm);
  c.mt5_commission_ppm = integer("HLA_MT5_COMMISSION_PPM", c.mt5_commission_ppm);
  c.skew_ppm = integer("HLA_SKEW_PPM", c.skew_ppm);
  c.hl_maker_ppm = integer("HLA_HL_MAKER_PPM", c.hl_maker_ppm);
  c.requote_ppm = integer("HLA_REQUOTE_PPM", c.requote_ppm);
  c.min_requote_ms = integer("HLA_MIN_REQUOTE_MS", c.min_requote_ms);
  c.basis_tau_s = number("HLA_BASIS_TAU_S", c.basis_tau_s);
  c.basis_warmup_s = integer("HLA_BASIS_WARMUP_S", c.basis_warmup_s);
  c.max_fair_gap_ppm = integer("HLA_MAX_FAIR_GAP_PPM", c.max_fair_gap_ppm);
  c.max_basis_ppm = integer("HLA_MAX_BASIS_PPM", c.max_basis_ppm);
  c.max_mark_oracle_ppm = integer("HLA_MAX_MARK_ORACLE_PPM", c.max_mark_oracle_ppm);
  c.max_mt5_spread_ppm = integer("HLA_MAX_MT5_SPREAD_PPM", c.max_mt5_spread_ppm);

  c.mt5_tick_stale_ms = integer("HLA_MT5_TICK_STALE_MS", c.mt5_tick_stale_ms);
  c.mt5_heartbeat_stale_ms = integer("HLA_MT5_HEARTBEAT_STALE_MS", c.mt5_heartbeat_stale_ms);
  c.hl_stale_ms = integer("HLA_HL_STALE_MS", c.hl_stale_ms);
  c.aux_stale_ms = integer("HLA_AUX_STALE_MS", c.aux_stale_ms);
  if (const auto* windows = raw("HLA_ROLLOVER_UTC")) c.rollover_windows = parse_windows(windows);

  c.hedge_timeout_ms = integer("HLA_HEDGE_TIMEOUT_MS", c.hedge_timeout_ms);
  c.hedge_deviation_points = integer("HLA_HEDGE_DEVIATION_POINTS", c.hedge_deviation_points);
  c.mt5_order_max_age_ms = integer("HLA_MT5_ORDER_MAX_AGE_MS", c.mt5_order_max_age_ms);
  c.flatten_slippage_ppm = integer("HLA_FLATTEN_SLIPPAGE_PPM", c.flatten_slippage_ppm);
  c.max_daily_loss_usd = number("HLA_MAX_DAILY_LOSS_USD", c.max_daily_loss_usd);
  c.max_gross_notional_usd = number("HLA_MAX_GROSS_NOTIONAL_USD", c.max_gross_notional_usd);
  c.hl_min_margin_usd = number("HLA_HL_MIN_MARGIN_USD", c.hl_min_margin_usd);
  c.mt5_min_margin_usd = number("HLA_MT5_MIN_MARGIN_USD", c.mt5_min_margin_usd);
  c.hl_leverage = integer("HLA_HL_LEVERAGE", c.hl_leverage);
  c.deadman_s = integer("HLA_DEADMAN_S", c.deadman_s);
  c.expires_after_ms = integer("HLA_EXPIRES_AFTER_MS", c.expires_after_ms);
  c.min_request_budget = integer("HLA_MIN_REQUEST_BUDGET", c.min_request_budget);
  c.adopt_positions = integer("HLA_ADOPT_POSITIONS", 0) != 0;

  c.decision_core = integer("HLA_DECISION_CORE", c.decision_core);
  c.gateway_core = integer("HLA_GATEWAY_CORE", c.gateway_core);
  c.feed_core = integer("HLA_FEED_CORE", c.feed_core);

  if (c.quote_lots <= 0 || c.max_inventory_lots < c.quote_lots)
    throw std::invalid_argument("need 0 < HLA_QUOTE_LOTS <= HLA_MAX_INVENTORY_LOTS");
  if (c.hl_leverage == 0 || c.hl_leverage > 50) throw std::invalid_argument("HLA_HL_LEVERAGE must be 1..50");
  if (c.deadman_s != 0 && c.deadman_s < 10) throw std::invalid_argument("HLA_DEADMAN_S must be 0 or >= 10");
  if (c.hedge_timeout_ms >= c.mt5_order_max_age_ms)
    throw std::invalid_argument("HLA_HEDGE_TIMEOUT_MS must be below HLA_MT5_ORDER_MAX_AGE_MS");
  return c;
}

}  // namespace hla
