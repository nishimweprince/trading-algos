#include "bpmm/allocation.hpp"
#include "bpmm/book.hpp"
#include "bpmm/engine.hpp"
#include "bpmm/journal.hpp"
#include "bpmm/parser.hpp"
#include "bpmm/risk.hpp"
#include "bpmm/shadow.hpp"
#include "bpmm/spsc.hpp"
#include "bpmm/strategy.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>

namespace {
int tests = 0;

void require(bool condition, std::string_view message) {
  ++tests;
  if (!condition) throw std::runtime_error(std::string(message));
}

bpmm::Snapshot snapshot(std::uint64_t id = 100) {
  bpmm::Snapshot value;
  value.last_update_id = id;
  value.bids[0] = {bpmm::Decimal::parse("100.00"), bpmm::Decimal::parse("2")};
  value.asks[0] = {bpmm::Decimal::parse("100.02"), bpmm::Decimal::parse("3")};
  value.bid_count = value.ask_count = 1;
  return value;
}

bpmm::DepthEvent depth(std::uint64_t previous = 100, std::uint64_t current = 101) {
  bpmm::DepthEvent value;
  value.receive_ns = 1'000'000'000;
  value.event_ms = 1'000;
  value.transaction_ms = 999;
  value.first_id = current;
  value.final_id = current;
  value.previous_final_id = previous;
  value.bids[0] = {bpmm::Decimal::parse("100.00"), bpmm::Decimal::parse("2")};
  value.asks[0] = {bpmm::Decimal::parse("100.02"), bpmm::Decimal::parse("3")};
  value.bid_count = value.ask_count = 1;
  return value;
}

void test_decimal_and_rounding() {
  require(bpmm::Decimal::parse("123.45678901").raw == 12'345'678'901LL, "decimal exact");
  require(bpmm::Decimal::parse("-0.25").raw == -25'000'000, "negative decimal");
  const auto tick = bpmm::Decimal::parse("0.01");
  require(bpmm::round_down(bpmm::Decimal::parse("100.019"), tick) == bpmm::Decimal::parse("100.01"),
          "round down");
  require(bpmm::round_up(bpmm::Decimal::parse("100.011"), tick) == bpmm::Decimal::parse("100.02"),
          "round up");
}

void test_parser() {
  const std::string depth_json = R"({"stream":"btcusdt@depth@0ms","data":{"e":"depthUpdate","E":10,"T":9,"U":101,"u":102,"pu":100,"b":[["100.00","2.5"]],"a":[["100.02","3.5"]]}})";
  bpmm::ParsedEvent event;
  require(bpmm::parse_stream_event(depth_json, 77, event), "parse depth");
  require(event.kind == bpmm::ParsedKind::depth && event.depth.final_id == 102 &&
              event.depth.bids[0].quantity == bpmm::Decimal::parse("2.5"),
          "depth fields");
  const std::string trade_json = R"({"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":10,"a":44,"p":"100.01","q":"0.5","T":9,"m":true}})";
  require(bpmm::parse_stream_event(trade_json, 88, event), "parse trade");
  require(event.trade.trade_id == 44 && event.trade.buyer_is_maker, "trade fields");
  const std::string candle_json = R"({"stream":"btcusdt@kline_1m","data":{"e":"kline","E":10,"k":{"t":0,"T":59999,"o":"100","h":"101","l":"99","c":"100.5","x":true}}})";
  require(bpmm::parse_stream_event(candle_json, 99, event), "parse candle");
  require(event.candle.closed && event.candle.close == bpmm::Decimal::parse("100.5"), "candle fields");
  const std::string mark_json = R"({"stream":"btcusdt@markPrice@1s","data":{"e":"markPriceUpdate","E":10,"p":"100.1","i":"100","P":"100","r":"0.0001","T":12345}})";
  require(bpmm::parse_stream_event(mark_json, 100, event) &&
              event.kind == bpmm::ParsedKind::mark_price && event.mark_price.next_funding_ms == 12345,
          "parse mark price and funding");
  const std::string snapshot_json = R"({"lastUpdateId":100,"bids":[["100","2"]],"asks":[["101","3"]]})";
  bpmm::Snapshot snap;
  require(bpmm::parse_snapshot(snapshot_json, snap) && snap.last_update_id == 100, "snapshot");
}

void test_book_sequences() {
  bpmm::OrderBook book;
  const auto snap = snapshot();
  require(book.apply_snapshot(snap.last_update_id, {snap.bids.data(), snap.bid_count},
                              {snap.asks.data(), snap.ask_count}), "snapshot applies");
  require(book.state() == bpmm::BookState::syncing, "snapshot sync state");
  auto update = depth();
  require(book.apply(update) && book.state() == bpmm::BookState::verified, "first bridge verifies");
  update = depth(99, 102);
  require(!book.apply(update) && book.state() == bpmm::BookState::gap, "pu gap fails closed");
}

void test_bar_portion_and_regression() {
  bpmm::BarPortion bp;
  require(!bp.on_closed({0, 1, {}, {}, {}, {}, false}), "open candle ignored");
  require(bp.on_closed({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("110"),
                        bpmm::Decimal::parse("90"), bpmm::Decimal::parse("105"), true}),
          "closed candle accepted");
  require(std::abs(bp.value() - 0.25) < 1e-12, "paper BP equation");
  require(bp.on_closed({60'000, 119'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("100"),
                        bpmm::Decimal::parse("100"), bpmm::Decimal::parse("100"), true}) &&
              bp.value() == 0.0,
          "zero range maps to zero");
  bpmm::RollingRegression regression(4);
  regression.push(-1, 0.01);
  regression.push(0, 0);
  regression.push(1, -0.01);
  const auto fit = regression.fit();
  require(fit.calibrated && fit.slope < 0 && std::abs(fit.slope + 0.01) < 1e-12,
          "contrarian OLS");
}

void test_quotes_and_risk() {
  bpmm::OrderBook book;
  const auto snap = snapshot();
  book.apply_snapshot(snap.last_update_id, {snap.bids.data(), snap.bid_count},
                      {snap.asks.data(), snap.ask_count});
  book.apply(depth());
  bpmm::BarPortion bp;
  bp.on_closed({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("101"),
                bpmm::Decimal::parse("99"), bpmm::Decimal::parse("101"), true});
  bpmm::QuoteStrategy strategy;
  const auto neutral = strategy.quote(book, bp, {0, -0.0001, true}, {}, 1'000'000'000);
  require(neutral.size == 4, "two-sided two-level quotes");
  require(neutral.values[0].price.raw < book.best_ask().price.raw &&
              neutral.values[1].price.raw > book.best_bid().price.raw,
          "post-only prices");
  bpmm::RiskEngine risk;
  bpmm::RiskSnapshot state;
  state.book_verified = true;
  state.book_receive_ns = 1'000'000'000;
  state.equity_raw = state.peak_equity_raw = 10'000 * bpmm::kScale;
  require(risk.check(neutral.values[0], book.best_bid().price, book.best_ask().price, state,
                     1'000'001'000, 1'000) == bpmm::RiskCode::ok,
          "fresh quote passes risk");
  require(risk.check(neutral.values[0], book.best_bid().price, book.best_ask().price, state,
                     2'000'000'000, 1'000) == bpmm::RiskCode::stale_book,
          "stale quote blocked");
  bpmm::RiskLimits tight;
  tight.max_inventory_raw = bpmm::Decimal::parse("0.005").raw;
  bpmm::RiskEngine inventory_risk(tight);
  require(inventory_risk.check(neutral.values[0], book.best_bid().price, book.best_ask().price,
                               state, 1'000'001'000, 1'000) == bpmm::RiskCode::inventory_limit,
          "projected inventory blocked");
  state.toxicity_ppm = 900'000;
  require(risk.check(neutral.values[1], book.best_bid().price, book.best_ask().price, state,
                     1'000'002'000, 1'000) == bpmm::RiskCode::toxicity_limit,
          "toxic-side quote blocked");
  bpmm::PositionState long_position{bpmm::Decimal::parse("1").raw,
                                    bpmm::Decimal::parse("100"), 1'000, {}};
  require(strategy.barrier_exit(long_position, bpmm::Decimal::parse("99.7"), 1'001),
          "stop-loss barrier");
}

void test_shadow_queue() {
  bpmm::OrderBook book;
  const auto snap = snapshot();
  book.apply_snapshot(snap.last_update_id, {snap.bids.data(), snap.bid_count},
                      {snap.asks.data(), snap.ask_count});
  book.apply(depth());
  bpmm::ShadowVenue venue(0, 200);
  bpmm::QuoteIntent order{bpmm::IntentKind::place, bpmm::Side::buy, 0,
                          bpmm::Decimal::parse("100"), bpmm::Decimal::parse("1"), false, 10, 1};
  venue.submit(order, book);
  venue.advance(10, book);
  venue.on_trade({1, 1, 20, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("2"), true});
  require(venue.fills().empty(), "queue ahead prevents optimistic fill");
  venue.on_trade({2, 2, 21, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("1"), true});
  require(venue.fills().size() == 1 && venue.fills()[0].quantity == bpmm::Decimal::parse("1"),
          "excess trade fills after queue");
}

void test_engine_gap_and_replay() {
  bpmm::Engine engine;
  const auto snap = snapshot();
  require(engine.on_snapshot(snap), "engine snapshot");
  engine.on_candle({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("101"),
                    bpmm::Decimal::parse("99"), bpmm::Decimal::parse("100.5"), true});
  auto update = depth();
  require(engine.on_depth(update, update.receive_ns).size > 0, "engine quotes");
  update.previous_final_id = 7;
  update.first_id = update.final_id = 102;
  engine.on_depth(update, update.receive_ns + 1);
  require(engine.halted() && engine.metrics().gaps == 1, "gap halts and cancels");

  const auto path = std::filesystem::temp_directory_path() / "bpmm-replay-test.jsonl";
  {
    bpmm::Journal journal(path.string());
    journal.snapshot(snap, 900'000'000);
    journal.candle({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("101"),
                    bpmm::Decimal::parse("99"), bpmm::Decimal::parse("100.5"), true});
    journal.depth(depth());
  }
  bpmm::Engine first;
  bpmm::Engine second;
  std::uint64_t h1 = 0;
  std::uint64_t h2 = 0;
  std::string error;
  require(bpmm::replay_normalized(path.string(), first, h1, error), "first replay");
  require(bpmm::replay_normalized(path.string(), second, h2, error), "second replay");
  require(h1 == h2, "deterministic replay hash");
  std::filesystem::remove(path);
}

void test_parser_fuzz_smoke() {
  std::mt19937_64 random(0xBADC0FFEEULL);
  std::string input(512, 'x');
  bpmm::ParsedEvent event;
  for (int iteration = 0; iteration < 10'000; ++iteration) {
    const auto size = static_cast<std::size_t>(random() % input.size());
    for (std::size_t i = 0; i < size; ++i) input[i] = static_cast<char>(32 + random() % 95);
    static_cast<void>(bpmm::parse_stream_event(std::string_view(input.data(), size), 1, event));
  }
  require(true, "parser fuzz smoke did not crash");
}

void test_spsc() {
  bpmm::SpscRing<int, 4> ring;
  require(ring.empty(), "ring starts empty");
  require(ring.try_push(1) && ring.try_push(2) && ring.try_push(3), "ring accepts capacity minus one");
  require(!ring.try_push(4), "ring reports full");
  int value = 0;
  require(ring.try_pop(value) && value == 1, "ring preserves order");
  require(ring.try_pop(value) && value == 2 && ring.try_pop(value) && value == 3 && ring.empty(),
          "ring drains");
}
}  // namespace

int main() {
  try {
    test_decimal_and_rounding();
    test_parser();
    test_book_sequences();
    test_bar_portion_and_regression();
    test_quotes_and_risk();
    test_shadow_queue();
    test_engine_gap_and_replay();
    test_parser_fuzz_smoke();
    test_spsc();
    std::cout << "passed " << tests << " assertions\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "failed after " << tests << " assertions: " << error.what() << '\n';
    return 1;
  }
}
