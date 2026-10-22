#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hla {

// One quoted pair: the HL coin and the MT5 symbol that prices and hedges it, with
// hl_price ~= ratio * mt5_price (1 unless the two contracts quote different scales).
struct PairConfig final {
  std::string coin;
  std::string mt5_symbol;
  double ratio{1.0};
};

// "xyz:JPY=USDJPY, xyz:XYZ100=NAS100*1.0, BTC=BTCUSD". Throws std::invalid_argument.
std::vector<PairConfig> parse_pairs(std::string_view text);
// "20:55-21:10,21:55-22:10" -> [start, end) minutes of the UTC day. Throws on bad input.
std::vector<std::pair<int, int>> parse_windows(std::string_view text);

// Every field maps to HLA_<UPPER_NAME>; see config.example.env for meanings.
struct Config final {
  bool live{};
  std::vector<PairConfig> pairs;
  std::string hl_account;          // master address (fills, positions, margin)
  std::string agent_key_file;      // file holding the API/agent wallet key; never the master key
  std::vector<std::int64_t> mt5_logins;  // allow-list; empty refuses live
  std::string bridge_name{"Local\\hla_bridge_v1"};
  std::string kill_file{"hlarb.kill"};
  std::string journal{"hlarb-%Y%m%dT%H%M%SZ.jsonl"};
  std::string status_file{"hlarb-status.json"};
  std::uint16_t status_port{0};  // 0: disabled; e.g. 8071 for Cloudflare tunnel

  // Quoting.
  std::int64_t quote_lots{1};          // MT5 lot steps per quote
  std::int64_t max_inventory_lots{2};  // MT5 lot steps of unhedged-equivalent HL inventory
  std::int64_t edge_ppm{100};
  std::int64_t min_edge_ppm{10};  // joining the HL touch never quotes inside cost + this
  std::int64_t adverse_ppm{100};
  std::int64_t mt5_commission_ppm{35};  // per hedge side
  std::int64_t skew_ppm{50};            // fair shift per lot step of inventory
  std::int64_t hl_maker_ppm{-1};        // -1: from userFees, x2 for HIP-3, x0.1 in growth mode
  std::int64_t requote_ppm{10};
  std::uint64_t min_requote_ms{250};
  double basis_tau_s{1800};
  std::uint64_t basis_warmup_s{300};
  std::int64_t max_fair_gap_ppm{3'000};
  std::int64_t max_basis_ppm{20'000};  // |HL - ratio*MT5| beyond this means a bad pair mapping
  std::int64_t max_mark_oracle_ppm{5'000};
  std::int64_t max_mt5_spread_ppm{1'000};

  // Staleness.
  std::uint64_t mt5_tick_stale_ms{30'000};
  std::uint64_t mt5_heartbeat_stale_ms{1'000};
  std::uint64_t hl_stale_ms{5'000};
  std::uint64_t aux_stale_ms{30'000};
  std::vector<std::pair<int, int>> rollover_windows{{20 * 60 + 55, 21 * 60 + 10}, {21 * 60 + 55, 22 * 60 + 10}};

  // Hedging and risk.
  std::uint64_t hedge_timeout_ms{1'500};
  std::int32_t hedge_deviation_points{30};
  std::uint64_t mt5_order_max_age_ms{2'000};
  std::int64_t flatten_slippage_ppm{5'000};
  double max_daily_loss_usd{50};
  double max_gross_notional_usd{5'000};
  std::uint64_t margin_park_s{600};  // an "Insufficient margin" reject parks the pair this long
  double hl_min_margin_usd{100};
  double mt5_min_margin_usd{100};
  std::uint32_t hl_leverage{3};
  std::uint32_t deadman_s{60};
  std::uint64_t expires_after_ms{0};
  std::int64_t min_request_budget{500};
  bool adopt_positions{false};

  // Threads (-1: not pinned).
  int decision_core{-1};
  int gateway_core{-1};
  int feed_core{-1};
};

Config load_config(bool live);

}  // namespace hla
