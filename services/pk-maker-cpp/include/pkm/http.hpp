#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "pkm/net.hpp"

namespace pkm::http {

// Parses one HTTP/1.1 response from bytes fed in pieces: Content-Length,
// chunked, or (for `Connection: close`) to end of stream.
class ResponseParser final {
 public:
  ResponseParser() { buffer_.reserve(16 * 1024); }
  void reset() noexcept;
  // Appends bytes; true once a full response is buffered.
  bool feed(const char* data, std::size_t size);
  // The peer closed: completes a read-to-close body.
  bool finish_on_close() noexcept;
  [[nodiscard]] bool complete() const noexcept { return complete_; }
  [[nodiscard]] bool error() const noexcept { return error_; }
  [[nodiscard]] int status() const noexcept { return status_; }
  [[nodiscard]] std::string_view body() const noexcept { return body_; }
  [[nodiscard]] bool keep_alive() const noexcept { return keep_alive_; }
  [[nodiscard]] std::string_view header(std::string_view name) const noexcept;
  // Bytes after this response (a pipelined next one); moved to the front on reset.
  [[nodiscard]] std::size_t consumed() const noexcept { return consumed_; }

 private:
  bool parse();

  std::string buffer_;
  std::string body_;
  std::size_t header_end_ = 0;
  std::size_t consumed_ = 0;
  long content_length_ = -1;
  bool chunked_ = false;
  bool to_close_ = false;
  bool complete_ = false;
  bool error_ = false;
  bool keep_alive_ = true;
  int status_ = 0;
};

struct Response {
  int status = 0;  // 0 = transport failure
  std::string body;
  std::string error;
  [[nodiscard]] bool ok() const noexcept { return status / 100 == 2; }
};

// Blocking, keep-alive, reconnecting client for one origin (control plane).
class Client final {
 public:
  Client(std::string base_url, std::string user_agent, int timeout_ms = 10'000);
  // `headers` are extra lines, each ending CRLF. One reconnect-and-retry on a
  // stale keep-alive connection (no bytes received).
  Response request(std::string_view method, std::string_view path, std::string_view headers = {},
                   std::string_view body = {});
  Response get(std::string_view path, std::string_view headers = {}) { return request("GET", path, headers); }
  [[nodiscard]] const net::Url& url() const noexcept { return url_; }

 private:
  Response attempt(std::string_view method, std::string_view path, std::string_view headers, std::string_view body,
                   bool& retryable);

  net::Url url_;
  std::string base_path_;
  std::string user_agent_;
  int timeout_ms_;
  net::Connection conn_;
  ResponseParser parser_;
  std::string request_;
};

// One request in flight at a time on a warm keep-alive connection, driven by
// the hot thread: `send` writes pre-built bytes, `poll` reads without blocking.
class Channel final {
 public:
  explicit Channel(net::Url url) : url_(std::move(url)) {}
  bool connect(std::string& error);
  [[nodiscard]] bool open() const noexcept { return conn_.open(); }
  [[nodiscard]] bool busy() const noexcept { return busy_; }
  [[nodiscard]] int fd() const noexcept { return conn_.fd(); }
  [[nodiscard]] std::int64_t sent_ns() const noexcept { return sent_ns_; }
  [[nodiscard]] std::int64_t last_used_ns() const noexcept { return last_used_ns_; }
  bool send(std::string_view bytes);
  // 1 a response is ready (read it, then `release`), 0 nothing yet, -1 failed.
  int poll();
  [[nodiscard]] const ResponseParser& response() const noexcept { return parser_; }
  void release() noexcept;
  void close() noexcept;
  [[nodiscard]] const net::Url& url() const noexcept { return url_; }

 private:
  net::Url url_;
  net::Connection conn_;
  ResponseParser parser_;
  bool busy_ = false;
  std::int64_t sent_ns_ = 0;
  std::int64_t last_used_ns_ = 0;
  char buf_[16 * 1024];
};

// "GET /path HTTP/1.1\r\nHost: …\r\n…" with Content-Length when there is a body.
void write_request(std::string& out, std::string_view method, std::string_view host, std::string_view path,
                   std::string_view headers, std::string_view body, std::string_view user_agent = "pk-maker-cpp/0.1");

}  // namespace pkm::http
