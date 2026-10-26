// Frame in → request bytes ready, on the hot thread's code path without sockets:
// parse, book update, the engine's decision, signing (pre-signed or inline),
// L2 headers and the HTTP request. Counts heap allocations on each path.

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <string>
#include <vector>

#include "pkm/clob.hpp"
#include "pkm/engine.hpp"
#include "pkm/feeds.hpp"
#include "pkm/http.hpp"
#include "pkm/json.hpp"
#include "pkm/net.hpp"
#include "pkm/presign.hpp"
#include "pkm/venue.hpp"

namespace {
std::atomic<bool> counting{false};
std::atomic<long> allocations{0};
}  // namespace

void* operator new(std::size_t n) {
  if (counting.load(std::memory_order_relaxed)) allocations.fetch_add(1, std::memory_order_relaxed);
  if (void* p = std::malloc(n == 0 ? 1 : n)) return p;
  throw std::bad_alloc();
}
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }

using namespace pkm;

namespace {

constexpr std::int64_t NS = 1'000'000'000LL;
const std::string kUp = "71321045679252212594626385532706912750332728571942532289631379312455583992563";
const std::string kDown = "52114319501245915516055106046884209969926127482827954674443846427813813222426";

struct Stats {
  std::vector<double> us;
  long allocs = 0;
  void print(const char* name) {
    std::sort(us.begin(), us.end());
    auto at = [&](double q) { return us[static_cast<std::size_t>(q * static_cast<double>(us.size() - 1))]; };
    std::printf("%-44s n=%-6zu p50 %7.2f us  p99 %7.2f us  max %7.2f us  allocs/op %.2f\n", name, us.size(), at(0.5), at(0.99),
                us.back(), static_cast<double>(allocs) / static_cast<double>(us.size()));
  }
};

std::string kframe(int seq, const char* price, const char* delta, const char* side) {
  return std::string(R"({"type":"orderbook_delta","sid":1,"seq":)") + std::to_string(seq) +
         R"(,"msg":{"market_ticker":"KXBTC15M-T","price_dollars":")" + price + R"(","delta_fp":")" + delta + R"(","side":")" +
         side + R"(","ts_ms":1761500000000}})";
}

}  // namespace

int main() {
  const std::int64_t open = venue::window_open(net::now_ns() / NS);
  engine::Config cfg;
  cfg.entry_early_s = 0;
  cfg.stop_s = 0;
  cfg.open_leg_s = 900;
  cfg.max_orders_per_s = 1e9;
  engine::Engine e(cfg, {"BTC"});
  engine::Window w;
  w.asset = "BTC";
  w.open_unix = open;
  w.kalshi_ticker = "KXBTC15M-T";
  w.up_token = kUp;
  w.down_token = kDown;
  w.condition_id = "0xc";
  e.set_window(w, open * NS);
  e.set_gates({false, true});

  json::Document doc;
  std::vector<char> touched;
  feeds::KalshiSession session;
  std::vector<std::string> send;
  auto now = [] { return net::now_ns(); };
  doc.parse(R"({"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-T","yes_dollars_fp":[["0.5400","100"],["0.5300","80"]],"no_dollars_fp":[["0.4400","100"],["0.4300","80"]]}})");
  e.on_kalshi_update(feeds::apply_kalshi(doc.root(), e, now()), now());
  const std::string books = R"([{"event_type":"book","asset_id":")" + kUp +
                            R"(","bids":[{"price":"0.53","size":"50"}],"asks":[{"price":"0.57","size":"50"}]},{"event_type":"book","asset_id":")" +
                            kDown + R"(","bids":[{"price":"0.43","size":"50"}],"asks":[{"price":"0.47","size":"50"}]}])";
  doc.parse(books);
  feeds::apply_poly(doc.root(), e, now(), touched, nullptr);
  e.on_poly_update(0, now());
  e.actions().clear();
  e.on_posted(0, true, 0.53, 5, true, "0x" + std::string(64, 'a'), now());
  e.on_posted(0, false, 0.43, 5, true, "0x" + std::string(64, 'b'), now());

  venue::Wallet wallet;
  std::string error;
  wallet.signer.load("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80", error);
  parse_address("0x59316c3d15C292D2c7a9EC206eCB6c6bAAdeAd3e", wallet.funder);
  clob::Credentials creds;
  creds.api_key = "0b0c7c6e-2b8f-4f4e-9d55-7c2f3b1a9e11";
  base64_decode("c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3I=", creds.secret, true);
  creds.passphrase = "pp";
  creds.address = checksum_address(wallet.signer.address());
  clob::L2Signer l2(creds);
  presign::Presigner presigner(wallet, creds.api_key, 60'000);
  std::string body, headers, request;
  body.reserve(presign::kBodyMax);
  headers.reserve(512);
  request.reserve(4096);
  Bytes32 up_id{};
  decimal_to_u256(kUp, up_id);

  // A: a Kalshi delta that changes size, not the mid: parse, book, engine, nothing to send.
  Stats quiet;
  std::vector<std::string> frames;
  for (int i = 0; i < 4000; ++i) frames.push_back(kframe(i + 2, "0.5300", i % 2 ? "-5.00" : "5.00", "yes"));
  doc.parse(R"({"type":"subscribed","id":1,"msg":{"sid":1}})");
  session.on_frame(doc.root(), send);
  for (int round = 0; round < 2; ++round) {
    quiet.us.clear();
    allocations = 0;
    counting = round == 1;
    for (std::size_t k = 0; k < 2000; ++k) {
      const auto& f = frames[static_cast<std::size_t>(round) * 2000 + k];
      const auto t0 = net::mono_ns();
      doc.parse(f);
      send.clear();
      session.on_frame(doc.root(), send);
      const int coin = feeds::apply_kalshi(doc.root(), e, now());
      if (coin >= 0) e.on_kalshi_update(coin, now());
      quiet.us.push_back(static_cast<double>(net::mono_ns() - t0) / 1e3);
    }
    counting = false;
  }
  quiet.allocs = allocations;
  quiet.print("kalshi delta, no action");

  // B: a Kalshi jump that pulls both bids: frame -> two DELETE requests written.
  Stats jump;
  // Kalshi's book a snapshot higher: YES 0.57/0.59, mid +3c within the second.
  const std::string shifted =
      R"({"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-T","yes_dollars_fp":[["0.5700","100"]],"no_dollars_fp":[["0.4100","100"]]}})";
  for (int i = 0; i < 400; ++i) {
    e = engine::Engine(cfg, {"BTC"});
    e.set_window(w, open * NS);
    e.set_gates({false, true});
    doc.parse(R"({"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTC15M-T","yes_dollars_fp":[["0.5400","100"],["0.5300","80"]],"no_dollars_fp":[["0.4400","100"],["0.4300","80"]]}})");
    e.on_kalshi_update(feeds::apply_kalshi(doc.root(), e, now()), now());
    doc.parse(books);
    feeds::apply_poly(doc.root(), e, now(), touched, nullptr);
    e.on_poly_update(0, now());
    e.actions().clear();
    e.on_posted(0, true, 0.53, 5, true, "0x" + std::string(64, 'a'), now());
    e.on_posted(0, false, 0.43, 5, true, "0x" + std::string(64, 'b'), now());
    allocations = 0;
    counting = true;
    const auto t0 = net::mono_ns();
    doc.parse(shifted);
    const int coin = feeds::apply_kalshi(doc.root(), e, now());
    e.on_kalshi_update(coin, now());
    for (auto& act : e.actions()) {
      if (act.kind != engine::ActionKind::cancel) continue;
      clob::write_cancel_body(body, act.order_id);
      headers.clear();
      request.clear();
      l2.write_headers(headers, now() / NS, "DELETE", "/order", body);
      http::write_request(request, "DELETE", "clob.polymarket.com", "/order", headers, body);
    }
    jump.us.push_back(static_cast<double>(net::mono_ns() - t0) / 1e3);
    counting = false;
    jump.allocs += allocations;
    if (e.actions().size() != 2) {
      std::printf("jump produced %zu actions (want 2)\n", e.actions().size());
      return 1;
    }
  }
  jump.print("kalshi jump -> 2 cancel requests");

  // C: a post from a pre-signed body: take, L2 headers, request.
  Stats pre;
  for (int i = 0; i < 400; ++i) {
    presigner.want(0, 1, 53, 5, kUp, false);
    presigner.refresh(now() / 1'000'000);
    allocations = 0;
    counting = true;
    const auto t0 = net::mono_ns();
    if (!presigner.take(0, 1, 53, 5, body)) {
      std::printf("presign miss\n");
      return 1;
    }
    headers.clear();
    request.clear();
    l2.write_headers(headers, now() / NS, "POST", "/order", body);
    http::write_request(request, "POST", "clob.polymarket.com", "/order", headers, body);
    pre.us.push_back(static_cast<double>(net::mono_ns() - t0) / 1e3);
    counting = false;
    pre.allocs += allocations;
  }
  pre.print("post, pre-signed body -> request");

  // D: a post signed inline (a pre-sign miss).
  Stats inline_sign;
  for (int i = 0; i < 400; ++i) {
    allocations = 0;
    counting = true;
    const auto t0 = net::mono_ns();
    const auto order = venue::make_order(wallet, up_id, 53, 5, venue::random_salt(), static_cast<std::uint64_t>(now() / 1'000'000));
    clob::write_order_body(body, order, kUp, eip712::sign_order(wallet.signer, order, false), creds.api_key, true);
    headers.clear();
    request.clear();
    l2.write_headers(headers, now() / NS, "POST", "/order", body);
    http::write_request(request, "POST", "clob.polymarket.com", "/order", headers, body);
    inline_sign.us.push_back(static_cast<double>(net::mono_ns() - t0) / 1e3);
    counting = false;
    inline_sign.allocs += allocations;
  }
  inline_sign.print("post, signed inline -> request");
  return 0;
}
