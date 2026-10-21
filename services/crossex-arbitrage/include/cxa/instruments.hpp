#pragma once

#include "cxa/types.hpp"

#include <array>
#include <string>
#include <string_view>
#include <vector>

namespace cxa {

// One basket entry: the same USDT perpetual on both venues. Contracts must have the
// same unit (no multiplier translation); a startup mid check rejects mismatches.
struct Instrument final {
  std::string name;
  std::array<std::string, kVenues> symbol{};
  std::array<Price, kVenues> tick{};
  std::array<Quantity, kVenues> step{};
  std::array<Quantity, kVenues> min_qty{};
  std::array<Decimal, kVenues> min_notional{};
  Quantity common_step{};
  Quantity common_min_qty{};
};

// "BTCUSDT,ETHUSDT,SHIB:1000SHIBUSDT:SHIB1000USDT" -> name[:binance:peer]. A bare name
// is the Binance symbol; the peer symbol is the same on Bybit and derived on
// Hyperliquid (BTCUSDT -> BTC, 1000PEPEUSDT -> kPEPE).
std::vector<Instrument> parse_basket(std::string_view text, Venue peer = Venue::bybit);
std::string hyperliquid_coin(std::string_view binance_symbol);

bool parse_binance_instrument(std::string_view exchange_info, Instrument& instrument, std::string& error);
bool parse_bybit_instrument(std::string_view instruments_info, Instrument& instrument, std::string& error);
// From the `meta` universe and `allMids`. Hyperliquid prices allow 5 significant figures
// and at most 6 - szDecimals decimals, so the tick is derived from the current mid.
bool parse_hyperliquid_instrument(std::string_view meta, std::string_view mids, Instrument& instrument,
                                  std::string& error);
Price hyperliquid_tick(Price mid, int size_decimals) noexcept;
// Derives the coarser common lot step and minimum; fails when steps are incompatible.
bool finalize_instrument(Instrument& instrument, std::string& error);

}  // namespace cxa
