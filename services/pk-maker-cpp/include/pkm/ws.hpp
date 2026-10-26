#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "pkm/net.hpp"

// RFC 6455 client: a blocking handshake, then non-blocking reads driven by the
// hot thread. Pings are answered inside `poll`; fragments are joined.
namespace pkm::ws {

class Client final {
 public:
  Client();
  // `headers`: extra handshake lines, each ending CRLF (Kalshi's auth).
  bool connect(const net::Url& url, std::string_view headers, std::string& error);
  void close() noexcept;
  [[nodiscard]] bool open() const noexcept { return conn_.open() && !closed_; }
  [[nodiscard]] int fd() const noexcept { return conn_.fd(); }
  [[nodiscard]] bool pending() const noexcept { return conn_.pending() || parsed_ < in_.size(); }

  bool send_text(std::string_view text);
  bool send_ping();
  [[nodiscard]] std::uint16_t close_code() const noexcept { return close_code_; }
  [[nodiscard]] std::int64_t last_rx_ns() const noexcept { return last_rx_ns_; }

  // Reads what is available and calls `on_message(std::string_view)` per
  // complete data message, reading at most 8 times so one busy socket cannot
  // starve the others. Returns false once the connection is gone.
  template <typename F>
  bool poll(F&& on_message) {
    if (!conn_.open() || closed_) return false;
    for (int reads = 0;; ++reads) {
      std::string_view message;
      while (next_message(message)) on_message(message);
      if (closed_) return false;
      if (reads == 8) return true;
      const int got = fill();
      if (got == 0) return true;
      if (got < 0) {
        closed_ = true;
        return false;
      }
    }
  }

  // Waits up to `timeout_ms` for one message (handshake replies, tests).
  bool read_message(std::string& out, int timeout_ms);

 private:
  // 1 bytes read, 0 none available, -1 connection gone.
  int fill();
  bool next_message(std::string_view& out);
  bool send_frame(std::uint8_t opcode, std::string_view payload);

  net::Connection conn_;
  std::string in_;
  std::size_t parsed_ = 0;
  std::string message_;  // fragments being joined, or the last delivered message
  std::string frame_;
  bool fragmented_ = false;
  bool closed_ = false;
  std::uint16_t close_code_ = 0;
  std::uint64_t mask_state_;
  std::int64_t last_rx_ns_ = 0;
};

}  // namespace pkm::ws
