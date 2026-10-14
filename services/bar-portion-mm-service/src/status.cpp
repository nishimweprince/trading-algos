#include "bpmm/status.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

namespace bpmm {

StatusServer::StatusServer(std::uint16_t port, Snapshot snapshot)
    : port_(port), snapshot_(std::move(snapshot)) {}
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
      const auto health = line.starts_with("GET /health/live ") || line.starts_with("GET /health/ready ") ||
                          line.starts_with("GET /metrics ");
      const auto body = health ? snapshot_() : std::string{"{\"error\":\"not_found\"}\n"};
      const auto status = health ? "200 OK" : "404 Not Found";
      const auto response = std::string{"HTTP/1.1 "} + status + "\r\nContent-Type: application/json\r\n" +
                            "Connection: close\r\nContent-Length: " + std::to_string(body.size()) +
                            "\r\n\r\n" + body;
      ::send(client, response.data(), response.size(), MSG_NOSIGNAL);
    }
    ::close(client);
  }
}

}  // namespace bpmm
