#include "hla/net.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <chrono>

namespace hla {
namespace {

constexpr std::string_view kHyperliquidInfo = "https://api.hyperliquid.xyz/info";
constexpr const char* kUserAgent = "trading-algos-hlarb/0.1";

std::size_t append(void* data, std::size_t size, std::size_t count, void* context) {
  static_cast<std::string*>(context)->append(static_cast<const char*>(data), size * count);
  return size * count;
}

}  // namespace

std::uint64_t wall_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

std::uint64_t steady_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}

HttpClient::HttpClient() : curl_(curl_easy_init()) {}
HttpClient::~HttpClient() {
  if (curl_ != nullptr) curl_easy_cleanup(static_cast<CURL*>(curl_));
}

HttpResult HttpClient::get(const std::string& url, const std::vector<std::string>& headers) {
  return perform(url, nullptr, headers);
}

HttpResult HttpClient::post(const std::string& url, const std::string& body, const std::vector<std::string>& headers) {
  return perform(url, &body, headers);
}

HttpResult HttpClient::perform(const std::string& url, const std::string* body,
                               const std::vector<std::string>& headers) {
  HttpResult result;
  auto* curl = static_cast<CURL*>(curl_);
  if (curl == nullptr) { result.error = "curl initialization failed"; return result; }
  curl_slist* list = nullptr;
  for (const auto& header : headers) list = curl_slist_append(list, header.c_str());
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  if (body == nullptr) {
    curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
  } else {
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body->c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body->size()));
  }
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, append);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &result.body);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, kUserAgent);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
  curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
  curl_easy_setopt(curl, CURLOPT_TCP_NODELAY, 1L);
  curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
  const auto start = std::chrono::steady_clock::now();
  const auto code = curl_easy_perform(curl);
  result.elapsed_ns = static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count());
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.status);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, nullptr);
  curl_slist_free_all(list);
  if (code != CURLE_OK) {
    result.error = curl_easy_strerror(code);
    return result;
  }
  result.ok = result.status == 200;
  if (!result.ok) result.error = "HTTP " + std::to_string(result.status) + " " + result.body.substr(0, 200);
  return result;
}

HttpResult hyperliquid_info(HttpClient& http, const std::string& body) {
  return http.post(std::string(kHyperliquidInfo), body, {"Content-Type: application/json"});
}

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
  if (curl_ != nullptr) curl_easy_cleanup(static_cast<CURL*>(curl_));  // reconnect on the same object
  auto* curl = curl_easy_init();
  if (curl == nullptr) { error = "curl initialization failed"; return false; }
  curl_ = curl;
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, kUserAgent);
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
      const auto socket = native_socket();
      platform::wait_readable(&socket, 1, static_cast<int>(std::max<long long>(remaining, 1)));
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

platform::SocketHandle WebSocket::native_socket() const noexcept {
  curl_socket_t socket = CURL_SOCKET_BAD;
  if (curl_ != nullptr) curl_easy_getinfo(static_cast<CURL*>(curl_), CURLINFO_ACTIVESOCKET, &socket);
  return static_cast<platform::SocketHandle>(socket);
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

}  // namespace hla
