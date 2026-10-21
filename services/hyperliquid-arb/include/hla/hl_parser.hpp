#pragma once

#include "hla/types.hpp"

#include <cstdint>
#include <span>
#include <string>
#include <string_view>

namespace hla {

enum class HlFrameKind : std::uint8_t { ignored, bbo, ctx, error };

// `bbo`: top of book, pushed when it changes (~100 ms cadence measured from Tokyo).
struct HlBbo final {
  std::uint16_t symbol{};
  std::uint64_t receive_ns{};
  std::uint64_t event_ms{};
  Level bid{};
  Level ask{};
  bool has_bid{};
  bool has_ask{};
};

// `activeAssetCtx`: hourly funding rate, mark and oracle. Ctx frames carry no timestamp.
struct HlCtx final {
  std::uint16_t symbol{};
  std::uint64_t receive_ns{};
  Decimal funding{};
  Price mark{};
  Price oracle{};
};

struct HlFrame final {
  HlFrameKind kind{HlFrameKind::ignored};
  HlBbo bbo{};
  HlCtx ctx{};
};

// `coins` are in basket order. Returns false on a server error frame or a malformed
// bbo/ctx frame; subscription acks, pongs and unknown coins are `ignored`.
bool parse_hl_frame(std::string_view json, std::uint64_t receive_ns, std::span<const std::string> coins,
                    HlFrame& output) noexcept;

int symbol_index(std::span<const std::string> symbols, std::string_view name) noexcept;

}  // namespace hla
