#include "pkm/net.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <charconv>
#include <cstdlib>
#include <cstring>
#include <ctime>

namespace pkm::net {
namespace {

SSL_CTX* client_context() {
  static SSL_CTX* const ctx = [] {
    SSL_CTX* c = SSL_CTX_new(TLS_client_method());
    SSL_CTX_set_min_proto_version(c, TLS1_2_VERSION);
    SSL_CTX_set_verify(c, SSL_VERIFY_PEER, nullptr);
    if (const char* file = std::getenv("SSL_CERT_FILE"); file != nullptr && *file != '\0')
      SSL_CTX_load_verify_locations(c, file, nullptr);
    SSL_CTX_set_default_verify_paths(c);
    SSL_CTX_set_mode(c, SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
    // Tests point PKM_TLS_CA at a self-signed CA.
    if (const char* ca = std::getenv("PKM_TLS_CA"); ca != nullptr && *ca != '\0') SSL_CTX_load_verify_locations(c, ca, nullptr);
    return c;
  }();
  return ctx;
}

std::string ssl_error_text() {
  const unsigned long code = ERR_get_error();
  if (code == 0) return "tls error";
  char buf[256];
  ERR_error_string_n(code, buf, sizeof(buf));
  return buf;
}

bool wait_fd(int fd, short events, int timeout_ms) {
  pollfd p{fd, events, 0};
  int r = 0;
  do {
    r = ::poll(&p, 1, timeout_ms);
  } while (r < 0 && errno == EINTR);
  return r > 0;
}

}  // namespace

bool parse_url(std::string_view text, Url& out) {
  std::size_t scheme_len = 0;
  if (text.starts_with("https://")) {
    out.tls = true;
    out.port = 443;
    scheme_len = 8;
  } else if (text.starts_with("wss://")) {
    out.tls = true;
    out.port = 443;
    scheme_len = 6;
  } else if (text.starts_with("http://")) {
    out.tls = false;
    out.port = 80;
    scheme_len = 7;
  } else if (text.starts_with("ws://")) {
    out.tls = false;
    out.port = 80;
    scheme_len = 5;
  } else {
    return false;
  }
  text.remove_prefix(scheme_len);
  const auto slash = text.find('/');
  std::string_view authority = text.substr(0, slash);
  out.path = slash == std::string_view::npos ? "/" : std::string(text.substr(slash));
  const auto colon = authority.rfind(':');
  if (colon != std::string_view::npos) {
    unsigned port = 0;
    const auto digits = authority.substr(colon + 1);
    const auto r = std::from_chars(digits.data(), digits.data() + digits.size(), port);
    if (r.ec != std::errc{} || port == 0 || port > 65535) return false;
    out.port = static_cast<std::uint16_t>(port);
    authority = authority.substr(0, colon);
  }
  out.host = std::string(authority);
  return !out.host.empty();
}

std::int64_t now_ns() noexcept {
  timespec ts{};
  clock_gettime(CLOCK_REALTIME, &ts);
  return static_cast<std::int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
}

std::int64_t mono_ns() noexcept {
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<std::int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
}

Connection::~Connection() { close(); }

Connection::Connection(Connection&& other) noexcept : fd_(other.fd_), ssl_(other.ssl_) {
  other.fd_ = -1;
  other.ssl_ = nullptr;
}

Connection& Connection::operator=(Connection&& other) noexcept {
  if (this != &other) {
    close();
    fd_ = other.fd_;
    ssl_ = other.ssl_;
    other.fd_ = -1;
    other.ssl_ = nullptr;
  }
  return *this;
}

void Connection::close() noexcept {
  if (ssl_ != nullptr) {
    SSL_free(static_cast<SSL*>(ssl_));
    ssl_ = nullptr;
  }
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
}

bool Connection::connect(const std::string& host, std::uint16_t port, bool tls, int timeout_ms, std::string& error) {
  close();
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* result = nullptr;
  const std::string service = std::to_string(port);
  if (const int rc = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &result); rc != 0) {
    error = "resolve " + host + ": " + gai_strerror(rc);
    return false;
  }
  for (addrinfo* ai = result; ai != nullptr; ai = ai->ai_next) {
    const int fd = ::socket(ai->ai_family, ai->ai_socktype | SOCK_CLOEXEC | SOCK_NONBLOCK, ai->ai_protocol);
    if (fd < 0) continue;
    const int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    ::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    int rc = ::connect(fd, ai->ai_addr, ai->ai_addrlen);
    if (rc < 0 && errno == EINPROGRESS) {
      if (wait_fd(fd, POLLOUT, timeout_ms)) {
        int so_error = 0;
        socklen_t len = sizeof(so_error);
        ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &so_error, &len);
        rc = so_error == 0 ? 0 : -1;
        errno = so_error;
      } else {
        errno = ETIMEDOUT;
      }
    }
    if (rc == 0) {
      fd_ = fd;
      break;
    }
    error = "connect " + host + ":" + service + ": " + std::strerror(errno);
    ::close(fd);
  }
  ::freeaddrinfo(result);
  if (fd_ < 0) {
    if (error.empty()) error = "connect " + host + ": no address";
    return false;
  }
  if (!tls) return true;

  SSL* ssl = SSL_new(client_context());
  ssl_ = ssl;
  SSL_set_fd(ssl, fd_);
  SSL_set_tlsext_host_name(ssl, host.c_str());
  SSL_set1_host(ssl, host.c_str());
  const std::int64_t deadline = mono_ns() + static_cast<std::int64_t>(timeout_ms) * 1'000'000;
  while (true) {
    const int rc = SSL_connect(ssl);
    if (rc == 1) return true;
    const int err = SSL_get_error(ssl, rc);
    const int left_ms = static_cast<int>((deadline - mono_ns()) / 1'000'000);
    if ((err == SSL_ERROR_WANT_READ || err == SSL_ERROR_WANT_WRITE) && left_ms > 0) {
      wait_fd(fd_, err == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, left_ms);
      continue;
    }
    const long verify = SSL_get_verify_result(ssl);
    error = "tls " + host + ": " + (verify != X509_V_OK ? X509_verify_cert_error_string(verify) : ssl_error_text());
    close();
    return false;
  }
}

long Connection::read(char* buffer, std::size_t size) noexcept {
  if (fd_ < 0) return -1;
  if (ssl_ == nullptr) {
    const ssize_t n = ::recv(fd_, buffer, size, 0);
    if (n > 0) return static_cast<long>(n);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return 0;
    return -1;
  }
  auto* ssl = static_cast<SSL*>(ssl_);
  std::size_t got = 0;
  const int rc = SSL_read_ex(ssl, buffer, size, &got);
  if (rc == 1) return static_cast<long>(got);
  const int err = SSL_get_error(ssl, rc);
  if (err == SSL_ERROR_WANT_READ || err == SSL_ERROR_WANT_WRITE) return 0;
  ERR_clear_error();
  return -1;
}

bool Connection::write_all(std::string_view data, int timeout_ms) noexcept {
  if (fd_ < 0) return false;
  const std::int64_t deadline = mono_ns() + static_cast<std::int64_t>(timeout_ms) * 1'000'000;
  while (!data.empty()) {
    if (ssl_ == nullptr) {
      const ssize_t n = ::send(fd_, data.data(), data.size(), MSG_NOSIGNAL);
      if (n > 0) {
        data.remove_prefix(static_cast<std::size_t>(n));
        continue;
      }
      if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return false;
      const int left = static_cast<int>((deadline - mono_ns()) / 1'000'000);
      if (left <= 0 || !wait_fd(fd_, POLLOUT, left)) return false;
      continue;
    }
    auto* ssl = static_cast<SSL*>(ssl_);
    std::size_t written = 0;
    const int rc = SSL_write_ex(ssl, data.data(), data.size(), &written);
    if (rc == 1) {
      data.remove_prefix(written);
      continue;
    }
    const int err = SSL_get_error(ssl, rc);
    if (err != SSL_ERROR_WANT_READ && err != SSL_ERROR_WANT_WRITE) {
      ERR_clear_error();
      return false;
    }
    const int left = static_cast<int>((deadline - mono_ns()) / 1'000'000);
    if (left <= 0 || !wait_fd(fd_, err == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, left)) return false;
  }
  return true;
}

bool Connection::pending() const noexcept { return ssl_ != nullptr && SSL_pending(static_cast<SSL*>(ssl_)) > 0; }

bool Connection::wait_readable(int timeout_ms) const noexcept {
  if (fd_ < 0) return false;
  if (pending()) return true;
  return wait_fd(fd_, POLLIN, timeout_ms);
}

}  // namespace pkm::net
