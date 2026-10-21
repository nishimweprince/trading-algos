#pragma once
// Exit-trigger evaluation, ported from pump-fun/src/exits/engine.ts
// (evaluateExit) and the state updates in src/positions/position.ts.
// LiquidityDump / Stall are pump-desk extensions for post-migrate vault dumps
// and stalled climbers.
#include <cstdint>

#include "core/config.hpp"

namespace pd {

enum class ExitTrigger : uint8_t {
  None,
  StopLoss,
  TrailingStop,
  LiquidityDump,
  Stall,
  TimeStop,
  DeadMoney,
  TakeProfit0,
  TakeProfit1,
  TakeProfit2
};

const char* exit_trigger_name(ExitTrigger t);
/** Protective exits use the protective slippage tiers; take-profits the TP tiers. */
bool is_protective(ExitTrigger t);

struct ExitState {
  double entry_price = 0;
  int64_t opened_ms = 0;
  double high_water = 0;
  int64_t high_water_ms = 0;     // wall ms of the last new high (for STALL)
  uint64_t peak_quote_res = 0;   // peak pool quote vault since open
  uint64_t last_quote_res = 0;   // latest quote vault (for LIQUIDITY_DUMP)
  bool tp0_done = false, tp1_done = false, trailing_armed = false, high_volatility = false;
  double stop_price = 0;
};

struct ExitDecision {
  ExitTrigger trigger = ExitTrigger::None;
  double sell_fraction = 0;  // of the ORIGINAL position; >= 1 = the full remainder
};

inline double gain_pct(double entry, double price) { return entry <= 0 ? 0 : (price / entry - 1) * 100; }

void exit_init(ExitState& s, double entry_price, int64_t opened_ms, const Config::Exits& cfg);
/** High-water mark, trailing arm, and stall clock. */
void exit_observe(ExitState& s, double price, int64_t now_ms, const Config::Exits& cfg);
/** Track peak and current quote vault for LiquidityDump. */
void exit_observe_quote(ExitState& s, uint64_t quote_res);
ExitDecision evaluate_exit(const ExitState& s, double price, int64_t now_ms, const Config::Exits& cfg);
/** After a take-profit fill lands: mark it done and move the stop. */
void exit_on_fill(ExitState& s, ExitTrigger t, const Config::Exits& cfg);

}  // namespace pd
