#include "bpmm/journal.hpp"

#include "bpmm/engine.hpp"

#include <charconv>
#include <sstream>

namespace bpmm {

Journal::Journal(const std::string& path) : stream_(path, std::ios::app) {}

bool Journal::snapshot(const Snapshot& snapshot, std::uint64_t receive_ns) {
  stream_ << "S " << receive_ns << ' ' << snapshot.last_update_id << ' ' << snapshot.bid_count;
  for (std::size_t i = 0; i < snapshot.bid_count; ++i)
    stream_ << ' ' << snapshot.bids[i].price.raw << ' ' << snapshot.bids[i].quantity.raw;
  stream_ << ' ' << snapshot.ask_count;
  for (std::size_t i = 0; i < snapshot.ask_count; ++i)
    stream_ << ' ' << snapshot.asks[i].price.raw << ' ' << snapshot.asks[i].quantity.raw;
  stream_ << '\n';
  return stream_.good();
}

bool Journal::raw(std::uint64_t receive_ns, std::string_view frame) {
  stream_ << "R " << receive_ns << ' ' << frame << '\n';
  return stream_.good();
}

bool Journal::depth(const DepthEvent& event) {
  stream_ << "D " << event.receive_ns << ' ' << event.event_ms << ' ' << event.transaction_ms << ' '
          << event.first_id << ' ' << event.final_id << ' ' << event.previous_final_id << ' '
          << event.bid_count;
  for (std::size_t i = 0; i < event.bid_count; ++i)
    stream_ << ' ' << event.bids[i].price.raw << ' ' << event.bids[i].quantity.raw;
  stream_ << ' ' << event.ask_count;
  for (std::size_t i = 0; i < event.ask_count; ++i)
    stream_ << ' ' << event.asks[i].price.raw << ' ' << event.asks[i].quantity.raw;
  stream_ << '\n';
  return stream_.good();
}

bool Journal::trade(const TradeEvent& event) {
  stream_ << "T " << event.receive_ns << ' ' << event.trade_ms << ' ' << event.trade_id << ' '
          << event.price.raw << ' ' << event.quantity.raw << ' ' << event.buyer_is_maker << '\n';
  return stream_.good();
}

bool Journal::candle(const CandleEvent& event) {
  stream_ << "K " << event.open_ms << ' ' << event.close_ms << ' ' << event.open.raw << ' '
          << event.high.raw << ' ' << event.low.raw << ' ' << event.close.raw << ' ' << event.closed
          << '\n';
  return stream_.good();
}

bool Journal::mark_price(const MarkPriceEvent& event) {
  stream_ << "M " << event.receive_ns << ' ' << event.event_ms << ' ' << event.next_funding_ms << ' '
          << event.mark.raw << ' ' << event.funding_rate_ppm << '\n';
  return stream_.good();
}

bool Journal::intent(const QuoteIntent& intent) {
  stream_ << "I " << intent.decision_ns << ' ' << intent.sequence << ' '
          << static_cast<int>(intent.kind) << ' ' << static_cast<int>(intent.side) << ' '
          << static_cast<int>(intent.level) << ' ' << intent.price.raw << ' ' << intent.quantity.raw
          << ' ' << intent.reduce_only << '\n';
  return stream_.good();
}

bool Journal::fill(const ShadowFill& fill) {
  stream_ << "F " << fill.fill_ns << ' ' << fill.order_id << ' ' << static_cast<int>(fill.side) << ' '
          << fill.price.raw << ' ' << fill.quantity.raw << ' ' << fill.fee_raw << '\n';
  return stream_.good();
}

bool replay_normalized(const std::string& path, Engine& engine, std::uint64_t& final_hash,
                       std::string& error) {
  std::ifstream input(path);
  if (!input) { error = "cannot open journal"; return false; }
  std::string line;
  std::uint64_t previous_receive = 0;
  while (std::getline(input, line)) {
    std::istringstream row(line);
    char kind = 0;
    row >> kind;
    if (kind == 'R' || kind == 'I' || kind == 'F') continue;
    if (kind == 'S') {
      Snapshot snapshot;
      std::uint64_t receive_ns = 0;
      unsigned bids = 0;
      unsigned asks = 0;
      row >> receive_ns >> snapshot.last_update_id >> bids;
      if (bids > snapshot.bids.size()) { error = "too many snapshot bids"; return false; }
      snapshot.bid_count = static_cast<std::uint16_t>(bids);
      for (unsigned i = 0; i < bids; ++i)
        row >> snapshot.bids[i].price.raw >> snapshot.bids[i].quantity.raw;
      row >> asks;
      if (asks > snapshot.asks.size()) { error = "too many snapshot asks"; return false; }
      snapshot.ask_count = static_cast<std::uint16_t>(asks);
      for (unsigned i = 0; i < asks; ++i)
        row >> snapshot.asks[i].price.raw >> snapshot.asks[i].quantity.raw;
      if (!row || !engine.on_snapshot(snapshot)) { error = "invalid snapshot line"; return false; }
    } else if (kind == 'D') {
      DepthEvent event;
      unsigned bids = 0;
      unsigned asks = 0;
      row >> event.receive_ns >> event.event_ms >> event.transaction_ms >> event.first_id >>
          event.final_id >> event.previous_final_id >> bids;
      if (bids > event.bids.size()) { error = "too many bids"; return false; }
      event.bid_count = static_cast<std::uint16_t>(bids);
      for (unsigned i = 0; i < bids; ++i) row >> event.bids[i].price.raw >> event.bids[i].quantity.raw;
      row >> asks;
      if (asks > event.asks.size()) { error = "too many asks"; return false; }
      event.ask_count = static_cast<std::uint16_t>(asks);
      for (unsigned i = 0; i < asks; ++i) row >> event.asks[i].price.raw >> event.asks[i].quantity.raw;
      if (!row || (previous_receive != 0 && event.receive_ns < previous_receive)) {
        error = "invalid or backwards depth line"; return false;
      }
      previous_receive = event.receive_ns;
      engine.on_depth(event, event.receive_ns);
    } else if (kind == 'T') {
      TradeEvent event;
      row >> event.receive_ns >> event.trade_ms >> event.trade_id >> event.price.raw >>
          event.quantity.raw >> event.buyer_is_maker;
      if (!row) { error = "invalid trade line"; return false; }
      engine.on_trade(event);
    } else if (kind == 'K') {
      CandleEvent event;
      row >> event.open_ms >> event.close_ms >> event.open.raw >> event.high.raw >> event.low.raw >>
          event.close.raw >> event.closed;
      if (!row) { error = "invalid candle line"; return false; }
      engine.on_candle(event);
    } else if (kind == 'M') {
      MarkPriceEvent event;
      row >> event.receive_ns >> event.event_ms >> event.next_funding_ms >> event.mark.raw >>
          event.funding_rate_ppm;
      if (!row) { error = "invalid mark-price line"; return false; }
      engine.set_next_funding_ms(event.next_funding_ms);
    }
  }
  final_hash = engine.state_hash();
  return true;
}

bool gapcheck(const std::string& path, std::string& error) {
  std::ifstream input(path);
  if (!input) { error = "cannot open journal"; return false; }
  std::string line;
  std::uint64_t previous_receive = 0;
  std::uint64_t previous_final = 0;
  while (std::getline(input, line)) {
    if (line.starts_with("S ")) {
      std::istringstream row(line);
      char kind = 0;
      std::uint64_t receive = 0;
      row >> kind >> receive >> previous_final;
      if (!row || (previous_receive && receive < previous_receive)) {
        error = "backwards or invalid snapshot";
        return false;
      }
      previous_receive = receive;
      continue;
    }
    if (!line.starts_with("D ")) continue;
    std::istringstream row(line);
    char kind = 0;
    DepthEvent event;
    row >> kind >> event.receive_ns >> event.event_ms >> event.transaction_ms >> event.first_id >>
        event.final_id >> event.previous_final_id;
    if (!row || (previous_receive && event.receive_ns < previous_receive)) {
      error = "backwards or invalid depth event"; return false;
    }
    if (previous_final && event.previous_final_id != previous_final) {
      error = "depth sequence gap"; return false;
    }
    previous_receive = event.receive_ns;
    previous_final = event.final_id;
  }
  return true;
}

}  // namespace bpmm
