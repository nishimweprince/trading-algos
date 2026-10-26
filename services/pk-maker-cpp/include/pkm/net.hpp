#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

// TCP (+ OpenSSL TLS) connections. Connect is blocking with a timeout; after it
// the socket is non-blocking, so the hot thread can poll many of them with
// epoll. TCP_NODELAY is always on.
namespace pkm::net {

struct Url {
  bool tls = true;
  std::string host;
  std::uint16_t port = 443;
  std::string path = "/";  // with the query
};
// https:// wss:// http:// ws://
bool parse_url(std::string_view text, Url& out);

std::int64_t now_ns() noexcept;       // CLOCK_REALTIME
std::int64_t mono_ns() noexcept;      // CLOCK_MONOTONIC

class Connection final {
 public:
  Connection() = default;
  ~Connection();
  Connection(const Connection&) = delete;
  Connection& operator=(const Connection&) = delete;
  Connection(Connection&& other) noexcept;
  Connection& operator=(Connection&& other) noexcept;

  bool connect(const std::string& host, std::uint16_t port, bool tls, int timeout_ms, std::string& error);
  void close() noexcept;
  [[nodiscard]] bool open() const noexcept { return fd_ >= 0; }
  [[nodiscard]] int fd() const noexcept { return fd_; }

  // > 0 bytes read, 0 nothing available yet, -1 closed or failed.
  long read(char* buffer, std::size_t size) noexcept;
  // Writes all of `data`, waiting up to `timeout_ms` for socket space.
  bool write_all(std::string_view data, int timeout_ms = 1000) noexcept;
  // Bytes OpenSSL already decrypted and holds (read before waiting on the fd).
  [[nodiscard]] bool pending() const noexcept;
  // Waits until readable (or pending TLS bytes) for up to `timeout_ms`.
  bool wait_readable(int timeout_ms) const noexcept;

 private:
  int fd_ = -1;
  void* ssl_ = nullptr;  // SSL*
};

}  // namespace pkm::net
