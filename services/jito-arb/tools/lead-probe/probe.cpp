// Lead-time probe (shadow, read-only): how much earlier does Helius preprocessedSubscribe (pre-execution, WebSocket)
// show a transaction on our pools than LaserStream gRPC at processed commitment (what the bot trades on today)?
// Both feeds are filtered to the same accounts (LEAD_ACCOUNTS_FILE, one address per line) and joined by signature in
// one process, so the lead is measured on one clock. Nothing is signed or sent; no state is shared with the bot.
//
//   env: HELIUS_API_KEY, LASERSTREAM_ENDPOINT, LEAD_ACCOUNTS_FILE, LEAD_SECONDS (default 1800), LEAD_OUT (JSON path)
//
// Prints a line every 60 s and a summary at the end. Uses one LaserStream connection and one WebSocket connection.
#include <grpcpp/grpcpp.h>
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "geyser.grpc.pb.h"

namespace {

using Clock = std::chrono::steady_clock;
int64_t now_us() { return std::chrono::duration_cast<std::chrono::microseconds>(Clock::now().time_since_epoch()).count(); }

struct Seen {
  int64_t ws_us = 0, grpc_us = 0;
  uint64_t ws_slot = 0, grpc_slot = 0;
};

std::mutex mu;
std::unordered_map<std::string, Seen> seen;  // key: raw 64-byte signature
std::atomic<bool> stop{false};
std::atomic<uint64_t> ws_msgs{0}, grpc_msgs{0}, ws_bad{0};
std::atomic<int> ws_state{0};  // 0 connecting, 1 subscribed, -1 failed

void note(const std::string& sig, bool ws, uint64_t slot) {
  int64_t t = now_us();
  std::lock_guard<std::mutex> g(mu);
  Seen& s = seen[sig];
  if (ws && !s.ws_us) { s.ws_us = t; s.ws_slot = slot; }
  if (!ws && !s.grpc_us) { s.grpc_us = t; s.grpc_slot = slot; }
}

// ---- minimal WebSocket client over OpenSSL (RFC 6455: client frames masked) ----
struct Ws {
  int fd = -1;
  SSL_CTX* ctx = nullptr;
  SSL* ssl = nullptr;
  std::string buf;
  bool connect(const std::string& host, const std::string& path) {
    addrinfo hints{}, *res = nullptr;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host.c_str(), "443", &hints, &res) != 0) return false;
    for (addrinfo* a = res; a; a = a->ai_next) {
      fd = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
      if (fd >= 0 && ::connect(fd, a->ai_addr, a->ai_addrlen) == 0) break;
      if (fd >= 0) ::close(fd);
      fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) return false;
    ctx = SSL_CTX_new(TLS_client_method());
    SSL_CTX_set_default_verify_paths(ctx);
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, nullptr);
    ssl = SSL_new(ctx);
    SSL_set_tlsext_host_name(ssl, host.c_str());
    SSL_set1_host(ssl, host.c_str());
    SSL_set_fd(ssl, fd);
    if (SSL_connect(ssl) != 1) return false;
    unsigned char key[16];
    RAND_bytes(key, 16);
    static const char* b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string k;
    for (int i = 0; i < 16; i += 3) {
      uint32_t v = key[i] << 16 | (i + 1 < 16 ? key[i + 1] << 8 : 0) | (i + 2 < 16 ? key[i + 2] : 0);
      k += b64[(v >> 18) & 63];
      k += b64[(v >> 12) & 63];
      k += i + 1 < 16 ? b64[(v >> 6) & 63] : '=';
      k += i + 2 < 16 ? b64[v & 63] : '=';
    }
    std::string req = "GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + k +
                      "\r\nSec-WebSocket-Version: 13\r\n\r\n";
    if (SSL_write(ssl, req.data(), int(req.size())) <= 0) return false;
    char tmp[4096];
    while (buf.find("\r\n\r\n") == std::string::npos) {
      int n = SSL_read(ssl, tmp, sizeof tmp);
      if (n <= 0) return false;
      buf.append(tmp, size_t(n));
    }
    if (buf.rfind("HTTP/1.1 101", 0) != 0) {
      std::fprintf(stderr, "ws: upgrade refused: %s\n", buf.substr(0, buf.find("\r\n")).c_str());
      return false;
    }
    buf.erase(0, buf.find("\r\n\r\n") + 4);
    return true;
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
  // Next complete frame: opcode and payload. False on close or error.
  bool next(int& opcode, std::string& payload) {
    char tmp[65536];
    for (;;) {
      if (buf.size() >= 2) {
        const unsigned char* b = reinterpret_cast<const unsigned char*>(buf.data());
        uint64_t len = b[1] & 127;
        size_t hdr = 2;
        if (len == 126) { if (buf.size() < 4) goto more; len = uint64_t(b[2]) << 8 | b[3]; hdr = 4; }
        else if (len == 127) { if (buf.size() < 10) goto more; len = 0; for (int i = 0; i < 8; ++i) len = len << 8 | b[2 + i]; hdr = 10; }
        if (b[1] & 128) hdr += 4;  // servers do not mask, but tolerate it
        if (buf.size() >= hdr + len) {
          opcode = b[0] & 15;
          payload.assign(buf, hdr, size_t(len));
          buf.erase(0, hdr + size_t(len));
          return true;
        }
      }
    more:
      int n = SSL_read(ssl, tmp, sizeof tmp);
      if (n <= 0) return false;
      buf.append(tmp, size_t(n));
    }
  }
  void close() {
    if (ssl) { SSL_shutdown(ssl); SSL_free(ssl); ssl = nullptr; }
    if (ctx) { SSL_CTX_free(ctx); ctx = nullptr; }
    if (fd >= 0) { ::close(fd); fd = -1; }
  }
};

void run_ws(const std::string& key, const std::vector<std::string>& accounts) {
  Ws ws;
  if (!ws.connect("beta.helius-rpc.com", "/?api-key=" + key)) { ws_state = -1; return; }
  std::string req = R"({"jsonrpc":"2.0","id":1,"method":"preprocessedSubscribe","params":{"accountInclude":[)";
  for (size_t i = 0; i < accounts.size(); ++i) req += (i ? ",\"" : "\"") + accounts[i] + "\"";
  req += "]}}";
  if (!ws.send(1, req)) { ws_state = -1; return; }
  auto last_ping = Clock::now();
  std::thread pinger([&] {
    while (!stop) {
      std::this_thread::sleep_for(std::chrono::seconds(1));
      if (Clock::now() - last_ping > std::chrono::seconds(20)) { last_ping = Clock::now(); ws.send(9, "p"); }
    }
  });
  int op;
  std::string p;
  while (!stop && ws.next(op, p)) {
    if (op == 1) {
      if (p.find("\"result\"") != std::string::npos) ws_state = 1;
      else std::fprintf(stderr, "ws text: %.300s\n", p.c_str());
    } else if (op == 2) {
      // version (1) | slot u64 LE (8) | first signature (64) | transaction
      if (p.size() < 73 || uint8_t(p[0]) != 1) { ++ws_bad; continue; }
      uint64_t slot;
      std::memcpy(&slot, p.data() + 1, 8);
      ++ws_msgs;
      note(p.substr(9, 64), true, slot);
    } else if (op == 9) {
      ws.send(10, p);
    } else if (op == 8) {
      std::fprintf(stderr, "ws: server closed (code %u)\n", p.size() >= 2 ? unsigned(uint8_t(p[0]) << 8 | uint8_t(p[1])) : 0u);
      break;
    }
  }
  if (ws_state != 1) ws_state = -1;
  stop = true;
  pinger.join();
  ws.close();
}

void run_grpc(const std::string& endpoint, const std::string& key, const std::vector<std::string>& accounts) {
  std::string target = endpoint;
  if (target.rfind("https://", 0) == 0) target.erase(0, 8);
  while (!target.empty() && target.back() == '/') target.pop_back();
  if (target.find(':') == std::string::npos) target += ":443";
  grpc::ChannelArguments args;
  args.SetInt(GRPC_ARG_MAX_RECEIVE_MESSAGE_LENGTH, 64 << 20);
  auto stub = geyser::Geyser::NewStub(grpc::CreateCustomChannel(target, grpc::SslCredentials({}), args));
  grpc::ClientContext ctx;
  ctx.AddMetadata("x-token", key);
  auto stream = stub->Subscribe(&ctx);
  geyser::SubscribeRequest req;
  req.set_commitment(geyser::PROCESSED);
  auto& f = (*req.mutable_transactions_status())["lead"];
  f.set_vote(false);
  for (const auto& a : accounts) f.add_account_include(a);
  if (!stream->Write(req)) { std::fprintf(stderr, "grpc: subscribe write failed\n"); stop = true; return; }
  std::thread pinger([&] {
    int id = 0;
    while (!stop) {
      std::this_thread::sleep_for(std::chrono::seconds(10));
      geyser::SubscribeRequest ping;
      ping.mutable_ping()->set_id(++id);
      if (!stream->Write(ping)) break;
    }
  });
  geyser::SubscribeUpdate u;
  while (!stop && stream->Read(&u)) {
    if (u.has_transaction_status()) {
      ++grpc_msgs;
      note(u.transaction_status().signature(), false, u.transaction_status().slot());
    }
  }
  stop = true;
  ctx.TryCancel();
  pinger.join();
  auto st = stream->Finish();
  if (st.error_code() != grpc::StatusCode::CANCELLED && st.error_code() != grpc::StatusCode::OK)
    std::fprintf(stderr, "grpc: stream ended with code %d\n", int(st.error_code()));
}

struct Report {
  size_t both = 0, ws_only = 0, grpc_only = 0, ws_first = 0;
  std::vector<double> lead_ms;  // grpc time minus ws time (positive: the pre-execution feed was earlier)
  double pct(double p) const {
    if (lead_ms.empty()) return 0;
    std::vector<double> v = lead_ms;
    std::sort(v.begin(), v.end());
    return v[size_t(p * double(v.size() - 1))];
  }
};

Report report(int64_t settle_us) {
  Report r;
  int64_t t = now_us();
  std::lock_guard<std::mutex> g(mu);
  for (const auto& [sig, s] : seen) {
    // Only judge entries old enough for the other feed to have caught up.
    if (std::max(s.ws_us, s.grpc_us) > t - settle_us) continue;
    if (s.ws_us && s.grpc_us) {
      ++r.both;
      r.lead_ms.push_back(double(s.grpc_us - s.ws_us) / 1000.0);
      if (s.ws_us < s.grpc_us) ++r.ws_first;
    } else if (s.ws_us) ++r.ws_only;
    else ++r.grpc_only;
  }
  return r;
}

}  // namespace

int main() {
  const char* key = std::getenv("HELIUS_API_KEY");
  const char* endpoint = std::getenv("LASERSTREAM_ENDPOINT");
  const char* file = std::getenv("LEAD_ACCOUNTS_FILE");
  if (!key || !*key || !endpoint || !*endpoint || !file) {
    std::fprintf(stderr, "need HELIUS_API_KEY, LASERSTREAM_ENDPOINT, LEAD_ACCOUNTS_FILE\n");
    return 2;
  }
  int seconds = std::getenv("LEAD_SECONDS") ? std::atoi(std::getenv("LEAD_SECONDS")) : 1800;
  std::vector<std::string> accounts;
  std::ifstream in(file);
  for (std::string line; std::getline(in, line);)
    if (!line.empty()) accounts.push_back(line);
  if (accounts.empty() || accounts.size() > 5000) {
    std::fprintf(stderr, "accounts file must list 1..5000 addresses\n");
    return 2;
  }
  SSL_library_init();
  std::thread ws([&] { run_ws(key, accounts); });
  std::thread gr([&] { run_grpc(endpoint, key, accounts); });
  auto t0 = Clock::now();
  int minute = 0;
  while (!stop && Clock::now() - t0 < std::chrono::seconds(seconds)) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    if (Clock::now() - t0 >= std::chrono::seconds(60 * (minute + 1))) {
      ++minute;
      Report r = report(5'000'000);
      std::printf("[%3d min] ws=%s ws_msgs=%llu grpc_msgs=%llu both=%zu ws_first=%zu lead_ms p10=%.1f p50=%.1f p90=%.1f ws_only=%zu grpc_only=%zu\n",
                  minute, ws_state == 1 ? "up" : ws_state == 0 ? "connecting" : "FAILED", (unsigned long long)ws_msgs.load(),
                  (unsigned long long)grpc_msgs.load(), r.both, r.ws_first, r.pct(.1), r.pct(.5), r.pct(.9), r.ws_only, r.grpc_only);
      std::fflush(stdout);
    }
  }
  stop = true;
  ws.detach();  // blocked in SSL_read; the process exits below
  gr.join();
  Report r = report(0);
  std::printf("SUMMARY accounts=%zu seconds=%lld ws_msgs=%llu grpc_msgs=%llu ws_bad=%llu both=%zu ws_first=%zu (%.0f%%) "
              "lead_ms p10=%.1f p25=%.1f p50=%.1f p75=%.1f p90=%.1f ws_only=%zu grpc_only=%zu\n",
              accounts.size(), (long long)std::chrono::duration_cast<std::chrono::seconds>(Clock::now() - t0).count(),
              (unsigned long long)ws_msgs.load(), (unsigned long long)grpc_msgs.load(), (unsigned long long)ws_bad.load(), r.both,
              r.ws_first, r.both ? 100.0 * double(r.ws_first) / double(r.both) : 0.0, r.pct(.1), r.pct(.25), r.pct(.5), r.pct(.75),
              r.pct(.9), r.ws_only, r.grpc_only);
  if (const char* out = std::getenv("LEAD_OUT")) {
    std::ofstream o(out);
    o << "{\"accounts\":" << accounts.size() << ",\"both\":" << r.both << ",\"ws_first\":" << r.ws_first << ",\"ws_only\":" << r.ws_only
      << ",\"grpc_only\":" << r.grpc_only << ",\"lead_ms\":{\"p10\":" << r.pct(.1) << ",\"p25\":" << r.pct(.25) << ",\"p50\":" << r.pct(.5)
      << ",\"p75\":" << r.pct(.75) << ",\"p90\":" << r.pct(.9) << "}}\n";
  }
  std::fflush(stdout);
  std::_Exit(r.both ? 0 : 1);
}
