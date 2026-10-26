#include "check.hpp"
#include "pkm/feeds.hpp"

using namespace pkm;
using pkm::test::require;
using pkm::test::require_eq;

namespace {

engine::Engine make() {
  engine::Engine e({}, {"BTC", "ETH"});
  engine::Window w;
  w.asset = "BTC";
  w.open_unix = 1'761'500'700;
  w.kalshi_ticker = "KXBTC15M-26OCT261245";
  w.up_token = "111";
  w.down_token = "222";
  w.condition_id = "0xc";
  e.set_window(w, 0);
  return e;
}

}  // namespace

PKM_TEST(kalshi_snapshot_and_deltas) {
  auto e = make();
  json::Document doc;
  require(doc.parse(R"({"type":"orderbook_snapshot","sid":2,"seq":1,"msg":{"market_ticker":"KXBTC15M-26OCT261245","yes":[[45,10],[44,3]],"no_dollars_fp":[["0.5300","5.00"]]}})"),
          "parses");
  require(feeds::apply_kalshi(doc.root(), e, 10) == 0, "snapshot applied");
  const Book& k = e.kalshi_book(0);
  auto q = k.quote();
  require(q.valid && q.bid == 0.45 && q.ask == 0.47, "cents YES bids; NO 0.53 is a YES ask 0.47");
  require(doc.parse(R"({"type":"orderbook_delta","sid":2,"seq":2,"msg":{"market_ticker":"KXBTC15M-26OCT261245","price_dollars":"0.4550","delta_fp":"7.00","side":"yes"}})"),
          "delta parses");
  require(feeds::apply_kalshi(doc.root(), e, 20) == 0, "delta applied");
  q = k.quote();
  require(q.bid == 0.455 && k.size_at(Side::bid, px_key(0.455)) == 7.0, "deci-cent bid");
  require(doc.parse(R"({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-26OCT261245","price":53,"delta":-5,"side":"no"}})"), "cents delta");
  feeds::apply_kalshi(doc.root(), e, 30);
  require(!k.quote().valid, "ask side emptied: no quote");
  require(doc.parse(R"({"type":"orderbook_delta","msg":{"market_ticker":"OTHER","price":53,"delta":5,"side":"no"}})"), "other");
  require(feeds::apply_kalshi(doc.root(), e, 40) == -1, "not our ticker");
  // Fractional deltas that sum to zero leave no ghost level.
  require(doc.parse(R"({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-26OCT261245","price_dollars":"0.40","delta_fp":"0.1","side":"yes"}})"), "frac");
  for (int i = 0; i < 3; ++i) feeds::apply_kalshi(doc.root(), e, 50);
  require(doc.parse(R"({"type":"orderbook_delta","msg":{"market_ticker":"KXBTC15M-26OCT261245","price_dollars":"0.40","delta_fp":"-0.3","side":"yes"}})"), "frac2");
  feeds::apply_kalshi(doc.root(), e, 60);
  require(k.size_at(Side::bid, px_key(0.40)) == 0.0, "no ghost level");
}

PKM_TEST(kalshi_session_subscribes_once_and_checks_seq) {
  feeds::KalshiSession s;
  auto out = s.sync({"A", "B"});
  require(out.size() == 1 && out[0].find("\"cmd\":\"subscribe\"") != std::string::npos, "first subscribe");
  require(out[0].find(R"("market_tickers":["A","B"])") != std::string::npos, "tickers");
  out = s.sync({"A", "B", "C"});
  require(out.empty(), "adds wait for the first ack");
  json::Document doc;
  std::vector<std::string> send;
  require(doc.parse(R"({"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":7}})"), "ack");
  require(s.on_frame(doc.root(), send) == feeds::KalshiSession::Verdict::ok && s.sid() == 7, "sid from the ack");
  require(send.size() == 1 && send[0].find(R"("action":"add_markets")") != std::string::npos &&
              send[0].find(R"("sids":[7])") != std::string::npos,
          "pending add sent on the sid");
  out = s.sync({"C"});
  require(out.size() == 1 && out[0].find("delete_markets") != std::string::npos && s.subscribed().size() == 1, "remove");
  require(doc.parse(R"({"type":"orderbook_snapshot","sid":7,"seq":1,"msg":{}})"), "seq1");
  require(s.on_frame(doc.root(), send) == feeds::KalshiSession::Verdict::ok, "seq 1");
  require(doc.parse(R"({"type":"orderbook_delta","sid":7,"seq":2,"msg":{}})"), "seq2");
  require(s.on_frame(doc.root(), send) == feeds::KalshiSession::Verdict::ok, "seq 2");
  require(doc.parse(R"({"type":"orderbook_delta","sid":7,"seq":4,"msg":{}})"), "seq4");
  require(s.on_frame(doc.root(), send) == feeds::KalshiSession::Verdict::gap, "gap");
  require(doc.parse(R"({"type":"error","id":3,"msg":{"code":6,"msg":"Already subscribed"}})"), "err");
  require(s.on_frame(doc.root(), send) == feeds::KalshiSession::Verdict::error && s.last_error.find("Already") != std::string::npos,
          "error restarts");
}

PKM_TEST(poly_book_price_change_and_resolution) {
  auto e = make();
  json::Document doc;
  std::vector<char> touched;
  std::vector<feeds::Resolution> resolved;
  require(doc.parse(R"([{"event_type":"book","asset_id":"111","market":"0xc","bids":[{"price":"0.48","size":"30"},{"price":"0.47","size":"10"}],"asks":[{"price":"0.52","size":"25"}],"timestamp":"1"},{"asset_id":"222","bids":[{"price":"0.48","size":"5"}],"asks":[{"price":"0.52","size":"5"}]}])"),
          "books parse");
  feeds::apply_poly(doc.root(), e, 10, touched, &resolved);
  require(touched[0] && !touched[1], "BTC touched only");
  auto q = e.poly_book(0, true).quote();
  require(q.valid && q.bid == 0.48 && q.ask == 0.52, "Up book");
  require(e.poly_book(0, false).quote().valid, "untyped frame with bids is a book");
  touched.assign(2, 0);
  require(doc.parse(R"({"event_type":"price_change","market":"0xc","price_changes":[{"asset_id":"111","price":"0.48","size":"0","side":"BUY","best_bid":"0.47","best_ask":"0.52"},{"asset_id":"111","price":"0.51","size":"4","side":"SELL","best_bid":"0.47","best_ask":"0.51"},{"asset_id":"999","price":"0.5","size":"1","side":"BUY"}]})"),
          "price_change parses");
  feeds::apply_poly(doc.root(), e, 20, touched, &resolved);
  q = e.poly_book(0, true).quote();
  require(touched[0] && q.bid == 0.47 && q.ask == 0.51, "levels set absolutely; matches the venue's BBO fields");
  require(doc.parse(R"({"event_type":"best_bid_ask","asset_id":"111","best_bid":"0.10","best_ask":"0.90"})"), "bba");
  feeds::apply_poly(doc.root(), e, 30, touched, &resolved);
  require(e.poly_book(0, true).quote().bid == 0.47, "best_bid_ask ignored");
  require(doc.parse(R"({"event_type":"market_resolved","market":"0xc","winning_asset_id":"222"})"), "resolved");
  feeds::apply_poly(doc.root(), e, 40, touched, &resolved);
  require(resolved.size() == 1 && resolved[0].condition_id == "0xc" && resolved[0].winning_token == "222", "resolution");
  e.invalidate_poly(0);
  require(doc.parse(R"({"event_type":"price_change","price_changes":[{"asset_id":"111","price":"0.48","size":"3","side":"BUY"}]})"), "pc");
  touched.assign(2, 0);
  feeds::apply_poly(doc.root(), e, 50, touched, nullptr);
  require(!touched[0] && !e.poly_book(0, true).quote().valid, "deltas onto no snapshot are not a book");
}

PKM_TEST(poly_and_user_messages) {
  const auto sub = feeds::poly_subscribe({"1", "2"}, true);
  require_eq(sub, std::string(R"({"assets_ids":["1","2"],"type":"market","custom_feature_enabled":true})"), "initial");
  require(feeds::poly_subscribe({"3"}, false).find(R"("operation":"subscribe")") != std::string::npos, "later");
  require(feeds::poly_unsubscribe({"3"}).find("unsubscribe") != std::string::npos, "unsubscribe");
  const auto user = feeds::user_subscribe("k", "s", "p", {"0xc"});
  require_eq(user, std::string(R"({"auth":{"apiKey":"k","secret":"s","passphrase":"p"},"markets":["0xc"],"type":"user"})"), "user");

  json::Document doc;
  std::vector<feeds::OrderUpdate> ups;
  require(doc.parse(R"({"event_type":"order","id":"0xo1","original_size":"5","size_matched":"2","type":"UPDATE"})"), "o");
  feeds::parse_user(doc.root(), ups);
  require(ups.size() == 1 && ups[0].id == "0xo1" && ups[0].has_matched && ups[0].matched == 2 && !ups[0].done, "partial");
  require(doc.parse(R"([{"event_type":"order","id":"0xo1","original_size":"5","size_matched":"5","type":"UPDATE"},{"event_type":"order","id":"0xo2","original_size":"5","size_matched":"0","type":"CANCELLATION"}])"), "o2");
  ups.clear();
  feeds::parse_user(doc.root(), ups);
  require(ups.size() == 2 && ups[0].done && ups[1].done && ups[1].matched == 0, "filled and cancelled");
  require(doc.parse(R"({"event_type":"trade","taker_order_id":"0xt","maker_orders":[{"order_id":"0xo3","matched_amount":"5"}],"status":"MATCHED"})"), "t");
  ups.clear();
  feeds::parse_user(doc.root(), ups);
  require(ups.size() == 2 && ups[0].id == "0xo3" && !ups[0].has_matched, "trade is a hint");
}

PKM_TEST(iso_timestamps) {
  std::int64_t t = 0;
  require(feeds::iso_to_unix("2026-10-26T12:00:00Z", t) && t == 1'793'016'000, "Z");
  require(feeds::iso_to_unix("2026-10-26 12:00:00.123+00:00", t) && t == 1'793'016'000, "space, fraction, offset");
  require(feeds::iso_to_unix("2026-10-26T08:00:00-04:00", t) && t == 1'793'016'000, "negative offset");
  require(!feeds::iso_to_unix("2026-10-26", t) && !feeds::iso_to_unix("garbage-and-more-text", t), "refused");
}
