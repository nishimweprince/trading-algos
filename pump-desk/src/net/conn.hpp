#pragma once
// TCP (TCP_NODELAY) with optional TLS, plus an incremental HTTP/1.1 response
// parser. Used by exec (non-blocking, pinned) and aux RPC (blocking, with deadlines).
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_st SSL;

namespace pd {

struct Url {
  bool tls = false;
  std::string host;
  int port = 0;
  std::string path;  // path + query, starts with '/'
  std::string origin() const { return std::string(tls ? "https://" : "http://") + host + ":" + std::to_string(port); }
};

Url parse_url(const std::string& url);

class Conn {
 public:
  Conn() = default;
  ~Conn();
  Conn(const Conn&) = delete;
  Conn& operator=(const Conn&) = delete;

  /** Blocking connect (+ TLS handshake) within timeout_ms, then the socket is left non-blocking. */
  bool connect(const Url& url, int timeout_ms, std::string* err = nullptr);
  void close();
  bool open() const { return fd_ >= 0; }
  int fd() const { return fd_; }

  /** Write everything, waiting up to timeout_ms for socket space. */
  bool write_all(const char* data, size_t len, int timeout_ms);
  /** Non-blocking read: >0 bytes, 0 = nothing yet, -1 = closed/error. */
  ssize_t read_some(char* buf, size_t cap);
  /** Wait until readable (or TLS has buffered bytes). */
  bool wait_readable(int timeout_ms);

 private:
  int fd_ = -1;
  SSL* ssl_ = nullptr;
};

/** Incremental HTTP/1.1 response parser (Content-Length or chunked). */
class HttpResponseParser {
 public:
  /** Feed bytes; returns true when a full response is available in status()/body(). */
  bool feed(const char* data, size_t len);
  /** After a complete response: drop it, keep any pipelined remainder; returns true if another is complete. */
  bool next();
  int status() const { return status_; }
  std::string_view body() const { return body_; }
  void reset() { buf_.clear(); status_ = 0; done_ = false; consumed_ = 0; body_ = {}; decoded_.clear(); }

 private:
  bool parse();
  std::string buf_;
  std::string decoded_;
  int status_ = 0;
  bool done_ = false;
  size_t consumed_ = 0;
  std::string_view body_;
};

/** Format a request into `out` (reused buffer). */
void format_request(std::string& out, const char* method, const Url& url, std::string_view path, std::string_view body);

}  // namespace pd
