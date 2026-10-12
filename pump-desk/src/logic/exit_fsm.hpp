#pragma once
// Exit-trigger evaluation, ported from pump-fun/src/exits/engine.ts
// (evaluateExit) and the state updates in src/positions/position.ts.
#include <cstdint>

#include "core/config.hpp"

namespace pd {

enum class ExitTrigger : uint8_t { None, StopLoss, TrailingStop, TimeStop, DeadMoney, TakeProfit0, TakeProfit1, TakeProfit2 };

const char* exit_trigger_name(ExitTrigger t);
/** Protective exits (stop/trail/time) use the protective slippage tiers; take-profits the TP tiers. */
bool is_protective(ExitTrigger t);

struct ExitState {
  double entry_price = 0;
  int64_t opened_ms = 0;
  double high_water = 0;
  bool tp0_done = false, tp1_done = false, trailing_armed = false, high_volatility = false;
  double stop_price = 0;
};

struct ExitDecision {
  ExitTrigger trigger = ExitTrigger::None;
  double sell_fraction = 0;  // of the ORIGINAL position; >= 1 = the full remainder
};

inline double gain_pct(double entry, double price) { return entry <= 0 ? 0 : (price / entry - 1) * 100; }

void exit_init(ExitState& s, double entry_price, int64_t opened_ms, const Config::Exits& cfg);
/** High-water mark and trailing arm (Position.observePrice). */
void exit_observe(ExitState& s, double price, const Config::Exits& cfg);
ExitDecision evaluate_exit(const ExitState& s, double price, int64_t now_ms, const Config::Exits& cfg);
/** After a take-profit fill lands: mark it done and move the stop (Position.applyFill). */
void exit_on_fill(ExitState& s, ExitTrigger t, const Config::Exits& cfg);

}  // namespace pd
