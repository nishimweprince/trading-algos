#include "pkm/ws.hpp"

#include <openssl/rand.h>
#include <openssl/sha.h>

#include <cstring>

#include "pkm/crypto.hpp"

namespace pkm::ws {
namespace {

constexpr std::string_view kGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
constexpr std::size_t kMaxMessage = 16 * 1024 * 1024;

std::string accept_key(std::string_view key) {
  std::string input(key);
  input.append(kGuid);
  unsigned char digest[SHA_DIGEST_LENGTH];
  SHA1(reinterpret_cast<const unsigned char*>(input.data()), input.size(), digest);
  return base64_encode(digest, sizeof(digest), false);
}

}  // namespace

Client::Client() {
  in_.reserve(1 << 20);
  message_.reserve(1 << 16);
  frame_.reserve(1 << 12);
  RAND_bytes(reinterpret_cast<unsigned char*>(&mask_state_), sizeof(mask_state_));
  mask_state_ |= 1;
}

void Client::close() noexcept {
  if (conn_.open() && !closed_) send_frame(0x8, std::string_view("\x03\xe8", 2));
  conn_.close();
  closed_ = true;
}

bool Client::connect(const net::Url& url, std::string_view headers, std::string& error) {
  conn_.close();
  in_.clear();
  parsed_ = 0;
  message_.clear();
  fragmented_ = false;
  closed_ = false;
  close_code_ = 0;
  if (!conn_.connect(url.host, url.port, url.tls, 10'000, error)) return false;
  std::uint8_t nonce[16];
  RAND_bytes(nonce, sizeof(nonce));
  const std::string key = base64_encode(nonce, sizeof(nonce), false);
  std::string request = "GET " + url.path + " HTTP/1.1\r\nHost: " + url.host +
                        "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " +
                        key + "\r\nUser-Agent: pk-maker-cpp/0.1\r\n";
  request.append(headers);
  request.append("\r\n");
  if (!conn_.write_all(request, 5'000)) {
    error = "handshake write failed";
    conn_.close();
    return false;
  }
  const std::int64_t deadline = net::mono_ns() + 10'000'000'000LL;
  char buf[4096];
  while (in_.find("\r\n\r\n") == std::string::npos) {
    const long n = conn_.read(buf, sizeof(buf));
    if (n > 0) {
      in_.append(buf, static_cast<std::size_t>(n));
      continue;
    }
    const int left = static_cast<int>((deadline - net::mono_ns()) / 1'000'000);
    if (n < 0 || left <= 0) {
      error = "handshake: no response";
      conn_.close();
      return false;
    }
    conn_.wait_readable(left);
  }
  const auto head_end = in_.find("\r\n\r\n") + 4;
  const std::string head = in_.substr(0, head_end);
  if (head.compare(0, 12, "HTTP/1.1 101") != 0) {
    error = "handshake refused: " + head.substr(0, head.find("\r\n"));
    conn_.close();
    return false;
  }
  if (head.find(accept_key(key)) == std::string::npos) {
    error = "handshake: bad Sec-WebSocket-Accept";
    conn_.close();
    return false;
  }
  in_.erase(0, head_end);
  last_rx_ns_ = net::mono_ns();
  return true;
}

int Client::fill() {
  if (parsed_ > 0) {
    in_.erase(0, parsed_);
    parsed_ = 0;
  }
  char buf[64 * 1024];
  const long n = conn_.read(buf, sizeof(buf));
  if (n > 0) {
    in_.append(buf, static_cast<std::size_t>(n));
    last_rx_ns_ = net::mono_ns();
    return 1;
  }
  return n == 0 ? 0 : -1;
}

bool Client::next_message(std::string_view& out) {
  while (true) {
    const std::size_t avail = in_.size() - parsed_;
    if (avail < 2) return false;
    const auto* p = reinterpret_cast<const std::uint8_t*>(in_.data() + parsed_);
    const bool fin = (p[0] & 0x80) != 0;
    const std::uint8_t opcode = p[0] & 0x0F;
    const bool masked = (p[1] & 0x80) != 0;
    std::uint64_t len = p[1] & 0x7F;
    std::size_t header = 2;
    if (len == 126) {
      if (avail < 4) return false;
      len = (static_cast<std::uint64_t>(p[2]) << 8) | p[3];
      header = 4;
    } else if (len == 127) {
      if (avail < 10) return false;
      len = 0;
      for (int i = 2; i < 10; ++i) len = (len << 8) | p[i];
      header = 10;
    }
    if (len > kMaxMessage) {
      closed_ = true;
      return false;
    }
    const std::size_t mask_at = header;
    if (masked) header += 4;
    if (avail < header + len) return false;
    char* payload = in_.data() + parsed_ + header;
    if (masked) {
      const auto* mask = reinterpret_cast<const std::uint8_t*>(in_.data() + parsed_ + mask_at);
      for (std::uint64_t i = 0; i < len; ++i) payload[i] = static_cast<char>(payload[i] ^ mask[i % 4]);
    }
    const std::string_view data(payload, static_cast<std::size_t>(len));
    parsed_ += header + static_cast<std::size_t>(len);
    switch (opcode) {
      case 0x9:  // ping
        send_frame(0xA, data);
        continue;
      case 0xA:  // pong
        continue;
      case 0x8:  // close
        close_code_ = data.size() >= 2 ? static_cast<std::uint16_t>((static_cast<std::uint8_t>(data[0]) << 8) |
                                                                      static_cast<std::uint8_t>(data[1]))
                                       : 1005;
        send_frame(0x8, data.substr(0, 2));
        closed_ = true;
        return false;
      case 0x1:
      case 0x2:
        if (fin && !fragmented_) {
          out = data;
          return true;
        }
        message_.assign(data);
        fragmented_ = !fin;
        if (fin) {
          out = message_;
          return true;
        }
        continue;
      case 0x0:  // continuation
        if (!fragmented_) continue;
        message_.append(data);
        if (message_.size() > kMaxMessage) {
          closed_ = true;
          return false;
        }
        if (fin) {
          fragmented_ = false;
          out = message_;
          return true;
        }
        continue;
      default:
        closed_ = true;
        return false;
    }
  }
}

bool Client::send_frame(std::uint8_t opcode, std::string_view payload) {
  frame_.clear();
  frame_.push_back(static_cast<char>(0x80 | opcode));
  const std::size_t len = payload.size();
  if (len < 126) {
    frame_.push_back(static_cast<char>(0x80 | len));
  } else if (len <= 0xFFFF) {
    frame_.push_back(static_cast<char>(0x80 | 126));
    frame_.push_back(static_cast<char>((len >> 8) & 0xFF));
    frame_.push_back(static_cast<char>(len & 0xFF));
  } else {
    frame_.push_back(static_cast<char>(0x80 | 127));
    for (int i = 7; i >= 0; --i) frame_.push_back(static_cast<char>((static_cast<std::uint64_t>(len) >> (8 * i)) & 0xFF));
  }
  // xorshift: the mask only has to be unpredictable to proxies, not secret.
  mask_state_ ^= mask_state_ << 13;
  mask_state_ ^= mask_state_ >> 7;
  mask_state_ ^= mask_state_ << 17;
  char mask[4];
  std::memcpy(mask, &mask_state_, 4);
  frame_.append(mask, 4);
  const std::size_t start = frame_.size();
  frame_.append(payload);
  for (std::size_t i = 0; i < len; ++i) frame_[start + i] = static_cast<char>(frame_[start + i] ^ mask[i % 4]);
  return conn_.write_all(frame_, 1000);
}

bool Client::send_text(std::string_view text) { return open() && send_frame(0x1, text); }

bool Client::send_ping() { return open() && send_frame(0x9, {}); }

bool Client::read_message(std::string& out, int timeout_ms) {
  const std::int64_t deadline = net::mono_ns() + static_cast<std::int64_t>(timeout_ms) * 1'000'000;
  while (true) {
    std::string_view message;
    if (next_message(message)) {
      out.assign(message);
      return true;
    }
    if (closed_) return false;
    const int got = fill();
    if (got < 0) {
      closed_ = true;
      return false;
    }
    if (got > 0) continue;
    const int left = static_cast<int>((deadline - net::mono_ns()) / 1'000'000);
    if (left <= 0) return false;
    conn_.wait_readable(left);
  }
}

}  // namespace pkm::ws
