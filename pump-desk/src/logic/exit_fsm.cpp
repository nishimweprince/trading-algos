#include "logic/exit_fsm.hpp"

namespace pd {

const char* exit_trigger_name(ExitTrigger t) {
  switch (t) {
    case ExitTrigger::CurveStall: return "CURVE_STALL";
    case ExitTrigger::CurveAbort: return "CURVE_ABORT";
    case ExitTrigger::AwaitTimeout: return "AWAIT_TIMEOUT";
    case ExitTrigger::PoolTakeProfit: return "POOL_TP";
    case ExitTrigger::PoolTimeout: return "POOL_TIMEOUT";
    default: return "NONE";
  }
}

const char* phase_name(Phase p) {
  switch (p) {
    case Phase::Curve: return "curve";
    case Phase::AwaitMigrate: return "await_migrate";
    default: return "pool";
  }
}

bool is_protective(ExitTrigger t) { return t != ExitTrigger::PoolTakeProfit; }

ExitTrigger evaluate_curve_exit(double held_s, double remaining_sol, const Config::Curves& cfg) {
  if (cfg.abort_remaining_sol > 0 && remaining_sol > cfg.abort_remaining_sol) return ExitTrigger::CurveAbort;
  if (held_s >= cfg.max_hold_s) return ExitTrigger::CurveStall;
  return ExitTrigger::None;
}

ExitTrigger evaluate_pool_exit(double gain, uint64_t slot, uint64_t migrate_slot, const Config::Exits& cfg) {
  if (cfg.tp_pct > 0 && gain >= cfg.tp_pct) return ExitTrigger::PoolTakeProfit;
  if (migrate_slot && slot >= migrate_slot + uint64_t(cfg.timeout_slots)) return ExitTrigger::PoolTimeout;
  return ExitTrigger::None;
}

}  // namespace pd
