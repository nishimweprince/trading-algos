#include "logic/exit_fsm.hpp"

#include <algorithm>

namespace pd {

const char* exit_trigger_name(ExitTrigger t) {
  switch (t) {
    case ExitTrigger::StopLoss: return "STOP_LOSS";
    case ExitTrigger::TrailingStop: return "TRAILING_STOP";
    case ExitTrigger::TimeStop: return "TIME_STOP";
    case ExitTrigger::DeadMoney: return "DEAD_MONEY";
    case ExitTrigger::TakeProfit0: return "TAKE_PROFIT_0";
    case ExitTrigger::TakeProfit1: return "TAKE_PROFIT_1";
    case ExitTrigger::TakeProfit2: return "TAKE_PROFIT_2";
    default: return "NONE";
  }
}

bool is_protective(ExitTrigger t) {
  return t == ExitTrigger::StopLoss || t == ExitTrigger::TrailingStop || t == ExitTrigger::TimeStop ||
         t == ExitTrigger::DeadMoney;
}

void exit_init(ExitState& s, double entry_price, int64_t opened_ms, const Config::Exits& cfg) {
  s = ExitState{};
  s.entry_price = entry_price;
  s.opened_ms = opened_ms;
  s.high_water = entry_price;
  s.stop_price = entry_price * (1 - cfg.hard_stop_pct / 100);
}

void exit_observe(ExitState& s, double price, const Config::Exits& cfg) {
  if (price > s.high_water) s.high_water = price;
  if (!s.trailing_armed && gain_pct(s.entry_price, price) >= cfg.trailing_arm_pct) s.trailing_armed = true;
}

ExitDecision evaluate_exit(const ExitState& s, double price, int64_t now_ms, const Config::Exits& cfg) {
  double gain = gain_pct(s.entry_price, price);
  double trailing_gap = s.high_volatility ? cfg.trailing_gap_high_vol_pct : cfg.trailing_gap_pct;
  int64_t age_ms = now_ms - s.opened_ms;

  // Risk triggers first (a full exit of the remainder).
  if (price <= s.stop_price) return {ExitTrigger::StopLoss, 1};
  if (s.trailing_armed && price <= s.high_water * (1 - trailing_gap / 100)) return {ExitTrigger::TrailingStop, 1};
  if (!s.tp1_done && age_ms >= int64_t(cfg.time_stop_minutes * 60'000)) return {ExitTrigger::TimeStop, 1};
  if (cfg.dead_money_enabled && !s.tp0_done && !s.tp1_done && age_ms >= int64_t(cfg.dead_money_minutes * 60'000) &&
      gain_pct(s.entry_price, s.high_water) < cfg.dead_money_max_mfe_pct)
    return {ExitTrigger::DeadMoney, 1};

  // Profit triggers.
  if (s.tp1_done && gain >= cfg.tp2_pct) return {ExitTrigger::TakeProfit2, 1};
  if (!s.tp1_done && gain >= cfg.tp1_pct) return {ExitTrigger::TakeProfit1, cfg.tp1_sell_fraction};
  if (cfg.tp0_enabled && !s.tp0_done && !s.tp1_done && gain >= cfg.tp0_pct)
    return {ExitTrigger::TakeProfit0, cfg.tp0_sell_fraction};
  return {};
}

void exit_on_fill(ExitState& s, ExitTrigger t, const Config::Exits& cfg) {
  if (t == ExitTrigger::TakeProfit0) {
    s.tp0_done = true;
    s.stop_price = std::max(s.stop_price, s.entry_price * (1 + cfg.tp0_move_stop_to_pct / 100));
  } else if (t == ExitTrigger::TakeProfit1) {
    s.tp1_done = true;
    s.stop_price = s.entry_price * (1 + cfg.tp1_move_stop_to_pct / 100);
  }
}

}  // namespace pd
