#include "sol/constants.hpp"

#include "sol/crypto.hpp"

namespace pd {

namespace {
Derived g_derived;
}

const Derived& derived() { return g_derived; }

void init_derived() {
  g_derived.global_config = pda({std::string_view("global_config")}, prog::PUMP_SWAP);
  g_derived.event_authority = pda({std::string_view("__event_authority")}, prog::PUMP_SWAP);
  g_derived.global_volume_acc = pda({std::string_view("global_volume_accumulator")}, prog::PUMP_SWAP);
  g_derived.fee_config = pda({std::string_view("fee_config"), prog::PUMP_SWAP}, prog::PUMP_FEE);
  g_derived.pump_global = pda({std::string_view("global")}, prog::PUMP_FUN);
  g_derived.pump_event_authority = pda({std::string_view("__event_authority")}, prog::PUMP_FUN);
  g_derived.pump_global_volume_acc = pda({std::string_view("global_volume_accumulator")}, prog::PUMP_FUN);
  g_derived.pump_fee_config = pda({std::string_view("fee_config"), prog::PUMP_FUN}, prog::PUMP_FEE);
}

}  // namespace pd
