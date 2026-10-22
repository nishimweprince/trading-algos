#include "hla/drills.hpp"

#include "hla/engine.hpp"

#include <cmath>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

namespace hla {
namespace {

constexpr std::uint64_t kMs = 1'000'000ULL;
constexpr std::uint64_t kSecond = 1'000'000'000ULL;
constexpr std::uint64_t kNoon = 1'761'048'000ULL * kSecond;  // 2025-10-21 12:00 UTC, outside rollover

struct RecordingIo final : EngineIo {
  std::vector<HlCommand> commands;
  std::vector<bridge::OrderRequest> orders;
  std::vector<std::string> lines;
  std::vector<std::string> kills;
  bool hl_command(const HlCommand& command) override {
    commands.push_back(command);
    return true;
  }
  bool mt5_order(const bridge::OrderRequest& order) override {
    orders.push_back(order);
    return true;
  }
  void journal(std::string_view line) override { lines.emplace_back(line); }
  void kill(std::string_view reason) override { kills.emplace_back(reason); }
  [[nodiscard]] bool journaled(std::string_view needle) const {
    for (const auto& line : lines)
      if (line.find(needle) != std::string::npos) return true;
    return false;
  }
};

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

Decimal d(std::string_view text) { return Decimal::parse(text); }

Config drill_config() {
  Config c;
  c.live = true;
  c.basis_warmup_s = 0;
  c.min_requote_ms = 0;
  c.mt5_logins = {42};
  return c;
}

PairSetup jpy() {
  PairSetup p;
  p.hl.coin = "xyz:JPY";
  p.hl.dex = "xyz";
  p.hl.dex_index = 1;
  p.hl.meta_index = 24;
  p.hl.asset_id = 110'024;
  p.hl.size_decimals = 2;
  p.hl.max_leverage = 50;
  p.hl.growth_mode = true;
  p.hl.only_isolated = true;
  p.hl.mid = d("157.8");
  p.hl.tick = d("0.01");
  p.hl.step = d("0.01");
  p.mt5_symbol = "USDJPY";
  p.hl_maker_ppm = 3;
  return p;
}

Mt5Spec usdjpy() {
  Mt5Spec s;
  s.valid = true;
  s.contract_size = 100'000;
  s.volume_step = 0.01;
  s.volume_min = 0.01;
  s.volume_max = 100;
  s.tick_size = 0.001;
  s.tick_value = 0.6337;  // ~100000 * 0.001 / 157.8 USD per tick per lot
  s.digits = 3;
  s.trade_mode = 4;
  s.session_open = true;
  return s;
}

// One engine on xyz:JPY / USDJPY with healthy feeds at time `now`.
struct Rig final {
  Config config = drill_config();
  RecordingIo io;
  std::unique_ptr<Engine> engine;
  std::uint64_t now = kNoon;
  Mt5Account account;
  double withdrawable = 5'000;

  explicit Rig(std::function<void(Config&)> tweak = {}) {
    if (tweak) tweak(config);
    engine = std::make_unique<Engine>(config, std::vector<PairSetup>{jpy()}, io);
    account.valid = true;
    account.login = 42;
    account.connected = true;
    account.trade_allowed = true;
    account.equity = 10'000;
    account.margin_free = 10'000;
    engine->on_mt5_state(0, usdjpy());
    refresh();
    HlEvent up;
    up.kind = HlEventKind::connection;
    up.connected = true;
    engine->on_hl_event(up, now);
    ctx();
    mt5(157.795, 157.805);
    hl("157.78", "157.82");
    engine->on_timer(now);
  }
  void refresh(double hl_value = 10'000) {
    account.updated_ns = now;
    engine->on_mt5_account(account, now, now, now);
    AuxReport aux;
    aux.receive_ns = now;
    aux.ok = true;
    aux.hl_account_value = hl_value;
    aux.hl_min_withdrawable = withdrawable;
    aux.hl_positions[0] = engine->hl_position(0);
    engine->on_aux(aux, now);
  }
  void ctx() {
    HlFrame f;
    f.kind = HlFrameKind::ctx;
    f.ctx = {0, now, Decimal{}, d("157.80"), d("157.80")};
    engine->on_hl_frame(f, now);
  }
  void mt5(double bid, double ask) { engine->on_mt5_tick({0, 0, bid, ask, 0, now}, now); }
  void hl(std::string_view bid, std::string_view ask) {
    HlFrame f;
    f.kind = HlFrameKind::bbo;
    f.bbo.symbol = 0;
    f.bbo.receive_ns = now;
    f.bbo.bid = {d(bid), d("5000")};
    f.bbo.ask = {d(ask), d("5000")};
    f.bbo.has_bid = f.bbo.has_ask = true;
    engine->on_hl_frame(f, now);
  }
  // Advances time keeping every feed fresh, then runs the timer.
  void advance(std::uint64_t ns, bool keep_mt5 = true) {
    now += ns;
    refresh();
    ctx();
    if (keep_mt5) mt5(engine->mt5_bid(0), engine->mt5_ask(0));
    engine->on_timer(now);
  }
  void ack_resting(std::size_t command, std::uint64_t first_oid) {
    const auto c = io.commands.at(command);  // copy: acking may append commands
    HlEvent ack;
    ack.kind = HlEventKind::ack;
    ack.request_id = c.request_id;
    ack.ok = true;
    ack.count = c.count;
    for (std::size_t i = 0; i < c.count; ++i) ack.items[i] = {AckStatus::resting, first_oid + i, {}, {}, false};
    engine->on_hl_event(ack, now);
  }
  void fill(Side side, std::string_view size, bool forced = false) {
    HlEvent f;
    f.kind = HlEventKind::fill;
    f.receive_ns = now;
    f.symbol = 0;
    const auto& q = engine->quote(0, side);
    f.oid = q.oid;
    f.side = side;
    f.price = q.price;
    f.size = d(size);
    f.forced = forced;
    engine->on_hl_event(f, now);
  }
  void txn(std::uint32_t status, double lots) {
    const auto& order = io.orders.back();
    engine->on_mt5_txn({order.request_id, 0, order.side, status, status == bridge::filled ? 10009U : 10006U, lots,
                        157.80, -0.07, 1, 1, now + 3 * kMs},
                       now);
  }
  // Acks every command not yet acknowledged: places/modifies rest, cancels succeed.
  std::size_t acked = 0;
  std::uint64_t next_oid = 100;
  void ack_pending() {
    for (; acked < io.commands.size(); ++acked) {
      const auto c = io.commands[acked];
      if (c.kind == HlCommandKind::place || c.kind == HlCommandKind::modify) {
        if (c.orders[0].tif == Tif::ioc) continue;
        ack_resting(acked, next_oid);
        next_oid += c.count;
      } else if (c.kind == HlCommandKind::cancel_cloid) {
        HlEvent ack;
        ack.kind = HlEventKind::ack;
        ack.request_id = c.request_id;
        ack.ok = true;
        ack.count = c.count;
        for (std::size_t i = 0; i < c.count; ++i) ack.items[i].status = AckStatus::success;
        engine->on_hl_event(ack, now);
      }
    }
  }
  [[nodiscard]] std::size_t count(HlCommandKind kind) const {
    std::size_t n = 0;
    for (const auto& c : io.commands) n += c.kind == kind ? 1 : 0;
    return n;
  }
  // A rig with both quotes resting (oids 11 and 12).
  void rest() {
    require(!io.commands.empty() && io.commands[0].kind == HlCommandKind::place && io.commands[0].count == 2,
            "first command places both quotes");
    ack_resting(0, 11);
    acked = 1;
    require(engine->quote(0, Side::buy).state == QuoteState::resting, "bid resting");
  }
};

void drill_quotes_and_hedge() {
  Rig r;
  const auto& place = r.io.commands.at(0);
  require(place.orders[0].tif == Tif::alo && place.orders[0].asset == 110'024, "ALO on xyz:JPY");
  // Fair 157.80; floor = 3 maker + 35 commission + 100 adverse + 31.7 MT5 half-spread + 10 min edge = 180 ppm.
  // The HL touch (157.78/157.82) is inside it, so both sides join only as far as the floor.
  require(place.orders[0].is_buy && place.orders[0].price == d("157.77"), "bid joins toward the touch, stops at the floor");
  require(!place.orders[1].is_buy && place.orders[1].price == d("157.83"), "ask joins toward the touch, stops at the floor");
  require(place.orders[0].size == d("6.33"), "one 0.01-lot step is 6.33 HL units");
  r.rest();
  r.fill(Side::buy, "6.33");
  require(r.io.orders.size() == 1 && r.io.orders[0].side == bridge::sell && std::abs(r.io.orders[0].volume - 0.01) < 1e-9,
          "HL buy hedged by a 0.01-lot MT5 sell");
  require(r.io.orders[0].expire_ns > r.now, "hedge order carries an expiry");
  r.txn(bridge::filled, 0.01);
  require(std::abs(r.engine->mt5_position(0) + 0.01) < 1e-9 && !r.engine->hedge_in_flight(0), "hedge booked");
  require(std::abs(r.engine->unhedged_lots(0)) < 0.005, "delta within half a lot step");
  require(!r.engine->halted() && r.io.kills.empty(), "no halt");
}

void drill_hedge_rejected_twice() {
  Rig r;
  r.rest();
  r.fill(Side::buy, "6.33");
  r.txn(bridge::rejected, 0);
  require(r.io.orders.size() == 2, "first rejection retried once");
  r.txn(bridge::rejected, 0);
  require(r.engine->halt() == Halt::emergency && !r.io.kills.empty(), "second rejection halts and kills");
  const auto& flatten = r.io.commands.back();
  require(flatten.kind == HlCommandKind::place && flatten.orders[0].tif == Tif::ioc && !flatten.orders[0].is_buy &&
              flatten.orders[0].size == d("6.33") && flatten.orders[0].reduce_only,
          "unhedged long flattened with a reduce-only IOC sell");
  require(flatten.orders[0].price < d("157.78"), "flatten priced through the bid within the slippage cap");
  r.ack_pending();  // quotes that were in flight at the halt are cancelled once acknowledged
  require(r.count(HlCommandKind::cancel_cloid) >= 1, "quotes cancelled");
  r.ack_pending();
  require(r.engine->quote(0, Side::buy).state == QuoteState::idle &&
              r.engine->quote(0, Side::sell).state == QuoteState::idle,
          "no quote left after the emergency");
}

void drill_hedge_timeout_inferred() {
  Rig r;
  r.rest();
  r.fill(Side::buy, "6.33");
  r.now += 1'600 * kMs;
  r.account.positions[0] = -0.01;
  r.account.positions_ns = r.now;
  r.advance(0);
  require(r.io.journaled("hedge_inferred") && !r.engine->hedge_in_flight(0), "broker position proves the fill");
  require(std::abs(r.engine->mt5_position(0) + 0.01) < 1e-9 && !r.engine->halted(), "booked without a halt");
}

void drill_hedge_timeout_escalates() {
  Rig r;
  r.rest();
  r.fill(Side::buy, "6.33");
  r.advance(2 * kSecond);
  require(!r.engine->halted() && r.io.journaled("hedge_unknown"), "unknown hedge pulls quotes first");
  r.advance(3 * kSecond);
  require(r.engine->halt() == Halt::emergency, "missing hedge result escalates to emergency");
}

void drill_stale_mt5_pulls() {
  Rig r;
  r.rest();
  r.advance(31 * kSecond, false);
  require(r.engine->block(0) == Block::mt5_stale, "stale MT5 ticks block the symbol");
  require(r.count(HlCommandKind::cancel_cloid) == 2, "both quotes cancelled");
}

void drill_kill_file() {
  Rig r;
  r.rest();
  r.engine->on_kill_file(true, r.now);
  require(r.count(HlCommandKind::cancel_cloid) == 2, "kill file cancels both quotes");
  const auto commands = r.io.commands.size();
  r.advance(kSecond);
  require(r.io.commands.size() == commands, "no new quotes after the kill file");
}

void drill_forced_fill() {
  Rig r;
  r.rest();
  r.fill(Side::sell, "6.33", true);
  require(r.engine->halt() == Halt::forced_fill && !r.io.kills.empty(), "ADL fill halts and kills");
  require(r.io.orders.size() == 1 && r.io.orders[0].side == bridge::buy, "ADL fill is still hedged");
}

void drill_post_only_reject() {
  Rig r;
  const auto& place = r.io.commands.at(0);
  HlEvent ack;
  ack.kind = HlEventKind::ack;
  ack.request_id = place.request_id;
  ack.ok = true;
  ack.count = 2;
  ack.items[0] = {AckStatus::error, 0, {}, {}, true};
  ack.items[1] = {AckStatus::resting, 12, {}, {}, false};
  ack.set_text("Post only order would have immediately matched");
  r.engine->on_hl_event(ack, r.now);
  require(r.engine->quote(0, Side::buy).state == QuoteState::idle, "rejected bid waits before re-placing");
  r.advance(250 * kMs);
  require(r.engine->quote(0, Side::buy).state == QuoteState::placing, "rejected bid is placed again after 200 ms");
  require(r.engine->quote(0, Side::sell).state == QuoteState::resting, "ask rests");
  require(r.io.kills.empty(), "post-only reject is not an error");
}

void drill_reject_backoff() {
  Rig r;
  const auto& place = r.io.commands.at(0);
  HlEvent ack;
  ack.kind = HlEventKind::ack;
  ack.request_id = place.request_id;
  ack.ok = false;
  ack.set_text("Order price cannot be more than 80% away from the reference price");
  r.engine->on_hl_event(ack, r.now);
  r.acked = r.io.commands.size();
  const auto after_reject = r.io.commands.size();
  for (int i = 0; i < 500; ++i) r.advance(kMs);
  require(r.io.commands.size() == after_reject, "no re-place within the 1 s backoff");
  r.advance(600 * kMs);
  require(r.count(HlCommandKind::place) == 2, "re-placed once after the backoff");
}

void drill_margin_reject_park() {
  Rig r;
  const auto& place = r.io.commands.at(0);
  HlEvent ack;
  ack.kind = HlEventKind::ack;
  ack.request_id = place.request_id;
  ack.ok = true;
  ack.count = 2;
  ack.items[0] = {AckStatus::error, 0, {}, {}, false};
  ack.items[1] = {AckStatus::error, 0, {}, {}, false};
  ack.set_text("Insufficient margin to place order. asset=110024");
  r.engine->on_hl_event(ack, r.now);
  require(r.io.journaled(R"("ok":false,"post_ok":true)"), "an item-level reject is journaled as not ok");
  r.acked = r.io.commands.size();
  for (int i = 0; i < 60; ++i) r.advance(5 * kSecond);
  require(r.count(HlCommandKind::place) == 1, "no re-place for 5 minutes after a margin reject");
  r.advance(301 * kSecond);
  require(r.count(HlCommandKind::place) == 2, "re-placed once the 600 s park ends");
}

void drill_quote_floor() {
  Rig r([](Config& c) { c.edge_ppm = 0; });  // passive target alone would sit inside the floor
  r.hl("157.79", "157.81");
  const auto want = r.engine->desired_quotes(0, r.now);
  require(want[0].active && want[1].active, "both sides quoted");
  require(want[0].price == d("157.77") && want[0].cap == d("157.77"), "bid held at the cost floor");
  require(want[1].price == d("157.83") && want[1].cap == d("157.83"), "ask held at the cost floor");
}

void drill_skew_floor() {
  Rig r([](Config& c) { c.skew_ppm = 2'000; });
  r.engine->seed_positions(0, d("-6.33"), 0.01);  // short one step, hedged
  const auto want = r.engine->desired_quotes(0, r.now);
  require(want[0].active && want[0].price <= d("157.77"), "a big skew never lifts the unwind bid inside the floor");
  require(want[1].price >= d("157.83"), "the ask is only pushed further away");
}

void drill_margin_block() {
  Rig r([](Config& c) {
    c.hl_leverage = 20;
    c.hl_min_margin_usd = 5;
  });
  r.rest();
  r.withdrawable = 20;  // one $1k step at 20x needs $55 with headroom
  r.advance(kMs);
  require(r.engine->block(0) == Block::margin, "flat and unable to margin a quote blocks the pair");
  require(r.count(HlCommandKind::cancel_cloid) == 2, "resting quotes pulled");
  r.ack_pending();
  r.advance(10 * kSecond);
  require(r.count(HlCommandKind::place) == 1, "no places while blocked");
  r.engine->seed_positions(0, d("6.33"), -0.01);
  const auto want = r.engine->desired_quotes(0, r.now);
  require(r.engine->block(0) == Block::none && !want[0].active && want[1].active,
          "holding a position: the reducing ask is still quoted, the adding bid is not");
}

void drill_stale_quote_inside_floor() {
  Rig r([](Config& c) { c.min_requote_ms = 60'000; });
  r.rest();
  r.mt5(157.815, 157.825);  // fair +127 ppm: the resting 157.83 ask is now inside the floor
  require(r.count(HlCommandKind::modify) == 1, "moved at once despite min_requote_ms");
  const auto& modify = r.io.commands.back();
  require(modify.count == 1 && !modify.orders[0].is_buy && modify.orders[0].price >= d("157.85"),
          "only the ask moves, back out to the floor");
}

void drill_partial_fill_resize() {
  Rig r;
  r.rest();
  r.fill(Side::buy, "1.90");  // 0.3 of a 6.33-unit step: too small to hedge
  require(r.io.orders.empty(), "a sub-half-step fill is not hedged");
  const auto want = r.engine->desired_quotes(0, r.now);
  require(want[1].active && want[1].size == d("1.90"), "the ask works off just the off-grid 1.90");
  require(want[0].size == d("6.33"), "the bid keeps the full step");
}

void drill_requote_on_move() {
  Rig r;
  r.rest();
  r.mt5(157.895, 157.905);  // +634 ppm
  require(r.count(HlCommandKind::modify) == 1, "fair move triggers one batchModify");
  const auto& modify = r.io.commands.back();
  require(modify.count == 2 && modify.oids[0] == 11 && modify.orders[0].price > d("157.75"), "both sides move up");
}

void drill_rollover() {
  Rig r([](Config& c) { c.rollover_windows = {{11 * 60 + 55, 12 * 60 + 10}}; });
  require(r.io.commands.empty() && r.engine->block(0) == Block::rollover, "no quotes in the rollover window");
}

void drill_gateway_down() {
  Rig r;
  r.rest();
  HlEvent down;
  down.kind = HlEventKind::connection;
  down.connected = false;
  r.engine->on_hl_event(down, r.now);
  r.advance(kMs);
  require(r.count(HlCommandKind::cancel_cloid) == 2, "gateway loss pulls quotes (sent over REST)");
}

void drill_budget() {
  Rig r;
  r.rest();
  AuxReport aux;
  aux.receive_ns = r.now;
  aux.ok = true;
  aux.hl_account_value = 10'000;
  aux.hl_min_withdrawable = 5'000;
  aux.has_rate_limit = true;
  aux.requests_remaining = 100;
  r.engine->on_aux(aux, r.now);
  r.engine->on_timer(r.now);
  require(r.count(HlCommandKind::cancel_cloid) == 2, "low request budget pulls quotes");
}

void drill_daily_loss() {
  Rig r;
  r.advance(kSecond);
  r.now += kSecond;
  r.refresh(9'940);
  r.engine->on_timer(r.now);
  require(r.engine->halt() == Halt::daily_loss && !r.io.kills.empty(), "$60 loss over a $50 limit halts");
}

void drill_position_mismatch() {
  Rig r;
  r.account.positions[0] = -0.05;
  r.account.positions_ns = r.now;
  r.engine->on_mt5_account(r.account, r.now, r.now, r.now);
  r.now += 6 * kSecond;
  r.account.positions_ns = r.now;
  r.account.updated_ns = r.now;
  r.engine->on_mt5_account(r.account, r.now, r.now, r.now);
  require(r.engine->halt() == Halt::position_mismatch, "broker position disagreeing for 5 s halts");
}

void drill_inventory_cap() {
  Rig r;
  r.rest();
  r.fill(Side::buy, "6.33");
  r.txn(bridge::filled, 0.01);
  r.ack_pending();
  require(r.engine->quote(0, Side::buy).state == QuoteState::resting, "bid replaced after the first fill");
  r.fill(Side::buy, "6.33");
  r.txn(bridge::filled, 0.01);
  r.ack_pending();
  r.advance(kMs);
  r.ack_pending();
  require(r.engine->quote(0, Side::buy).state == QuoteState::idle, "bid withdrawn at max inventory");
  require(r.engine->quote(0, Side::sell).state == QuoteState::resting, "ask stays to work inventory down");
}

}  // namespace

int run_drills(std::ostream& out) {
  const std::vector<std::pair<const char*, void (*)()>> drills{
      {"quotes_and_hedge", drill_quotes_and_hedge},
      {"hedge_rejected_twice", drill_hedge_rejected_twice},
      {"hedge_timeout_inferred", drill_hedge_timeout_inferred},
      {"hedge_timeout_escalates", drill_hedge_timeout_escalates},
      {"stale_mt5_pulls", drill_stale_mt5_pulls},
      {"kill_file", drill_kill_file},
      {"forced_fill", drill_forced_fill},
      {"post_only_reject", drill_post_only_reject},
      {"reject_backoff", drill_reject_backoff},
      {"margin_reject_park", drill_margin_reject_park},
      {"quote_floor", drill_quote_floor},
      {"skew_floor", drill_skew_floor},
      {"margin_block", drill_margin_block},
      {"stale_quote_inside_floor", drill_stale_quote_inside_floor},
      {"partial_fill_resize", drill_partial_fill_resize},
      {"requote_on_move", drill_requote_on_move},
      {"rollover", drill_rollover},
      {"gateway_down", drill_gateway_down},
      {"budget", drill_budget},
      {"daily_loss", drill_daily_loss},
      {"position_mismatch", drill_position_mismatch},
      {"inventory_cap", drill_inventory_cap},
  };
  int failed = 0;
  for (const auto& [name, drill] : drills) {
    try {
      drill();
      out << "PASS " << name << '\n';
    } catch (const std::exception& e) {
      ++failed;
      out << "FAIL " << name << ": " << e.what() << '\n';
    }
  }
  return failed;
}

}  // namespace hla
