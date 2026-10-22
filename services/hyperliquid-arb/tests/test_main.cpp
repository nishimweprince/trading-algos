#include "hla/config.hpp"
#include "hla/drills.hpp"
#include "hla/hl_feed.hpp"
#include "hla/hl_gateway.hpp"
#include "hla/hl_info.hpp"
#include "hla/hl_instruments.hpp"
#include "hla/hl_parser.hpp"
#include "hla/hl_sign.hpp"
#include "hla/journal.hpp"
#include "hla/mt5_bridge.hpp"
#include "hla/platform.hpp"
#include "hla/spsc.hpp"
#include "hla/types.hpp"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace hla;

int tests = 0;
std::string fixtures;

void require(bool condition, std::string_view message) {
  ++tests;
  if (!condition) throw std::runtime_error(std::string(message));
}

Decimal d(std::string_view text) { return Decimal::parse(text); }

std::string fixture(const std::string& name) {
  std::ifstream in(fixtures + "/" + name);
  if (!in) throw std::runtime_error("missing fixture " + name);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

void test_decimal_and_ppm() {
  require(d("123.45678901").raw == 12'345'678'901LL, "decimal exact");
  require(d("-0.0000125").raw == -1'250, "negative funding decimal");
  require(round_down(d("157.849"), d("0.01")) == d("157.84"), "round down");
  require(round_up(d("157.841"), d("0.01")) == d("157.85"), "round up");
  require(ratio_ppm(d("0.35").raw, d("100").raw) == 3'500, "ratio ppm");
  require(apply_ppm(d("1000").raw, 90) == d("0.09").raw, "apply ppm");
  require(mul_scaled(d("157.845").raw, d("1000").raw) == d("157845").raw, "mul scaled");
}

void test_spsc() {
  SpscRing<int, 4> ring;
  require(ring.try_push(1) && ring.try_push(2) && ring.try_push(3), "push to capacity - 1");
  require(!ring.try_push(4), "full ring refuses");
  int value = 0;
  require(ring.try_pop(value) && value == 1 && *ring.front() == 2, "fifo order");
}

void test_coins() {
  const auto coins = parse_coins(" xyz:JPY, BTC ,xyz:EUR");
  require(coins.size() == 3 && coins[0] == "xyz:JPY" && coins[1] == "BTC", "coin list trimmed in order");
  require(coin_dex("xyz:JPY") == "xyz" && coin_dex("BTC").empty(), "coin dex prefix");
  bool threw = false;
  try { parse_coins("BTC,BTC"); } catch (const std::invalid_argument&) { threw = true; }
  require(threw, "duplicate coin rejected");
}

void test_dex_index() {
  const auto dexs = fixture("perp_dexs.json");
  std::uint32_t index = 99;
  std::string error;
  require(parse_dex_index(dexs, "", index, error) && index == 0, "validator dex is 0");
  require(parse_dex_index(dexs, "xyz", index, error) && index == 1, "xyz is dex 1");
  require(parse_dex_index(dexs, "vntl", index, error) && index == 3, "vntl is dex 3");
  require(!parse_dex_index(dexs, "nope", index, error), "unknown dex rejected");
  // Nested arrays and commas inside objects must not shift the count.
  const std::string nested = R"([null,{"name":"a","caps":[["a:X","1"],["a:Y","2"]]},{"name":"b"}])";
  require(parse_dex_index(nested, "b", index, error) && index == 2, "nested arrays skipped");
}

void test_hip3_markets() {
  const auto meta = fixture("meta_xyz.json");
  const auto mids = fixture("mids_xyz.json");
  std::string error;
  HlMarket jpy{.coin = "xyz:JPY", .dex = "xyz", .dex_index = 1};
  require(parse_hl_market(meta, mids, jpy, error), error);
  require(jpy.meta_index == 24 && jpy.asset_id == 110'024, "xyz:JPY asset id");
  require(jpy.size_decimals == 2 && jpy.step == d("0.01") && jpy.max_leverage == 50, "xyz:JPY lot and leverage");
  require(jpy.growth_mode && jpy.only_isolated, "xyz:JPY growth mode and isolated");
  require(jpy.tick == d("0.01"), "xyz:JPY tick from 5 significant figures");

  HlMarket eur{.coin = "xyz:EUR", .dex = "xyz", .dex_index = 1};
  require(parse_hl_market(meta, mids, eur, error) && eur.asset_id == 110'025, "xyz:EUR asset id");
  require(eur.tick == d("0.0001"), "xyz:EUR tick: 5 significant figures, about 0.9 bps");

  HlMarket gold{.coin = "xyz:GOLD", .dex = "xyz", .dex_index = 1};
  require(parse_hl_market(meta, mids, gold, error) && !gold.growth_mode && !gold.only_isolated,
          "xyz:GOLD is not in growth mode");

  HlMarket missing{.coin = "xyz:AUD", .dex = "xyz", .dex_index = 1};
  require(!parse_hl_market(meta, mids, missing, error), "unknown HIP-3 coin rejected");

  HlMarket eth{.coin = "ETH", .dex = {}};
  require(parse_hl_market(fixture("meta_main.json"), fixture("mids_main.json"), eth, error), error);
  require(eth.asset_id == 1 && eth.step == d("0.0001") && eth.tick == d("0.1"), "validator perp asset id and tick");
}

void test_frames() {
  const std::vector<std::string> coins{"BTC", "xyz:JPY"};
  HlFrame frame;
  const std::string bbo =
      R"({"channel":"bbo","data":{"coin":"xyz:JPY","time":1761024000123,)"
      R"("bbo":[{"px":"157.84","sz":"12000.0","n":3},{"px":"157.85","sz":"8000.0","n":2}]}})";
  require(parse_hl_frame(bbo, 5, coins, frame) && frame.kind == HlFrameKind::bbo, "bbo parsed");
  require(frame.bbo.symbol == 1 && frame.bbo.event_ms == 1761024000123ULL, "bbo symbol and time");
  require(frame.bbo.has_bid && frame.bbo.bid.price == d("157.84") && frame.bbo.ask.quantity == d("8000"),
          "bbo levels");

  const std::string one_sided = R"({"channel":"bbo","data":{"coin":"BTC","time":1,"bbo":[null,{"px":"1","sz":"2","n":1}]}})";
  require(parse_hl_frame(one_sided, 5, coins, frame) && !frame.bbo.has_bid && frame.bbo.has_ask, "null bid side");

  const std::string ctx =
      R"({"channel":"activeAssetCtx","data":{"coin":"xyz:JPY","ctx":{"funding":"-0.0000153",)"
      R"("openInterest":"30890000.0","markPx":"157.845","oraclePx":"157.92","midPx":"157.845"}}})";
  require(parse_hl_frame(ctx, 7, coins, frame) && frame.kind == HlFrameKind::ctx, "ctx parsed");
  require(frame.ctx.funding == d("-0.0000153") && frame.ctx.oracle == d("157.92") && frame.ctx.receive_ns == 7,
          "ctx funding and oracle");

  require(parse_hl_frame(R"({"channel":"subscriptionResponse","data":{}})", 1, coins, frame) &&
              frame.kind == HlFrameKind::ignored,
          "subscription ack ignored");
  require(parse_hl_frame(R"({"channel":"bbo","data":{"coin":"ETH","time":1,"bbo":[null,null]}})", 1, coins, frame) &&
              frame.kind == HlFrameKind::ignored,
          "unknown coin ignored");
  require(!parse_hl_frame(R"({"channel":"error","data":"bad"})", 1, coins, frame), "error channel fails");
  require(!parse_hl_frame(R"({"channel":"bbo","data":{"coin":"BTC","bbo":[]}})", 1, coins, frame),
          "bbo without time fails");
  require(hl_subscription("subscribe", "bbo", "xyz:JPY") ==
              R"({"method":"subscribe","subscription":{"type":"bbo","coin":"xyz:JPY"}})",
          "subscription text");
}

std::vector<std::string> split(const std::string& line, char separator) {
  std::vector<std::string> parts;
  std::size_t start = 0;
  for (std::size_t pos; (pos = line.find(separator, start)) != std::string::npos; start = pos + 1)
    parts.push_back(line.substr(start, pos - start));
  parts.push_back(line.substr(start));
  return parts;
}

Cloid cloid(std::uint8_t last) {
  Cloid c{};
  c[15] = last;
  return c;
}

// Every action hlarb sends, built the way the gateway builds it; compared byte for byte
// with vectors produced by hyperliquid-python-sdk 0.24.0 (tests/fixtures/hl_sign_vectors.txt).
void test_signing_vectors() {
  const std::uint32_t jpy = 110'024;
  const OrderSpec bid{jpy, true, d("157.84"), d("6.33"), false, Tif::alo, cloid(1)};
  const OrderSpec ask{jpy, false, d("157.86"), d("6.33"), false, Tif::alo, cloid(2)};
  const std::vector<OrderSpec> two{bid, ask};
  const std::vector<OrderSpec> one{bid};
  const std::vector<OrderSpec> ioc{{0, false, d("83000"), d("0.00123"), true, Tif::ioc, std::nullopt}};
  const std::vector<ModifySpec> modify{{123'456'789, {jpy, true, d("157.83"), d("6.33"), false, Tif::alo, cloid(1)}}};
  const std::vector<CancelSpec> cancels{{jpy, 123'456'789}, {0, 42}};
  const std::vector<CancelCloidSpec> by_cloid{{jpy, cloid(1)}};
  std::map<std::string, Node> actions{
      {"order_alo", order_action(two)},
      {"order_alo_expires", order_action(one)},
      {"order_ioc", order_action(ioc)},
      {"batch_modify", modify_action(modify)},
      {"cancel", cancel_action(cancels)},
      {"cancel_by_cloid", cancel_by_cloid_action(by_cloid)},
      {"schedule_cancel", schedule_cancel_action(1'761'024'060'000ULL)},
      {"schedule_cancel_clear", schedule_cancel_action(std::nullopt)},
      {"update_leverage", update_leverage_action(jpy, false, 3)},
  };
  Signer signer;
  std::string error;
  require(signer.load("0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318", error), error);
  std::istringstream lines(fixture("hl_sign_vectors.txt"));
  std::size_t checked = 0;
  for (std::string line; std::getline(lines, line);) {
    if (line.empty() || line.front() == '#') continue;
    const auto f = split(line, '|');
    if (f[0] == "address") {
      require(signer.address() == f[1], "agent address derived from key");
      continue;
    }
    const auto found = actions.find(f[0]);
    require(found != actions.end() && f.size() == 8, "vector " + f[0] + " is known");
    const auto& action = found->second;
    const auto expires = std::stoull(f[1]);
    std::string packed;
    append_msgpack(action, packed);
    const std::span<const std::uint8_t> packed_bytes(reinterpret_cast<const std::uint8_t*>(packed.data()), packed.size());
    require(hex(packed_bytes, false) == f[2], f[0] + ": msgpack matches SDK");
    require(hex(action_hash(action, 1'761'024'000'123ULL, expires), false) == f[3], f[0] + ": action hash");
    const auto signature = signer.sign_action(action, 1'761'024'000'123ULL, expires);
    require(signature.r == f[4] && signature.s == f[5] && std::to_string(signature.v) == f[6],
            f[0] + ": signature matches SDK");
    std::string json;
    append_json(action, json);
    require(json == f[7], f[0] + ": JSON matches SDK");
    ++checked;
  }
  require(checked == actions.size(), "all signing vectors checked");
  require(to_wire(d("83000")) == "83000" && to_wire(d("0.00123")) == "0.00123" && to_wire(d("0")) == "0" &&
              to_wire(d("-1.5")) == "-1.5",
          "float_to_wire equivalent");
  require(!signer.load("0x00", error), "short key rejected");
}

void test_gateway_parsing() {
  HlEvent event;
  std::uint64_t id = 0;
  const std::string placed =
      R"({"channel":"post","data":{"id":7,"response":{"type":"action","payload":{"status":"ok","response":{"type":"order",)"
      R"("data":{"statuses":[{"resting":{"oid":77738308}},{"error":"Post only order would have immediately matched, bbo was 157.84@157.85. asset=110024"}]}}}}}})";
  require(parse_post_frame(placed, id, event) && id == 7 && event.ok && event.count == 2, "order ack parsed");
  require(event.items[0].status == AckStatus::resting && event.items[0].oid == 77'738'308, "resting oid");
  require(event.items[1].status == AckStatus::error && event.items[1].post_only_reject, "post-only reject flagged");

  const std::string margin =
      R"({"channel":"post","data":{"id":8,"response":{"type":"action","payload":{"status":"err","response":"Insufficient margin to place order."}}}})";
  require(parse_post_frame(margin, id, event) && !event.ok && event.text_view().starts_with("Insufficient margin"),
          "action-level error");
  const std::string cancel =
      R"({"status":"ok","response":{"type":"cancel","data":{"statuses":["success",{"error":"Order was never placed, already canceled, or filled."}]}}})";
  require(parse_action_payload(cancel, event) && event.count == 2 && event.items[0].status == AckStatus::success &&
              event.items[1].status == AckStatus::error,
          "cancel statuses");
  const std::string ioc =
      R"({"status":"ok","response":{"type":"order","data":{"statuses":[{"filled":{"totalSz":"6.33","avgPx":"157.71","oid":99}}]}}})";
  require(parse_action_payload(ioc, event) && event.items[0].status == AckStatus::filled &&
              event.items[0].filled_size == d("6.33") && event.items[0].average_price == d("157.71"),
          "IOC fill status");
  require(parse_action_payload(R"({"status":"ok","response":{"type":"default"}})", event) && event.ok && event.count == 0,
          "scheduleCancel ack");
  require(parse_post_frame(R"({"channel":"post","data":{"id":9,"response":{"type":"error","payload":"rate limited"}}})", id,
                           event) &&
              !event.ok && event.text_view() == "rate limited",
          "post error response");

  const std::vector<std::string> coins{"BTC", "xyz:JPY"};
  std::vector<HlEvent> events;
  require(parse_user_fills(R"({"channel":"userFills","data":{"isSnapshot":true,"user":"0x1","fills":[{"coin":"BTC","px":"1","sz":"1","side":"B","oid":1}]}})",
                           coins, 5, events) &&
              events.empty(),
          "fill snapshot ignored");
  const std::string fills =
      R"({"channel":"userFills","data":{"user":"0x1","fills":[)"
      R"({"coin":"xyz:JPY","px":"157.75","sz":"6.33","side":"B","time":1761048000000,"startPosition":"0.0","dir":"Open Long",)"
      R"("closedPnl":"0.0","hash":"0xab","oid":11,"crossed":false,"fee":"0.0029","tid":555,"feeToken":"USDC"},)"
      R"({"coin":"BTC","px":"83000","sz":"0.001","side":"A","time":1,"dir":"Auto-Deleveraging","oid":12,"crossed":true,"fee":"0","tid":556},)"
      R"({"coin":"ETH","px":"1","sz":"1","side":"A","oid":13}]}})";
  require(parse_user_fills(fills, coins, 5, events) && events.size() == 2, "two basket fills");
  require(events[0].symbol == 1 && events[0].side == Side::buy && events[0].size == d("6.33") && events[0].oid == 11 &&
              events[0].fee == d("0.0029") && !events[0].forced,
          "maker fill fields");
  require(events[1].symbol == 0 && events[1].side == Side::sell && events[1].forced && events[1].crossed, "ADL fill flagged");
  const std::string updates =
      R"({"channel":"orderUpdates","data":[{"order":{"coin":"xyz:JPY","side":"B","limitPx":"157.75","sz":"0.0","oid":11,)"
      R"("timestamp":1,"origSz":"6.33","cloid":"0x01"},"status":"filled","statusTimestamp":2},)"
      R"({"order":{"coin":"BTC","side":"A","limitPx":"1","sz":"1","oid":12,"timestamp":1,"origSz":"1"},"status":"marginCanceled","statusTimestamp":2}]})";
  require(parse_order_updates(updates, coins, 5, events) && events.size() == 2 &&
              events[0].order_status == OrderStatus::filled && events[1].order_status == OrderStatus::canceled,
          "order updates");
  require(order_status_from("open") == OrderStatus::open && order_status_from("perpMarginRejected") == OrderStatus::rejected,
          "status mapping");

  HlCommand command;
  command.kind = HlCommandKind::cancel_cloid;
  command.count = 1;
  command.orders[0].asset = 110'024;
  command.orders[0].cloid = cloid(1);
  std::string json;
  append_json(command_action(command), json);
  require(json == R"({"type":"cancelByCloid","cancels":[{"asset":110024,"cloid":"0x00000000000000000000000000000001"}]})",
          "command action JSON");
}

void test_info_parsing() {
  HlAccountState state;
  const std::string clearing =
      R"({"marginSummary":{"accountValue":"1250.5","totalNtlPos":"0.0","totalRawUsd":"1250.5","totalMarginUsed":"0.0"},)"
      R"("crossMarginSummary":{"accountValue":"1250.5"},"withdrawable":"1100.25","assetPositions":[)"
      R"({"type":"oneWay","position":{"coin":"xyz:JPY","szi":"-6.33","leverage":{"type":"isolated","value":3,"rawUsd":"1.0"},"entryPx":"157.8"}}],"time":1})";
  require(parse_clearinghouse(clearing, state) && state.account_value == d("1250.5") &&
              state.withdrawable == d("1100.25") && state.positions.size() == 1 &&
              state.positions[0].coin == "xyz:JPY" && state.positions[0].size == d("-6.33"),
          "clearinghouse parsed");
  HlRateLimit limit;
  require(parse_rate_limit(R"({"cumVlm":"2854.5","nRequestsUsed":2890,"nRequestsCap":12854,"nRequestsSurplus":0})", limit) &&
              limit.remaining() == 9'964,
          "rate limit parsed");
  std::vector<HlOpenOrder> open;
  require(parse_open_orders(R"([{"coin":"BTC","limitPx":"1","oid":42,"side":"A","sz":"1","timestamp":1}])", open) &&
              open.size() == 1 && open[0].oid == 42,
          "open orders parsed");
  std::vector<std::string> agents;
  require(parse_agents(R"([{"name":"hlarb","address":"0x2C7536E3605D9C16a7a3D7b1898e529396a65c23","validUntil":1}])", agents) &&
              agents[0] == "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23",
          "agents lowercased");
  std::int64_t taker = 0;
  std::int64_t maker = 0;
  require(parse_user_fees(R"({"userCrossRate":"0.00045","userAddRate":"0.00015"})", taker, maker) && taker == 450 &&
              maker == 150,
          "fees in ppm");
}

// The engine side and the exact C ABI the MQL5 services call, over real shared memory.
void test_bridge() {
  const std::string name = "Local\\hla_test_" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count() % 100000);
  const std::wstring wide(name.begin(), name.end());
  bridge::SharedMemory::remove(name.c_str());
  require(hla_open(wide.c_str()) == 0, "DLL waits while the engine is absent");
  Mt5Bridge engine;
  std::string error;
  const std::vector<std::string> symbols{"USDJPY", "EURUSD"};
  require(engine.create(name, symbols, error), error);
  require(hla_open(wide.c_str()) == 1 && hla_open(wide.c_str()) == 1, "publisher and executor open");
  require(hla_symbol_count() == 2 && hla_generation() >= 1, "symbols visible");
  std::array<wchar_t, 32> buffer{};
  require(hla_symbol_name(1, buffer.data(), 32) == 6 && std::wstring(buffer.data()) == L"EURUSD", "symbol name");
  require(!engine.spec(0).valid, "no spec before publish");
  hla_publish_spec(0, 100000, 0.01, 0.01, 100, 0.001, 0.6337, 3, 4, 2, 1);
  const auto spec = engine.spec(0);
  require(spec.valid && spec.tradeable() && std::abs(spec.value_per_point() - 633.7) < 1e-6, "spec through seqlock");
  require(hla_push_tick(0, 157.795, 157.805, 123) == 1 && hla_push_tick(5, 1, 1, 1) == 0, "tick push, bad index refused");
  bridge::Tick tick{};
  require(engine.pop_tick(tick) && tick.symbol == 0 && tick.ask == 157.805 && tick.publish_ns > 0, "tick received");
  const auto now = bridge::wall_ns();
  require(engine.push_order({1, 0, bridge::sell, 0.01, 30, 0, now, now - 1}), "expired order queued");
  require(engine.push_order({2, 0, bridge::sell, 0.01, 30, 0, now, now + 5'000'000'000ULL}), "live order queued");
  long long request = 0;
  int index = -1;
  int side = -1;
  double volume = 0;
  int deviation = 0;
  require(hla_pop_order(&request, &index, &side, &volume, &deviation) == 1 && request == 2 && side == 1 &&
              volume == 0.01 && deviation == 30,
          "executor skips the expired order");
  bridge::Txn txn{};
  require(engine.pop_txn(txn) && txn.request_id == 1 && txn.status == bridge::expired, "expired order answered by the DLL");
  hla_push_txn(2, 0, 1, bridge::filled, 10009, 0.01, 157.795, -0.07, 555, 777);
  require(engine.pop_txn(txn) && txn.request_id == 2 && txn.status == bridge::filled && txn.deal == 555, "fill txn");
  hla_account(42, 1000, 1001, 900, 1, 1, 0);
  hla_position(0, -0.01);
  hla_positions_done();
  hla_heartbeat(0);
  hla_heartbeat(1);
  const auto account = engine.account();
  require(account.valid && account.login == 42 && account.equity == 1001 && account.trade_allowed &&
              account.positions[0] == -0.01 && account.positions_ns > 0,
          "account and positions");
  require(engine.publisher_ns() > 0 && engine.executor_ns() > 0 && hla_engine_age_ms() >= 0, "heartbeats");
  // An engine restart clears the rings: an order left by the previous run never executes.
  require(engine.push_order({3, 0, bridge::buy, 0.01, 30, 0, now, now + 5'000'000'000ULL}), "order before restart");
  const auto generation = hla_generation();
  Mt5Bridge restarted;
  require(restarted.create(name, symbols, error) && hla_generation() == generation + 1, "restart bumps generation");
  require(hla_pop_order(&request, &index, &side, &volume, &deviation) == 0, "stale order dropped by restart");
  hla_close();
  hla_close();
  bridge::SharedMemory::remove(name.c_str());
}

void test_config_parsing() {
  const auto pairs = parse_pairs("xyz:JPY=USDJPY, xyz:XYZ100=NAS100*1.25");
  require(pairs.size() == 2 && pairs[1].mt5_symbol == "NAS100" && pairs[1].ratio == 1.25, "pairs with ratio");
  bool threw = false;
  try { parse_pairs("BTC=BTCUSD,ETH=BTCUSD"); } catch (const std::invalid_argument&) { threw = true; }
  require(threw, "duplicate MT5 symbol rejected");
  const auto windows = parse_windows("20:55-21:10,21:55-22:10");
  require(windows.size() == 2 && windows[0].first == 1255 && windows[1].second == 1330, "rollover windows");
}

void test_report() {
  const auto path = (std::filesystem::temp_directory_path() / "hla_report_test.jsonl").string();
  {
    std::ofstream out(path);
    out << R"({"k":"f","t":1000000000,"coin":"xyz:JPY","fair":157.80})" << '\n'
        << R"({"k":"fill","t":1000000000,"coin":"xyz:JPY","side":"B","px":157.75,"sz":6.33,"fee":0.003,"crossed":false})" << '\n'
        << R"({"k":"hedge","t":1000000000})" << '\n'
        << R"({"k":"txn","t":1,"status":1,"hedge_ms":4.5,"order_ms":3.0,"ours":true})" << '\n'
        << R"({"k":"f","t":2000000000,"coin":"xyz:JPY","fair":157.80})" << '\n'
        << R"({"k":"f","t":11000000000,"coin":"xyz:JPY","fair":157.70})" << '\n';
  }
  std::ostringstream report;
  require(journal_report({path}, report) == 0, "report runs");
  const auto text = report.str();
  require(text.find("\"fills\":1") != std::string::npos && text.find("\"hedge_ms\":{\"p50\":4.5") != std::string::npos,
          "report counts fills and hedge latency");
  // Bought at 157.75: +317 ppm at 1 s (fair 157.80), -317 ppm at 10 s (fair 157.70).
  require(text.find("\"markout_1s_ppm\":316.9") != std::string::npos &&
              text.find("\"markout_10s_ppm\":-316.9") != std::string::npos,
          "markouts signed by side");
  std::remove(path.c_str());
}

void test_drills() {
  std::ostringstream out;
  require(run_drills(out) == 0, "drills:\n" + out.str());
}

void test_platform() {
  // No sockets: wait degrades to a timed sleep and must not fail.
  require(platform::wait_readable(nullptr, 0, 1), "empty wait");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: hla-tests <fixtures-dir>\n";
    return 2;
  }
  fixtures = argv[1];
  try {
    test_decimal_and_ppm();
    test_spsc();
    test_coins();
    test_dex_index();
    test_hip3_markets();
    test_frames();
    test_signing_vectors();
    test_gateway_parsing();
    test_info_parsing();
    test_bridge();
    test_config_parsing();
    test_report();
    test_drills();
    test_platform();
  } catch (const std::exception& error) {
    std::cerr << "FAIL after " << tests << " checks: " << error.what() << '\n';
    return 1;
  }
  std::cout << tests << " checks passed\n";
  return 0;
}
