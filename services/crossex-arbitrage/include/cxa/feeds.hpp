#pragma once

#include "cxa/parser.hpp"
#include "cxa/rest.hpp"
#include "cxa/types.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

namespace cxa {

// Blocking-with-timeout WebSocket on libcurl's CONNECT_ONLY mode. Waits in poll() on
// the socket instead of sleeping, so frame receipt is not quantised.
class WebSocket final {
 public:
  enum class Status : std::uint8_t { frame, timeout, closed, error };

  WebSocket() = default;
  ~WebSocket();
  WebSocket(const WebSocket&) = delete;
  WebSocket& operator=(const WebSocket&) = delete;

  bool connect(const std::string& url, std::string& error);
  Status receive(std::string& frame, int timeout_ms, std::string& error);
  bool send_text(std::string_view text, std::string& error);
  [[nodiscard]] int native_socket() const noexcept;

 private:
  void* curl_{};
  std::array<char, 1 << 16> buffer_{};
};

// False when the linked libcurl cannot speak wss:// (e.g. the macOS system build).
bool websocket_supported() noexcept;

using ResyncFlags = std::array<std::atomic<bool>, kMaxSymbols>;
// Returns false to stop the feed (pipeline failure).
using FrameHandler = std::function<bool(std::uint64_t receive_ns, std::string_view frame)>;
using SnapshotHandler = std::function<bool(BookSnapshot&& snapshot)>;

// Binance USD-M: <s>@depth@0ms on the /public route and <s>@markPrice@1s on the /market
// route (markPrice is not served on /public or the legacy /stream path), polled on one
// thread, with REST depth snapshots at start and on request.
class BinanceFeed final {
 public:
  BinanceFeed(std::vector<std::string> symbols, FrameHandler frames, SnapshotHandler snapshots);
  bool run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error);
  [[nodiscard]] std::string stream_url(std::string_view route, std::string_view stream) const;

 private:
  bool snapshot(std::size_t symbol, std::string& error);
  std::vector<std::string> symbols_;
  FrameHandler frames_;
  SnapshotHandler snapshots_;
  HttpClient http_;  // keep-alive for snapshot requests
};

// Bybit V5 public linear: orderbook.50.<S> + tickers.<S>. Resync re-subscribes the
// book topic, which makes Bybit push a fresh snapshot.
class BybitFeed final {
 public:
  BybitFeed(std::vector<std::string> symbols, FrameHandler frames);
  bool run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error);

 private:
  std::vector<std::string> symbols_;
  FrameHandler frames_;
};

// Hyperliquid public WS: `bbo` + `activeAssetCtx` per coin. `l2Book` is not used: the
// public feed pushes it about every 5.4 s (measured 2026-10-21) against ~100 ms for bbo.
// Resync re-subscribes bbo, which pushes the current top of book.
class HyperliquidFeed final {
 public:
  HyperliquidFeed(std::vector<std::string> coins, FrameHandler frames);
  bool run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error);

 private:
  std::vector<std::string> coins_;
  FrameHandler frames_;
};

}  // namespace cxa
