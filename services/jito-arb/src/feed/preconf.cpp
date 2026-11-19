#include "feed/preconf.hpp"

#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>

#include "core/sys.hpp"
#include "obs/log.hpp"

namespace arb::feed {

bool parse_preconf(std::string_view p, PreconfTx& out) {
  if (p.size() < 18 + 65 || uint8_t(p[0]) != 1) return false;  // header + at least one signature byte count + sig
  std::memcpy(&out.slot, p.data() + 1, 8);
  std::memcpy(&out.tx_index, p.data() + 9, 8);
  out.status = uint8_t(p[17]);
  out.tx.assign(p.data() + 18, p.size() - 18);
  return true;
}

namespace {

struct Reader {
  const uint8_t* b;
  size_t n, p = 0;
  bool take(size_t k, const uint8_t** out = nullptr) {
    if (p + k > n) return false;
    if (out) *out = b + p;
    p += k;
    return true;
  }
  bool u8(uint8_t& v) {
    const uint8_t* q;
    if (!take(1, &q)) return false;
    v = *q;
    return true;
  }
  bool u16(uint16_t& v) {
    const uint8_t* q;
    if (!take(2, &q)) return false;
    v = uint16_t(q[0] | q[1] << 8);
    return true;
  }
  bool u32(uint32_t& v) {
    const uint8_t* q;
    if (!take(4, &q)) return false;
    std::memcpy(&v, q, 4);
    return true;
  }
  bool shortvec(size_t& v) {
    v = 0;
    for (int i = 0; i < 3; ++i) {
      uint8_t x;
      if (!u8(x)) return false;
      v |= size_t(x & 0x7f) << (7 * i);
      if (!(x & 0x80)) return true;
    }
    return false;
  }
  bool key(Pubkey& k) {
    const uint8_t* q;
    if (!take(32, &q)) return false;
    std::memcpy(k.data(), q, 32);
    return true;
  }
};

}  // namespace

bool parse_tx_keys(const uint8_t* d, size_t n, TxKeys& out) {
  out = TxKeys{};
  Reader r{d, n};
  if (n == 0) return false;
  if (d[0] == 0x81) {  // v1 (SIMD-0385): message first, signatures last, no lookup tables
    out.version = 1;
    uint8_t nsig, nix, nkeys, ro_s, ro_u;
    uint32_t mask;
    if (!r.take(1) || !r.u8(nsig) || !r.u8(ro_s) || !r.u8(ro_u) || !r.u32(mask) || !r.take(32) || !r.u8(nix) || !r.u8(nkeys)) return false;
    out.statics.resize(nkeys);
    for (auto& k : out.statics)
      if (!r.key(k)) return false;
    if (!r.take(4 * size_t(__builtin_popcount(mask)))) return false;
    size_t payload = 0;
    for (uint8_t i = 0; i < nix; ++i) {
      uint8_t prog, na;
      uint16_t nd;
      if (!r.u8(prog) || !r.u8(na) || !r.u16(nd)) return false;
      payload += size_t(na) + nd;
    }
    if (!r.take(payload) || nsig == 0) return false;
    const uint8_t* sig;
    if (!r.take(64 * size_t(nsig), &sig)) return false;
    std::memcpy(out.sig.data(), sig, 64);
    return true;
  }
  size_t nsig;
  const uint8_t* sigs;
  if (!r.shortvec(nsig) || nsig == 0 || !r.take(64 * nsig, &sigs)) return false;
  std::memcpy(out.sig.data(), sigs, 64);
  if (r.p >= n) return false;
  const bool v0 = d[r.p] & 0x80;
  out.version = v0 ? 0 : 0xff;
  if (v0 && !r.take(1)) return false;
  if (!r.take(3)) return false;
  size_t nk;
  if (!r.shortvec(nk)) return false;
  out.statics.resize(nk);
  for (auto& k : out.statics)
    if (!r.key(k)) return false;
  if (!r.take(32)) return false;
  size_t nix;
  if (!r.shortvec(nix)) return false;
  for (size_t i = 0; i < nix; ++i) {
    size_t na, nd;
    if (!r.take(1) || !r.shortvec(na) || !r.take(na) || !r.shortvec(nd) || !r.take(nd)) return false;
  }
  if (v0) {
    size_t nl;
    if (!r.shortvec(nl)) return false;
    out.lookups.resize(nl);
    for (auto& l : out.lookups) {
      size_t nw, nr;
      const uint8_t* q;
      if (!r.key(l.table) || !r.shortvec(nw) || !r.take(nw, &q)) return false;
      l.writable.assign(q, q + nw);
      if (!r.shortvec(nr) || !r.take(nr, &q)) return false;
      l.readonly.assign(q, q + nr);
    }
  }
  return true;
}

std::string preconf_subscribe_body(const std::vector<std::string>& accounts, bool include_failed, const std::vector<std::string>& regions) {
  std::string b = R"({"jsonrpc":"2.0","id":1,"method":"preconfSubscribe","params":[{"accountInclude":[)";
  for (size_t i = 0; i < accounts.size(); ++i) b += (i ? ",\"" : "\"") + accounts[i] + "\"";
  b += "]";
  b += include_failed ? R"(,"failed":true)" : R"(,"failed":false)";
  if (!regions.empty()) {
    b += R"(,"regionInclude":[)";
    for (size_t i = 0; i < regions.size(); ++i) b += (i ? ",\"" : "\"") + regions[i] + "\"";
    b += "]";
  }
  b += "}]}";
  return b;
}

// ---- credit guard ----

void CreditGuard::roll(int64_t wall_ms) {
  int64_t day = wall_ms / 86'400'000;
  if (day != day_) {
    day_ = day;
    finalized_ = 0;
    open_.clear();
  }
}

static uint64_t slot_charge(uint64_t msgs) { return std::max<uint64_t>(300, 10 * msgs); }

void CreditGuard::on_message(uint64_t slot, int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  ++open_[slot];
  // Slots 8 behind the newest are complete: fold them into the finalized total.
  const uint64_t newest = open_.rbegin()->first;
  while (!open_.empty() && open_.begin()->first + 8 < newest) {
    finalized_ += slot_charge(open_.begin()->second);
    open_.erase(open_.begin());
  }
}

void CreditGuard::add(uint64_t credits, int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  finalized_ += credits;
}

uint64_t CreditGuard::today(int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  uint64_t t = finalized_;
  for (const auto& [slot, n] : open_) t += slot_charge(n);
  return t;
}

bool CreditGuard::blocked(int64_t wall_ms) { return today(wall_ms) + 300 > cap_; }

bool CreditGuard::load(const std::string& path, int64_t wall_ms) {
  FILE* f = std::fopen(path.c_str(), "r");
  if (!f) return false;
  long long day = -1;
  unsigned long long credits = 0;
  bool ok = std::fscanf(f, "%lld %llu", &day, &credits) == 2;
  std::fclose(f);
  if (!ok) return false;
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  if (day == day_) finalized_ = std::max<uint64_t>(finalized_, credits);
  return true;
}

bool CreditGuard::save(const std::string& path, int64_t wall_ms) {
  const uint64_t t = today(wall_ms);
  const std::string tmp = path + ".tmp";
  FILE* f = std::fopen(tmp.c_str(), "w");
  if (!f) return false;
  std::fprintf(f, "%lld %llu\n", (long long)(wall_ms / 86'400'000), (unsigned long long)t);
  bool ok = std::fclose(f) == 0;
  return ok && std::rename(tmp.c_str(), path.c_str()) == 0;
}

// ---- WebSocket client (RFC 6455; client frames masked), one thread: reads with a 1 s timeout, pings when idle ----

namespace {

struct Ws {
  int fd = -1;
  SSL_CTX* ctx = nullptr;
  SSL* ssl = nullptr;
  std::string buf;
  std::string err;

  bool connect(const std::string& host, const std::string& path) {
    addrinfo hints{}, *res = nullptr;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host.c_str(), "443", &hints, &res) != 0) return fail("dns");
    for (addrinfo* a = res; a; a = a->ai_next) {
      fd = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
      if (fd >= 0 && ::connect(fd, a->ai_addr, a->ai_addrlen) == 0) break;
      if (fd >= 0) ::close(fd);
      fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) return fail("connect");
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    timeval tv{5, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    ctx = SSL_CTX_new(TLS_client_method());
    SSL_CTX_set_default_verify_paths(ctx);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
    ssl = SSL_new(ctx);
    SSL_set_tlsext_host_name(ssl, host.c_str());
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    SSL_set1_host(ssl, host.c_str());  // deprecated in OpenSSL 3.6+, still the portable call (as in submit/conn.cpp)
#pragma GCC diagnostic pop
    SSL_set_fd(ssl, fd);
    if (SSL_connect(ssl) != 1) return fail("tls");
    unsigned char key[16];
    RAND_bytes(key, 16);
    static const char* b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string k;
    for (int i = 0; i < 16; i += 3) {
      uint32_t v = uint32_t(key[i]) << 16 | (i + 1 < 16 ? uint32_t(key[i + 1]) << 8 : 0) | (i + 2 < 16 ? key[i + 2] : 0);
      k += b64[(v >> 18) & 63];
      k += b64[(v >> 12) & 63];
      k += i + 1 < 16 ? b64[(v >> 6) & 63] : '=';
      k += i + 2 < 16 ? b64[v & 63] : '=';
    }
    std::string req = "GET " + path + " HTTP/1.1\r\nHost: " + host +
                      "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + k + "\r\nSec-WebSocket-Version: 13\r\n\r\n";
    if (SSL_write(ssl, req.data(), int(req.size())) <= 0) return fail("write");
    char tmp[4096];
    while (buf.find("\r\n\r\n") == std::string::npos) {
      int n = SSL_read(ssl, tmp, sizeof tmp);
      if (n <= 0) return fail("upgrade read");
      buf.append(tmp, size_t(n));
    }
    if (buf.rfind("HTTP/1.1 101", 0) != 0) return fail("upgrade refused: " + buf.substr(0, buf.find("\r\n")));
    buf.erase(0, buf.find("\r\n\r\n") + 4);
    timeval shortv{1, 0};  // reads now wake every second, so the same thread can ping and stop
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &shortv, sizeof shortv);
    return true;
  }
  bool fail(const std::string& why) {
    err = why;
    return false;
  }
  bool send(int opcode, const std::string& payload) {
    std::string f;
    f += char(0x80 | opcode);
    size_t n = payload.size();
    if (n < 126) f += char(0x80 | n);
    else if (n < 65536) { f += char(0x80 | 126); f += char(n >> 8); f += char(n & 255); }
    else { f += char(0x80 | 127); for (int i = 7; i >= 0; --i) f += char((uint64_t(n) >> (8 * i)) & 255); }
    unsigned char mask[4];
    RAND_bytes(mask, 4);
    f.append(reinterpret_cast<char*>(mask), 4);
    for (size_t i = 0; i < n; ++i) f += char(payload[i] ^ mask[i % 4]);
    return SSL_write(ssl, f.data(), int(f.size())) > 0;
  }
  // 1: a frame; 0: timeout (no frame within ~1 s); -1: closed or error.
  int next(int& opcode, std::string& payload) {
    char tmp[65536];
    for (;;) {
      if (buf.size() >= 2) {
        const unsigned char* b = reinterpret_cast<const unsigned char*>(buf.data());
        uint64_t len = b[1] & 127;
        size_t hdr = 2;
        bool have = true;
        if (len == 126) {
          if (buf.size() < 4) have = false;
          else { len = uint64_t(b[2]) << 8 | b[3]; hdr = 4; }
        } else if (len == 127) {
          if (buf.size() < 10) have = false;
          else { len = 0; for (int i = 0; i < 8; ++i) len = len << 8 | b[2 + i]; hdr = 10; }
        }
        if (have) {
          if (b[1] & 128) hdr += 4;
          if (buf.size() >= hdr + len) {
            opcode = b[0] & 15;
            payload.assign(buf, hdr, size_t(len));
            buf.erase(0, hdr + size_t(len));
            return 1;
          }
        }
      }
      int n = SSL_read(ssl, tmp, sizeof tmp);
      if (n > 0) {
        buf.append(tmp, size_t(n));
        continue;
      }
      int e = SSL_get_error(ssl, n);
      if ((e == SSL_ERROR_WANT_READ || e == SSL_ERROR_SYSCALL) && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
      if (e == SSL_ERROR_WANT_READ) return 0;
      return -1;
    }
  }
  void close() {
    if (ssl) { SSL_shutdown(ssl); SSL_free(ssl); ssl = nullptr; }
    if (ctx) { SSL_CTX_free(ctx); ctx = nullptr; }
    if (fd >= 0) { ::close(fd); fd = -1; }
    buf.clear();
  }
};

}  // namespace

PreconfFeed::PreconfFeed(PreconfConfig c, CreditGuard* guard, OnTx on_tx)
    : c_(std::move(c)), guard_(guard), on_tx_(std::move(on_tx)), seen_ring_(65536, 0) {}

PreconfFeed::~PreconfFeed() { stop(); }

void PreconfFeed::start() {
  if (th_.joinable()) return;
  stop_ = false;
  th_ = std::thread([this] { run(); });
}

void PreconfFeed::stop() {
  stop_ = true;
  if (th_.joinable()) th_.join();
}

bool PreconfFeed::seen(const Signature& sig) {
  uint64_t k;
  std::memcpy(&k, sig.data(), 8);
  if (seen_.count(k)) return true;
  if (seen_ring_[seen_pos_]) seen_.erase(seen_ring_[seen_pos_]);
  seen_ring_[seen_pos_] = k;
  seen_pos_ = (seen_pos_ + 1) % seen_ring_.size();
  seen_.insert(k);
  return false;
}

void PreconfFeed::run() {
  int backoff = c_.reconnect_min_ms;
  while (!stop_) {
    if (guard_ && guard_->blocked(wall_ms())) {
      if (!capped_.exchange(true)) LOG_WARN("preconf: daily credit cap reached (%llu); off until the next UTC day", (unsigned long long)guard_->cap());
      for (int i = 0; i < 10 && !stop_; ++i) std::this_thread::sleep_for(std::chrono::seconds(1));
      continue;
    }
    capped_ = false;
    Ws ws;
    if (!ws.connect(c_.host, "/?api-key=" + c_.api_key) || !ws.send(1, preconf_subscribe_body(c_.accounts, c_.include_failed, c_.regions))) {
      LOG_WARN("preconf: connect failed (%s); retry in %d ms", ws.err.c_str(), backoff);
      ws.close();
      for (int slept = 0; slept < backoff && !stop_; slept += 100) std::this_thread::sleep_for(std::chrono::milliseconds(100));
      backoff = std::min(backoff * 2, c_.reconnect_max_ms);
      ++reconnects_;
      continue;
    }
    auto last_rx = std::chrono::steady_clock::now(), last_ping = last_rx;
    int op;
    std::string p;
    bool capped_now = false;
    while (!stop_) {
      int r = ws.next(op, p);
      auto now = std::chrono::steady_clock::now();
      if (r < 0) break;
      if (r == 0) {
        if (now - last_ping > std::chrono::seconds(c_.ping_s)) {
          last_ping = now;
          if (!ws.send(9, "p")) break;
        }
        if (now - last_rx > std::chrono::seconds(90)) break;  // silent for 90 s: reconnect
        if (guard_ && guard_->blocked(wall_ms())) { capped_now = true; break; }
        continue;
      }
      last_rx = now;
      if (op == 1) {
        if (p.find("\"result\"") != std::string::npos && !subscribed_) {
          subscribed_ = true;
          backoff = c_.reconnect_min_ms;
          LOG_INFO("preconf: subscribed (%zu accounts)", c_.accounts.size());
        } else if (p.find("\"error\"") != std::string::npos) {
          LOG_WARN("preconf: %.300s", p.c_str());
        }
      } else if (op == 2) {
        PreconfTx t;
        t.recv_ns = now_ns();
        if (!parse_preconf(p, t)) { ++bad_; continue; }
        ++msgs_;
        if (guard_) guard_->on_message(t.slot, wall_ms());
        TxKeys keys;
        if (!parse_tx_keys(reinterpret_cast<const uint8_t*>(t.tx.data()), t.tx.size(), keys)) { ++bad_; continue; }
        if (seen(keys.sig)) { ++dups_; continue; }
        if (t.status == 0) { ++failed_; continue; }
        if (on_tx_) on_tx_(std::move(t));
        if (guard_ && guard_->blocked(wall_ms())) { capped_now = true; break; }
      } else if (op == 9) {
        ws.send(10, p);
      } else if (op == 8) {
        LOG_WARN("preconf: server closed the stream");
        break;
      }
    }
    subscribed_ = false;
    ws.close();  // closing the socket ends the subscription (and its billing)
    if (!stop_ && !capped_now) {
      ++reconnects_;
      for (int slept = 0; slept < backoff && !stop_; slept += 100) std::this_thread::sleep_for(std::chrono::milliseconds(100));
      backoff = std::min(backoff * 2, c_.reconnect_max_ms);
    }
  }
}

}  // namespace arb::feed
