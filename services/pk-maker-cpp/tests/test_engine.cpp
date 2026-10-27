#include <cmath>

#include "check.hpp"
#include "pkm/engine.hpp"
#include "pkm/feeds.hpp"
#include "pkm/json.hpp"

using namespace pkm;
using namespace pkm::engine;
using pkm::test::require;
using pkm::test::require_eq;

namespace {

constexpr std::int64_t NS = 1'000'000'000LL;
constexpr std::int64_t OPEN = 1'800'000'000 - 1'800'000'000 % 900;

Window btc_window() {
  Window w;
  w.asset = "BTC";
  w.open_unix = OPEN;
  w.kalshi_ticker = "KXBTC15M-X";
  w.up_token = "BTC-up";
  w.down_token = "BTC-down";
  w.condition_id = "0xBTC";
  return w;
}

Engine make(Config cfg = {}) {
  Engine e(cfg, {"BTC"});
  e.set_window(btc_window(), OPEN * NS);
  e.set_gates({false, true});
  return e;
}

std::string px2(double v) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "%.2f", v);
  return buf;
}

std::string px3(double v) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "%.4f", v);
  return buf;
}

// Kalshi YES bid/ask and Polymarket Up/Down books at `t`, through the feed decoders.
void books(Engine& e, double kb, double ka, std::pair<double, double> up, std::pair<double, double> down, std::int64_t t) {
  json::Document doc;
  const std::string snap = R"({"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{"market_ticker":"KXBTC15M-X","yes_dollars_fp":[[")" +
                           px3(kb) + R"(","100"]],"no_dollars_fp":[[")" + px3(1.0 - ka) + R"(","100"]]}})";
  require(doc.parse(snap), "snapshot parses");
  const int coin = feeds::apply_kalshi(doc.root(), e, t);
  require(coin == 0, "kalshi snapshot applied");
  e.on_kalshi_update(coin, t);
  auto book = [](const char* id, std::pair<double, double> ba) {
    return std::string(R"({"event_type":"book","asset_id":")") + id + R"(","bids":[{"price":")" + px2(ba.first) +
           R"(","size":"50"}],"asks":[{"price":")" + px2(ba.second) + R"(","size":"50"}]})";
  };
  const std::string poly = "[" + book("BTC-up", up) + "," + book("BTC-down", down) + "]";
  require(doc.parse(poly), "poly parses");
  std::vector<char> touched;
  feeds::apply_poly(doc.root(), e, t, touched, nullptr);
  require(touched.size() == 1 && touched[0], "poly books applied");
  e.on_poly_update(0, t);
}

std::vector<Action> take(Engine& e) {
  std::vector<Action> out;
  out.swap(e.actions());
  return out;
}

bool near(double a, double b) { return std::abs(a - b) < 1e-9; }

}  // namespace

PKM_TEST(engine_pure_rules) {
  const Config c;
  auto [u, d] = desired_px(c, 0.555, 0.0, 0.60, 0.50);
  require(u && near(*u, 0.53) && d && near(*d, 0.42), "quotes around Kalshi mid in cents");
  std::tie(u, d) = desired_px(c, 0.70, 0.0, 0.55, 0.47);
  require(u && near(*u, 0.54) && d && near(*d, 0.28), "post-only clamp a tick under the ask");
  std::tie(u, d) = desired_px(c, 0.04, 0.0, 0.05, 0.99);
  require(!u && d && near(*d, 0.94), "range");
  std::tie(u, d) = desired_px(c, 0.50, 20.0, 0.60, 0.60);
  require(u && near(*u, 0.47) && d && near(*d, 0.49), "skew leans against inventory");
  require(!room(c, 20.0, true) && room(c, 20.0, false) == 5.0, "net cap");
  require(!room(c, 17.0, true), "3 shares left is under the venue minimum");
  require(!room(c, -18.0, false), "net cap short");
  require(pair_cap(c, 0.45, 0.60) == 0.38 && pair_cap(c, 0.30, 0.60) == 0.30, "pair guard");
  require(pair_cap(c, 0.45, std::nullopt) == 0.45 && !pair_cap(c, 0.20, 0.94), "pair guard edges");
  require(opens(0.0, true) && opens(0.0, false) && opens(5.0, true) && !opens(5.0, false), "opens");
  require(!opens(-5.0, true) && opens(-5.0, false), "opens short");
  require(first_leg_ok(c, 100, 0.50) && !first_leg_ok(c, 300, 0.50), "first leg deadline");
  require(!first_leg_ok(c, 100, 0.90) && !first_leg_ok(c, 100, 0.10), "first leg band");
  require(near(floor_tick(0.53), 0.53) && near(floor_tick(0.529999999), 0.53) && near(floor_tick(0.529), 0.52), "floor tick");
}

PKM_TEST(engine_posts_both_sides_then_requotes_and_pulls) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  auto a = take(e);
  require(a.size() == 2, "two posts");
  require(a[0].kind == ActionKind::post && a[0].up && near(a[0].px, 0.53) && a[0].size == 5.0, "up at 0.53");
  require(a[1].kind == ActionKind::post && !a[1].up && near(a[1].px, 0.43), "down at 0.43");
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  e.tick(t0 + 50'000'000);
  require(take(e).empty(), "nothing moved");

  // Kalshi up a cent: nothing on first sight; Down (too high) goes after
  // adverse_ms; Up rises after min_requote_ms.
  books(e, 0.55, 0.57, {0.53, 0.57}, {0.43, 0.47}, t0 + 100'000'000);
  e.tick(t0 + 100'000'000);
  require(take(e).empty(), "flicker guard");
  e.tick(t0 + 320'000'000);
  a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::cancel && !a[0].up && a[0].order_id == "o2", "down cancelled");
  e.tick(t0 + 410'000'000);
  a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::cancel && a[0].up && a[0].order_id == "o1", "up raised");

  e.on_cancelled(0, true, "o1", true, t0 + 450'000'000);
  e.on_cancelled(0, false, "o2", true, t0 + 450'000'000);
  e.tick(t0 + 460'000'000);
  require(take(e).size() == 2, "reposted at the new prices");
  e.on_posted(0, true, 0.53, 5, true, "o3", t0 + 470'000'000);
  e.on_posted(0, false, 0.42, 5, true, "o4", t0 + 470'000'000);
  books(e, 0.54, 0.555, {0.53, 0.57}, {0.43, 0.47}, t0 + 500'000'000);
  e.tick(t0 + 500'000'000);
  books(e, 0.55, 0.57, {0.53, 0.57}, {0.43, 0.47}, t0 + 600'000'000);
  e.tick(t0 + 650'000'000);
  require(take(e).empty(), "the flicker passed");

  e.set_gates({true, true});
  e.tick(t0 + 700'000'000);
  require(take(e).size() == 2, "kill pulls everything at once");
}

PKM_TEST(engine_kalshi_jump_pauses_and_cancels_in_the_frame) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  // +3¢ within a second: both bids go while handling that frame, no tick needed.
  books(e, 0.57, 0.59, {0.53, 0.57}, {0.43, 0.47}, t0 + 300'000'000);
  auto a = take(e);
  require(a.size() == 2 && a[0].kind == ActionKind::cancel && a[1].kind == ActionKind::cancel, "both cancelled");
  require(e.counters().pauses == 1, "one pause");
  e.on_cancelled(0, true, "o1", true, t0 + 350'000'000);
  e.on_cancelled(0, false, "o2", true, t0 + 350'000'000);
  e.tick(t0 + 1'000'000'000);
  require(take(e).empty(), "still paused");
  books(e, 0.57, 0.59, {0.56, 0.60}, {0.40, 0.44}, t0 + 1'900'000'000);
  e.tick(t0 + 1'900'000'000);
  require(take(e).size() == 2, "pause over");
}

PKM_TEST(engine_first_legs_only_early_and_inside_the_band) {
  auto e = make();
  const std::int64_t t6 = (OPEN + 360) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t6);
  e.tick(t6);
  require(take(e).empty(), "flat at minute 6: no bids");
  Inventory v;
  v.asset = "BTC";
  v.open_unix = OPEN;
  v.up = 5;
  v.up_cost = 2.5;
  e.put_inventory(window_key("BTC", OPEN), v);
  e.tick(t6 + NS);
  auto a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::post && !a[0].up, "only the pairing Down bid");
}

PKM_TEST(engine_balance_refusal_holds_posts) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, false,
              R"(not enough balance / allowance: the balance is not enough -> balance: 2400000, order amount: 2700000)", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  e.tick(t0 + NS);
  require(take(e).empty() && e.counters().cash_waits == 1, "held while the hint lasts");
  e.tick(t0 + 4 * NS);
  require(take(e).size() == 1, "tried again after");
  require(e.counters().balance_rejects == 1, "counted");
}

PKM_TEST(engine_fill_then_pair_guarded_requote) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  e.on_order("o1", 5, true, t0 + NS);
  e.on_order("o2", 0, true, t0 + NS);
  books(e, 0.52, 0.54, {0.51, 0.55}, {0.45, 0.49}, t0 + 2 * NS);
  books(e, 0.51, 0.53, {0.50, 0.54}, {0.46, 0.50}, t0 + 4 * NS);
  e.tick(t0 + 4 * NS);
  std::optional<double> down;
  for (const auto& x : take(e))
    if (x.kind == ActionKind::post && !x.up && !down) down = x.px;
  require(down && near(*down, 0.45), "pair guard caps Down at 1 - 0.53 - 0.02");
}

PKM_TEST(engine_window_edges_are_not_quoted) {
  auto e = make();
  for (const std::int64_t s : {std::int64_t{5}, kWindowS - 59}) {
    const std::int64_t t = (OPEN + s) * NS;
    books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t);
    e.tick(t);
    require(take(e).empty(), "edge second " + std::to_string(s));
  }
}

PKM_TEST(engine_fills_settle_and_latch_the_daily_loss) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  e.on_order("o1", 2, false, t0 + NS);
  e.on_order("o1", 5, true, t0 + 2 * NS);
  e.on_order("o2", 5, true, t0 + 2 * NS);
  const std::string key = window_key("BTC", OPEN);
  const auto& v = e.inventories().at(key);
  require(v.up == 5 && v.down == 5 && near(v.up_cost, 2.65) && near(v.down_cost, 2.15), "both legs booked");
  require(e.tracked().empty(), "nothing tracked");
  const std::int64_t end = (OPEN + kWindowS + 31) * NS;
  require(e.unsettled(end).size() == 1, "unsettled after the close");
  e.settle("BTC", OPEN, "no", end);
  require(near(e.inventories().at(key).pnl, 5.0 - 4.8), "pair pnl");
  const auto due = e.redeem_due();
  require(due.size() == 1 && due[0].first == key && due[0].second == "0xBTC", "redeem due");
  require(!e.halted(end), "not halted");

  Inventory loser;
  loser.asset = "BTC";
  loser.open_unix = 1;
  loser.up = 30;
  loser.up_cost = 16;
  e.put_inventory("BTC-1", loser);
  e.settle("BTC", 1, "no", end);
  require(e.halted(end), "latched");
  bool cancel_all = false;
  for (const auto& a : take(e)) cancel_all = cancel_all || a.kind == ActionKind::cancel_all;
  require(cancel_all, "halt cancels everything");

  std::string error;
  Engine e2(Config{}, {"BTC"});
  require(e2.load_json(e.save_json(), error), error);
  require(e2.halted(end), "persisted");
  require(!e2.halted(end + 86'400 * NS), "next UTC day");
  require(e2.inventories().size() == 2 && near(e2.inventories().at(key).down_cost, 2.15), "inventories persisted");
  e2.ack(end);
  require(!e2.halted(end), "ack clears");
  require(near(e2.today_realised(end), 0.0), "the limit counts from the ack");
}

PKM_TEST(engine_open_cost_cap_blocks_a_side) {
  auto e = make();
  Inventory v;
  v.asset = "ETH";
  v.open_unix = 1;
  v.up = 100;
  v.up_cost = 58;
  e.put_inventory("ETH-1", v);
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  require(take(e).empty(), "58 + 2.65 > 60 and 58 + 2.15 > 60");
}

PKM_TEST(engine_rate_limit_holds_posts) {
  Config c;
  c.max_orders_per_s = 1;
  auto e = make(c);
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  e.tick(t0);
  require(take(e).size() == 1, "one token");
  e.tick(t0 + 1'100'000'000);
  require(take(e).size() == 1, "refilled");
}

PKM_TEST(engine_drift_halts_after_three_short_checks) {
  auto e = make();
  Inventory v;
  v.asset = "BTC";
  v.open_unix = OPEN;
  v.up_token = "BTC-up";
  v.up = 5;
  v.up_cost = 2.5;
  e.put_inventory(window_key("BTC", OPEN), v);
  const std::int64_t t = (OPEN + 100) * NS;
  e.on_balance("BTC-up", 9.0, t);
  require(!e.halted(t), "more in the wallet is only recorded");
  for (int i = 0; i < 3; ++i) e.on_balance("BTC-up", 0.0, t);
  require(e.halted(t), "three short checks halt");
}

PKM_TEST(engine_stale_feeds_and_window_roll_pull_quotes) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_posted(0, false, 0.43, 5, true, "o2", t0);
  e.set_gates({false, false});
  e.tick(t0 + 10'000'000);
  require(take(e).size() == 2 && std::string(e.gate(0)) == "feeds stale", "stale feeds pull both");
  e.on_cancelled(0, true, "o1", true, t0 + 20'000'000);
  e.on_cancelled(0, false, "o2", true, t0 + 20'000'000);
  e.set_gates({false, true});
  // Kalshi's book older than kalshi_max_age_ms: no fair value.
  e.tick(t0 + 11 * NS);
  require(take(e).empty() && std::string(e.gate(0)) == "no fair", "old Kalshi book");
  // An order of the old window is cancelled at the roll.
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0 + 12 * NS);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o5", t0 + 12 * NS);
  auto w = btc_window();
  w.open_unix = OPEN + 900;
  w.kalshi_ticker = "KXBTC15M-Y";
  w.up_token = "BTC-up2";
  w.down_token = "BTC-down2";
  e.set_window(w, (OPEN + 900) * NS);
  e.tick((OPEN + 901) * NS);
  auto a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::cancel && a[0].order_id == "o5", "old window's bid cancelled");
}

PKM_TEST(engine_unknown_order_closes_after_ten_seconds) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "o1", t0);
  e.on_unknown("o1", t0 + 5 * NS);
  require(e.tracked().size() == 1, "a fresh order may not be indexed yet");
  e.on_unknown("o1", t0 + 11 * NS);
  require(e.tracked().empty() && e.counters().orders_unknown == 1, "closed past 10 s");
  require(!e.resting(0, true), "slot freed");
}

PKM_TEST(engine_loads_pk_polymaker_state) {
  // The shape serde writes for pk-polymaker's Persist (Options as null, ack as a pair).
  const std::string rust = R"({
  "inventories": {
    "BTC-1793050200": {"asset": "BTC", "open_unix": 1793050200, "condition_id": "0xc", "up_token": "11", "down_token": "22",
      "up": 5.0, "up_cost": 2.65, "down": 0.0, "down_cost": 0.0, "fills": 1, "settled": null, "pnl": null,
      "settled_ns": null, "redeemed": null, "redeem_tries": 0},
    "ETH-1793049300": {"asset": "ETH", "open_unix": 1793049300, "condition_id": "0xd", "up_token": "33", "down_token": "44",
      "up": 5.0, "up_cost": 2.0, "down": 5.0, "down_cost": 2.5, "fills": 2, "settled": "yes", "pnl": 0.5,
      "settled_ns": 1793050230000000000, "redeemed": null, "redeem_tries": 3}
  },
  "realised_by_day": {"2026-10-26": -3.25},
  "orders": {"0xabc": {"id": "0xabc", "key": "BTC-1793050200", "asset": "BTC", "up": true, "px": 0.53, "size": 5.0,
    "matched": 0.0, "posted_ns": 1793050300000000000, "closing": false}},
  "halt": null,
  "ack": ["2026-10-26", -1.0]
})";
  Engine e(Config{}, {"BTC", "ETH"});
  std::string error;
  require(e.load_json(rust, error), error);
  require(e.inventories().size() == 2 && e.inventories().at("BTC-1793050200").settled.empty(), "null settled reads as open");
  const auto& eth = e.inventories().at("ETH-1793049300");
  require(eth.settled == "yes" && eth.redeemed.empty() && eth.redeem_tries == 3, "settled, not yet redeemed");
  require(e.redeem_due().size() == 1, "the ETH winner is due for redemption");
  require(e.orders().size() == 1 && e.tracked()[0] == "0xabc", "orders carried over");
  require(!e.halt_state(), "no halt");
  const std::int64_t day = (1'793'016'000LL + 3600) * NS;  // 2026-10-26 13:00 UTC
  require(near(e.today_realised(day), -2.25), "ack base read from the pair");
}

PKM_TEST(engine_post_in_flight_across_a_roll_keeps_its_window) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  auto a = take(e);
  require(a.size() == 2, "posts in flight");
  auto w = btc_window();
  w.open_unix = OPEN + 900;
  w.kalshi_ticker = "KXBTC15M-Y";
  w.up_token = "BTC-up2";
  w.down_token = "BTC-down2";
  e.set_window(w, (OPEN + 900) * NS);
  e.on_posted(0, true, 0.53, 5, true, "late", (OPEN + 900) * NS);
  require(e.orders().at("late").key == window_key("BTC", OPEN), "booked to the window it was sent for");
  e.tick((OPEN + 901) * NS);
  bool cancelled = false;
  for (const auto& x : take(e)) cancelled = cancelled || (x.kind == ActionKind::cancel && x.order_id == "late");
  require(cancelled, "and cancelled as the old window's bid");
  e.on_order("late", 5, true, (OPEN + 901) * NS);
  require(e.inventories().contains(window_key("BTC", OPEN)) && !e.inventories().contains(window_key("BTC", OPEN + 900)),
          "its fill lands in the old window's inventory");
}

PKM_TEST(engine_late_cancel_answer_leaves_the_new_post_in_flight) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "u1", t0);
  e.on_posted(0, false, 0.43, 5, true, "d1", t0);
  books(e, 0.57, 0.59, {0.53, 0.57}, {0.43, 0.47}, t0 + 300'000'000);
  require(take(e).size() == 2, "both cancelled on the jump");
  // The user channel reports the final states before the cancel answers arrive.
  e.on_order("u1", 0, true, t0 + 350'000'000);
  e.on_order("d1", 0, true, t0 + 350'000'000);
  books(e, 0.57, 0.59, {0.56, 0.60}, {0.40, 0.44}, t0 + 1'900'000'000);
  auto a = take(e);
  require(a.size() == 2 && a[0].kind == ActionKind::post && a[1].kind == ActionKind::post, "reposted after the pause");
  e.on_cancelled(0, true, "u1", true, t0 + 1'950'000'000);
  e.on_cancelled(0, false, "d1", true, t0 + 1'950'000'000);
  e.tick(t0 + 2 * NS);
  require(take(e).empty(), "the late answers do not release the posts in flight");
}

PKM_TEST(engine_room_counts_orders_still_closing) {
  Config c;
  c.max_net = 10;
  auto e = make(c);
  Inventory v;
  v.asset = "BTC";
  v.open_unix = OPEN;
  v.down = 5;
  v.down_cost = 2.15;
  e.put_inventory(window_key("BTC", OPEN), v);
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  require(take(e).size() == 2, "both sides quoted at net -5");
  e.on_posted(0, true, 0.53, 5, true, "u1", t0);
  e.on_posted(0, false, 0.43, 5, true, "d1", t0);
  e.set_gates({true, true});
  e.tick(t0 + 10'000'000);
  require(take(e).size() == 2, "kill pulls both");
  e.on_cancelled(0, true, "u1", true, t0 + 20'000'000);
  e.on_cancelled(0, false, "d1", true, t0 + 20'000'000);
  e.set_gates({false, true});
  e.tick(t0 + 30'000'000);
  auto a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::post && a[0].up, "no Down while d1 could still fill to net -10");
  e.on_order("d1", 0, true, t0 + 40'000'000);
  e.tick(t0 + 50'000'000);
  a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::post && !a[0].up, "Down once d1's final state is known");
}

PKM_TEST(engine_sweeps_orders_no_slot_holds) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "u1", t0);
  e.on_posted(0, false, 0.43, 5, true, "d1", t0);
  e.on_posted(0, true, 0.53, 5, true, "u2", t0);
  require(e.resting(0, true)->first == "u1", "the slot keeps the order it holds");
  e.tick(t0 + 10'000'000);
  auto a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::cancel && a[0].order_id == "u2", "the stray is cancelled");
  e.tick(t0 + 500'000'000);
  require(take(e).empty(), "not resent within a second");
  e.on_cancelled(0, true, "u2", true, t0 + 600'000'000);
  require(e.resting(0, true)->first == "u1", "its answer leaves the slot alone");
  require(e.counters().stray_cancels == 1, "counted");
}

PKM_TEST(engine_bid_at_fair_is_pulled_without_the_hold) {
  Config c;
  c.pause_jump = 0.10;
  auto e = make(c);
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "u1", t0);
  e.on_posted(0, false, 0.43, 5, true, "d1", t0);
  books(e, 0.53, 0.55, {0.53, 0.57}, {0.43, 0.47}, t0 + 50'000'000);
  require(take(e).empty(), "fair 0.54 over the 0.53 bid: the flicker hold applies");
  books(e, 0.52, 0.54, {0.53, 0.57}, {0.43, 0.47}, t0 + 60'000'000);
  auto a = take(e);
  require(a.size() == 1 && a[0].kind == ActionKind::cancel && a[0].order_id == "u1", "fair 0.53 at the bid: pulled at once");
}

PKM_TEST(engine_cross_refusal_caps_the_next_bid) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, false, "invalid post-only order: order crosses book", t0);
  e.on_posted(0, false, 0.43, 5, false, "invalid amount for a marketable BUY order ($2.15), min size: $1", t0);
  require(e.counters().post_only_rejects == 2, "both read as a cross");
  e.tick(t0 + 600'000'000);
  auto a = take(e);
  require(a.size() == 2 && near(a[0].px, 0.52) && near(a[1].px, 0.42), "a tick under the refused prices");
  e.on_posted(0, true, 0.52, 5, false, "invalid post-only order: order crosses book", t0 + 650'000'000);
  e.on_posted(0, false, 0.42, 5, true, "d1", t0 + 650'000'000);
  e.tick(t0 + 3 * NS);
  a = take(e);
  require(a.size() == 1 && a[0].up && near(a[0].px, 0.53), "the cap lapses after two seconds");
}

PKM_TEST(engine_drift_waits_for_a_fresh_fill_to_settle) {
  auto e = make();
  const std::int64_t t0 = (OPEN + 100) * NS;
  books(e, 0.54, 0.56, {0.53, 0.57}, {0.43, 0.47}, t0);
  take(e);
  e.on_posted(0, true, 0.53, 5, true, "u1", t0);
  e.on_order("u1", 5, true, t0);
  for (int i = 0; i < 3; ++i) e.on_balance("BTC-up", 0.0, t0 + NS);
  require(!e.halted(t0 + NS) && e.counters().drift_checks == 0, "a just-filled token is not checked");
  for (int i = 0; i < 3; ++i) e.on_balance("BTC-up", 0.0, t0 + 31 * NS);
  require(e.halted(t0 + 31 * NS), "checked again after the grace");
}
