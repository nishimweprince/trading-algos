#include "cxa/book.hpp"
#include "cxa/engine.hpp"
#include "cxa/instruments.hpp"
#include "cxa/journal.hpp"
#include "cxa/latency.hpp"
#include "cxa/parser.hpp"
#include "cxa/rest.hpp"
#include "cxa/scenario.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

namespace {
using namespace cxa;

int tests = 0;
std::string fixtures;

void require(bool condition, std::string_view message) {
  ++tests;
  if (!condition) throw std::runtime_error(std::string(message));
}

Decimal d(std::string_view text) { return Decimal::parse(text); }

std::unique_ptr<DepthEvent> depth(Venue venue, std::uint64_t first, std::uint64_t final, std::uint64_t previous,
                                  std::initializer_list<Level> bids, std::initializer_list<Level> asks,
                                  bool snapshot = false) {
  auto e = std::make_unique<DepthEvent>();
  e->venue = venue;
  e->snapshot = snapshot;
  e->first_id = first;
  e->final_id = final;
  e->previous_final_id = previous;
  for (const auto& level : bids) e->bids[e->bid_count++] = level;
  for (const auto& level : asks) e->asks[e->ask_count++] = level;
  return e;
}

void test_decimal_and_ppm() {
  require(d("123.45678901").raw == 12'345'678'901LL, "decimal exact");
  require(d("-0.25").raw == -25'000'000, "negative decimal");
  require(round_down(d("100.019"), d("0.01")) == d("100.01"), "round down");
  require(round_up(d("100.011"), d("0.01")) == d("100.02"), "round up");
  require(ratio_ppm(d("0.35").raw, d("100").raw) == 3'500, "ratio ppm");
  require(apply_ppm(d("1000").raw, 550) == d("0.55").raw, "apply ppm");
  require(mul_scaled(d("100.02").raw, d("9.969").raw) == d("997.09938").raw, "mul scaled");
}

void test_book_and_sweep() {
  OrderBook book;
  const std::array<Level, 3> bids{{{d("100.00"), d("1")}, {d("99.99"), d("2")}, {d("99.98"), d("3")}}};
  const std::array<Level, 3> asks{{{d("100.02"), d("1")}, {d("100.03"), d("2")}, {d("100.05"), d("3")}}};
  require(book.apply_snapshot(10, bids, asks, 1, true), "snapshot accepted");
  require(book.best_bid().price == d("100.00") && book.best_ask().price == d("100.02"), "best levels");
  const auto full = book.sweep(Side::buy, d("2"));
  require(full.complete(d("2")) && full.vwap == d("100.025") && full.worst == d("100.03"), "vwap sweep");
  const auto limited = book.sweep(Side::buy, d("5"), d("100.03"));
  require(limited.filled == d("3"), "limit stops sweep");
  const auto haircut = book.sweep(Side::sell, d("3"), {}, 500'000);
  require(haircut.filled == d("3") && haircut.worst == d("99.98"), "haircut walks deeper");
  // Truncation keeps the best kCapacity levels.
  OrderBook deep;
  std::vector<Level> many;
  for (std::size_t i = 0; i < OrderBook::kCapacity + 10; ++i)
    many.push_back({Price{(1'000 - static_cast<std::int64_t>(i)) * kScale}, d("1")});
  const std::array<Level, 1> ask{{{Price{2'000 * kScale}, d("1")}}};
  require(deep.apply_snapshot(1, many, ask, 1, true), "deep snapshot");
  require(deep.depth(Side::buy) == OrderBook::kCapacity && deep.best_bid().price.raw == 1'000 * kScale,
          "truncated far levels");
}

void test_binance_sync() {
  OrderBook book;
  const std::array<Level, 1> bids{{{d("100"), d("1")}}};
  const std::array<Level, 1> asks{{{d("100.02"), d("1")}}};
  book.apply_snapshot(100, bids, asks, 1, false);
  require(book.state() == BookState::syncing, "binance snapshot syncs");
  require(book.apply_binance(*depth(Venue::binance, 90, 100, 89, {}, {})), "stale diff dropped");
  require(book.apply_binance(*depth(Venue::binance, 99, 102, 98, {{d("100.01"), d("1")}}, {})), "bridging diff");
  require(book.verified() && book.best_bid().price == d("100.01"), "verified after bridge");
  require(!book.apply_binance(*depth(Venue::binance, 104, 104, 103, {}, {})), "broken pu");
  require(book.state() == BookState::gap, "gap state");
  // Futures U is not contiguous: after the event ending at lastUpdateId is dropped, the
  // next one bridges through pu even though U > lastUpdateId + 1.
  OrderBook chained;
  chained.apply_snapshot(200, bids, asks, 1, false);
  require(chained.apply_binance(*depth(Venue::binance, 195, 200, 190, {}, {})), "event ending at id dropped");
  require(chained.apply_binance(*depth(Venue::binance, 207, 210, 200, {}, {})) && chained.verified(),
          "pu bridge after snapshot");
}

void test_bybit_sync() {
  OrderBook book;
  require(book.apply_bybit(*depth(Venue::bybit, 5, 5, 0, {{d("100"), d("1")}}, {{d("100.02"), d("1")}}, true)),
          "bybit snapshot verified");
  require(book.apply_bybit(*depth(Venue::bybit, 6, 6, 0, {{d("100"), d("0")}, {d("99.99"), d("2")}}, {})),
          "delta applies");
  require(book.best_bid().price == d("99.99"), "delete then insert");
  require(!book.apply_bybit(*depth(Venue::bybit, 8, 8, 0, {}, {})), "skipped u is a gap");
  require(book.apply_bybit(*depth(Venue::bybit, 1, 1, 0, {{d("100"), d("1")}}, {{d("100.02"), d("1")}}, true)),
          "restart snapshot recovers");
}

void test_parsers() {
  const std::vector<std::string> symbols{"BTCUSDT", "ETHUSDT"};
  ParsedFrame frame;
  const std::string binance_depth =
      R"({"stream":"ethusdt@depth@0ms","data":{"e":"depthUpdate","E":1700000000123,"T":1700000000120,"s":"ETHUSDT","U":10,"u":12,"pu":9,"b":[["2000.10","1.5"],["2000.00","0"]],"a":[["2000.20","3"]]}})";
  require(parse_binance_frame(binance_depth, 7, symbols, frame) && frame.kind == FrameKind::depth, "binance depth");
  require(frame.depth.symbol == 1 && frame.depth.final_id == 12 && frame.depth.previous_final_id == 9 &&
              frame.depth.bid_count == 2 && frame.depth.asks[0].price == d("2000.20"),
          "binance depth fields");
  const std::string mark =
      R"({"stream":"btcusdt@markPrice@1s","data":{"e":"markPriceUpdate","E":1700000000000,"s":"BTCUSDT","p":"60000.5","P":"60001","i":"60000","r":"-0.00001234","T":1700006400000}})";
  require(parse_binance_frame(mark, 7, symbols, frame) && frame.kind == FrameKind::funding, "binance mark");
  require(frame.funding.funding_rate == d("-0.00001234") && frame.funding.next_funding_ms == 1700006400000ULL &&
              frame.funding.mark == d("60000.5"),
          "binance funding fields");
  const std::string book =
      R"({"topic":"orderbook.50.BTCUSDT","type":"snapshot","ts":1700000000500,"data":{"s":"BTCUSDT","b":[["60000.0","0.5"]],"a":[["60000.1","0.7"]],"u":42,"seq":9},"cts":1700000000499})";
  require(parse_bybit_frame(book, 7, symbols, frame) && frame.kind == FrameKind::depth, "bybit book");
  require(frame.depth.snapshot && frame.depth.final_id == 42 && frame.depth.event_ms == 1700000000500ULL,
          "bybit book fields");
  const std::string restart =
      R"({"topic":"orderbook.50.BTCUSDT","type":"delta","ts":1,"data":{"s":"BTCUSDT","b":[["1","1"]],"a":[["2","1"]],"u":1,"seq":1}})";
  require(parse_bybit_frame(restart, 7, symbols, frame) && frame.depth.snapshot, "u=1 is a snapshot");
  const std::string ticker =
      R"({"topic":"tickers.ETHUSDT","type":"delta","data":{"symbol":"ETHUSDT","fundingRate":"0.0001"},"cs":1,"ts":1700000000600})";
  require(parse_bybit_frame(ticker, 7, symbols, frame) && frame.kind == FrameKind::funding, "bybit ticker");
  require(frame.funding.has_rate && frame.funding.next_funding_ms == 0 && frame.funding.mark.raw == 0,
          "partial ticker delta");
  require(!parse_bybit_frame(R"({"success":false,"ret_msg":"bad topic","op":"subscribe"})", 7, symbols, frame),
          "failed subscribe is an error");
  require(parse_bybit_frame(R"({"topic":"orderbook.50.XRPUSDT","type":"delta","ts":1,"data":{"u":2}})", 7,
                            symbols, frame) &&
              frame.kind == FrameKind::ignored,
          "unknown symbol ignored");
  // Live BTCUSDT @depth@0ms diffs exceed 512 levels per side; dropping one breaks
  // the pu chain and forces a resync, so the parser must accept them.
  std::string many;
  for (int i = 0; i < 600; ++i) {
    if (i != 0) many += ',';
    many += "[\"60000.00\",\"0.5\"]";
  }
  const std::string wide =
      "{\"stream\":\"btcusdt@depth@0ms\",\"data\":{\"e\":\"depthUpdate\",\"E\":1,\"T\":1,\"s\":\"BTCUSDT\","
      "\"U\":1,\"u\":2,\"pu\":0,\"b\":[" +
      many + "],\"a\":[[\"60001.00\",\"0.5\"]]}}";
  require(parse_binance_frame(wide, 7, symbols, frame) && frame.kind == FrameKind::depth,
          "600-level diff parses");
  require(frame.depth.symbol == 0 && frame.depth.bid_count == 600, "wide diff fields");
}

void test_instruments() {
  const auto basket = parse_basket("BTCUSDT, SHIB:1000SHIBUSDT:SHIB1000USDT");
  require(basket.size() == 2 && basket[1].symbol[0] == "1000SHIBUSDT" && basket[1].symbol[1] == "SHIB1000USDT",
          "basket mapping");
  Instrument instrument = basket[0];
  const std::string binance =
      R"({"symbols":[{"symbol":"BTCUSDT","pair":"BTCUSDT","contractType":"PERPETUAL","status":"TRADING","quoteAsset":"USDT","filters":[{"minPrice":"0.10","filterType":"PRICE_FILTER","tickSize":"0.10"},{"stepSize":"0.001","filterType":"LOT_SIZE","minQty":"0.001"},{"stepSize":"0.010","filterType":"MARKET_LOT_SIZE","minQty":"0.010"},{"notional":"100","filterType":"MIN_NOTIONAL"}]},{"symbol":"ETHUSDT"}]})";
  const std::string bybit =
      R"({"retCode":0,"result":{"list":[{"symbol":"BTCUSDT","contractType":"LinearPerpetual","status":"Trading","quoteCoin":"USDT","priceFilter":{"tickSize":"0.10"},"lotSizeFilter":{"qtyStep":"0.002","minOrderQty":"0.002","minNotionalValue":"5"}}]}})";
  std::string error;
  require(parse_binance_instrument(binance, instrument, error), "binance instrument: " + error);
  require(instrument.step[0] == d("0.001") && instrument.min_notional[0] == d("100"), "LOT_SIZE not MARKET_LOT_SIZE");
  require(parse_bybit_instrument(bybit, instrument, error), "bybit instrument: " + error);
  require(finalize_instrument(instrument, error), "finalize");
  require(instrument.common_step == d("0.002") && instrument.common_min_qty == d("0.002"), "common step is lcm");
}

void test_hmac() {
  require(hmac_sha256_hex("key", "The quick brown fox jumps over the lazy dog") ==
              "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
          "rfc hmac vector");
  // Example from the Binance signed-endpoint documentation.
  require(hmac_sha256_hex("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
                          "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&"
                          "timestamp=1499827319559") ==
              "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71",
          "binance doc signature");
}

void test_latency_model() {
  LatencyModel fixed(LatencyMode::fixed, 5'000'000, 1'000'000, 1);
  fixed.add_rtt_sample(100'000'000);
  require(fixed.draw() == 6'000'000, "fixed ignores samples");
  LatencyModel sampled(LatencyMode::sampled, 5'000'000, 0, 7);
  require(sampled.draw() == 5'000'000, "sampled falls back when empty");
  for (std::uint64_t i = 1; i <= 100; ++i) sampled.add_rtt_sample(i * 2'000'000);
  LatencyModel replayed(LatencyMode::sampled, 5'000'000, 0, 7);
  for (std::uint64_t i = 1; i <= 100; ++i) replayed.add_rtt_sample(i * 2'000'000);
  bool same = true;
  for (int i = 0; i < 50; ++i) same = same && sampled.draw() == replayed.draw();
  require(same, "seeded draws reproduce");
  LatencyModel pessimistic(LatencyMode::pessimistic, 5'000'000, 0, 7);
  for (std::uint64_t i = 1; i <= 100; ++i) pessimistic.add_rtt_sample(i * 2'000'000);
  require(pessimistic.draw() == 95'000'000, "pessimistic uses p95 one-way");
}

Scenario started() {
  Scenario s;
  s.quote(Venue::binance, "100.00", "100.02");
  s.quote(Venue::bybit, "100.00", "100.02");
  const auto next = s.now_ns() / 1'000'000 + 8 * 3'600'000;
  s.funding(Venue::binance, "0.0001", next);
  s.funding(Venue::bybit, "0.0001", next);
  s.advance_ms(1);
  return s;
}

void test_edge_math() {
  auto s = started();
  s.engine().clear_outputs();
  s.quote(Venue::bybit, "100.60", "100.62");
  const auto* decision = s.last(OutputKind::decision);
  require(decision != nullptr && decision->decision.dir == Direction::long_binance, "rich bybit -> long binance");
  const auto& x = decision->decision;
  // gap = (100.60 - 100.02) / 100.31; exit = (100.62 - 100.00) / 100.31; basis = 0.60 / 100.31.
  require(x.gap_ppm == ratio_ppm(d("0.58").raw, d("100.31").raw), "entry gap");
  require(x.exit_gap_ppm == ratio_ppm(d("0.62").raw, d("100.31").raw), "exit gap");
  require(x.capture_ppm == x.gap_ppm - x.mean_ppm - (x.exit_gap_ppm - x.basis_ppm), "capture identity");
  require(x.fees_ppm == 2 * (500 + 550) && x.funding_ppm == 0, "fees, no funding inside hold");
  require(x.net_ppm == x.capture_ppm - x.fees_ppm, "net");
}

void test_cycle_latency_and_pnl() {
  auto s = started();
  s.quote(Venue::bybit, "100.60", "100.62");
  require(s.engine().pair(0).phase == PairPhase::opening, "opening");
  s.advance_ms(2);
  require(s.count(OutputKind::leg_fill) == 0, "no fill before the 3 ms Binance latency");
  s.advance_ms(1);
  require(s.count(OutputKind::leg_fill) == 1, "Binance leg fills at arrival");
  s.advance_ms(40);
  require(s.engine().pair(0).phase == PairPhase::open, "both legs filled");
  require(s.engine().pair(0).qty_raw[0] == d("9.969").raw && s.engine().pair(0).qty_raw[1] == -d("9.969").raw,
          "balanced legs");
  s.quote(Venue::bybit, "100.00", "100.02");
  require(s.engine().pair(0).phase == PairPhase::closing, "converged exit");
  s.advance_ms(50);
  const auto* cycle = s.last(OutputKind::cycle);
  require(cycle != nullptr && cycle->cycle.reason == ExitReason::converged, "cycle closed");
  // gross = 9.969 * ((100.00 - 100.02) + (100.60 - 100.02))
  require(cycle->cycle.gross_raw == mul_scaled(d("9.969").raw, d("0.56").raw), "gross P&L");
  require(cycle->cycle.net_raw == cycle->cycle.gross_raw - cycle->cycle.fees_raw, "net = gross - fees");
  require(cycle->cycle.net_raw > 0 && s.engine().metrics().cycles == 1, "profitable cycle");
}

void test_partial_fill_haircut() {
  auto s = started();
  s.quote(Venue::bybit, "100.60", "100.62");
  s.advance_ms(5);  // Binance leg filled 9.969
  // Before the Bybit leg arrives its bid shrinks to 4; the 50% haircut leaves 2.
  s.quote(Venue::bybit, "100.60", "100.62", "4");
  s.advance_ms(40);
  s.advance_ms(10);  // repair (sell the Binance excess) arrives
  const auto& pair = s.engine().pair(0);
  require(s.engine().metrics().repairs == 1, "one repair for the residual");
  require(pair.phase == PairPhase::open && pair.qty_raw[0] == d("2").raw && pair.qty_raw[1] == -d("2").raw,
          "rebalanced to the filled size");
}

void test_funding_settlement() {
  auto s = started();
  s.quote(Venue::bybit, "100.60", "100.62");
  s.advance_ms(40);
  require(s.engine().pair(0).phase == PairPhase::open, "position open");
  const auto settle_ms = s.engine().funding(Venue::binance, 0).next_ms;
  // Long Binance pays +0.0001; short Bybit receives +0.0001 at the venue mark.
  const auto before = s.engine().pair(0).funding_raw;
  FundingEvent paid;
  paid.venue = Venue::binance;
  paid.receive_ns = s.now_ns() + 1'000'000;
  paid.event_ms = settle_ms + 1;
  paid.next_funding_ms = settle_ms + 8 * 3'600'000;
  paid.funding_rate = d("0.0002");
  paid.has_rate = true;
  s.engine().on_funding(paid);
  // Scenario funding marks at the venue mid (100.01).
  const auto expected = -mul_scaled(mul_scaled(d("9.969").raw, d("100.01").raw), d("0.0001").raw);
  require(s.engine().pair(0).funding_raw - before == expected, "long pays the pre-settlement rate");
  require(s.engine().funding(Venue::binance, 0).rate == d("0.0002"), "new rate after settlement");
}

void test_entry_risk_gates() {
  auto config = scenario_config();
  config.strategy.basis_warmup_ms = 60'000;
  Scenario s(config);
  s.quote(Venue::binance, "100.00", "100.02");
  s.quote(Venue::bybit, "100.00", "100.02");
  require(s.engine().entry_risk(0, 1'000 * kScale) == RiskCode::funding_unknown, "needs funding state");
  const auto next = s.now_ns() / 1'000'000 + 8 * 3'600'000;
  s.funding(Venue::binance, "0.0001", next);
  s.funding(Venue::bybit, "0.0001", next);
  require(s.engine().entry_risk(0, 1'000 * kScale) == RiskCode::basis_cold, "basis warmup gate");
  require(Scenario().engine().entry_risk(0, 1'000 * kScale) != RiskCode::ok, "empty books blocked");
  // Divergent venues disable the symbol at first sight.
  Scenario divergent;
  divergent.quote(Venue::binance, "100.00", "100.02");
  divergent.quote(Venue::bybit, "103.00", "103.02");
  require(divergent.engine().symbol_disabled(0) && divergent.count(OutputKind::disabled) == 1, "mid check");
}

void test_replay_fixture() {
  RuntimeConfig config = scenario_config();
  ReplayResult first, second;
  std::string error;
  const auto path = fixtures + "/session.jsonl";
  require(replay_journal(path, config, first, error), "replay: " + error);
  require(replay_journal(path, config, second, error) && first.hash == second.hash, "deterministic replay");
  require(first.metrics.cycles == 1 && first.metrics.net_raw > 0, "fixture cycle replays");
  require(first.metrics.gaps[0] == 1, "fixture gap detected");
  GapReport gaps;
  require(gapcheck(path, gaps, error) && gaps.gaps[0] == 1 && gaps.recoveries[0] == 1, "gapcheck recovery");
}

void test_replay_interleaved_header() {
  // The writer emits each instrument with its fees (Y C C Y C C); replay must
  // accept that order rather than fail on the second instrument.
  const auto path = std::filesystem::temp_directory_path() / "cxa-interleaved-test.jsonl";
  {
    std::ofstream out(path);
    out << "Z 1 shadow pessimistic 100000000000 1000 500000 T0,T1\n";
    out << "Y 0 T0 A0 B0 1000000 100000 100000 500000000 1000000 100000 100000 500000000 100000 100000\n";
    out << "C 0 0 500\nC 1 0 550\n";
    out << "Y 1 T1 A1 B1 1000000 100000 100000 500000000 1000000 100000 100000 500000000 100000 100000\n";
    out << "C 0 1 500\nC 1 1 550\n";
    out << "S 0 0 1700000000000000000 1000 1 10000000000 10000000000 1 10002000000 10000000000\n";
    out << "D 0 0 0 1700000000001000000 1700000000001 1001 1001 1000 1 10000000000 10000000000 1 "
           "10002000000 10000000000\n";
    out << "S 0 1 1700000000000000000 2000 1 10000000000 10000000000 1 10002000000 10000000000\n";
    out << "D 0 1 0 1700000000001000000 1700000000001 2001 2001 2000 1 10000000000 10000000000 1 "
           "10002000000 10000000000\n";
  }
  RuntimeConfig config = scenario_config();
  ReplayResult result;
  std::string error;
  require(replay_journal(path.string(), config, result, error), "interleaved replay: " + error);
  require(result.events == 4, "interleaved events replayed");
  std::filesystem::remove(path);
}

void test_reports() {
  const auto path = std::filesystem::temp_directory_path() / "cxa-report-test.jsonl";
  {
    std::ofstream out(path);
    out << "Z 1 shadow pessimistic 100000000000 1000 500000 TEST\n";
    for (int i = 0; i < 160; ++i) {
      // 1000 USD notional, net 0.10 USD (100 ppm); every 10th cycle loses 0.30 USD (-300 ppm).
      const auto net = i % 10 == 0 ? -30'000'000 : 10'000'000;
      out << "P " << i << " 0 1 converged 0 1000000000 100000000 4000 1000 2500 1200 30000000 20000000 0 " << net
          << " 100000000000 " << (i % 25 == 0 ? 1 : 0) << '\n';
    }
    out << "D 0 0 0 " << 15ULL * 86'400'000'000'000ULL << " 0 0 0 0 0 0\n";
    out << "O 0 1 1 3000 0 600000000 3500\nO 0 1 1 3000 0 1000000 3100\n";
  }
  std::string json, error;
  require(daily_report({path.string()}, json, error), "daily report");
  require(json.find("\"cycles\":160") != std::string::npos, "cycle count");
  require(json.find("\"mean_net_ppm\":60.00") != std::string::npos, "mean net ppm");
  require(json.find("\"mean_net\":false") != std::string::npos, "60 ppm fails the 800 ppm gate");
  require(json.find("\"days\":true") != std::string::npos && json.find("\"pass\":false") != std::string::npos,
          "gate evaluated");
  require(census_report({path.string()}, json, error), "census report");
  require(json.find("\"episodes\":2") != std::string::npos && json.find("\"ge_500ms\":1") != std::string::npos,
          "census buckets");
  std::filesystem::remove(path);
}

}  // namespace

int main(int argc, char** argv) {
  fixtures = argc > 1 ? argv[1] : "tests/fixtures";
  const std::array<std::pair<const char*, void (*)()>, 15> cases{{
      {"decimal_and_ppm", test_decimal_and_ppm},
      {"book_and_sweep", test_book_and_sweep},
      {"binance_sync", test_binance_sync},
      {"bybit_sync", test_bybit_sync},
      {"parsers", test_parsers},
      {"instruments", test_instruments},
      {"hmac", test_hmac},
      {"latency_model", test_latency_model},
      {"edge_math", test_edge_math},
      {"cycle_latency_and_pnl", test_cycle_latency_and_pnl},
      {"partial_fill_haircut", test_partial_fill_haircut},
      {"funding_settlement", test_funding_settlement},
      {"entry_risk_gates", test_entry_risk_gates},
      {"replay_fixture", test_replay_fixture},
      {"replay_interleaved_header", test_replay_interleaved_header},
  }};
  try {
    for (const auto& [name, test] : cases) {
      test();
      std::cout << "ok " << name << '\n';
    }
    test_reports();
    std::cout << "ok reports\n";
  } catch (const std::exception& error) {
    std::cerr << "FAILED after " << tests << " checks: " << error.what() << '\n';
    return 1;
  }
  std::cout << tests << " checks passed\n";
  return 0;
}
