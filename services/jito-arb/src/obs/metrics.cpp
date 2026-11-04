#include "obs/metrics.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstring>

namespace arb::obs {

Histogram::Histogram(std::vector<double> b) : bounds(std::move(b)), buckets(new std::atomic<uint64_t>[bounds.size() + 1]) {
  for (size_t i = 0; i <= bounds.size(); ++i) buckets[i].store(0);
}

void Histogram::observe(double x) {
  size_t i = 0;
  while (i < bounds.size() && x > bounds[i]) ++i;
  buckets[i].fetch_add(1, std::memory_order_relaxed);
  count.fetch_add(1, std::memory_order_relaxed);
  sum_milli.fetch_add(uint64_t(x * 1000.0), std::memory_order_relaxed);
}

Registry& Registry::get() {
  static Registry r;
  return r;
}

Counter& Registry::counter(const std::string& name, const std::string& help) {
  std::lock_guard<std::mutex> g(mu_);
  for (auto& e : entries_)
    if (e->name == name && e->c) return *e->c;
  auto e = std::make_unique<Entry>();
  e->name = name;
  e->help = help;
  e->type = "counter";
  e->c = std::make_unique<Counter>();
  Counter& c = *e->c;
  entries_.push_back(std::move(e));
  return c;
}

Gauge& Registry::gauge(const std::string& name, const std::string& help) {
  std::lock_guard<std::mutex> g(mu_);
  for (auto& e : entries_)
    if (e->name == name && e->g) return *e->g;
  auto e = std::make_unique<Entry>();
  e->name = name;
  e->help = help;
  e->type = "gauge";
  e->g = std::make_unique<Gauge>();
  Gauge& r = *e->g;
  entries_.push_back(std::move(e));
  return r;
}

Histogram& Registry::histogram(const std::string& name, const std::string& help, std::vector<double> bounds) {
  std::lock_guard<std::mutex> g(mu_);
  for (auto& e : entries_)
    if (e->name == name && e->h) return *e->h;
  auto e = std::make_unique<Entry>();
  e->name = name;
  e->help = help;
  e->type = "histogram";
  e->h = std::make_unique<Histogram>(std::move(bounds));
  Histogram& r = *e->h;
  entries_.push_back(std::move(e));
  return r;
}

std::string Registry::render() const {
  std::lock_guard<std::mutex> g(mu_);
  std::string out;
  char buf[256];
  for (auto& e : entries_) {
    out += "# HELP " + e->name + " " + e->help + "\n# TYPE " + e->name + " " + e->type + "\n";
    if (e->c) {
      std::snprintf(buf, sizeof buf, "%s %llu\n", e->name.c_str(), (unsigned long long)e->c->v.load());
      out += buf;
    } else if (e->g) {
      std::snprintf(buf, sizeof buf, "%s %lld\n", e->name.c_str(), (long long)e->g->v.load());
      out += buf;
    } else if (e->h) {
      uint64_t cum = 0;
      for (size_t i = 0; i <= e->h->bounds.size(); ++i) {
        cum += e->h->buckets[i].load();
        if (i < e->h->bounds.size())
          std::snprintf(buf, sizeof buf, "%s_bucket{le=\"%g\"} %llu\n", e->name.c_str(), e->h->bounds[i], (unsigned long long)cum);
        else
          std::snprintf(buf, sizeof buf, "%s_bucket{le=\"+Inf\"} %llu\n", e->name.c_str(), (unsigned long long)cum);
        out += buf;
      }
      std::snprintf(buf, sizeof buf, "%s_sum %.3f\n%s_count %llu\n", e->name.c_str(), double(e->h->sum_milli.load()) / 1000.0,
                    e->name.c_str(), (unsigned long long)e->h->count.load());
      out += buf;
    }
  }
  return out;
}

MetricsServer::~MetricsServer() { stop(); }

bool MetricsServer::start(const std::string& addr, int port, std::string* err) {
  fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd_ < 0) {
    if (err) *err = "socket";
    return false;
  }
  int one = 1;
  ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  sockaddr_in sa{};
  sa.sin_family = AF_INET;
  sa.sin_port = htons(uint16_t(port));
  if (::inet_pton(AF_INET, addr.c_str(), &sa.sin_addr) != 1 || ::bind(fd_, reinterpret_cast<sockaddr*>(&sa), sizeof sa) != 0 ||
      ::listen(fd_, 16) != 0) {
    if (err) *err = std::string("bind/listen ") + addr + ":" + std::to_string(port) + ": " + std::strerror(errno);
    ::close(fd_);
    fd_ = -1;
    return false;
  }
  socklen_t len = sizeof sa;
  ::getsockname(fd_, reinterpret_cast<sockaddr*>(&sa), &len);
  port_ = ntohs(sa.sin_port);
  stop_ = false;
  th_ = std::thread([this] { loop(); });
  return true;
}

void MetricsServer::stop() {
  stop_ = true;
  if (th_.joinable()) th_.join();
  if (fd_ >= 0) ::close(fd_);
  fd_ = -1;
}

void MetricsServer::loop() {
  while (!stop_) {
    pollfd p{fd_, POLLIN, 0};
    if (::poll(&p, 1, 200) <= 0) continue;
    int c = ::accept(fd_, nullptr, nullptr);
    if (c < 0) continue;
    char req[2048];
    ssize_t n = ::recv(c, req, sizeof req - 1, 0);
    std::string body, status = "200 OK";
    if (n > 0) {
      req[n] = 0;
      if (std::strncmp(req, "GET /metrics", 12) == 0) body = Registry::get().render();
      else { status = "404 Not Found"; body = "not found\n"; }
    }
    std::string resp = "HTTP/1.0 " + status + "\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: " +
                       std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n" + body;
    size_t off = 0;
    while (off < resp.size()) {
      ssize_t w = ::send(c, resp.data() + off, resp.size() - off, 0);
      if (w <= 0) break;
      off += size_t(w);
    }
    ::close(c);
  }
}

}  // namespace arb::obs
