// End to end: `runtime::run` against an in-process mock of every venue route it
// uses (CLOB REST, Gamma, Kalshi REST, the Kalshi and Polymarket sockets), on
// one plain-HTTP port. It checks the whole live path: discovery, feeds, signed
// post-only bids at the rule's prices, the pause cancels on a Kalshi jump,
// a fill from the user channel booked into inventory, cancel-all on stop, and
// the state file.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/sha.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

#include "pkm/clob.hpp"
#include "pkm/config.hpp"
#include "pkm/crypto.hpp"
#include "pkm/eip712.hpp"
#include "pkm/feeds.hpp"
#include "pkm/json.hpp"
#include "pkm/net.hpp"
#include "pkm/runtime.hpp"
#include "pkm/venue.hpp"

using namespace pkm;

namespace {

const std::string kKey = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
const std::string kFunder = "0x59316c3d15C292D2c7a9EC206eCB6c6bAAdeAd3e";
const std::string kApiKey = "0b0c7c6e-2b8f-4f4e-9d55-7c2f3b1a9e11";
const std::string kSecret = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3I=";
const std::string kUp = "71321045679252212594626385532706912750332728571942532289631379312455583992563";
const std::string kDown = "52114319501245915516055106046884209969926127482827954674443846427813813222426";
const std::string kCondition = "0xaabc35349bc01a07a0ad8d863866175165a0640fddd0985e69f94570e0054c42";

int failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "  ok   " : "  FAIL ") << what << "\n";
  if (!ok) ++failures;
}

std::string iso(std::int64_t unix_s) {
  const std::time_t t = unix_s;
  std::tm tm{};
  gmtime_r(&t, &tm);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buf;
}

struct Conn {
  int fd = -1;
  std::mutex write_mutex;
  std::string path;
  void write(std::string_view s) {
    std::lock_guard<std::mutex> lock(write_mutex);
    while (!s.empty()) {
      const ssize_t n = ::send(fd, s.data(), s.size(), MSG_NOSIGNAL);
      if (n <= 0) return;
      s.remove_prefix(static_cast<std::size_t>(n));
    }
  }
  void ws_text(std::string_view payload) {
    std::string f;
    f.push_back(static_cast<char>(0x81));
    if (payload.size() < 126) {
      f.push_back(static_cast<char>(payload.size()));
    } else {
      f.push_back(126);
      f.push_back(static_cast<char>(payload.size() >> 8));
      f.push_back(static_cast<char>(payload.size() & 0xFF));
    }
    f.append(payload);
    write(f);
  }
};

class Mock final {
 public:
  struct Order {
    std::string id, token;
    std::int64_t cents = 0;
    std::int64_t shares = 0;
    double matched = 0;
    bool live = true;
  };

  Mock() {
    fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    const int one = 1;
    ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    ::listen(fd_, 32);
    socklen_t len = sizeof(addr);
    ::getsockname(fd_, reinterpret_cast<sockaddr*>(&addr), &len);
    port_ = ntohs(addr.sin_port);
    std::string error;
    signer_.load(kKey, error);
    parse_address(kFunder, funder_);
    clob::Credentials c;
    c.api_key = kApiKey;
    base64_decode(kSecret, c.secret, true);
    c.passphrase = "pp";
    c.address = checksum_address(signer_.address());
    l2_ = std::make_unique<clob::L2Signer>(c);
    open_ = venue::window_open(net::now_ns() / 1'000'000'000LL);
    accept_ = std::thread([this] { accept_loop(); });
  }
  ~Mock() {
    stop_ = true;
    ::shutdown(fd_, SHUT_RDWR);
    ::close(fd_);
    accept_.join();
    std::lock_guard<std::mutex> lock(mutex_);
    for (auto& c : conns_) ::shutdown(c->fd, SHUT_RDWR);
    for (auto& t : threads_) t.detach();
  }
  [[nodiscard]] std::uint16_t port() const { return port_; }
  [[nodiscard]] std::int64_t open() const { return open_; }

  // Polls `pred` (which takes the lock itself) until it holds or `ms` passes.
  static bool wait(const std::function<bool()>& pred, int ms) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(ms);
    while (std::chrono::steady_clock::now() < deadline) {
      if (pred()) return true;
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return pred();
  }
  std::vector<Order> posts() {
    std::lock_guard<std::mutex> lock(mutex_);
    return posted_;
  }
  int count(const std::string& route) {
    std::lock_guard<std::mutex> lock(mutex_);
    return routes_[route];
  }
  std::vector<std::string> errors() {
    std::lock_guard<std::mutex> lock(mutex_);
    return errors_;
  }
  std::string user_subscription() {
    std::lock_guard<std::mutex> lock(mutex_);
    return user_sub_;
  }
  void kalshi_send(const std::string& msg) {
    Conn* c = nullptr;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      c = kalshi_;
    }
    if (c != nullptr) c->ws_text(msg);
  }
  int next_kalshi_seq() {
    std::lock_guard<std::mutex> lock(mutex_);
    return ++kalshi_seq_;
  }
  // The venue matched `id` in full: order state and a user-channel event.
  void fill(const std::string& id) {
    Conn* c = nullptr;
    std::string shares;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      auto& o = orders_[id];
      o.matched = static_cast<double>(o.shares);
      o.live = false;
      shares = std::to_string(o.shares);
      held_[o.token] += static_cast<double>(o.shares);
      c = user_;
    }
    if (c != nullptr)
      c->ws_text(R"({"event_type":"order","id":")" + id + R"(","original_size":")" + shares + R"(","size_matched":")" + shares +
                 R"(","type":"UPDATE"})");
  }

 private:
  void accept_loop() {
    while (!stop_) {
      const int c = ::accept(fd_, nullptr, nullptr);
      if (c < 0) continue;
      auto conn = std::make_shared<Conn>();
      conn->fd = c;
      std::lock_guard<std::mutex> lock(mutex_);
      conns_.push_back(conn);
      threads_.emplace_back([this, conn] { serve(conn); });
    }
  }

  static bool read_more(int fd, std::string& buf) {
    char tmp[8192];
    const ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);
    if (n <= 0) return false;
    buf.append(tmp, static_cast<std::size_t>(n));
    return true;
  }

  void serve(std::shared_ptr<Conn> conn) {
    std::string buf;
    while (!stop_) {
      std::size_t head_end;
      while ((head_end = buf.find("\r\n\r\n")) == std::string::npos)
        if (!read_more(conn->fd, buf)) return;
      const std::string head = buf.substr(0, head_end + 4);
      buf.erase(0, head_end + 4);
      std::size_t length = 0;
      if (const auto at = head.find("Content-Length: "); at != std::string::npos) length = std::stoul(head.substr(at + 16));
      while (buf.size() < length)
        if (!read_more(conn->fd, buf)) return;
      const std::string body = buf.substr(0, length);
      buf.erase(0, length);
      const auto sp1 = head.find(' ');
      const auto sp2 = head.find(' ', sp1 + 1);
      const std::string method = head.substr(0, sp1);
      const std::string target = head.substr(sp1 + 1, sp2 - sp1 - 1);
      if (head.find("Upgrade: websocket") != std::string::npos) {
        websocket(conn, head, target, buf);
        return;
      }
      const auto [status, reply] = rest(method, target, head, body);
      conn->write("HTTP/1.1 " + std::to_string(status) + " X\r\nContent-Type: application/json\r\nContent-Length: " +
                  std::to_string(reply.size()) + "\r\n\r\n" + reply);
    }
  }

  static std::string header(const std::string& head, const std::string& name) {
    const auto at = head.find(name + ": ");
    if (at == std::string::npos) return {};
    const auto start = at + name.size() + 2;
    return head.substr(start, head.find("\r\n", start) - start);
  }

  bool l2_ok(const std::string& head, const std::string& method, const std::string& path, const std::string& body) {
    const auto ts = header(head, "POLY_TIMESTAMP");
    if (ts.empty() || header(head, "POLY_API_KEY") != kApiKey) return false;
    return l2_->signature(std::stoll(ts), method, path, body) == header(head, "POLY_SIGNATURE");
  }

  void note_error(const std::string& e) {
    errors_.push_back(e);
  }

  std::pair<int, std::string> rest(const std::string& method, const std::string& target, const std::string& head,
                                   const std::string& body) {
    const auto q = target.find('?');
    const std::string path = target.substr(0, q);
    const std::string query = q == std::string::npos ? "" : target.substr(q + 1);
    std::lock_guard<std::mutex> lock(mutex_);
    ++routes_[method + " " + path];
    cv_.notify_all();
    const bool authed = path.starts_with("/data/") || path == "/order" || path == "/cancel-all" || path == "/balance-allowance";
    if (authed && !l2_ok(head, method, path, body)) {
      note_error("bad L2 signature on " + method + " " + path);
      return {401, R"({"error":"Unauthorized"})"};
    }
    if (path == "/version") return {200, R"({"version":2})"};
    if (path == "/time") return {200, "1761500000"};
    if (path == "/auth/derive-api-key") {
      Address claimed{};
      parse_address(header(head, "POLY_ADDRESS"), claimed);
      Signature65 sig{};
      parse_hex(header(head, "POLY_SIGNATURE"), sig.data(), sig.size());
      Address got{};
      const auto digest = eip712::clob_auth_digest(claimed, std::stoll(header(head, "POLY_TIMESTAMP")), 0);
      if (!recover_address(digest, sig, got) || got != signer_.address()) note_error("bad L1 signature");
      return {200, R"({"apiKey":")" + kApiKey + R"(","secret":")" + kSecret + R"(","passphrase":"pp"})"};
    }
    if (path == "/gamma/events") {
      const std::string slug = query.substr(query.find("slug=") + 5);
      const std::int64_t open = std::stoll(slug.substr(slug.rfind('-') + 1));
      return {200, R"([{"markets":[{"eventStartTime":")" + iso(open) +
                       R"(","outcomes":"[\"Up\", \"Down\"]","clobTokenIds":"[\")" + kUp + R"(\", \")" + kDown +
                       R"(\"]","conditionId":")" + kCondition + R"(","negRisk":false,"closed":false}]}])"};
    }
    if (path == "/trade-api/v2/markets") {
      const auto at = query.find("min_close_ts=") + 13;
      const std::int64_t close = std::stoll(query.substr(at, query.find('&', at) - at)) + 1;
      return {200, R"({"markets":[{"ticker":"KXBTC15M-T)" + std::to_string(close) + R"(","open_time":")" + iso(close - 900) +
                       R"(","close_time":")" + iso(close) + R"("}]})"};
    }
    if (path == "/balance-allowance") {
      if (query.find("COLLATERAL") != std::string::npos) return {200, R"({"balance":"100000000"})"};
      const auto at = query.find("token_id=") + 9;
      const std::string token = query.substr(at, query.find('&', at) - at);
      return {200, R"({"balance":")" + std::to_string(static_cast<long long>(held_[token] * 1e6)) + R"("})"};
    }
    if (path == "/cancel-all") {
      std::string ids;
      for (auto& [id, o] : orders_)
        if (o.live) {
          o.live = false;
          ids += (ids.empty() ? "\"" : ",\"") + id + "\"";
        }
      return {200, R"({"canceled":[)" + ids + R"(],"not_canceled":{}})"};
    }
    if (path == "/order" && method == "DELETE") {
      json::Document doc;
      doc.parse(body);
      const std::string id = doc.root()["orderID"].string();
      auto it = orders_.find(id);
      if (it == orders_.end() || !it->second.live) return {200, R"({"canceled":[],"not_canceled":{")" + id + R"(":"not live"}})"};
      it->second.live = false;
      return {200, R"({"canceled":[")" + id + R"("],"not_canceled":{}})"};
    }
    if (path == "/order" && method == "POST") {
      if (body == "{}") return {400, R"({"error":"Invalid order payload"})"};
      return post_order(body);
    }
    if (path == "/data/orders") {
      std::string list;
      for (const auto& [id, o] : orders_)
        if (o.live)
          list += (list.empty() ? "" : ",") + std::string(R"({"id":")") + id + R"(","asset_id":")" + o.token +
                  R"(","original_size":")" + std::to_string(o.shares) + R"(","size_matched":")" + std::to_string(o.matched) +
                  R"(","created_at":)" + std::to_string(net::now_ns() / 1'000'000'000LL) + "}";
      return {200, R"({"data":[)" + list + R"(],"next_cursor":"LTE="})"};
    }
    if (path.starts_with("/data/order/")) {
      const auto it = orders_.find(path.substr(12));
      if (it == orders_.end()) return {200, "null"};
      return {200, R"({"id":")" + it->first + R"(","status":")" + (it->second.live ? "LIVE" : "MATCHED") +
                       R"(","size_matched":")" + std::to_string(it->second.matched) + R"("})"};
    }
    note_error("unexpected route " + method + " " + target);
    return {404, R"({"error":"not found"})"};
  }

  // Checks the order the way the venue would, and that its signature is the
  // deterministic one for its fields.
  std::pair<int, std::string> post_order(const std::string& body) {
    json::Document doc;
    if (!doc.parse(body)) {
      note_error("order body is not JSON");
      return {400, R"({"error":"bad json"})"};
    }
    const auto root = doc.root();
    const auto o = root["order"];
    eip712::OrderV2 order;
    order.salt = static_cast<std::uint64_t>(o["salt"].integer_or(0));
    parse_address(o["maker"].raw(), order.maker);
    parse_address(o["signer"].raw(), order.signer);
    decimal_to_u256(o["tokenId"].raw(), order.token_id);
    order.maker_amount = static_cast<std::uint64_t>(o["makerAmount"].integer_or(0));
    order.taker_amount = static_cast<std::uint64_t>(o["takerAmount"].integer_or(0));
    order.side = o["side"].raw() == "BUY" ? 0 : 1;
    order.signature_type = static_cast<eip712::SignatureType>(o["signatureType"].integer_or(0));
    order.timestamp_ms = static_cast<std::uint64_t>(o["timestamp"].integer_or(0));
    std::string problem;
    if (!root["postOnly"].boolean_or(false)) problem = "not post-only";
    if (root["owner"].raw() != kApiKey) problem = "wrong owner";
    if (root["orderType"].raw() != "GTC") problem = "not GTC";
    if (order.maker != funder_ || order.signer != funder_) problem = "maker/signer is not the deposit wallet";
    if (order.signature_type != eip712::SignatureType::deposit_wallet || order.side != 0) problem = "not a type-3 BUY";
    if (eip712::sign_order(signer_, order, false) != o["signature"].raw()) problem = "signature does not match the fields";
    const std::int64_t shares = static_cast<std::int64_t>(order.taker_amount / 1'000'000);
    const std::int64_t cents = static_cast<std::int64_t>(order.maker_amount / 10'000) / std::max<std::int64_t>(shares, 1);
    if (!problem.empty()) {
      note_error("POST /order: " + problem);
      return {400, R"({"error":")" + problem + R"("})"};
    }
    Order rec;
    char id[16];
    std::snprintf(id, sizeof(id), "0x%04d", ++next_id_);
    rec.id = id;
    rec.token = std::string(o["tokenId"].raw());
    rec.cents = cents;
    rec.shares = shares;
    orders_[rec.id] = rec;
    posted_.push_back(rec);
    cv_.notify_all();
    return {200, R"({"success":true,"errorMsg":"","orderID":")" + rec.id + R"(","status":"live"})"};
  }

  static std::string accept_key(const std::string& key) {
    const std::string input = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    unsigned char digest[SHA_DIGEST_LENGTH];
    SHA1(reinterpret_cast<const unsigned char*>(input.data()), input.size(), digest);
    return base64_encode(digest, sizeof(digest), false);
  }

  // Client frames are masked; returns (opcode, payload) or opcode -1 on close.
  static std::pair<int, std::string> read_frame(int fd, std::string& buf) {
    while (true) {
      if (buf.size() >= 2) {
        std::size_t len = static_cast<std::uint8_t>(buf[1]) & 0x7F;
        std::size_t at = 2;
        if (len == 126 && buf.size() >= 4) {
          len = (static_cast<std::size_t>(static_cast<std::uint8_t>(buf[2])) << 8) | static_cast<std::uint8_t>(buf[3]);
          at = 4;
        }
        if (len < 126 || at == 4) {
          if (buf.size() >= at + 4 + len) {
            const int op = static_cast<std::uint8_t>(buf[0]) & 0x0F;
            std::string payload = buf.substr(at + 4, len);
            for (std::size_t i = 0; i < len; ++i) payload[i] = static_cast<char>(payload[i] ^ buf[at + (i % 4)]);
            buf.erase(0, at + 4 + len);
            return {op, payload};
          }
        }
      }
      if (!read_more(fd, buf)) return {-1, {}};
    }
  }

  void websocket(const std::shared_ptr<Conn>& conn, const std::string& head, const std::string& path, std::string& buf) {
    conn->path = path;
    if (path == "/trade-api/ws/v2" && header(head, "KALSHI-ACCESS-KEY") != "kid") note_error("kalshi upgrade without auth");
    conn->write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " +
                accept_key(header(head, "Sec-WebSocket-Key")) + "\r\n\r\n");
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (path == "/trade-api/ws/v2") kalshi_ = conn.get();
      if (path == "/ws/user") user_ = conn.get();
      ++routes_["WS " + path];
      cv_.notify_all();
    }
    while (!stop_) {
      auto [op, payload] = read_frame(conn->fd, buf);
      if (op < 0 || op == 0x8) break;
      if (op == 0x9) continue;  // ping: the reply is not checked
      if (payload == "PING") {
        conn->ws_text("PONG");
        continue;
      }
      json::Document doc;
      if (!doc.parse(payload)) continue;
      const auto root = doc.root();
      if (path == "/trade-api/ws/v2" && root["cmd"].raw() == "subscribe") {
        const std::string ticker = root["params"]["market_tickers"].at(0).string();
        conn->ws_text(R"({"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1}})");
        conn->ws_text(R"({"type":"orderbook_snapshot","sid":1,"seq":)" + std::to_string(next_kalshi_seq()) +
                      R"(,"msg":{"market_ticker":")" + ticker + R"(","yes_dollars_fp":[["0.5400","100.00"]],"no_dollars_fp":[["0.4400","100.00"]]}})");
        std::lock_guard<std::mutex> lock(mutex_);
        kalshi_ticker_ = ticker;
      } else if (path == "/ws/market") {
        auto book = [](const std::string& id, const char* bid, const char* ask) {
          return R"({"event_type":"book","asset_id":")" + id + R"(","bids":[{"price":")" + bid + R"(","size":"50"}],"asks":[{"price":")" +
                 ask + R"(","size":"50"}]})";
        };
        conn->ws_text("[" + book(kUp, "0.53", "0.57") + "," + book(kDown, "0.43", "0.47") + "]");
      } else if (path == "/ws/user") {
        std::lock_guard<std::mutex> lock(mutex_);
        user_sub_ = payload;
        cv_.notify_all();
      }
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (kalshi_ == conn.get()) kalshi_ = nullptr;
    if (user_ == conn.get()) user_ = nullptr;
  }

 public:
  std::string kalshi_ticker() {
    std::lock_guard<std::mutex> lock(mutex_);
    return kalshi_ticker_;
  }

 private:
  int fd_ = -1;
  std::uint16_t port_ = 0;
  std::atomic<bool> stop_{false};
  std::thread accept_;
  std::vector<std::thread> threads_;
  std::vector<std::shared_ptr<Conn>> conns_;
  std::mutex mutex_;
  std::condition_variable cv_;
  EcSigner signer_;
  Address funder_{};
  std::unique_ptr<clob::L2Signer> l2_;
  std::int64_t open_ = 0;
  std::map<std::string, int> routes_;
  std::map<std::string, Order> orders_;
  std::vector<Order> posted_;
  std::vector<std::string> errors_;
  std::map<std::string, double> held_;
  std::string user_sub_;
  std::string kalshi_ticker_;
  Conn* kalshi_ = nullptr;
  Conn* user_ = nullptr;
  int next_id_ = 0;
  int kalshi_seq_ = 0;
};

}  // namespace

int main() {
  // Keep clear of a window roll during the ~10 s run.
  const std::int64_t s = net::now_ns() / 1'000'000'000LL % 900;
  if (s > 880) std::this_thread::sleep_for(std::chrono::seconds(902 - s));

  const auto dir = std::filesystem::temp_directory_path() / ("pkm-e2e-" + std::to_string(::getpid()));
  std::filesystem::create_directories(dir);
  EVP_PKEY* key = EVP_PKEY_Q_keygen(nullptr, nullptr, "ED25519");
  {
    FILE* f = std::fopen((dir / "kalshi.pem").c_str(), "w");
    PEM_write_PrivateKey(f, key, nullptr, nullptr, 0, nullptr, nullptr);
    std::fclose(f);
  }
  EVP_PKEY_free(key);

  Mock mock;
  const std::string base = "http://127.0.0.1:" + std::to_string(mock.port());
  std::map<std::string, std::string> vars = {
      {"POLY_PRIVATE_KEY", kKey},
      {"POLY_FUNDER", kFunder},
      {"POLY_SIGNATURE_TYPE", "3"},
      {"KALSHI_API_KEY_ID", "kid"},
      {"KALSHI_PRIVATE_KEY_PATH", (dir / "kalshi.pem").string()},
      {"POLY_CLOB_URL", base},
      {"POLY_WS_URL", "ws://127.0.0.1:" + std::to_string(mock.port()) + "/ws/market"},
      {"POLY_USER_WS_URL", "ws://127.0.0.1:" + std::to_string(mock.port()) + "/ws/user"},
      {"POLY_GAMMA_URL", base + "/gamma"},
      {"KALSHI_WS_URL", "ws://127.0.0.1:" + std::to_string(mock.port()) + "/trade-api/ws/v2"},
      {"KALSHI_REST_URL", base + "/trade-api/v2"},
      {"PK_PM_ASSETS", "BTC"},
      {"PK_PM_ENTRY_EARLY_S", "0"},
      {"PK_PM_STOP_S", "0"},
      {"PK_PM_OPEN_LEG_S", "900"},
      {"PKM_STATE_DIR", (dir / "state").string()},
      {"PKM_KILL_FILE", (dir / "KILL").string()},
      {"PKM_LIVE_CONFIRM", "yes"},
      {"PKM_FILLS_POLL_MS", "500"},
  };
  Config config;
  std::string error;
  if (!load_config(vars, config, error)) {
    std::cerr << "config: " << error << "\n";
    return 1;
  }
  venue::Wallet wallet;
  clob::Credentials creds;
  check(venue::load_wallet(config, wallet, error), "wallet loads");
  check(venue::Clob::derive_credentials(config, wallet, creds, error) && creds.api_key == kApiKey, "L1 derives the API key");
  venue::Clob clob(config, wallet, creds);
  check(clob.check_headers(error), "signed empty POST /order answers 400");
  int version = 0;
  check(clob.version(version, error) && version == 2, "protocol version 2");

  std::atomic<bool> stop{false};
  runtime::Startup startup;
  startup.config = &config;
  startup.wallet = &wallet;
  startup.credentials = creds;
  int code = -1;
  std::thread run([&] { code = runtime::run(startup, stop); });

  // 1. Discovery, feeds, then both bids at the rule's prices: q = 0.55, h = 0.02.
  const bool two = mock.wait([&] { return mock.posts().size() >= 2; }, 15'000);
  auto posts = mock.posts();
  check(two, "two bids posted after discovery and both feeds");
  auto find = [&](const std::string& token, std::size_t from) -> const Mock::Order* {
    for (std::size_t i = from; i < posts.size(); ++i)
      if (posts[i].token == token) return &posts[i];
    return nullptr;
  };
  const auto* up = find(kUp, 0);
  const auto* down = find(kDown, 0);
  check(up != nullptr && up->cents == 53 && up->shares == 5, "Up bid at 0.53 x5");
  check(down != nullptr && down->cents == 43 && down->shares == 5, "Down bid at 0.43 x5");
  check(mock.user_subscription().find(R"("apiKey":")" + kApiKey) != std::string::npos &&
            mock.user_subscription().find(kCondition) != std::string::npos,
        "user channel subscribed with the API key and the window's market");

  // 2. Kalshi jumps 4c within a second: both bids are pulled in that frame.
  const std::string ticker = mock.kalshi_ticker();
  auto delta = [&](const char* price, const char* d, const char* side) {
    mock.kalshi_send(R"({"type":"orderbook_delta","sid":1,"seq":)" + std::to_string(mock.next_kalshi_seq()) +
                     R"(,"msg":{"market_ticker":")" + ticker + R"(","price_dollars":")" + price + R"(","delta_fp":")" + d +
                     R"(","side":")" + side + R"("}})");
  };
  delta("0.4000", "100.00", "no");
  delta("0.4400", "-100.00", "no");
  delta("0.5800", "100.00", "yes");
  const bool pulled = mock.wait([&] { return mock.count("DELETE /order") >= 2; }, 2'000);
  check(pulled, "both bids cancelled on the jump");

  // 3. After the 1.5 s pause: q = 0.59. Up clamps a tick under its 0.57 ask; Down 0.39.
  const std::size_t before = mock.posts().size();
  const bool reposted = mock.wait([&] { return mock.posts().size() >= before + 2; }, 4'000);
  posts = mock.posts();
  up = find(kUp, before);
  down = find(kDown, before);
  check(reposted && up != nullptr && up->cents == 56, "Up requoted at 0.56 after the pause");
  check(down != nullptr && down->cents == 39, "Down requoted at 0.39 after the pause");

  // 4. The Up bid fills (user channel): booked, and Up is quoted again.
  const std::string up_id = up != nullptr ? up->id : "";
  const std::size_t before_fill = mock.posts().size();
  mock.fill(up_id);
  const bool again = mock.wait([&] { return mock.posts().size() > before_fill; }, 3'000);
  check(again, "a fresh Up bid after the fill");

  // 5. Stop: quotes pulled, cancel-all, state written.
  stop = true;
  run.join();
  check(code == 0, "run returns 0");
  check(mock.count("DELETE /cancel-all") >= 1, "cancel-all on stop");
  std::ifstream in(dir / "state" / "state.json");
  const std::string state((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
  json::Document doc;
  const bool parsed = doc.parse(state);
  const auto inv = parsed ? doc.root()["inventories"]["BTC-" + std::to_string(mock.open())] : json::Value();
  check(inv && inv["up"].number_or(0) == 5 && std::abs(inv["up_cost"].number_or(0) - 2.80) < 1e-9,
        "state.json holds the 5 Up shares at 0.56");
  for (const auto& e : mock.errors()) check(false, "venue: " + e);
  std::filesystem::remove_all(dir);
  std::cout << (failures == 0 ? "e2e passed\n" : "e2e FAILED\n");
  return failures == 0 ? 0 : 1;
}
