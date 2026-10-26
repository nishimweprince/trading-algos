#include "pkm/http.hpp"

#include <charconv>
#include <cstring>

namespace pkm::http {
namespace {

bool iequals(std::string_view a, std::string_view b) noexcept {
  if (a.size() != b.size()) return false;
  for (std::size_t i = 0; i < a.size(); ++i) {
    char x = a[i];
    char y = b[i];
    if (x >= 'A' && x <= 'Z') x = static_cast<char>(x - 'A' + 'a');
    if (y >= 'A' && y <= 'Z') y = static_cast<char>(y - 'A' + 'a');
    if (x != y) return false;
  }
  return true;
}

bool icontains(std::string_view text, std::string_view word) noexcept {
  if (word.size() > text.size()) return false;
  for (std::size_t i = 0; i + word.size() <= text.size(); ++i)
    if (iequals(text.substr(i, word.size()), word)) return true;
  return false;
}

std::string_view trim(std::string_view s) noexcept {
  while (!s.empty() && (s.front() == ' ' || s.front() == '\t')) s.remove_prefix(1);
  while (!s.empty() && (s.back() == ' ' || s.back() == '\t' || s.back() == '\r')) s.remove_suffix(1);
  return s;
}

}  // namespace

void ResponseParser::reset() noexcept {
  if (consumed_ > 0 && consumed_ < buffer_.size()) buffer_.erase(0, consumed_);
  else buffer_.clear();
  body_.clear();
  header_end_ = 0;
  consumed_ = 0;
  content_length_ = -1;
  chunked_ = to_close_ = complete_ = error_ = false;
  keep_alive_ = true;
  status_ = 0;
}

std::string_view ResponseParser::header(std::string_view name) const noexcept {
  if (header_end_ == 0) return {};
  std::string_view head(buffer_.data(), header_end_);
  std::size_t at = head.find("\r\n");
  while (at != std::string_view::npos && at + 2 < head.size()) {
    const std::size_t start = at + 2;
    const std::size_t end = head.find("\r\n", start);
    const std::string_view line = head.substr(start, end == std::string_view::npos ? std::string_view::npos : end - start);
    const auto colon = line.find(':');
    if (colon != std::string_view::npos && iequals(trim(line.substr(0, colon)), name)) return trim(line.substr(colon + 1));
    at = end;
  }
  return {};
}

bool ResponseParser::feed(const char* data, std::size_t size) {
  buffer_.append(data, size);
  return parse();
}

bool ResponseParser::finish_on_close() noexcept {
  if (complete_) return true;
  if (header_end_ != 0 && to_close_) {
    body_.assign(buffer_, header_end_, std::string::npos);
    consumed_ = buffer_.size();
    complete_ = true;
    keep_alive_ = false;
  }
  return complete_;
}

bool ResponseParser::parse() {
  if (complete_ || error_) return complete_;
  if (header_end_ == 0) {
    const auto end = buffer_.find("\r\n\r\n");
    if (end == std::string::npos) {
      if (buffer_.size() > 64 * 1024) error_ = true;
      return false;
    }
    header_end_ = end + 4;
    const std::string_view status_line(buffer_.data(), buffer_.find("\r\n"));
    if (!status_line.starts_with("HTTP/1.") || status_line.size() < 12) {
      error_ = true;
      return false;
    }
    std::from_chars(status_line.data() + 9, status_line.data() + 12, status_);
    if (status_line.starts_with("HTTP/1.0")) keep_alive_ = false;
    const auto connection = header("Connection");
    if (icontains(connection, "close")) keep_alive_ = false;
    if (icontains(connection, "keep-alive")) keep_alive_ = true;
    const auto te = header("Transfer-Encoding");
    chunked_ = icontains(te, "chunked");
    const auto cl = header("Content-Length");
    if (!cl.empty()) {
      long n = 0;
      std::from_chars(cl.data(), cl.data() + cl.size(), n);
      content_length_ = n;
    }
    const bool no_body = status_ == 204 || status_ == 304 || (status_ >= 100 && status_ < 200);
    if (no_body) content_length_ = 0;
    if (!chunked_ && content_length_ < 0) to_close_ = true;
  }
  if (content_length_ >= 0 && !chunked_) {
    const std::size_t need = header_end_ + static_cast<std::size_t>(content_length_);
    if (buffer_.size() < need) return false;
    body_.assign(buffer_, header_end_, static_cast<std::size_t>(content_length_));
    consumed_ = need;
    complete_ = true;
    return true;
  }
  if (chunked_) {
    body_.clear();
    std::size_t at = header_end_;
    while (true) {
      const auto line_end = buffer_.find("\r\n", at);
      if (line_end == std::string::npos) return false;
      std::size_t chunk = 0;
      const auto r = std::from_chars(buffer_.data() + at, buffer_.data() + line_end, chunk, 16);
      if (r.ptr == buffer_.data() + at) {
        error_ = true;
        return false;
      }
      const std::size_t data_start = line_end + 2;
      if (chunk == 0) {
        // Trailers end with an empty line.
        const auto trailer_end = buffer_.find("\r\n", data_start);
        if (trailer_end == std::string::npos) return false;
        std::size_t end = trailer_end + 2;
        if (trailer_end != data_start) {
          const auto blank = buffer_.find("\r\n\r\n", data_start);
          if (blank == std::string::npos) return false;
          end = blank + 4;
        }
        consumed_ = end;
        complete_ = true;
        return true;
      }
      if (buffer_.size() < data_start + chunk + 2) return false;
      body_.append(buffer_, data_start, chunk);
      at = data_start + chunk + 2;
    }
  }
  return false;  // read to close
}

void write_request(std::string& out, std::string_view method, std::string_view host, std::string_view path,
                   std::string_view headers, std::string_view body, std::string_view user_agent) {
  out.append(method).append(" ").append(path).append(" HTTP/1.1\r\nHost: ").append(host);
  out.append("\r\nUser-Agent: ").append(user_agent).append("\r\nAccept: application/json\r\nConnection: keep-alive\r\n");
  out.append(headers);
  if (!body.empty() || method == "POST" || method == "DELETE" || method == "PUT") {
    char len[24];
    const auto r = std::to_chars(len, len + sizeof(len), body.size());
    out.append("Content-Type: application/json\r\nContent-Length: ").append(len, r.ptr).append("\r\n");
  }
  out.append("\r\n").append(body);
}

Client::Client(std::string base_url, std::string user_agent, int timeout_ms)
    : user_agent_(std::move(user_agent)), timeout_ms_(timeout_ms) {
  net::parse_url(base_url, url_);
  base_path_ = url_.path == "/" ? "" : url_.path;
  while (!base_path_.empty() && base_path_.back() == '/') base_path_.pop_back();
}

Response Client::request(std::string_view method, std::string_view path, std::string_view headers, std::string_view body) {
  bool retryable = false;
  Response r = attempt(method, path, headers, body, retryable);
  if (r.status == 0 && retryable) r = attempt(method, path, headers, body, retryable);
  return r;
}

Response Client::attempt(std::string_view method, std::string_view path, std::string_view headers, std::string_view body,
                         bool& retryable) {
  Response out;
  retryable = false;
  const bool reused = conn_.open();
  if (!reused && !conn_.connect(url_.host, url_.port, url_.tls, timeout_ms_, out.error)) return out;
  request_.clear();
  std::string full_path = base_path_;
  full_path.append(path);
  write_request(request_, method, url_.host, full_path, headers, body, user_agent_);
  parser_.reset();
  if (!conn_.write_all(request_, timeout_ms_)) {
    conn_.close();
    out.error = "write failed";
    retryable = reused;
    return out;
  }
  const std::int64_t deadline = net::mono_ns() + static_cast<std::int64_t>(timeout_ms_) * 1'000'000;
  char buf[16 * 1024];
  bool any = false;
  while (!parser_.complete()) {
    const long n = conn_.read(buf, sizeof(buf));
    if (n > 0) {
      any = true;
      parser_.feed(buf, static_cast<std::size_t>(n));
      if (parser_.error()) break;
      continue;
    }
    if (n < 0) {
      parser_.finish_on_close();
      if (!parser_.complete()) retryable = reused && !any;
      break;
    }
    const int left = static_cast<int>((deadline - net::mono_ns()) / 1'000'000);
    if (left <= 0) break;
    conn_.wait_readable(left);
  }
  if (!parser_.complete()) {
    conn_.close();
    out.error = parser_.error() ? "malformed response" : (any ? "response cut off" : "no response");
    return out;
  }
  out.status = parser_.status();
  out.body.assign(parser_.body());
  if (!parser_.keep_alive()) conn_.close();
  return out;
}

bool Channel::connect(std::string& error) {
  busy_ = false;
  parser_.reset();
  if (!conn_.connect(url_.host, url_.port, url_.tls, 5'000, error)) return false;
  last_used_ns_ = net::mono_ns();
  return true;
}

bool Channel::send(std::string_view bytes) {
  if (busy_ || !conn_.open()) return false;
  parser_.reset();
  if (!conn_.write_all(bytes, 200)) {
    conn_.close();
    return false;
  }
  busy_ = true;
  sent_ns_ = net::mono_ns();
  last_used_ns_ = sent_ns_;
  return true;
}

int Channel::poll() {
  if (!conn_.open()) return -1;
  while (true) {
    const long n = conn_.read(buf_, sizeof(buf_));
    if (n > 0) {
      if (!busy_) continue;  // unsolicited bytes: drop
      if (parser_.feed(buf_, static_cast<std::size_t>(n))) return 1;
      if (parser_.error()) {
        conn_.close();
        return -1;
      }
      continue;
    }
    if (n < 0) {
      const bool done = busy_ && parser_.finish_on_close();
      conn_.close();
      return done ? 1 : -1;
    }
    return 0;
  }
}

void Channel::release() noexcept {
  busy_ = false;
  if (!parser_.keep_alive()) conn_.close();
  parser_.reset();
}

void Channel::close() noexcept {
  busy_ = false;
  conn_.close();
}

}  // namespace pkm::http
