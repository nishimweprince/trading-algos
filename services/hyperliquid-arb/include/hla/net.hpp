#pragma once

#include "hla/platform.hpp"

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

std::uint64_t wall_ns() noexcept;
std::uint64_t steady_ns() noexcept;

struct HttpResult final {
  bool ok{};
  long status{};
  std::string body;
  std::uint64_t elapsed_ns{};
  std::string error;
};

// One persistent curl handle per thread: connections are kept alive between calls so
// RTT samples measure request latency rather than TLS setup.
class HttpClient final {
 public:
  HttpClient();
  ~HttpClient();
  HttpClient(const HttpClient&) = delete;
  HttpClient& operator=(const HttpClient&) = delete;

  HttpResult get(const std::string& url, const std::vector<std::string>& headers = {});
  HttpResult post(const std::string& url, const std::string& body, const std::vector<std::string>& headers = {});

 private:
  HttpResult perform(const std::string& url, const std::string* body, const std::vector<std::string>& headers);
  void* curl_{};
};

// POST https://api.hyperliquid.xyz/info with a JSON body.
HttpResult hyperliquid_info(HttpClient& http, const std::string& body);

// Blocking-with-timeout WebSocket on libcurl's CONNECT_ONLY mode. Waits on the socket
// (poll / WSAPoll) instead of sleeping, so frame receipt is not quantised.
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
  [[nodiscard]] platform::SocketHandle native_socket() const noexcept;

 private:
  void* curl_{};
  std::array<char, 1 << 16> buffer_{};
};

// False when the linked libcurl cannot speak wss:// (e.g. the macOS system build).
bool websocket_supported() noexcept;

constexpr std::string_view kHyperliquidWs = "wss://api.hyperliquid.xyz/ws";

}  // namespace hla
