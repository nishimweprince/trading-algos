#include "exec/exec.hpp"

#include <cstdio>
#include <cstring>

#include "core/sys.hpp"

namespace pd {

namespace {
constexpr int WRITE_TIMEOUT_MS = 50;
constexpr int CONNECT_TIMEOUT_MS = 1500;
constexpr uint64_t RECONNECT_BACKOFF_NS = 500'000'000ULL;
}  // namespace

SendVerdict classify_response(int status, std::string_view body) {
  SendVerdict v;
  v.ok = status == 200 && body.find("\"result\"") != std::string_view::npos &&
         body.find("\"error\"") == std::string_view::npos;
  if (v.ok) return v;
  // 429 says nothing about the tx: back off and resend the same bytes.
  v.rate_limited = status == 429;
  // An HTTP response with a JSON-RPC error, or another 4xx, is a definitive rejection; anything else is not.
  v.definitive = !v.rate_limited && (status == 200 || (status >= 400 && status < 500));
  return v;
}

Exec::Exec(const Config& cfg, Shared& sh) : cfg_(cfg), sh_(sh) {
  body_.reserve(4096);
  Url sender = parse_url(cfg.sender.url);
  if (cfg.sender.swqos_only) sender.path += (sender.path.find('?') == std::string::npos ? "?" : "&") + std::string("swqos_only=true");
  for (int i = 0; i < std::max(1, cfg.sender.connections); ++i) {
    auto l = std::make_unique<Link>();
    l->route = Route::Sender;
    l->url = sender;
    l->send_path = sender.path;
    l->ping_path = "/ping";  // Sender answers GET /ping without auth or cost
    l->req.reserve(4096);
    sender_.push_back(std::move(l));
  }
  if (cfg.jito.enabled) {
    Url j = parse_url(cfg.jito.url);
    auto l = std::make_unique<Link>();
    l->route = Route::Jito;
    l->url = j;
    l->send_path = j.path;  // /api/v1/transactions
    l->ping_path = "/api/v1/getTipAccounts";
    l->ping_body = R"({"jsonrpc":"2.0","id":0,"method":"getTipAccounts","params":[]})";
    l->req.reserve(4096);
    jito_.push_back(std::move(l));
  }
}

Exec::~Exec() = default;

bool Exec::connect_all(std::string* err) {
  int ok = 0;
  for (auto& l : sender_) {
    std::string e;
    if (l->conn.connect(l->url, 3000, &e)) ++ok;
    else if (err) *err = "sender: " + e;
    l->last_io_ns = now_ns();
  }
  for (auto& l : jito_) {
    std::string e;
    if (!l->conn.connect(l->url, 3000, &e) && err) *err += " jito: " + e;
    l->last_io_ns = now_ns();
  }
  return ok > 0;
}

bool Exec::send(Link& l, const Packet& p) {
  Pending meta;
  meta.side = p.side;
  meta.pos = p.pos;
  meta.attempt = p.attempt;
  meta.sig = p.sig;
  meta.t_signed = p.t_signed_ns;
  return write_tx(l, p.b64, p.b64_len, meta);
}

bool Exec::write_tx(Link& l, const char* b64, size_t b64_len, const Pending& meta) {
  if (!l.conn.open() || l.inflight() >= 64) return false;
  body_.clear();
  body_.append(R"({"jsonrpc":"2.0","id":)");
  char id[24];
  int n = std::snprintf(id, sizeof id, "%llu", (unsigned long long)rpc_id_++);
  body_.append(id, size_t(n));
  body_.append(R"(,"method":"sendTransaction","params":[")");
  body_.append(b64, b64_len);
  if (l.route == Route::Sender) body_.append(R"(",{"encoding":"base64","skipPreflight":true,"maxRetries":0}]})");
  else body_.append(R"(",{"encoding":"base64"}]})");
  format_request(l.req, "POST", l.url, l.send_path, body_);
  if (!l.conn.write_all(l.req.data(), l.req.size(), WRITE_TIMEOUT_MS)) {
    drop(l, "write failed");
    return false;
  }
  uint64_t now = now_ns();
  uint32_t i = l.tail++ & 63;
  Pending& q = l.pending[i];
  q = meta;
  q.ping = false;
  q.t_written = now;
  // Kept after the write so the copy costs nothing before the bytes are out.
  l.bytes[i].len = uint16_t(b64_len);
  std::memcpy(l.bytes[i].b64, b64, b64_len);
  l.last_io_ns = now;
  return true;
}

void Exec::schedule_retry(const Pending& p, const Bytes& b, const Link& from, uint64_t now) {
  if (p.tries >= cfg_.sender.max_429_retries) return;
  for (Retry& r : retries_) {
    if (r.used) continue;
    r.used = true;
    r.p = p;
    r.p.tries = uint8_t(p.tries + 1);
    r.bytes.len = b.len;
    std::memcpy(r.bytes.b64, b.b64, b.len);
    r.due_ns = now + uint64_t(cfg_.sender.retry_429_ms) * r.p.tries * 1'000'000ULL;
    r.from = &from;
    return;
  }
}

void Exec::run_retries(uint64_t now) {
  for (Retry& r : retries_) {
    if (!r.used || now < r.due_ns) continue;
    // Prefer a different Sender link from the one that answered 429.
    bool sent = false;
    for (int pass = 0; pass < 2 && !sent; ++pass)
      for (auto& l : sender_) {
        if (sent) break;
        if (pass == 0 && l.get() == r.from) continue;
        sent = write_tx(*l, r.bytes.b64, r.bytes.len, r.p);
      }
    if (sent) r.used = false;
    else r.due_ns = now + 1'000'000ULL;  // no link free: try again in 1 ms
  }
}

void Exec::ping(Link& l, uint64_t now) {
  if (!l.conn.open() || l.inflight() > 0) return;
  format_request(l.req, l.ping_body.empty() ? "GET" : "POST", l.url, l.ping_path, l.ping_body);
  if (!l.conn.write_all(l.req.data(), l.req.size(), WRITE_TIMEOUT_MS)) {
    drop(l, "ping write failed");
    return;
  }
  Pending& q = l.pending[l.tail++ & 63];
  q = Pending{};
  q.ping = true;
  q.t_written = now;
  l.last_io_ns = now;
}

void Exec::report(const Pending& p, Route route, const SendVerdict& v, int status, std::string_view body, uint64_t now) {
  if (p.ping) return;
  SendResult* r = sh_.send_results.claim();
  if (!r) return;
  *r = SendResult{};
  r->side = p.side;
  r->pos = p.pos;
  r->attempt = p.attempt;
  r->route = route;
  r->ok = v.ok;
  r->definitive = v.definitive;
  r->rate_limited = v.rate_limited;
  r->http_status = status;
  r->sig = p.sig;
  r->t_signed_ns = p.t_signed;
  r->t_written_ns = p.t_written;
  r->t_ack_ns = now;
  if (!v.ok) {
    size_t n = std::min(body.size(), sizeof(r->error) - 1);
    std::memcpy(r->error, body.data(), n);
    r->error[n] = 0;
  }
  sh_.send_results.publish();
}

void Exec::read(Link& l, uint64_t now) {
  if (!l.conn.open()) return;
  for (;;) {
    ssize_t n = l.conn.read_some(rbuf_, sizeof rbuf_);
    if (n == 0) return;
    if (n < 0) {
      drop(l, "connection closed");
      return;
    }
    l.last_io_ns = now;
    bool complete = l.parser.feed(rbuf_, size_t(n));
    while (complete) {
      if (l.head == l.tail) {
        l.parser.reset();  // unsolicited response; resync
        break;
      }
      uint32_t i = l.head++ & 63;
      const Pending& p = l.pending[i];
      std::string_view body = l.parser.body();
      SendVerdict v = classify_response(l.parser.status(), body);
      report(p, l.route, v, l.parser.status(), body, now);
      if (v.rate_limited && !p.ping && l.route == Route::Sender) schedule_retry(p, l.bytes[i], l, now);
      complete = l.parser.next();
    }
  }
}

void Exec::drop(Link& l, const char* why) {
  uint64_t now = now_ns();
  // Requests on a dead socket may or may not have reached the server: not definitive.
  while (l.head != l.tail) {
    Pending& p = l.pending[l.head++ & 63];
    report(p, l.route, SendVerdict{}, 0, why, now);
  }
  l.conn.close();
  l.parser.reset();
  l.next_reconnect_ns = now + RECONNECT_BACKOFF_NS;
}

void Exec::maintain(uint64_t now) {
  // A reconnect blocks this thread for up to CONNECT_TIMEOUT_MS, so at most one per
  // pass; the other Sender link keeps taking packets meanwhile.
  auto tend = [&](std::vector<std::unique_ptr<Link>>& links) {
    for (auto& l : links) {
      if (!l->conn.open()) {
        if (now < l->next_reconnect_ns) continue;
        std::string e;
        if (l->conn.connect(l->url, CONNECT_TIMEOUT_MS, &e)) l->last_io_ns = now_ns();
        else l->next_reconnect_ns = now_ns() + RECONNECT_BACKOFF_NS;
        return true;
      }
      if (now - l->last_io_ns > uint64_t(cfg_.sender.ping_ms) * 1'000'000ULL) ping(*l, now);
    }
    return false;
  };
  if (!tend(sender_)) tend(jito_);
}

void Exec::run() {
  size_t rr = 0;
  while (!sh_.stop.load(std::memory_order_relaxed)) {
    if (Packet* p = sh_.packets.peek()) {
      // Sender first (primary), then Jito with the same signed bytes when the packet asks for it.
      bool sent = false;
      for (size_t i = 0; i < sender_.size() && !sent; ++i) {
        Link& l = *sender_[(rr + i) % sender_.size()];
        sent = send(l, *p);
      }
      ++rr;
      if (!sent) {
        Pending q;
        q.side = p->side;
        q.pos = p->pos;
        q.attempt = p->attempt;
        q.sig = p->sig;
        q.t_signed = p->t_signed_ns;
        q.t_written = now_ns();
        SendVerdict v;
        v.definitive = true;
        report(q, Route::Sender, v, 0, "no sender connection", now_ns());
      }
      // Jito only for packets priced for it (never BAM leaders: the block-engine hop costs ~50 ms).
      if (p->jito) for (auto& l : jito_) send(*l, *p);
      sh_.packets.pop();
      continue;  // drain the queue before anything else
    }
    uint64_t now = now_ns();
    run_retries(now);
    for (auto& l : sender_) read(*l, now);
    for (auto& l : jito_) read(*l, now);
    if (now >= next_maintain_ns_) {
      next_maintain_ns_ = now + 1'000'000ULL;
      maintain(now);
    }
    cpu_relax();
  }
}

}  // namespace pd
