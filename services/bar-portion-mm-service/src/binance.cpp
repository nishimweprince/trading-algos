#include "bpmm/binance.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <chrono>
#include <cctype>
#include <thread>

namespace bpmm {
namespace {

std::uint64_t now_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

std::size_t append(void* data, std::size_t size, std::size_t count, void* context) {
  auto* output = static_cast<std::string*>(context);
  output->append(static_cast<const char*>(data), size * count);
  return size * count;
}

}  // namespace

BinancePublicClient::BinancePublicClient(std::string symbol, SnapshotHandler snapshot_handler,
                                         FrameHandler handler)
    : symbol_(std::move(symbol)), lower_symbol_(symbol_),
      snapshot_handler_(std::move(snapshot_handler)), handler_(std::move(handler)) {
  std::transform(symbol_.begin(), symbol_.end(), symbol_.begin(),
                 [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  std::transform(lower_symbol_.begin(), lower_symbol_.end(), lower_symbol_.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  curl_global_init(CURL_GLOBAL_DEFAULT);
}

BinancePublicClient::~BinancePublicClient() { curl_global_cleanup(); }

bool BinancePublicClient::fetch_snapshot(Snapshot& snapshot, std::string& error) {
  auto* curl = curl_easy_init();
  if (curl == nullptr) { error = "curl initialization failed"; return false; }
  std::string body;
  const auto url = "https://fapi.binance.com/fapi/v1/depth?symbol=" + symbol_ + "&limit=1000";
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, append);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "trading-algos-bar-mm/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
  const auto code = curl_easy_perform(curl);
  long status = 0;
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
  curl_easy_cleanup(curl);
  if (code != CURLE_OK || status != 200) {
    error = "snapshot request failed: " + std::string(curl_easy_strerror(code)) +
            " HTTP " + std::to_string(status);
    return false;
  }
  if (!parse_snapshot(body, snapshot)) { error = "invalid snapshot response"; return false; }
  return true;
}

bool BinancePublicClient::run(std::atomic<bool>& stop, std::string& error) {
  auto* curl = curl_easy_init();
  if (curl == nullptr) { error = "curl initialization failed"; return false; }
  const auto streams = lower_symbol_ + "@depth@0ms/" + lower_symbol_ + "@aggTrade/" +
                       lower_symbol_ + "@kline_1m/" + lower_symbol_ + "@markPrice@1s";
  const auto url = "wss://fstream.binance.com/stream?streams=" + streams;
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_CONNECT_ONLY, 2L);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "trading-algos-bar-mm/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
  auto code = curl_easy_perform(curl);
  if (code != CURLE_OK) {
    error = "websocket connect failed: " + std::string(curl_easy_strerror(code));
    curl_easy_cleanup(curl);
    return false;
  }
  Snapshot snapshot;
  if (!fetch_snapshot(snapshot, error) || !snapshot_handler_(now_ns(), snapshot)) {
    if (error.empty()) error = "snapshot handler rejected snapshot";
    curl_easy_cleanup(curl);
    return false;
  }
  std::array<char, 1 << 20> frame{};
  std::string assembled;
  assembled.reserve(frame.size());
  while (!stop.load(std::memory_order_relaxed)) {
    std::size_t received = 0;
    const curl_ws_frame* meta = nullptr;
    code = curl_ws_recv(curl, frame.data(), frame.size(), &received, &meta);
    if (code == CURLE_AGAIN) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
      continue;
    }
    if (code != CURLE_OK || meta == nullptr) {
      error = "websocket receive failed: " + std::string(curl_easy_strerror(code));
      curl_easy_cleanup(curl);
      return false;
    }
    if ((meta->flags & CURLWS_PING) != 0) {
      std::size_t sent = 0;
      curl_ws_send(curl, frame.data(), received, &sent, 0, CURLWS_PONG);
      continue;
    }
    if ((meta->flags & CURLWS_CLOSE) != 0) {
      error = "websocket closed by server";
      curl_easy_cleanup(curl);
      return false;
    }
    assembled.append(frame.data(), received);
    if (meta->bytesleft == 0 && (meta->flags & CURLWS_CONT) == 0) {
      if (!handler_(now_ns(), assembled)) {
        error = "frame handler rejected event";
        curl_easy_cleanup(curl);
        return false;
      }
      assembled.clear();
    }
  }
  curl_easy_cleanup(curl);
  return true;
}

}  // namespace bpmm
