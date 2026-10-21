#pragma once

#include "hla/net.hpp"
#include "hla/types.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

using ResyncFlags = std::array<std::atomic<bool>, kMaxSymbols>;
// Returns false to stop the feed (pipeline failure).
using FrameHandler = std::function<bool(std::uint64_t receive_ns, std::string_view frame)>;

std::string hl_subscription(std::string_view method, std::string_view type, std::string_view coin);

// Hyperliquid public WS: `bbo` + `activeAssetCtx` per coin (validator or HIP-3 names).
// `l2Book` is not used: the public feed pushes it about every 5.4 s. Resync
// re-subscribes bbo, which pushes the current top of book.
class HyperliquidFeed final {
 public:
  HyperliquidFeed(std::vector<std::string> coins, FrameHandler frames);
  bool run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error);

 private:
  std::vector<std::string> coins_;
  FrameHandler frames_;
};

}  // namespace hla
