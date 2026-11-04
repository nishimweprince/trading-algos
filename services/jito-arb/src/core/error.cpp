#include "core/error.hpp"

namespace arb {

const char* errc_name(Errc c) {
  switch (c) {
    case Errc::bad_length: return "bad_length";
    case Errc::bad_discriminator: return "bad_discriminator";
    case Errc::bad_owner: return "bad_owner";
    case Errc::bad_data: return "bad_data";
    case Errc::overflow: return "overflow";
    case Errc::div_by_zero: return "div_by_zero";
    case Errc::zero_amount: return "zero_amount";
    case Errc::unsupported: return "unsupported";
    case Errc::not_quotable: return "not_quotable";
    case Errc::stale: return "stale";
    case Errc::insufficient_liquidity: return "insufficient_liquidity";
    case Errc::tick_sequence: return "tick_sequence";
    case Errc::tx_too_large: return "tx_too_large";
    case Errc::too_many_keys: return "too_many_keys";
    case Errc::sign_guard: return "sign_guard";
    case Errc::disabled: return "disabled";
    case Errc::io: return "io";
    case Errc::rpc: return "rpc";
  }
  return "?";
}

}  // namespace arb
