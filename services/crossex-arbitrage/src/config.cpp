#include "cxa/config.hpp"

#include <charconv>
#include <cstdlib>
#include <stdexcept>
#include <string_view>

namespace cxa {
namespace {

std::int64_t integer(const char* name, std::int64_t fallback) {
  const auto* raw = std::getenv(name);
  if (raw == nullptr) return fallback;
  const std::string_view text(raw);
  std::int64_t value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size())
    throw std::invalid_argument(std::string(name) + " must be an integer");
  return value;
}

std::uint64_t unsigned_integer(const char* name, std::uint64_t fallback) {
  const auto value = integer(name, static_cast<std::int64_t>(fallback));
  if (value < 0) throw std::invalid_argument(std::string(name) + " must be non-negative");
  return static_cast<std::uint64_t>(value);
}

std::int64_t usd(const char* name, std::int64_t fallback_raw) {
  const auto* raw = std::getenv(name);
  return raw == nullptr ? fallback_raw : Decimal::parse(raw).raw;
}

}  // namespace

std::string_view latency_mode_name(LatencyMode mode) noexcept {
  switch (mode) {
    case LatencyMode::fixed: return "fixed";
    case LatencyMode::sampled: return "sampled";
    case LatencyMode::pessimistic: return "pessimistic";
  }
  return "unknown";
}

std::string_view exec_mode_name(ExecMode mode) noexcept {
  switch (mode) {
    case ExecMode::taker_taker: return "taker_taker";
    case ExecMode::maker_taker: return "maker_taker";
  }
  return "unknown";
}

Venue parse_peer(std::string_view name) {
  if (name == "bybit") return Venue::bybit;
  if (name == "hyperliquid") return Venue::hyperliquid;
  throw std::invalid_argument("CXA_PEER must be bybit or hyperliquid");
}

RuntimeConfig load_runtime_config() {
  RuntimeConfig config;
  if (const auto* basket = std::getenv("CXA_BASKET")) config.basket = basket;
  auto& s = config.strategy;
  s.trade_notional_raw = usd("CXA_TRADE_NOTIONAL", s.trade_notional_raw);
  s.basis_halflife_ms = unsigned_integer("CXA_BASIS_HALFLIFE_MS", s.basis_halflife_ms);
  s.basis_warmup_ms = unsigned_integer("CXA_BASIS_WARMUP_MS", s.basis_warmup_ms);
  s.min_net_ppm = integer("CXA_MIN_NET_PPM", s.min_net_ppm);
  s.safety_ppm = integer("CXA_SAFETY_PPM", s.safety_ppm);
  s.exit_band_ppm = integer("CXA_EXIT_BAND_PPM", s.exit_band_ppm);
  s.expected_hold_ms = unsigned_integer("CXA_EXPECTED_HOLD_MS", s.expected_hold_ms);
  s.max_hold_ms = unsigned_integer("CXA_MAX_HOLD_MS", s.max_hold_ms);
  s.funding_guard_ms = unsigned_integer("CXA_FUNDING_GUARD_MS", s.funding_guard_ms);
  s.funding_tolerance_ppm = integer("CXA_FUNDING_TOLERANCE_PPM", s.funding_tolerance_ppm);
  s.pair_stop_ppm = integer("CXA_PAIR_STOP_PPM", s.pair_stop_ppm);
  s.close_slippage_ppm = integer("CXA_CLOSE_SLIPPAGE_PPM", s.close_slippage_ppm);
  s.hedge_slippage_ppm = integer("CXA_HEDGE_SLIPPAGE_PPM", s.hedge_slippage_ppm);
  s.cooldown_ms = unsigned_integer("CXA_COOLDOWN_MS", s.cooldown_ms);
  s.max_mid_divergence_ppm = integer("CXA_MAX_MID_DIVERGENCE_PPM", s.max_mid_divergence_ppm);
  s.maker_timeout_ms = unsigned_integer("CXA_MAKER_TIMEOUT_MS", s.maker_timeout_ms);
  if (const auto* mode = std::getenv("CXA_EXEC_MODE")) {
    const std::string_view text(mode);
    if (text == "taker_taker") s.exec_mode = ExecMode::taker_taker;
    else if (text == "maker_taker") s.exec_mode = ExecMode::maker_taker;
    else throw std::invalid_argument("CXA_EXEC_MODE must be taker_taker or maker_taker");
  }
  auto& r = config.risk;
  r.max_open_pairs = static_cast<std::uint32_t>(unsigned_integer("CXA_MAX_OPEN_PAIRS", r.max_open_pairs));
  r.max_pair_notional_raw = usd("CXA_MAX_PAIR_NOTIONAL", r.max_pair_notional_raw);
  r.daily_loss_raw = usd("CXA_DAILY_LOSS", r.daily_loss_raw);
  r.max_feed_age_ns = unsigned_integer("CXA_MAX_FEED_AGE_NS", r.max_feed_age_ns);
  r.max_book_age_ns = unsigned_integer("CXA_MAX_BOOK_AGE_NS", r.max_book_age_ns);
  auto& h = config.shadow;
  if (const auto* mode = std::getenv("CXA_LATENCY_MODE")) {
    const std::string_view text(mode);
    if (text == "fixed") h.latency_mode = LatencyMode::fixed;
    else if (text == "sampled") h.latency_mode = LatencyMode::sampled;
    else if (text == "pessimistic") h.latency_mode = LatencyMode::pessimistic;
    else throw std::invalid_argument("CXA_LATENCY_MODE must be fixed, sampled, or pessimistic");
  }
  if (const auto* peer = std::getenv("CXA_PEER")) h.peer = parse_peer(peer);
  h.fixed_one_way_ns[0] = unsigned_integer("CXA_BINANCE_ONE_WAY_NS", h.fixed_one_way_ns[0]);
  h.engine_penalty_ns = unsigned_integer("CXA_ENGINE_PENALTY_NS", h.engine_penalty_ns);
  h.available_ppm = integer("CXA_AVAILABLE_PPM", h.available_ppm);
  h.seed = unsigned_integer("CXA_SEED", h.seed);
  h.fallback_taker_ppm[0] = integer("CXA_BINANCE_TAKER_PPM", h.fallback_taker_ppm[0]);
  h.fallback_maker_ppm[0] = integer("CXA_BINANCE_MAKER_PPM", h.fallback_maker_ppm[0]);
  if (h.peer == Venue::hyperliquid) {
    // Base-tier perp fees (0.045% / 0.015%). An HL order only takes effect when its
    // block is built, so every leg pays an inclusion delay on top of RTT/2.
    h.fixed_one_way_ns[1] = unsigned_integer("CXA_HL_ONE_WAY_NS", 5'000'000);
    h.venue_penalty_ns[1] = unsigned_integer("CXA_HL_INCLUSION_NS", 200'000'000);
    h.fallback_taker_ppm[1] = integer("CXA_HL_TAKER_PPM", 450);
    h.fallback_maker_ppm[1] = integer("CXA_HL_MAKER_PPM", 150);
  } else {
    h.fixed_one_way_ns[1] = unsigned_integer("CXA_BYBIT_ONE_WAY_NS", h.fixed_one_way_ns[1]);
    h.fallback_taker_ppm[1] = integer("CXA_BYBIT_TAKER_PPM", h.fallback_taker_ppm[1]);
    h.fallback_maker_ppm[1] = integer("CXA_BYBIT_MAKER_PPM", h.fallback_maker_ppm[1]);
  }
  if (s.trade_notional_raw <= 0 || s.trade_notional_raw > r.max_pair_notional_raw)
    throw std::invalid_argument("CXA_TRADE_NOTIONAL must be positive and within CXA_MAX_PAIR_NOTIONAL");
  if (h.available_ppm <= 0 || h.available_ppm > kPpm)
    throw std::invalid_argument("CXA_AVAILABLE_PPM must be in (0, 1000000]");
  if (s.basis_halflife_ms == 0) throw std::invalid_argument("CXA_BASIS_HALFLIFE_MS must be positive");
  if (s.exec_mode == ExecMode::maker_taker && s.maker_timeout_ms == 0)
    throw std::invalid_argument("CXA_MAKER_TIMEOUT_MS must be positive for maker_taker");
  return config;
}

}  // namespace cxa
