#include "bpmm/status.hpp"

#include "bpmm/dashboard.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>
#include <string_view>

namespace bpmm {
namespace {

std::string_view request_path(std::string_view line) {
  constexpr std::string_view prefix = "GET ";
  if (!line.starts_with(prefix)) return {};
  line.remove_prefix(prefix.size());
  const auto end = line.find(' ');
  if (end != std::string_view::npos) line = line.substr(0, end);
  const auto query = line.find('?');
  if (query != std::string_view::npos) line = line.substr(0, query);
  return line;
}

}  // namespace

StatusServer::StatusServer(std::uint16_t port, Snapshot snapshot, std::string journal_path)
    : port_(port), snapshot_(std::move(snapshot)), journal_path_(std::move(journal_path)) {}
StatusServer::~StatusServer() { stop(); }

bool StatusServer::start(std::string& error) {
  socket_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (socket_ < 0) { error = std::strerror(errno); return false; }
  int enabled = 1;
  setsockopt(socket_, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port_);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (::bind(socket_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
      ::listen(socket_, 8) < 0) {
    error = std::strerror(errno);
    ::close(socket_);
    socket_ = -1;
    return false;
  }
  thread_ = std::thread([this] { serve(); });
  return true;
}

void StatusServer::stop() {
  stop_.store(true, std::memory_order_relaxed);
  if (socket_ >= 0) {
    ::shutdown(socket_, SHUT_RDWR);
    ::close(socket_);
    socket_ = -1;
  }
  if (thread_.joinable()) thread_.join();
}

void StatusServer::serve() {
  while (!stop_.load(std::memory_order_relaxed)) {
    const auto client = ::accept(socket_, nullptr, nullptr);
    if (client < 0) continue;
    std::array<char, 2048> request{};
    const auto count = ::recv(client, request.data(), request.size() - 1, 0);
    if (count > 0) {
      const std::string_view line(request.data(), static_cast<std::size_t>(count));
      const auto path = request_path(line);
      std::string body;
      std::string_view content_type = "application/json";
      std::string_view status = "200 OK";
      if (path == "/") {
        body = dashboard_page();
        content_type = "text/html; charset=utf-8";
      } else if (path == "/api/activity") {
        body = activity_json(read_journal_tail(journal_path_));
      } else if (path == "/api/summary" || path == "/health/live" || path == "/health/ready" ||
                 path == "/metrics") {
        body = snapshot_();
      } else {
        body = "{\"error\":\"not_found\"}\n";
        status = "404 Not Found";
      }
      const auto response = std::string{"HTTP/1.1 "} + std::string(status) +
                            "\r\nContent-Type: " + std::string(content_type) +
                            "\r\nConnection: close\r\nContent-Length: " + std::to_string(body.size()) +
                            "\r\n\r\n" + body;
      ::send(client, response.data(), response.size(), MSG_NOSIGNAL);
    }
    ::close(client);
  }
}

}  // namespace bpmm
