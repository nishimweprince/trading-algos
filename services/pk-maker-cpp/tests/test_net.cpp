#include <arpa/inet.h>
#include <netinet/in.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/sha.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <thread>

#include "check.hpp"
#include "pkm/crypto.hpp"
#include "pkm/http.hpp"
#include "pkm/ws.hpp"

using namespace pkm;
using pkm::test::require;
using pkm::test::require_eq;

namespace {

// One accepted connection, plain or TLS, with blocking reads and writes.
class Peer final {
 public:
  Peer(int fd, SSL* ssl) : fd_(fd), ssl_(ssl) {}
  ~Peer() {
    if (ssl_ != nullptr) SSL_free(ssl_);
    ::close(fd_);
  }
  std::string read_some() {
    char buf[8192];
    const int n = ssl_ != nullptr ? SSL_read(ssl_, buf, sizeof(buf)) : static_cast<int>(::recv(fd_, buf, sizeof(buf), 0));
    return n > 0 ? std::string(buf, static_cast<std::size_t>(n)) : std::string();
  }
  std::string read_until(std::string_view end) {
    while (data_.find(end) == std::string::npos) {
      const auto more = read_some();
      if (more.empty()) break;
      data_ += more;
    }
    const auto at = data_.find(end);
    if (at == std::string::npos) return {};
    std::string out = data_.substr(0, at + end.size());
    data_.erase(0, at + end.size());
    return out;
  }
  std::string read_exact(std::size_t n) {
    while (data_.size() < n) {
      const auto more = read_some();
      if (more.empty()) break;
      data_ += more;
    }
    std::string out = data_.substr(0, n);
    data_.erase(0, std::min(n, data_.size()));
    return out;
  }
  void write(std::string_view s) {
    if (ssl_ != nullptr) SSL_write(ssl_, s.data(), static_cast<int>(s.size()));
    else ::send(fd_, s.data(), s.size(), MSG_NOSIGNAL);
  }

 private:
  int fd_;
  SSL* ssl_;
  std::string data_;
};

class Server final {
 public:
  explicit Server(SSL_CTX* tls = nullptr) : tls_(tls) {
    fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    const int one = 1;
    ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    ::listen(fd_, 4);
    socklen_t len = sizeof(addr);
    ::getsockname(fd_, reinterpret_cast<sockaddr*>(&addr), &len);
    port_ = ntohs(addr.sin_port);
  }
  ~Server() {
    if (thread_.joinable()) thread_.join();
    ::close(fd_);
  }
  void run(std::function<void(Peer&)> script, int connections = 1) {
    thread_ = std::thread([this, script = std::move(script), connections] {
      for (int i = 0; i < connections; ++i) {
        const int c = ::accept(fd_, nullptr, nullptr);
        SSL* ssl = nullptr;
        if (tls_ != nullptr) {
          ssl = SSL_new(tls_);
          SSL_set_fd(ssl, c);
          if (SSL_accept(ssl) != 1) {
            SSL_free(ssl);
            ::close(c);
            continue;
          }
        }
        Peer peer(c, ssl);
        script(peer);
      }
    });
  }
  void join() { thread_.join(); }
  [[nodiscard]] std::uint16_t port() const { return port_; }

 private:
  int fd_ = -1;
  std::uint16_t port_ = 0;
  SSL_CTX* tls_;
  std::thread thread_;
};

// A self-signed certificate for 127.0.0.1/localhost; the client trusts it via PKM_TLS_CA.
SSL_CTX* test_tls_server() {
  static SSL_CTX* ctx = [] {
    EVP_PKEY* key = EVP_PKEY_Q_keygen(nullptr, nullptr, "EC", "P-256");
    X509* cert = X509_new();
    ASN1_INTEGER_set(X509_get_serialNumber(cert), 1);
    X509_gmtime_adj(X509_getm_notBefore(cert), -60);
    X509_gmtime_adj(X509_getm_notAfter(cert), 3600);
    X509_set_pubkey(cert, key);
    X509_NAME* name = X509_get_subject_name(cert);
    X509_NAME_add_entry_by_txt(name, "CN", MBSTRING_ASC, reinterpret_cast<const unsigned char*>("localhost"), -1, -1, 0);
    X509_set_issuer_name(cert, name);
    X509V3_CTX v3;
    X509V3_set_ctx_nodb(&v3);
    X509V3_set_ctx(&v3, cert, cert, nullptr, nullptr, 0);
    for (const char* ext : {"basicConstraints=critical,CA:TRUE", "subjectAltName=DNS:localhost,IP:127.0.0.1"}) {
      const std::string spec(ext);
      const auto eq = spec.find('=');
      X509_EXTENSION* e = X509V3_EXT_conf(nullptr, &v3, spec.substr(0, eq).c_str(), spec.substr(eq + 1).c_str());
      X509_add_ext(cert, e, -1);
      X509_EXTENSION_free(e);
    }
    X509_sign(cert, key, EVP_sha256());
    const auto path = std::filesystem::temp_directory_path() / ("pkm-test-ca-" + std::to_string(::getpid()) + ".pem");
    FILE* f = std::fopen(path.c_str(), "w");
    PEM_write_X509(f, cert);
    std::fclose(f);
    ::setenv("PKM_TLS_CA", path.c_str(), 1);
    SSL_CTX* c = SSL_CTX_new(TLS_server_method());
    SSL_CTX_use_certificate(c, cert);
    SSL_CTX_use_PrivateKey(c, key);
    X509_free(cert);
    EVP_PKEY_free(key);
    return c;
  }();
  return ctx;
}

std::string ws_accept(std::string_view key) {
  std::string input(key);
  input += "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
  unsigned char digest[SHA_DIGEST_LENGTH];
  SHA1(reinterpret_cast<const unsigned char*>(input.data()), input.size(), digest);
  return base64_encode(digest, sizeof(digest), false);
}

std::string server_frame(std::uint8_t first, std::string_view payload) {
  std::string f;
  f.push_back(static_cast<char>(first));
  if (payload.size() < 126) {
    f.push_back(static_cast<char>(payload.size()));
  } else {
    f.push_back(126);
    f.push_back(static_cast<char>(payload.size() >> 8));
    f.push_back(static_cast<char>(payload.size() & 0xFF));
  }
  return f + std::string(payload);
}

// Reads one masked client frame: (opcode, payload).
std::pair<int, std::string> client_frame(Peer& peer) {
  const auto head = peer.read_exact(2);
  if (head.size() < 2) return {-1, {}};
  std::size_t len = static_cast<std::uint8_t>(head[1]) & 0x7F;
  if (len == 126) {
    const auto ext = peer.read_exact(2);
    len = (static_cast<std::size_t>(static_cast<std::uint8_t>(ext[0])) << 8) | static_cast<std::uint8_t>(ext[1]);
  }
  const auto mask = peer.read_exact(4);
  auto payload = peer.read_exact(len);
  for (std::size_t i = 0; i < payload.size(); ++i) payload[i] = static_cast<char>(payload[i] ^ mask[i % 4]);
  return {static_cast<std::uint8_t>(head[0]) & 0x0F, payload};
}

void http_script(Peer& peer) {
  const auto first = peer.read_until("\r\n\r\n");
  const auto body = peer.read_exact(std::stoul(first.substr(first.find("Content-Length: ") + 16)));
  peer.write("HTTP/1.1 200 OK\r\nContent-Length: " + std::to_string(body.size() + 5) + "\r\n\r\necho:" + body);
  peer.read_until("\r\n\r\n");
  peer.write("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nchun\r\n3\r\nked\r\n0\r\n\r\n");
  peer.read_until("\r\n\r\n");
  peer.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 2\r\n\r\nno");
}

void run_http(bool tls) {
  Server server(tls ? test_tls_server() : nullptr);
  server.run(http_script);
  http::Client client(std::string(tls ? "https" : "http") + "://localhost:" + std::to_string(server.port()), "pkm-test", 3000);
  auto r = client.request("POST", "/order", "X-Test: 1\r\n", R"({"a":1})");
  require_eq(r.status, 200, "status");
  require_eq(r.body, std::string(R"(echo:{"a":1})"), "content-length body");
  r = client.get("/chunked");
  require_eq(r.body, std::string("chunked"), "chunked body on the same connection");
  r = client.get("/missing");
  require(r.status == 404 && r.body == "no", "error status");
  server.join();
}

}  // namespace

PKM_TEST(url_parsing) {
  net::Url u;
  require(net::parse_url("wss://ws-subscriptions-clob.polymarket.com/ws/market", u), "wss");
  require(u.tls && u.port == 443 && u.host == "ws-subscriptions-clob.polymarket.com" && u.path == "/ws/market", "wss parts");
  require(net::parse_url("http://127.0.0.1:8080", u) && !u.tls && u.port == 8080 && u.path == "/", "http with port");
  require(!net::parse_url("ftp://x", u), "unknown scheme");
}

PKM_TEST(response_parser_pieces) {
  http::ResponseParser p;
  const std::string text = "HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-A: b\r\n\r\nhello";
  for (std::size_t i = 0; i + 1 < text.size(); ++i) require(!p.feed(&text[i], 1), "incomplete");
  require(p.feed(&text.back(), 1) && p.body() == "hello" && p.header("x-a") == "b", "byte by byte");
  p.reset();
  require(p.feed("HTTP/1.1 200 OK\r\n\r\npartial", 26) == false, "read to close");
  require(p.finish_on_close() && p.body() == "partial" && !p.keep_alive(), "completes on close");
  p.reset();
  const std::string two = "HTTP/1.1 204 No Content\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx";
  require(p.feed(two.data(), two.size()) && p.status() == 204, "first pipelined");
  p.reset();
  require(p.feed("", 0) && p.body() == "x", "second from the leftover");
}

PKM_TEST(http_client_plain_and_tls) {
  run_http(false);
  run_http(true);
}

PKM_TEST(http_channel_one_in_flight) {
  Server server(test_tls_server());
  server.run([](Peer& peer) {
    for (int i = 0; i < 2; ++i) {
      peer.read_until("\r\n\r\n");
      peer.write("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok");
    }
  });
  net::Url url;
  net::parse_url("https://localhost:" + std::to_string(server.port()), url);
  http::Channel channel(url);
  std::string error;
  require(channel.connect(error), error);
  for (int i = 0; i < 2; ++i) {
    std::string request;
    http::write_request(request, "GET", url.host, "/time", "", "");
    require(channel.send(request) && channel.busy(), "sent");
    require(!channel.send(request), "a second request waits");
    int r = 0;
    for (int spin = 0; spin < 2'000'000 && r == 0; ++spin) r = channel.poll();
    require(r == 1 && channel.response().body() == "ok", "response");
    channel.release();
    require(!channel.busy(), "released");
  }
  server.join();
}

PKM_TEST(websocket_frames) {
  Server server(test_tls_server());
  std::string got_text;
  int got_pong = 0;
  server.run([&](Peer& peer) {
    const auto head = peer.read_until("\r\n\r\n");
    const auto k = head.find("Sec-WebSocket-Key: ") + 19;
    const auto key = head.substr(k, head.find("\r\n", k) - k);
    require(head.find("X-Auth: yes\r\n") != std::string::npos, "extra handshake header");
    peer.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " +
               ws_accept(key) + "\r\n\r\n" + server_frame(0x81, "hello"));
    peer.write(server_frame(0x01, "frag") + server_frame(0x89, "p") + server_frame(0x80, "mented"));
    peer.write(server_frame(0x81, std::string(300, 'x')));
    for (int i = 0; i < 2; ++i) {
      auto [op, payload] = client_frame(peer);
      if (op == 0xA) ++got_pong;
      if (op == 0x1) got_text = payload;
    }
    peer.write(server_frame(0x88, "\x03\xe8"));
  });
  ws::Client client;
  net::Url url;
  net::parse_url("wss://localhost:" + std::to_string(server.port()) + "/ws", url);
  std::string error;
  require(client.connect(url, "X-Auth: yes\r\n", error), error);
  std::vector<std::string> messages;
  for (int spin = 0; spin < 5'000'000 && messages.size() < 3; ++spin)
    client.poll([&](std::string_view m) { messages.emplace_back(m); });
  require(messages.size() == 3, "three messages");
  require_eq(messages[0], std::string("hello"), "first message (sent with the handshake reply)");
  require_eq(messages[1], std::string("fragmented"), "fragments joined around a ping");
  require(messages[2] == std::string(300, 'x'), "16-bit length");
  require(client.send_text(R"({"type":"market"})"), "send text");
  bool alive = true;
  for (int spin = 0; spin < 5'000'000 && alive; ++spin) alive = client.poll([](std::string_view) {});
  require(!alive && client.close_code() == 1000, "closed by the server with 1000");
  server.join();
  require(got_pong == 1, "ping answered with a pong");
  require_eq(got_text, std::string(R"({"type":"market"})"), "client frame masked correctly");
}
