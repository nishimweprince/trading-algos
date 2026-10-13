#pragma once
// The exec thread: keeps HTTP/1.1 keep-alive connections to Helius Sender (and
// optionally Jito's Frankfurt block engine) warm, spins on the packet queue and
// writes each pre-signed transaction the moment it appears. Responses are read
// between packets and reported back to logic.
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "core/config.hpp"
#include "logic/shared.hpp"
#include "net/conn.hpp"

namespace pd {

/** How a send's HTTP response reads. */
struct SendVerdict {
  bool ok = false;
  bool definitive = false;    // proves the tx was not accepted
  bool rate_limited = false;  // 429: resend the same bytes after a backoff
};
SendVerdict classify_response(int status, std::string_view body);

class Exec {
 public:
  Exec(const Config& cfg, Shared& sh);
  ~Exec();
  /** Boot: open every connection. False when no Sender connection could be opened. */
  bool connect_all(std::string* err);
  /** Pinned loop; returns when sh.stop is set. */
  void run();

 private:
  struct Pending {
    bool ping = false;
    Side side = Side::Buy;
    uint8_t pos = 0, attempt = 0;
    Signature sig{};
    uint64_t t_signed = 0, t_written = 0;
    uint8_t tries = 0;  // 429 resends so far
  };
  /** The signed bytes behind a pending request, kept for a 429 resend. */
  struct Bytes {
    uint16_t len = 0;
    char b64[TX_B64_MAX];
  };
  /** A 429'd request waiting for its backoff. */
  struct Retry {
    bool used = false;
    Pending p;
    Bytes bytes;
    uint64_t due_ns = 0;
    const void* from = nullptr;  // the sender link that returned the 429
  };
  struct Link {
    Route route = Route::Sender;
    Url url;
    std::string send_path, ping_path, ping_body;
    Conn conn;
    HttpResponseParser parser;
    Pending pending[64];
    Bytes bytes[64];  // parallel to pending
    uint32_t head = 0, tail = 0;  // FIFO of requests awaiting responses
    uint64_t last_io_ns = 0, next_reconnect_ns = 0;
    std::string req;
    size_t inflight() const { return tail - head; }
  };

  bool send(Link& l, const Packet& p);
  bool write_tx(Link& l, const char* b64, size_t b64_len, const Pending& meta);
  void schedule_retry(const Pending& p, const Bytes& b, const Link& from, uint64_t now);
  void run_retries(uint64_t now);
  void ping(Link& l, uint64_t now);
  void read(Link& l, uint64_t now);
  void drop(Link& l, const char* why);
  void report(const Pending& p, Route route, const SendVerdict& v, int status, std::string_view body, uint64_t now);
  void maintain(uint64_t now);

  const Config& cfg_;
  Shared& sh_;
  std::vector<std::unique_ptr<Link>> sender_, jito_;
  std::string body_;
  char rbuf_[65536];
  uint64_t next_maintain_ns_ = 0;
  uint64_t rpc_id_ = 1;
  Retry retries_[8];
};

}  // namespace pd
