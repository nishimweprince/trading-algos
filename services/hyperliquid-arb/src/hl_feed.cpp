#include "hla/hl_feed.hpp"

#include <chrono>

namespace hla {
namespace {

constexpr auto kResyncInterval = std::chrono::seconds(1);
// Hyperliquid closes connections idle for 60 s.
constexpr auto kPing = std::chrono::seconds(30);

}  // namespace

std::string hl_subscription(std::string_view method, std::string_view type, std::string_view coin) {
  return R"({"method":")" + std::string(method) + R"(","subscription":{"type":")" + std::string(type) +
         R"(","coin":")" + std::string(coin) + "\"}}";
}

HyperliquidFeed::HyperliquidFeed(std::vector<std::string> coins, FrameHandler frames)
    : coins_(std::move(coins)), frames_(std::move(frames)) {}

bool HyperliquidFeed::run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error) {
  WebSocket socket;
  if (!socket.connect(std::string(kHyperliquidWs), error)) return false;
  for (const auto& coin : coins_) {
    if (!socket.send_text(hl_subscription("subscribe", "bbo", coin), error) ||
        !socket.send_text(hl_subscription("subscribe", "activeAssetCtx", coin), error))
      return false;
  }
  auto last_ping = std::chrono::steady_clock::now();
  std::array<std::chrono::steady_clock::time_point, kMaxSymbols> last_resync{};
  std::string frame;
  while (!stop.load(std::memory_order_relaxed)) {
    const auto status = socket.receive(frame, 100, error);
    if (status == WebSocket::Status::frame) {
      if (!frames_(wall_ns(), frame)) {
        if (error.empty()) error = "hyperliquid frame handler stopped the feed";
        return false;
      }
    } else if (status != WebSocket::Status::timeout) {
      return false;
    }
    const auto now = std::chrono::steady_clock::now();
    if (now - last_ping >= kPing) {
      if (!socket.send_text(R"({"method":"ping"})", error)) return false;
      last_ping = now;
    }
    for (std::size_t i = 0; i < coins_.size(); ++i) {
      if (!resync[i].load(std::memory_order_acquire) || now - last_resync[i] < kResyncInterval) continue;
      resync[i].store(false, std::memory_order_release);
      last_resync[i] = now;
      if (!socket.send_text(hl_subscription("unsubscribe", "bbo", coins_[i]), error) ||
          !socket.send_text(hl_subscription("subscribe", "bbo", coins_[i]), error))
        return false;
    }
  }
  return true;
}

}  // namespace hla
