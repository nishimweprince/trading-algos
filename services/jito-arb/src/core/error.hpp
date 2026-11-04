#pragma once
// Errors as values: std::expected<T, Error> everywhere on the hot path (C++23 <expected>).
#include <cstdint>
#include <expected>

namespace arb {

enum class Errc : uint16_t {
  bad_length,
  bad_discriminator,
  bad_owner,
  bad_data,
  overflow,
  div_by_zero,
  zero_amount,
  unsupported,
  not_quotable,
  stale,
  insufficient_liquidity,
  tick_sequence,
  tx_too_large,
  too_many_keys,
  sign_guard,
  disabled,
  io,
  rpc,
};

struct Error {
  Errc code;
  const char* what = "";
};

template <class T>
using Result = std::expected<T, Error>;

inline std::unexpected<Error> fail(Errc c, const char* what = "") { return std::unexpected(Error{c, what}); }

const char* errc_name(Errc c);

}  // namespace arb
