#pragma once
// The exec thread: keeps HTTP/1.1 keep-alive connections to Helius Sender (and
// optionally Jito's Frankfurt block engine) warm, spins on the packet queue and
// writes each pre-signed transaction the moment it appears. Responses are read
// between packets and reported back to logic.
#include <memory>
#include <string>
#include <vector>

#include "core/config.hpp"
#include "logic/shared.hpp"
#include "net/conn.hpp"

namespace pd {

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
  };
  struct Link {
    Route route = Route::Sender;
    Url url;
    std::string send_path, ping_path, ping_body;
    Conn conn;
    HttpResponseParser parser;
    Pending pending[64];
    uint32_t head = 0, tail = 0;  // FIFO of requests awaiting responses
    uint64_t last_io_ns = 0, next_reconnect_ns = 0;
    std::string req;
    size_t inflight() const { return tail - head; }
  };

  bool send(Link& l, const Packet& p);
  void ping(Link& l, uint64_t now);
  void read(Link& l, uint64_t now);
  void drop(Link& l, const char* why);
  void report(const Pending& p, Route route, bool ok, bool definitive, int status, std::string_view body, uint64_t now);
  void maintain(uint64_t now);

  const Config& cfg_;
  Shared& sh_;
  std::vector<std::unique_ptr<Link>> sender_, jito_;
  std::string body_;
  char rbuf_[65536];
  uint64_t next_maintain_ns_ = 0;
  uint64_t rpc_id_ = 1;
};

}  // namespace pd
