#include "cxa/feeds.hpp"

#include "cxa/rest.hpp"

#include <curl/curl.h>
#include <poll.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <thread>

namespace cxa {
namespace {

constexpr auto kResyncInterval = std::chrono::seconds(1);
constexpr auto kBybitPing = std::chrono::seconds(20);
constexpr std::size_t kBybitTopicsPerRequest = 10;
// Hyperliquid closes connections idle for 60 s.
constexpr auto kHyperliquidPing = std::chrono::seconds(30);

std::string hyperliquid_subscription(std::string_view method, std::string_view type, std::string_view coin) {
  return R"({"method":")" + std::string(method) + R"(","subscription":{"type":")" + std::string(type) +
         R"(","coin":")" + std::string(coin) + "\"}}";
}

std::string lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return text;
}

}  // namespace

bool websocket_supported() noexcept {
  const auto* info = curl_version_info(CURLVERSION_NOW);
  for (auto protocol = info->protocols; protocol != nullptr && *protocol != nullptr; ++protocol)
    if (std::string_view(*protocol) == "wss") return true;
  return false;
}

WebSocket::~WebSocket() {
  if (curl_ != nullptr) curl_easy_cleanup(static_cast<CURL*>(curl_));
}

bool WebSocket::connect(const std::string& url, std::string& error) {
  auto* curl = curl_easy_init();
  if (curl == nullptr) { error = "curl initialization failed"; return false; }
  curl_ = curl;
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "trading-algos-crossex/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
  curl_easy_setopt(curl, CURLOPT_TCP_NODELAY, 1L);
  const auto code = curl_easy_perform(curl);
  if (code != CURLE_OK) {
    error = "websocket connect failed: " + std::string(curl_easy_strerror(code));
    return false;
  }
  return true;
}

WebSocket::Status WebSocket::receive(std::string& frame, int timeout_ms, std::string& error) {
  auto* curl = static_cast<CURL*>(curl_);
  frame.clear();
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  for (;;) {
    std::size_t received = 0;
    const curl_ws_frame* meta = nullptr;
    const auto code = curl_ws_recv(curl, buffer_.data(), buffer_.size(), &received, &meta);
    if (code == CURLE_AGAIN) {
      const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
          deadline - std::chrono::steady_clock::now()).count();
      if (remaining <= 0 && frame.empty()) return Status::timeout;
      curl_socket_t socket = CURL_SOCKET_BAD;
      curl_easy_getinfo(curl, CURLINFO_ACTIVESOCKET, &socket);
      pollfd descriptor{socket, POLLIN, 0};
      ::poll(&descriptor, 1, static_cast<int>(std::max<long long>(remaining, 1)));
      continue;
    }
    if (code != CURLE_OK || meta == nullptr) {
      error = "websocket receive failed: " + std::string(curl_easy_strerror(code));
      return Status::error;
    }
    if ((meta->flags & CURLWS_PING) != 0) {
      std::size_t sent = 0;
      curl_ws_send(curl, buffer_.data(), received, &sent, 0, CURLWS_PONG);
      continue;
    }
    if ((meta->flags & CURLWS_PONG) != 0) continue;
    if ((meta->flags & CURLWS_CLOSE) != 0) {
      error = "websocket closed by server";
      return Status::closed;
    }
    frame.append(buffer_.data(), received);
    if (meta->bytesleft == 0 && (meta->flags & CURLWS_CONT) == 0) return Status::frame;
  }
}

int WebSocket::native_socket() const noexcept {
  curl_socket_t socket = CURL_SOCKET_BAD;
  if (curl_ != nullptr) curl_easy_getinfo(static_cast<CURL*>(curl_), CURLINFO_ACTIVESOCKET, &socket);
  return static_cast<int>(socket);
}

bool WebSocket::send_text(std::string_view text, std::string& error) {
  std::size_t sent = 0;
  const auto code = curl_ws_send(static_cast<CURL*>(curl_), text.data(), text.size(), &sent, 0, CURLWS_TEXT);
  if (code != CURLE_OK || sent != text.size()) {
    error = "websocket send failed: " + std::string(curl_easy_strerror(code));
    return false;
  }
  return true;
}

// ---------------------------------------------------------------- Binance

BinanceFeed::BinanceFeed(std::vector<std::string> symbols, FrameHandler frames, SnapshotHandler snapshots)
    : symbols_(std::move(symbols)), frames_(std::move(frames)), snapshots_(std::move(snapshots)) {}

std::string BinanceFeed::stream_url(std::string_view route, std::string_view stream) const {
  std::string streams;
  for (const auto& symbol : symbols_) {
    if (!streams.empty()) streams += '/';
    streams += lower(symbol) + std::string(stream);
  }
  return "wss://fstream.binance.com/" + std::string(route) + "/stream?streams=" + streams;
}

bool BinanceFeed::snapshot(std::size_t symbol, std::string& error) {
  const auto result = http_.get("https://fapi.binance.com/fapi/v1/depth?symbol=" + symbols_[symbol] + "&limit=500");
  if (!result.ok) {
    error = "binance snapshot " + symbols_[symbol] + ": " + result.error;
    return false;
  }
  BookSnapshot book;
  book.venue = Venue::binance;
  book.symbol = static_cast<std::uint16_t>(symbol);
  book.receive_ns = wall_ns();
  if (!parse_binance_snapshot(result.body, book)) {
    error = "binance snapshot " + symbols_[symbol] + ": invalid response";
    return false;
  }
  return snapshots_(std::move(book));
}

bool BinanceFeed::run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error) {
  std::array<WebSocket, 2> sockets;  // depth, markPrice
  if (!sockets[0].connect(stream_url("public", "@depth@0ms"), error) ||
      !sockets[1].connect(stream_url("market", "@markPrice@1s"), error))
    return false;
  // The combined stream only starts dispatching about 100 ms after the handshake;
  // snapshots taken before the first event per symbol can never bridge, because the
  // spanning diffs were never sent. Buffer raw frames until every symbol has produced
  // a depth event (or a cap expires), snapshot, then replay the buffer in order.
  std::vector<std::pair<std::uint64_t, std::string>> buffered;
  std::vector<bool> seen(symbols_.size(), false);
  std::size_t seen_count = 0;
  std::string frame;
  const auto preroll_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
  while (seen_count < symbols_.size() && std::chrono::steady_clock::now() < preroll_deadline &&
         !stop.load(std::memory_order_relaxed)) {
    for (std::size_t k = 0; k < sockets.size(); ++k) {
      const auto status = sockets[k].receive(frame, 100, error);
      if (status == WebSocket::Status::timeout) continue;
      if (status != WebSocket::Status::frame) return false;
      buffered.emplace_back(wall_ns(), frame);
      if (k == 0 && frame.find("depthUpdate") != std::string_view::npos) {
        for (std::size_t i = 0; i < symbols_.size(); ++i) {
          if (seen[i] || frame.find("\"s\":\"" + symbols_[i] + "\"") == std::string_view::npos) continue;
          seen[i] = true;
          ++seen_count;
        }
      }
    }
  }
  // Keep draining the sockets while REST snapshots run. A 30-symbol basket can
  // spend several seconds here; an idle combined stream is closed by Binance.
  for (std::size_t i = 0; i < symbols_.size(); ++i) {
    for (auto& socket : sockets) {
      for (;;) {
        const auto status = socket.receive(frame, 0, error);
        if (status == WebSocket::Status::timeout) break;
        if (status != WebSocket::Status::frame) return false;
        buffered.emplace_back(wall_ns(), frame);
      }
    }
    if (!snapshot(i, error)) return false;
    if (i + 1 < symbols_.size()) std::this_thread::sleep_for(std::chrono::milliseconds(40));
  }
  for (const auto& [receive_ns, data] : buffered) {
    if (!frames_(receive_ns, data)) {
      if (error.empty()) error = "binance frame handler stopped the feed";
      return false;
    }
  }
  std::array<std::chrono::steady_clock::time_point, kMaxSymbols> last_resync{};
  while (!stop.load(std::memory_order_relaxed)) {
    bool received = false;
    for (auto& socket : sockets) {
      for (;;) {
        const auto status = socket.receive(frame, 0, error);
        if (status == WebSocket::Status::timeout) break;
        if (status != WebSocket::Status::frame) return false;
        received = true;
        if (!frames_(wall_ns(), frame)) {
          if (error.empty()) error = "binance frame handler stopped the feed";
          return false;
        }
      }
    }
    if (!received) {
      std::array<pollfd, 2> descriptors{{{sockets[0].native_socket(), POLLIN, 0},
                                         {sockets[1].native_socket(), POLLIN, 0}}};
      ::poll(descriptors.data(), descriptors.size(), 100);
    }
    const auto now = std::chrono::steady_clock::now();
    for (std::size_t i = 0; i < symbols_.size(); ++i) {
      if (!resync[i].load(std::memory_order_acquire) || now - last_resync[i] < kResyncInterval) continue;
      resync[i].store(false, std::memory_order_release);
      last_resync[i] = now;
      if (!snapshot(i, error)) return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------- Bybit

BybitFeed::BybitFeed(std::vector<std::string> symbols, FrameHandler frames)
    : symbols_(std::move(symbols)), frames_(std::move(frames)) {}

bool BybitFeed::run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error) {
  WebSocket socket;
  if (!socket.connect("wss://stream.bybit.com/v5/public/linear", error)) return false;
  std::vector<std::string> topics;
  for (const auto& symbol : symbols_) {
    topics.push_back("orderbook.50." + symbol);
    topics.push_back("tickers." + symbol);
  }
  for (std::size_t start = 0; start < topics.size(); start += kBybitTopicsPerRequest) {
    std::string request = R"({"op":"subscribe","args":[)";
    for (std::size_t i = start; i < std::min(topics.size(), start + kBybitTopicsPerRequest); ++i) {
      if (i != start) request += ',';
      request += '"' + topics[i] + '"';
    }
    request += "]}";
    if (!socket.send_text(request, error)) return false;
  }
  auto last_ping = std::chrono::steady_clock::now();
  std::array<std::chrono::steady_clock::time_point, kMaxSymbols> last_resync{};
  std::string frame;
  while (!stop.load(std::memory_order_relaxed)) {
    const auto status = socket.receive(frame, 100, error);
    if (status == WebSocket::Status::frame) {
      if (!frames_(wall_ns(), frame)) {
        if (error.empty()) error = "bybit frame handler stopped the feed";
        return false;
      }
    } else if (status != WebSocket::Status::timeout) {
      return false;
    }
    const auto now = std::chrono::steady_clock::now();
    if (now - last_ping >= kBybitPing) {
      if (!socket.send_text(R"({"op":"ping"})", error)) return false;
      last_ping = now;
    }
    for (std::size_t i = 0; i < symbols_.size(); ++i) {
      if (!resync[i].load(std::memory_order_acquire) || now - last_resync[i] < kResyncInterval) continue;
      resync[i].store(false, std::memory_order_release);
      last_resync[i] = now;
      const auto topic = "\"orderbook.50." + symbols_[i] + "\"";
      if (!socket.send_text(R"({"op":"unsubscribe","args":[)" + topic + "]}", error) ||
          !socket.send_text(R"({"op":"subscribe","args":[)" + topic + "]}", error))
        return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------- Hyperliquid

HyperliquidFeed::HyperliquidFeed(std::vector<std::string> coins, FrameHandler frames)
    : coins_(std::move(coins)), frames_(std::move(frames)) {}

bool HyperliquidFeed::run(std::atomic<bool>& stop, ResyncFlags& resync, std::string& error) {
  WebSocket socket;
  if (!socket.connect("wss://api.hyperliquid.xyz/ws", error)) return false;
  for (const auto& coin : coins_) {
    if (!socket.send_text(hyperliquid_subscription("subscribe", "bbo", coin), error) ||
        !socket.send_text(hyperliquid_subscription("subscribe", "activeAssetCtx", coin), error))
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
    if (now - last_ping >= kHyperliquidPing) {
      if (!socket.send_text(R"({"method":"ping"})", error)) return false;
      last_ping = now;
    }
    for (std::size_t i = 0; i < coins_.size(); ++i) {
      if (!resync[i].load(std::memory_order_acquire) || now - last_resync[i] < kResyncInterval) continue;
      resync[i].store(false, std::memory_order_release);
      last_resync[i] = now;
      if (!socket.send_text(hyperliquid_subscription("unsubscribe", "bbo", coins_[i]), error) ||
          !socket.send_text(hyperliquid_subscription("subscribe", "bbo", coins_[i]), error))
        return false;
    }
  }
  return true;
}

}  // namespace cxa
