#pragma once
// Exit rules of curve-in, migrate-out. A position lives in one phase at a time:
//   Curve        bought on the bonding curve, not complete yet: stall / abort sell back on the curve
//   AwaitMigrate curve complete: nothing can trade until the migrate creates the pool
//   Pool         migrated: take profit over entry, else sell timeout_slots after the migrate slot
#include <cstdint>

#include "core/config.hpp"

namespace pd {

enum class Phase : uint8_t { Curve, AwaitMigrate, Pool };

enum class ExitTrigger : uint8_t { None, CurveStall, CurveAbort, AwaitTimeout, PoolTakeProfit, PoolTimeout };

const char* exit_trigger_name(ExitTrigger t);
const char* phase_name(Phase p);
/** Protective exits use the protective slippage tiers; the take-profit the TP tiers. */
bool is_protective(ExitTrigger t);

inline double gain_pct(double entry, double price) { return entry <= 0 ? 0 : (price / entry - 1) * 100; }

/** Curve phase: held `held_s` since the buy filled; `remaining_sol` = completion cost now. */
ExitTrigger evaluate_curve_exit(double held_s, double remaining_sol, const Config::Curves& cfg);

/** Pool phase: `gain` over entry at the current mark; `slot` = processed tip. */
ExitTrigger evaluate_pool_exit(double gain, uint64_t slot, uint64_t migrate_slot, const Config::Exits& cfg);

}  // namespace pd
