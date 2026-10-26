#include "pkm/runtime.hpp"

#include <sys/epoll.h>
#include <unistd.h>

#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <memory>
#include <set>
#include <thread>

#include "pkm/engine.hpp"
#include "pkm/feeds.hpp"
#include "pkm/http.hpp"
#include "pkm/json.hpp"
#include "pkm/net.hpp"
#include "pkm/platform.hpp"
#include "pkm/presign.hpp"
#include "pkm/ws.hpp"

namespace pkm::runtime {
namespace {

constexpr std::int64_t kMs = 1'000'000;
constexpr std::int64_t kSec = 1'000'000'000;

enum class FeedKind : std::uint8_t { kalshi, poly, user };
struct FeedId {
  FeedKind kind = FeedKind::kalshi;
  int coin = -1;
};

std::string feed_name(const FeedId& id, const engine::Engine& e) {
  switch (id.kind) {
    case FeedKind::kalshi: return "kalshi";
    case FeedKind::user: return "poly user";
    case FeedKind::poly: return "poly " + e.asset(id.coin);
  }
  return "?";
}

// What the control thread works from, published by the hot thread each second.
struct Snapshot {
  std::vector<std::pair<std::string, std::int64_t>> tracked;  // order id, posted ns
  std::map<std::string, double> held;
  std::vector<engine::Inventory> unsettled;
  std::vector<std::pair<std::string, std::string>> redeem_due;
  std::vector<std::int64_t> window_open;  // per coin, 0 = none
};

struct ToHot {
  enum class Kind : std::uint8_t { window, feed, channel, order_state, order_unknown, balance, settle, redeemed, kill, ack };
  Kind kind = Kind::window;
  engine::Window window;
  FeedId feed;
  std::unique_ptr<ws::Client> ws;
  std::unique_ptr<http::Channel> channel;
  std::string id;
  std::string text;
  double value = 0;
  bool flag = false;
  std::int64_t open_unix = 0;
};

struct ToControl {
  enum class Kind : std::uint8_t { save, log, snapshot, hint };
  Kind kind = Kind::log;
  std::string text;
  std::shared_ptr<const Snapshot> snapshot;
};

struct ConnectRequest {
  bool channel = false;
  FeedId feed;
};

// --- connector ----------------------------------------------------------------

class Connector final {
 public:
  Connector(const Config& config, Mailbox<ToHot>& to_hot) : config_(config), to_hot_(to_hot) {
    std::string error;
    std::ifstream in(config.kalshi_key_path);
    std::string pem((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (!kalshi_.load_pem(config.kalshi_key_id, pem, error)) platform::log("kalshi key: " + error);
  }
  void request(const ConnectRequest& r) {
    std::lock_guard<std::mutex> lock(mutex_);
    queue_.push_back(r);
    cv_.notify_one();
  }
  void run(const std::atomic<bool>& stop) {
    while (!stop.load()) {
      ConnectRequest r;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait_for(lock, std::chrono::milliseconds(200), [this] { return !queue_.empty(); });
        if (queue_.empty()) continue;
        r = queue_.front();
        queue_.pop_front();
      }
      connect(r, stop);
    }
  }

 private:
  void connect(const ConnectRequest& r, const std::atomic<bool>& stop) {
    for (int attempt = 0; !stop.load(); ++attempt) {
      if (attempt > 0) {
        const auto wait_ms = std::min<std::int64_t>(10'000, 500LL << std::min(attempt - 1, 5));
        for (std::int64_t waited = 0; waited < wait_ms && !stop.load(); waited += 100)
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
      std::string error;
      if (r.channel) {
        net::Url url;
        net::parse_url(config_.clob_url, url);
        auto ch = std::make_unique<http::Channel>(url);
        if (ch->connect(error)) {
          ToHot m;
          m.kind = ToHot::Kind::channel;
          m.channel = std::move(ch);
          to_hot_.push(std::move(m));
          return;
        }
        platform::log("order connection: " + error);
        continue;
      }
      net::Url url;
      std::string headers;
      if (r.feed.kind == FeedKind::kalshi) {
        net::parse_url(config_.kalshi_ws_url, url);
        if (!kalshi_.loaded()) {
          platform::log("kalshi: no key loaded (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)");
          continue;
        }
        headers = kalshi_.headers(net::now_ns() / kMs, "GET", url.path) + "\r\n";
      } else {
        net::parse_url(r.feed.kind == FeedKind::user ? config_.poly_user_ws_url : config_.poly_ws_url, url);
      }
      auto ws = std::make_unique<ws::Client>();
      if (ws->connect(url, headers, error)) {
        ToHot m;
        m.kind = ToHot::Kind::feed;
        m.feed = r.feed;
        m.ws = std::move(ws);
        to_hot_.push(std::move(m));
        return;
      }
      platform::log("connect " + url.host + url.path + ": " + error);
    }
  }

  const Config& config_;
  Mailbox<ToHot>& to_hot_;
  KalshiSigner kalshi_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<ConnectRequest> queue_;
};

// --- hot thread -----------------------------------------------------------------

class Hot final {
 public:
  Hot(const Startup& s, engine::Engine& engine, presign::Presigner& presigner, Mailbox<ToHot>& in, Mailbox<ToControl>& out,
      Connector& connector, std::atomic<std::int64_t>& heartbeat)
      : config_(*s.config),
        wallet_(*s.wallet),
        engine_(engine),
        presigner_(presigner),
        in_(in),
        out_(out),
        connector_(connector),
        heartbeat_(heartbeat),
        l2_(s.credentials),
        owner_(s.credentials.api_key) {
    net::parse_url(config_.clob_url, clob_url_);
    const std::size_t n = engine_.coins();
    poly_.resize(n);
    gen_.assign(n, 0);
    for (std::size_t i = 0; i < n; ++i) poly_[i].id = {FeedKind::poly, static_cast<int>(i)};
    kalshi_.id = {FeedKind::kalshi, -1};
    user_.id = {FeedKind::user, -1};
    orders_.resize(static_cast<std::size_t>(config_.order_channels));
    touched_.assign(n, 0);
    body_.reserve(presign::kBodyMax);
    queued_.reserve(128);
    request_.reserve(4096);
    headers_.reserve(512);
    updates_.reserve(16);
    inbox_.reserve(16);
  }

  void run(std::atomic<bool>& stop) {
    if (config_.hot_core >= 0 && !platform::pin_current_thread(static_cast<unsigned>(config_.hot_core)))
      platform::log("hot thread: could not pin to core " + std::to_string(config_.hot_core));
    epoll_ = ::epoll_create1(EPOLL_CLOEXEC);
    for (auto& o : orders_) request_channel(o);
    std::int64_t stop_at = 0;
    while (true) {
      const std::int64_t mono = net::mono_ns();
      heartbeat_.store(mono, std::memory_order_relaxed);
      if (stop.load(std::memory_order_relaxed) && stop_at == 0) {
        stop_at = mono;
        engine_.stop();
        engine_.tick(net::now_ns());
        engine::Action all;
        all.kind = engine::ActionKind::cancel_all;
        all.reason = "shutdown";
        engine_.actions().push_back(std::move(all));
        execute(mono);
      }
      if (stop_at != 0 && (mono - stop_at > 1500 * kMs || idle())) break;
      poll_sockets(mono);
      drain_inbox(mono);
      if (mono - last_tick_ >= 10 * kMs) {
        last_tick_ = mono;
        housekeeping(mono);
      }
      execute(mono);
    }
    status_line();
    publish(true);
    for (auto* f : all_feeds()) close_feed(*f, false);
    for (auto& o : orders_) close_channel(o);
    ::close(epoll_);
  }

 private:
  struct Feed {
    FeedId id;
    std::unique_ptr<ws::Client> ws;
    bool requested = false;
    std::int64_t last_rx = 0;
    std::int64_t last_ping = 0;
    std::vector<std::string> subscribed;  // poly tokens, or user condition ids
    bool initial_sent = false;
  };
  struct Pending {
    enum class Kind : std::uint8_t { none, post, cancel, cancel_all, keepalive };
    Kind kind = Kind::none;
    int coin = -1;
    bool up = true;
    double px = 0;
    double size = 0;
    engine::OrderId order_id;
    std::string reason;  // cancel-all
  };
  struct Order {
    std::unique_ptr<http::Channel> ch;
    bool requested = false;
    Pending pending;
  };
  struct Queued {
    engine::Action action;
    std::int64_t since = 0;
  };

  // epoll tags: kind << 32 | index
  static constexpr std::uint64_t kTagKalshi = 1, kTagPoly = 2, kTagUser = 3, kTagOrder = 4;

  std::vector<Feed*> all_feeds() {
    std::vector<Feed*> out{&kalshi_, &user_};
    for (auto& p : poly_) out.push_back(&p);
    return out;
  }

  [[nodiscard]] bool idle() const {
    for (const auto& o : orders_)
      if (o.ch && o.ch->busy()) return false;
    return queued_.empty();
  }

  void add_fd(int fd, std::uint64_t tag) {
    epoll_event ev{};
    ev.events = EPOLLIN;
    ev.data.u64 = tag;
    ::epoll_ctl(epoll_, EPOLL_CTL_ADD, fd, &ev);
  }

  void remove_fd(int fd) {
    if (fd >= 0) ::epoll_ctl(epoll_, EPOLL_CTL_DEL, fd, nullptr);
  }

  void log(std::string text) {
    ToControl m;
    m.kind = ToControl::Kind::log;
    m.text = std::move(text);
    out_.push(std::move(m));
  }

  // --- feeds ---

  bool wanted(const Feed& f) const {
    switch (f.id.kind) {
      case FeedKind::kalshi:
      case FeedKind::user:
        for (std::size_t i = 0; i < engine_.coins(); ++i)
          if (engine_.window(static_cast<int>(i)) != nullptr) return true;
        return false;
      case FeedKind::poly: return engine_.window(f.id.coin) != nullptr;
    }
    return false;
  }

  void request_feed(Feed& f) {
    if (f.ws || f.requested || !wanted(f)) return;
    f.requested = true;
    connector_.request({false, f.id});
  }

  void close_feed(Feed& f, bool reconnect) {
    if (f.ws) {
      remove_fd(f.ws->fd());
      f.ws->close();
      f.ws.reset();
    }
    f.subscribed.clear();
    f.initial_sent = false;
    if (f.id.kind == FeedKind::kalshi) {
      session_.reset();
      engine_.invalidate_kalshi();
    } else if (f.id.kind == FeedKind::poly) {
      engine_.invalidate_poly(f.id.coin);
    }
    if (reconnect) request_feed(f);
  }

  void attach_feed(FeedId id, std::unique_ptr<ws::Client> ws, std::int64_t mono) {
    Feed& f = id.kind == FeedKind::kalshi ? kalshi_ : id.kind == FeedKind::user ? user_ : poly_[static_cast<std::size_t>(id.coin)];
    f.requested = false;
    if (f.ws || !wanted(f)) {
      ws->close();
      return;
    }
    f.ws = std::move(ws);
    f.last_rx = mono;
    f.last_ping = mono;
    const std::uint64_t tag = id.kind == FeedKind::kalshi ? kTagKalshi : id.kind == FeedKind::user ? kTagUser : kTagPoly;
    add_fd(f.ws->fd(), (tag << 32) | static_cast<std::uint32_t>(id.coin < 0 ? 0 : id.coin));
    log(feed_name(id, engine_) + " connected");
    sync_feed(f);
  }

  // Brings a connected feed's subscription to the engine's current windows.
  void sync_feed(Feed& f) {
    if (!f.ws) return;
    if (f.id.kind == FeedKind::kalshi) {
      std::set<std::string> tickers;
      for (std::size_t i = 0; i < engine_.coins(); ++i)
        if (const auto* w = engine_.window(static_cast<int>(i))) tickers.insert(w->kalshi_ticker);
      for (const auto& m : session_.sync(tickers)) f.ws->send_text(m);
      return;
    }
    if (f.id.kind == FeedKind::user) {
      std::vector<std::string> markets;
      for (std::size_t i = 0; i < engine_.coins(); ++i)
        if (const auto* w = engine_.window(static_cast<int>(i)); w != nullptr && !w->condition_id.empty())
          markets.push_back(w->condition_id);
      if (f.initial_sent && markets == f.subscribed) return;
      if (f.initial_sent) {
        close_feed(f, true);  // a new market set: subscribe afresh
        return;
      }
      const auto& c = l2_.credentials();
      f.ws->send_text(feeds::user_subscribe(c.api_key, base64_encode(c.secret.data(), c.secret.size(), true), c.passphrase, markets));
      f.subscribed = markets;
      f.initial_sent = true;
      return;
    }
    const auto* w = engine_.window(f.id.coin);
    if (w == nullptr) {
      close_feed(f, false);  // an empty subscription is closed by the venue anyway
      return;
    }
    const std::vector<std::string> tokens{w->up_token, w->down_token};
    if (f.subscribed == tokens) return;
    if (!f.subscribed.empty()) f.ws->send_text(feeds::poly_unsubscribe(f.subscribed));
    f.ws->send_text(feeds::poly_subscribe(tokens, !f.initial_sent));
    f.initial_sent = true;
    f.subscribed = tokens;
  }

  void on_kalshi(std::string_view text, std::int64_t mono) {
    kalshi_.last_rx = mono;
    if (!doc_.parse(text)) return;
    const auto root = doc_.root();
    send_.clear();
    const auto verdict = session_.on_frame(root, send_);
    for (const auto& m : send_) kalshi_.ws->send_text(m);
    if (verdict != feeds::KalshiSession::Verdict::ok) {
      log(session_.last_error + ": reconnecting");
      kalshi_dead_ = true;
      return;
    }
    const std::int64_t wall = net::now_ns();
    last_kalshi_wall_ = wall;
    const int coin = feeds::apply_kalshi(root, engine_, wall);
    if (coin >= 0) {
      engine_.on_kalshi_update(coin, wall);
      execute(mono);  // the frame's cancels and posts leave now
    }
  }

  void on_poly(int coin, std::string_view text, std::int64_t mono) {
    poly_[static_cast<std::size_t>(coin)].last_rx = mono;
    if (text == "PONG") return;
    if (!doc_.parse(text)) return;
    const std::int64_t wall = net::now_ns();
    last_poly_wall_ = wall;
    std::fill(touched_.begin(), touched_.end(), 0);
    feeds::apply_poly(doc_.root(), engine_, wall, touched_, nullptr);
    for (std::size_t i = 0; i < touched_.size(); ++i)
      if (touched_[i]) engine_.on_poly_update(static_cast<int>(i), wall);
    execute(mono);
  }

  void on_user(std::string_view text, std::int64_t mono) {
    user_.last_rx = mono;
    if (text == "PONG" || !doc_.parse(text)) return;
    updates_.clear();
    feeds::parse_user(doc_.root(), updates_);
    const std::int64_t wall = net::now_ns();
    for (auto& u : updates_) {
      if (u.has_matched) {
        engine_.on_order(u.id, u.matched, u.done, wall);
      } else if (engine_.orders().contains(u.id)) {
        ToControl m;
        m.kind = ToControl::Kind::hint;
        m.text = u.id;
        out_.push(std::move(m));
      }
    }
    execute(mono);
  }

  void poll_feed(Feed& f, std::int64_t mono) {
    if (!f.ws) return;
    bool alive = true;
    switch (f.id.kind) {
      case FeedKind::kalshi: alive = f.ws->poll([&](std::string_view m) { if (!kalshi_dead_) on_kalshi(m, mono); }); break;
      case FeedKind::poly: alive = f.ws->poll([&](std::string_view m) { on_poly(f.id.coin, m, mono); }); break;
      case FeedKind::user: alive = f.ws->poll([&](std::string_view m) { on_user(m, mono); }); break;
    }
    if (f.id.kind == FeedKind::kalshi && kalshi_dead_) {
      kalshi_dead_ = false;
      alive = false;
    }
    if (!alive) {
      log(feed_name(f.id, engine_) + " closed (code " + std::to_string(f.ws ? f.ws->close_code() : 0) + "): reconnecting");
      close_feed(f, true);
    }
  }

  void poll_sockets(std::int64_t mono) {
    epoll_event events[32];
    const int n = ::epoll_wait(epoll_, events, 32, 0);
    for (int i = 0; i < n; ++i) {
      const std::uint64_t tag = events[i].data.u64 >> 32;
      const auto index = static_cast<std::size_t>(events[i].data.u64 & 0xFFFFFFFFULL);
      if (tag == kTagKalshi) poll_feed(kalshi_, mono);
      else if (tag == kTagUser) poll_feed(user_, mono);
      else if (tag == kTagPoly && index < poly_.size()) poll_feed(poly_[index], mono);
      else if (tag == kTagOrder && index < orders_.size()) poll_order(orders_[index], mono);
    }
    // TLS records already decrypted are invisible to epoll.
    if (kalshi_.ws && kalshi_.ws->pending()) poll_feed(kalshi_, mono);
    for (auto& p : poly_)
      if (p.ws && p.ws->pending()) poll_feed(p, mono);
    if (user_.ws && user_.ws->pending()) poll_feed(user_, mono);
  }

  // --- orders ---

  void request_channel(Order& o) {
    if (o.ch || o.requested) return;
    o.requested = true;
    connector_.request({true, {}});
  }

  void close_channel(Order& o) {
    if (o.ch) {
      remove_fd(o.ch->fd());
      o.ch->close();
      o.ch.reset();
    }
  }

  void attach_channel(std::unique_ptr<http::Channel> ch) {
    for (std::size_t i = 0; i < orders_.size(); ++i) {
      auto& o = orders_[i];
      if (o.ch || !o.requested) continue;
      o.requested = false;
      o.ch = std::move(ch);
      add_fd(o.ch->fd(), (kTagOrder << 32) | i);
      return;
    }
  }

  Order* free_channel() {
    for (auto& o : orders_)
      if (o.ch && o.ch->open() && !o.ch->busy()) return &o;
    return nullptr;
  }

  // Signs (or takes a pre-signed body) and writes one action's request.
  void send_action(Order& o, engine::Action& a) {
    headers_.clear();
    request_.clear();
    Pending p;
    p.coin = a.coin;
    p.up = a.up;
    std::string_view method = "POST";
    std::string_view path = "/order";
    const std::int64_t ts = net::now_ns() / kSec;
    switch (a.kind) {
      case engine::ActionKind::post: {
        const auto* w = engine_.window(a.coin);
        if (w == nullptr) {
          engine_.on_posted(a.coin, a.up, a.px, a.size, false, "no window", net::now_ns());
          return;
        }
        const std::int64_t cents = std::lround(a.px * 100.0);
        const std::int64_t shares = std::lround(a.size);
        const int slot = a.coin * 2 + (a.up ? 0 : 1);
        const std::string& token = a.up ? w->up_token : w->down_token;
        if (presigner_.take(slot, gen_[static_cast<std::size_t>(a.coin)], cents, shares, body_)) {
          ++presign_hits_;
        } else {
          ++presign_misses_;
          Bytes32 id{};
          decimal_to_u256(token, id);
          const auto order = venue::make_order(wallet_, id, cents, shares, venue::random_salt(),
                                               static_cast<std::uint64_t>(net::now_ns() / kMs));
          clob::write_order_body(body_, order, token, eip712::sign_order(wallet_.signer, order, w->neg_risk), owner_, true);
        }
        p.kind = Pending::Kind::post;
        p.px = a.px;
        p.size = a.size;
        break;
      }
      case engine::ActionKind::cancel:
        clob::write_cancel_body(body_, a.order_id);
        method = "DELETE";
        p.kind = Pending::Kind::cancel;
        p.order_id = a.order_id;
        break;
      case engine::ActionKind::cancel_all:
        body_.clear();
        method = "DELETE";
        path = "/cancel-all";
        p.kind = Pending::Kind::cancel_all;
        p.reason = a.reason;
        break;
    }
    l2_.write_headers(headers_, ts, method, path, body_);
    http::write_request(request_, method, clob_url_.host, path, headers_, body_);
    if (!o.ch->send(request_)) {
      fail_pending(p, "send failed");
      close_channel(o);
      request_channel(o);
      return;
    }
    o.pending = std::move(p);
  }

  void fail_pending(const Pending& p, std::string_view why) {
    const std::int64_t wall = net::now_ns();
    switch (p.kind) {
      case Pending::Kind::post:
        // The order may rest anyway: the orphan sweep cancels what we do not track.
        engine_.on_posted(p.coin, p.up, p.px, p.size, false, why, wall);
        break;
      case Pending::Kind::cancel: engine_.on_cancelled(p.coin, p.up, p.order_id, false, wall); break;
      case Pending::Kind::cancel_all: log("cancel-all (" + p.reason + ") failed: " + std::string(why)); break;
      default: break;
    }
  }

  void poll_order(Order& o, std::int64_t mono) {
    if (!o.ch) return;
    const int r = o.ch->poll();
    if (r == 0) return;
    Pending p = std::move(o.pending);
    o.pending = Pending{};
    if (r < 0) {
      if (p.kind != Pending::Kind::none) fail_pending(p, "order connection lost");
      close_channel(o);
      request_channel(o);
      return;
    }
    const int status = o.ch->response().status();
    const std::string_view body = o.ch->response().body();
    const std::int64_t wall = net::now_ns();
    switch (p.kind) {
      case Pending::Kind::post: {
        const auto result = clob::parse_post_response(status, body);
        engine_.on_posted(p.coin, p.up, p.px, p.size, result.ok, result.ok ? result.order_id : result.error, wall);
        break;
      }
      case Pending::Kind::cancel: {
        const auto result = clob::parse_cancel_response(status, body, p.order_id);
        engine_.on_cancelled(p.coin, p.up, p.order_id, result.ok, wall);
        break;
      }
      case Pending::Kind::cancel_all:
        log("cancel-all (" + p.reason + "): http " + std::to_string(status));
        break;
      default: break;
    }
    o.ch->release();
    if (!o.ch->open()) {
      close_channel(o);
      request_channel(o);
    }
    execute(mono);
  }

  void execute(std::int64_t mono) {
    auto& actions = engine_.actions();
    for (auto& a : actions) queued_.push_back({std::move(a), mono});
    actions.clear();
    std::size_t sent = 0;
    while (sent < queued_.size()) {
      Order* o = free_channel();
      if (o == nullptr) break;
      send_action(*o, queued_[sent].action);
      ++sent;
    }
    if (sent > 0) queued_.erase(queued_.begin(), queued_.begin() + static_cast<std::ptrdiff_t>(sent));
    // A post that waited 100 ms for a connection is stale: refuse it.
    for (auto it = queued_.begin(); it != queued_.end();) {
      if (it->action.kind == engine::ActionKind::post && mono - it->since > 100 * kMs) {
        engine_.on_posted(it->action.coin, it->action.up, it->action.px, it->action.size, false, "no free order connection",
                          net::now_ns());
        it = queued_.erase(it);
      } else {
        ++it;
      }
    }
  }

  // --- control messages, timers ---

  void drain_inbox(std::int64_t mono) {
    inbox_.clear();
    if (!in_.try_drain(inbox_)) return;
    const std::int64_t wall = net::now_ns();
    for (auto& m : inbox_) {
      switch (m.kind) {
        case ToHot::Kind::window: {
          const int coin = engine_.coin_of(m.window.asset);
          if (coin < 0) break;
          engine_.set_window(m.window, wall);
          ++gen_[static_cast<std::size_t>(coin)];
          request_feed(kalshi_);
          request_feed(poly_[static_cast<std::size_t>(coin)]);
          request_feed(user_);
          sync_feed(kalshi_);
          sync_feed(poly_[static_cast<std::size_t>(coin)]);
          sync_feed(user_);
          break;
        }
        case ToHot::Kind::feed: attach_feed(m.feed, std::move(m.ws), mono); break;
        case ToHot::Kind::channel: attach_channel(std::move(m.channel)); break;
        case ToHot::Kind::order_state: engine_.on_order(m.id, m.value, m.flag, wall); break;
        case ToHot::Kind::order_unknown: engine_.on_unknown(m.id, wall); break;
        case ToHot::Kind::balance: engine_.on_balance(m.id, m.value, wall); break;
        case ToHot::Kind::settle: engine_.settle(m.window.asset, m.open_unix, m.text, wall); break;
        case ToHot::Kind::redeemed: engine_.on_redeemed(m.id, m.flag, m.text, wall); break;
        case ToHot::Kind::kill:
          if (m.flag && !kill_) {
            engine::Action all;
            all.kind = engine::ActionKind::cancel_all;
            all.reason = "KILL file";
            engine_.actions().push_back(std::move(all));
            log("KILL file: pulling everything");
          }
          kill_ = m.flag;
          break;
        case ToHot::Kind::ack: engine_.ack(wall); break;
      }
    }
  }

  void housekeeping(std::int64_t mono) {
    const std::int64_t wall = net::now_ns();
    const std::int64_t stale = config_.feed_stale_ms * kMs;
    engine_.set_gates({kill_, wall - last_kalshi_wall_ < stale && wall - last_poly_wall_ < stale});
    engine_.tick(wall);

    for (auto* f : all_feeds()) {
      if (!f->ws) {
        request_feed(*f);
        continue;
      }
      const std::int64_t silence = f->id.kind == FeedKind::poly ? config_.poly_silence_ms * kMs : 60 * kSec;
      if (mono - f->last_rx > silence) {
        log(feed_name(f->id, engine_) + " silent: reconnecting");
        close_feed(*f, true);
        continue;
      }
      const std::int64_t every = f->id.kind == FeedKind::kalshi ? 20 * kSec : 10 * kSec;
      if (mono - f->last_ping > every) {
        f->last_ping = mono;
        if (f->id.kind == FeedKind::kalshi) f->ws->send_ping();
        else f->ws->send_text("PING");
      }
    }
    for (std::size_t i = 0; i < orders_.size(); ++i) {
      auto& o = orders_[i];
      if (!o.ch) {
        request_channel(o);
        continue;
      }
      if (o.ch->busy() && mono - o.ch->sent_ns() > 3 * kSec) {
        log("order connection timed out");
        Pending p = std::move(o.pending);
        o.pending = Pending{};
        fail_pending(p, "timeout");
        close_channel(o);
        request_channel(o);
      } else if (!o.ch->busy() && mono - o.ch->last_used_ns() > 15 * kSec) {
        // Keep the connection warm.
        request_.clear();
        http::write_request(request_, "GET", clob_url_.host, "/time", "", "");
        if (o.ch->send(request_)) {
          o.pending = Pending{};
          o.pending.kind = Pending::Kind::keepalive;
        }
      }
    }
    // What each slot wants, for the signer.
    for (std::size_t i = 0; i < engine_.coins(); ++i) {
      const auto* w = engine_.window(static_cast<int>(i));
      for (const bool up : {true, false}) {
        const int slot = static_cast<int>(i) * 2 + (up ? 0 : 1);
        const auto t = engine_.target(static_cast<int>(i), up);
        if (w == nullptr || !t) {
          presigner_.want(slot, gen_[i], 0, 0, {}, false);
          continue;
        }
        presigner_.want(slot, gen_[i], std::lround(t->first * 100.0), std::lround(t->second), up ? w->up_token : w->down_token,
                        w->neg_risk);
      }
    }
    publish(false);
    if (mono - last_status_ > 60 * kSec) {
      last_status_ = mono;
      status_line();
    }
  }

  void publish(bool force) {
    for (auto& line : engine_.log()) log(std::move(line));
    engine_.log().clear();
    if (engine_.dirty) {
      engine_.dirty = false;
      ToControl m;
      m.kind = ToControl::Kind::save;
      m.text = engine_.save_json();
      out_.push(std::move(m));
    }
    const std::int64_t mono = net::mono_ns();
    if (!force && mono - last_snapshot_ < kSec) return;
    last_snapshot_ = mono;
    auto snap = std::make_shared<Snapshot>();
    for (const auto& [id, o] : engine_.orders()) snap->tracked.emplace_back(id, o.posted_ns);
    const std::int64_t wall = net::now_ns();
    snap->held = engine_.held_tokens(wall);
    snap->unsettled = engine_.unsettled(wall);
    snap->redeem_due = engine_.redeem_due();
    for (std::size_t i = 0; i < engine_.coins(); ++i) {
      const auto* w = engine_.window(static_cast<int>(i));
      snap->window_open.push_back(w ? w->open_unix : 0);
    }
    ToControl m;
    m.kind = ToControl::Kind::snapshot;
    m.snapshot = std::move(snap);
    out_.push(std::move(m));
  }

  void status_line() {
    const auto& c = engine_.counters();
    std::string s = "status:";
    for (std::size_t i = 0; i < engine_.coins(); ++i) {
      s += " " + engine_.asset(static_cast<int>(i)) + "=" + engine_.gate(static_cast<int>(i));
      for (const bool up : {true, false})
        if (const auto r = engine_.resting(static_cast<int>(i), up)) {
          char buf[24];
          std::snprintf(buf, sizeof(buf), "%s%.2f", up ? "^" : "v", r->second);
          s += buf;
        }
    }
    s += " | posts " + std::to_string(c.posts) + " cancels " + std::to_string(c.cancels) + " fills " + std::to_string(c.fills) +
         " pauses " + std::to_string(c.pauses) + " rejects " +
         std::to_string(c.post_only_rejects + c.balance_rejects + c.post_errors) + " presign " +
         std::to_string(presign_hits_) + "/" + std::to_string(presign_hits_ + presign_misses_);
    char buf[64];
    std::snprintf(buf, sizeof(buf), " | realised today %.2f open cost %.2f", engine_.today_realised(net::now_ns()),
                  engine_.open_cost());
    s += buf;
    log(std::move(s));
  }

  const Config& config_;
  const venue::Wallet& wallet_;
  engine::Engine& engine_;
  presign::Presigner& presigner_;
  Mailbox<ToHot>& in_;
  Mailbox<ToControl>& out_;
  Connector& connector_;
  std::atomic<std::int64_t>& heartbeat_;
  clob::L2Signer l2_;
  std::string owner_;
  net::Url clob_url_;

  int epoll_ = -1;
  Feed kalshi_;
  Feed user_;
  std::vector<Feed> poly_;
  std::vector<Order> orders_;
  std::vector<Queued> queued_;
  std::vector<std::uint32_t> gen_;
  feeds::KalshiSession session_;
  bool kalshi_dead_ = false;
  bool kill_ = false;
  std::int64_t last_kalshi_wall_ = 0;
  std::int64_t last_poly_wall_ = 0;
  std::int64_t last_tick_ = 0;
  std::int64_t last_snapshot_ = 0;
  std::int64_t last_status_ = 0;
  std::uint64_t presign_hits_ = 0;
  std::uint64_t presign_misses_ = 0;

  json::Document doc_;
  std::vector<std::string> send_;
  std::vector<char> touched_;
  std::vector<feeds::OrderUpdate> updates_;
  std::vector<ToHot> inbox_;
  std::string body_;
  std::string request_;
  std::string headers_;
};

// --- redeemer ----------------------------------------------------------------------

class Redeemer final {
 public:
  Redeemer(const Config& config, const venue::Wallet& wallet, Mailbox<ToHot>& to_hot)
      : relayer_(config, wallet), to_hot_(to_hot) {}
  [[nodiscard]] bool configured() const noexcept { return relayer_.configured(); }
  void submit(const std::string& key, const std::string& condition) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (busy_.contains(key)) return;
    busy_.insert(key);
    queue_.emplace_back(key, condition);
    cv_.notify_one();
  }
  void run(const std::atomic<bool>& stop) {
    while (!stop.load()) {
      std::pair<std::string, std::string> job;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait_for(lock, std::chrono::milliseconds(500), [this] { return !queue_.empty(); });
        if (queue_.empty()) continue;
        job = queue_.front();
        queue_.pop_front();
      }
      std::string result;
      const bool ok = relayer_.redeem(job.second, result);
      ToHot m;
      m.kind = ToHot::Kind::redeemed;
      m.id = job.first;
      m.flag = ok;
      m.text = result;
      to_hot_.push(std::move(m));
      // Let the next snapshot show the result before this key can queue again.
      std::this_thread::sleep_for(std::chrono::seconds(3));
      std::lock_guard<std::mutex> lock(mutex_);
      busy_.erase(job.first);
    }
  }

 private:
  venue::Relayer relayer_;
  Mailbox<ToHot>& to_hot_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<std::pair<std::string, std::string>> queue_;
  std::set<std::string> busy_;
};

// --- control thread -------------------------------------------------------------

class Control final {
 public:
  Control(const Startup& s, const std::vector<std::string>& assets, Mailbox<ToHot>& to_hot, Mailbox<ToControl>& in,
          Redeemer& redeemer, std::atomic<std::int64_t>& heartbeat)
      : config_(*s.config),
        assets_(assets),
        clob_(*s.config, *s.wallet, s.credentials),
        discovery_(*s.config),
        to_hot_(to_hot),
        in_(in),
        redeemer_(redeemer),
        heartbeat_(heartbeat) {}

  void run(const std::atomic<bool>& stop) {
    std::filesystem::create_directories(config_.state_dir);
    if (!redeemer_.configured()) platform::log("POLY_RELAYER_API_KEY is not set: settled winners are not redeemed");
    while (!stop.load()) {
      const std::int64_t mono = net::mono_ns();
      drain();
      files(mono);
      deadman(mono);
      if (mono - last_discovery_ > 2 * kSec) {
        last_discovery_ = mono;
        discover();
      }
      if (!hints_.empty() || mono - last_fills_ > config_.fills_poll_ms * kMs) {
        if (hints_.empty()) last_fills_ = mono;
        fills();
      }
      if (mono - last_reconcile_ > config_.reconcile_s * kSec) {
        last_reconcile_ = mono;
        reconcile();
      }
      if (mono - last_settle_ > 15 * kSec) {
        last_settle_ = mono;
        settle();
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    drain();
  }

  // After the hot thread stopped: what still rests is cancelled, and late fills are read.
  void final_pass(engine::Engine& engine) {
    int n = 0;
    std::string error;
    if (!clob_.cancel_all(n, error)) platform::log("shutdown cancel-all: " + error);
    else platform::log("shutdown cancel-all: " + std::to_string(n) + " cancelled");
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    for (const auto& id : engine.tracked()) {
      std::optional<venue::Clob::OrderState> st;
      if (clob_.order_state(id, st, error) && st) engine.on_order(id, st->matched, !st->live, net::now_ns());
    }
    for (auto& line : engine.log()) platform::log(line);
    engine.log().clear();
    save(engine.save_json());
  }

 private:
  void drain() {
    batch_.clear();
    in_.drain(batch_);
    for (auto& m : batch_) {
      switch (m.kind) {
        case ToControl::Kind::save: save(m.text); break;
        case ToControl::Kind::log: platform::log(m.text); break;
        case ToControl::Kind::snapshot: snapshot_ = m.snapshot; break;
        case ToControl::Kind::hint: hints_.insert(m.text); break;
      }
    }
  }

  void save(const std::string& text) {
    const std::string path = config_.state_file();
    const std::string tmp = path + ".tmp";
    {
      std::ofstream out(tmp, std::ios::trunc);
      out << text;
      if (!out) {
        platform::log("state write failed: " + tmp);
        return;
      }
    }
    std::filesystem::rename(tmp, path);
  }

  void files(std::int64_t mono) {
    if (mono - last_files_ < 250 * kMs) return;
    last_files_ = mono;
    const bool kill = std::filesystem::exists(config_.kill_file);
    if (kill != killed_ || !kill_sent_) {
      killed_ = kill;
      kill_sent_ = true;
      ToHot m;
      m.kind = ToHot::Kind::kill;
      m.flag = kill;
      to_hot_.push(std::move(m));
    }
    std::error_code ec;
    if (std::filesystem::exists(config_.ack_file(), ec)) {
      std::filesystem::remove(config_.ack_file(), ec);
      ToHot m;
      m.kind = ToHot::Kind::ack;
      to_hot_.push(std::move(m));
    }
  }

  void deadman(std::int64_t mono) {
    const bool stalled = mono - heartbeat_.load(std::memory_order_relaxed) > 2 * kSec;
    if (stalled && !deadman_fired_) {
      deadman_fired_ = true;
      int n = 0;
      std::string error;
      platform::log("DEAD MAN: hot loop stalled for 2 s, cancel-all");
      if (!clob_.cancel_all(n, error)) platform::log("dead man cancel-all failed: " + error);
    } else if (!stalled) {
      deadman_fired_ = false;
    }
  }

  void discover() {
    const std::int64_t open = venue::window_open(net::now_ns() / kSec);
    for (std::size_t i = 0; i < assets_.size(); ++i) {
      if (snapshot_ && i < snapshot_->window_open.size() && snapshot_->window_open[i] == open) continue;
      if (sent_open_.size() <= i) sent_open_.resize(assets_.size(), 0);
      if (sent_open_[i] == open) continue;  // sent; the snapshot will show it
      std::optional<engine::Window> w;
      std::string error;
      if (!discovery_.discover(assets_[i], open, w, error)) {
        platform::log("discover " + assets_[i] + ": " + error);
        continue;
      }
      if (!w) continue;
      sent_open_[i] = open;
      platform::log("window " + w->asset + " " + std::to_string(w->open_unix) + ": kalshi " + w->kalshi_ticker +
                    ", condition " + w->condition_id + (w->neg_risk ? " (neg risk)" : ""));
      ToHot m;
      m.kind = ToHot::Kind::window;
      m.window = *w;
      to_hot_.push(std::move(m));
    }
  }

  void fills() {
    std::string error;
    // The user channel's trade hints first: those orders, now.
    const auto hints = std::move(hints_);
    hints_.clear();
    for (const auto& id : hints) order_state(id);
    if (!snapshot_ || snapshot_->tracked.empty() || !hints.empty()) return;
    std::vector<venue::Clob::OpenOrder> open;
    if (!clob_.open_orders(open, error)) {
      platform::log(error);
      return;
    }
    std::map<std::string, double> matched;
    for (const auto& o : open) matched[o.id] = o.matched;
    for (const auto& [id, posted] : snapshot_->tracked) {
      if (const auto it = matched.find(id); it != matched.end()) {
        ToHot m;
        m.kind = ToHot::Kind::order_state;
        m.id = id;
        m.value = it->second;
        m.flag = false;
        to_hot_.push(std::move(m));
        continue;
      }
      order_state(id);
    }
  }

  void order_state(const std::string& id) {
    std::optional<venue::Clob::OrderState> st;
    std::string error;
    if (!clob_.order_state(id, st, error)) {
      platform::log(error);
      return;
    }
    ToHot m;
    m.id = id;
    if (st) {
      m.kind = ToHot::Kind::order_state;
      m.value = st->matched;
      m.flag = !st->live;
    } else {
      m.kind = ToHot::Kind::order_unknown;
    }
    to_hot_.push(std::move(m));
  }

  void reconcile() {
    std::string error;
    double cash = 0;
    if (clob_.cash(cash, error)) {
      if (std::abs(cash - last_cash_) > 0.005) platform::log("cash " + std::to_string(cash));
      last_cash_ = cash;
    } else {
      platform::log(error);
    }
    if (!snapshot_) return;
    for (const auto& [token, ours] : snapshot_->held) {
      double wallet = 0;
      if (!clob_.token_balance(token, wallet, error)) {
        platform::log(error);
        continue;
      }
      ToHot m;
      m.kind = ToHot::Kind::balance;
      m.id = token;
      m.value = wallet;
      to_hot_.push(std::move(m));
    }
    // Orders resting on the account that the engine does not track (a post
    // whose answer was lost): cancelled once two passes in a row saw them
    // untracked and they are 10 s old, so a post still being acknowledged is safe.
    std::vector<venue::Clob::OpenOrder> open;
    if (!clob_.open_orders(open, error)) return;
    std::set<std::string> tracked;
    for (const auto& [id, posted] : snapshot_->tracked) tracked.insert(id);
    const std::int64_t now_s = net::now_ns() / kSec;
    std::set<std::string> untracked;
    for (const auto& o : open) {
      if (tracked.contains(o.id)) continue;
      untracked.insert(o.id);
      if (!orphans_.contains(o.id) || (o.created_s > 0 && now_s - o.created_s < 10)) continue;
      std::string why;
      const bool ok = clob_.cancel(o.id, why);
      platform::log("orphan order " + o.id + (ok ? " cancelled" : " cancel failed: " + why));
    }
    orphans_ = std::move(untracked);
  }

  void settle() {
    if (!snapshot_) return;
    for (const auto& v : snapshot_->unsettled) {
      std::string outcome;
      std::string error;
      if (!discovery_.outcome(v.asset, v.open_unix, outcome, error)) {
        platform::log("outcome " + v.asset + " " + std::to_string(v.open_unix) + ": " + error);
        continue;
      }
      if (outcome.empty()) continue;
      ToHot m;
      m.kind = ToHot::Kind::settle;
      m.window.asset = v.asset;
      m.open_unix = v.open_unix;
      m.text = outcome;
      to_hot_.push(std::move(m));
    }
    if (!redeemer_.configured()) return;
    for (const auto& [key, condition] : snapshot_->redeem_due) redeemer_.submit(key, condition);
  }

  const Config& config_;
  const std::vector<std::string>& assets_;
  venue::Clob clob_;
  venue::Discovery discovery_;
  Mailbox<ToHot>& to_hot_;
  Mailbox<ToControl>& in_;
  Redeemer& redeemer_;
  std::atomic<std::int64_t>& heartbeat_;
  std::shared_ptr<const Snapshot> snapshot_;
  std::vector<ToControl> batch_;
  std::set<std::string> hints_;
  std::set<std::string> orphans_;  // untracked on the last reconcile pass
  std::vector<std::int64_t> sent_open_;
  std::int64_t last_discovery_ = 0, last_fills_ = 0, last_reconcile_ = 0, last_settle_ = 0, last_files_ = 0;
  bool killed_ = false;
  bool kill_sent_ = false;
  bool deadman_fired_ = false;
  double last_cash_ = -1;
};

}  // namespace

int run(const Startup& s, std::atomic<bool>& stop) {
  const Config& config = *s.config;
  engine::Engine engine(config.strategy, config.assets);
  {
    std::ifstream in(config.state_file());
    if (in) {
      const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
      std::string error;
      if (!engine.load_json(text, error)) {
        platform::log("state " + config.state_file() + ": " + error + " (refusing to start over a broken state)");
        return 1;
      }
      platform::log("state loaded: " + std::to_string(engine.inventories().size()) + " windows, " +
                    std::to_string(engine.orders().size()) + " orders tracked");
    }
  }
  Mailbox<ToHot> to_hot;
  Mailbox<ToControl> to_control;
  std::atomic<std::int64_t> heartbeat{net::mono_ns()};
  std::atomic<bool> stop_aux{false};
  std::atomic<bool> stop_hot{false};

  presign::Presigner presigner(*s.wallet, s.credentials.api_key, config.presign_ttl_ms);
  Connector connector(config, to_hot);
  Redeemer redeemer(config, *s.wallet, to_hot);
  Control control(s, config.assets, to_hot, to_control, redeemer, heartbeat);
  Hot hot(s, engine, presigner, to_hot, to_control, connector, heartbeat);

  std::thread signer_thread([&] { presigner.run(stop_aux, config.signer_core); });
  std::thread connector_thread([&] { connector.run(stop_aux); });
  std::thread redeemer_thread([&] { redeemer.run(stop_aux); });
  std::thread control_thread([&] { control.run(stop_aux); });
  std::thread hot_thread([&] { hot.run(stop_hot); });

  while (!stop.load()) std::this_thread::sleep_for(std::chrono::milliseconds(100));
  platform::log("stopping: pulling quotes");
  stop_hot.store(true);
  hot_thread.join();
  stop_aux.store(true);
  control_thread.join();
  signer_thread.join();
  connector_thread.join();
  redeemer_thread.join();
  control.final_pass(engine);
  platform::log("stopped");
  return 0;
}

}  // namespace pkm::runtime
