#include "net/conn.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <mutex>
#include <stdexcept>

#include "core/sys.hpp"

namespace pd {

namespace {

SSL_CTX* tls_ctx() {
  static SSL_CTX* ctx = [] {
    SSL_CTX* c = SSL_CTX_new(TLS_client_method());
    if (!c) throw std::runtime_error("SSL_CTX_new failed");
    SSL_CTX_set_min_proto_version(c, TLS1_2_VERSION);
    SSL_CTX_set_verify(c, SSL_VERIFY_PEER, nullptr);
    SSL_CTX_set_default_verify_paths(c);
    return c;
  }();
  return ctx;
}

bool set_nonblocking(int fd, bool on) {
  int fl = fcntl(fd, F_GETFL, 0);
  if (fl < 0) return false;
  return fcntl(fd, F_SETFL, on ? (fl | O_NONBLOCK) : (fl & ~O_NONBLOCK)) == 0;
}

bool wait_fd(int fd, short events, int timeout_ms) {
  pollfd p{fd, events, 0};
  int r = ::poll(&p, 1, timeout_ms);
  return r > 0 && (p.revents & (events | POLLHUP | POLLERR));
}

std::string ssl_error() {
  char buf[256];
  ERR_error_string_n(ERR_get_error(), buf, sizeof buf);
  return buf;
}

}  // namespace

Url parse_url(const std::string& url) {
  Url u;
  std::string rest;
  if (url.rfind("https://", 0) == 0) { u.tls = true; rest = url.substr(8); }
  else if (url.rfind("http://", 0) == 0) { rest = url.substr(7); }
  else throw std::runtime_error("url must start with http:// or https://: " + url);
  size_t slash = rest.find('/');
  std::string hostport = slash == std::string::npos ? rest : rest.substr(0, slash);
  u.path = slash == std::string::npos ? "/" : rest.substr(slash);
  size_t q = hostport.find('?');
  if (q != std::string::npos) {  // "host?query" without a path
    u.path = "/" + hostport.substr(q);
    hostport = hostport.substr(0, q);
  }
  size_t colon = hostport.rfind(':');
  if (colon != std::string::npos) {
    u.host = hostport.substr(0, colon);
    u.port = std::stoi(hostport.substr(colon + 1));
  } else {
    u.host = hostport;
    u.port = u.tls ? 443 : 80;
  }
  if (u.host.empty()) throw std::runtime_error("url without host: " + url);
  return u;
}

Conn::~Conn() { close(); }

void Conn::close() {
  if (ssl_) {
    SSL_free(ssl_);
    ssl_ = nullptr;
  }
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
}

bool Conn::connect(const Url& url, int timeout_ms, std::string* err) {
  close();
  auto fail = [&](const std::string& m) {
    if (err) *err = m;
    close();
    return false;
  };
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* res = nullptr;
  int gai = getaddrinfo(url.host.c_str(), std::to_string(url.port).c_str(), &hints, &res);
  if (gai != 0) return fail(std::string("dns: ") + gai_strerror(gai));
  uint64_t deadline = now_ns() + uint64_t(timeout_ms) * 1'000'000ULL;
  for (addrinfo* ai = res; ai; ai = ai->ai_next) {
    int fd = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (fd < 0) continue;
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);  // no Nagle: packets go out at once
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
#if defined(__linux__)
    int idle = 10;
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle);
    setsockopt(fd, IPPROTO_TCP, TCP_QUICKACK, &one, sizeof one);
#endif
    set_nonblocking(fd, true);
    int rc = ::connect(fd, ai->ai_addr, ai->ai_addrlen);
    if (rc != 0 && errno != EINPROGRESS) { ::close(fd); continue; }
    int left = int((deadline - std::min(deadline, now_ns())) / 1'000'000ULL);
    if (rc != 0 && !wait_fd(fd, POLLOUT, left)) { ::close(fd); continue; }
    int soerr = 0;
    socklen_t sl = sizeof soerr;
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
    if (soerr != 0) { ::close(fd); continue; }
    fd_ = fd;
    break;
  }
  freeaddrinfo(res);
  if (fd_ < 0) return fail("connect failed: " + url.host);

  if (url.tls) {
    ssl_ = SSL_new(tls_ctx());
    SSL_set_fd(ssl_, fd_);
    SSL_set_tlsext_host_name(ssl_, url.host.c_str());
    SSL_set1_host(ssl_, url.host.c_str());
    for (;;) {
      int r = SSL_connect(ssl_);
      if (r == 1) break;
      int e = SSL_get_error(ssl_, r);
      int left = int((deadline - std::min(deadline, now_ns())) / 1'000'000ULL);
      if (left <= 0) return fail("tls handshake timeout");
      if (e == SSL_ERROR_WANT_READ) { if (!wait_fd(fd_, POLLIN, left)) return fail("tls handshake timeout"); }
      else if (e == SSL_ERROR_WANT_WRITE) { if (!wait_fd(fd_, POLLOUT, left)) return fail("tls handshake timeout"); }
      else return fail("tls: " + ssl_error());
    }
  }
  return true;
}

bool Conn::write_all(const char* data, size_t len, int timeout_ms) {
  uint64_t deadline = now_ns() + uint64_t(timeout_ms) * 1'000'000ULL;
  size_t off = 0;
  while (off < len) {
    ssize_t n;
    if (ssl_) {
      int r = SSL_write(ssl_, data + off, int(len - off));
      if (r > 0) { off += size_t(r); continue; }
      int e = SSL_get_error(ssl_, r);
      if (e != SSL_ERROR_WANT_WRITE && e != SSL_ERROR_WANT_READ) return false;
      n = 0;
    } else {
#if defined(MSG_NOSIGNAL)
      n = ::send(fd_, data + off, len - off, MSG_NOSIGNAL);
#else
      n = ::send(fd_, data + off, len - off, 0);
#endif
      if (n > 0) { off += size_t(n); continue; }
      if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return false;
    }
    int left = int((deadline - std::min(deadline, now_ns())) / 1'000'000ULL);
    if (left <= 0 || !wait_fd(fd_, POLLOUT, left)) return false;
  }
  return true;
}

ssize_t Conn::read_some(char* buf, size_t cap) {
  if (fd_ < 0) return -1;
  if (ssl_) {
    int r = SSL_read(ssl_, buf, int(cap));
    if (r > 0) return r;
    int e = SSL_get_error(ssl_, r);
    if (e == SSL_ERROR_WANT_READ || e == SSL_ERROR_WANT_WRITE) return 0;
    return -1;
  }
  ssize_t n = ::recv(fd_, buf, cap, 0);
  if (n > 0) return n;
  if (n == 0) return -1;
  return (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) ? 0 : -1;
}

bool Conn::wait_readable(int timeout_ms) {
  if (ssl_ && SSL_pending(ssl_) > 0) return true;
  return wait_fd(fd_, POLLIN, timeout_ms);
}

// ---------------------------------------------------------------------------

bool HttpResponseParser::feed(const char* data, size_t len) {
  buf_.append(data, len);
  return parse();
}

bool HttpResponseParser::next() {
  if (!done_) return false;
  buf_.erase(0, consumed_);
  status_ = 0;
  done_ = false;
  consumed_ = 0;
  body_ = {};
  decoded_.clear();
  return parse();
}

bool HttpResponseParser::parse() {
  if (done_) return true;
  size_t hdr_end = buf_.find("\r\n\r\n");
  if (hdr_end == std::string::npos) return false;
  std::string_view head(buf_.data(), hdr_end);
  // "HTTP/1.1 200 OK"
  size_t sp = head.find(' ');
  if (sp == std::string_view::npos) return false;
  status_ = std::atoi(std::string(head.substr(sp + 1, 3)).c_str());
  long content_length = -1;
  bool chunked = false;
  size_t pos = head.find("\r\n");
  while (pos != std::string_view::npos && pos < head.size()) {
    size_t next = head.find("\r\n", pos + 2);
    std::string_view line = head.substr(pos + 2, (next == std::string_view::npos ? head.size() : next) - pos - 2);
    size_t colon = line.find(':');
    if (colon != std::string_view::npos) {
      std::string name(line.substr(0, colon));
      for (auto& c : name) c = char(std::tolower(static_cast<unsigned char>(c)));
      std::string_view value = line.substr(colon + 1);
      while (!value.empty() && value.front() == ' ') value.remove_prefix(1);
      if (name == "content-length") content_length = std::atol(std::string(value).c_str());
      if (name == "transfer-encoding" && value.find("chunked") != std::string_view::npos) chunked = true;
    }
    pos = next;
  }
  size_t body_start = hdr_end + 4;
  if (chunked) {
    decoded_.clear();
    size_t p = body_start;
    for (;;) {
      size_t le = buf_.find("\r\n", p);
      if (le == std::string::npos) return false;
      long n = std::strtol(buf_.substr(p, le - p).c_str(), nullptr, 16);
      p = le + 2;
      if (n == 0) {
        size_t end = buf_.find("\r\n", p);
        if (end == std::string::npos) return false;
        consumed_ = end + 2;
        break;
      }
      if (buf_.size() < p + size_t(n) + 2) return false;
      decoded_.append(buf_, p, size_t(n));
      p += size_t(n) + 2;
    }
    body_ = decoded_;
  } else {
    size_t n = content_length < 0 ? 0 : size_t(content_length);
    if (buf_.size() < body_start + n) return false;
    body_ = std::string_view(buf_.data() + body_start, n);
    consumed_ = body_start + n;
  }
  done_ = true;
  return true;
}

void format_request(std::string& out, const char* method, const Url& url, std::string_view path, std::string_view body) {
  out.clear();
  out.append(method).append(" ").append(path).append(" HTTP/1.1\r\nHost: ").append(url.host);
  out.append("\r\nConnection: keep-alive\r\nAccept: application/json\r\n");
  if (!body.empty() || std::strcmp(method, "POST") == 0) {
    out.append("Content-Type: application/json\r\nContent-Length: ").append(std::to_string(body.size())).append("\r\n");
  }
  out.append("\r\n").append(body);
}

}  // namespace pd
