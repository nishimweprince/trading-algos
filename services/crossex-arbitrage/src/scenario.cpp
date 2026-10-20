#include "cxa/scenario.hpp"

#include <algorithm>

namespace cxa {

Instrument test_instrument() {
  Instrument instrument;
  instrument.name = "TEST";
  instrument.symbol = {"TESTUSDT", "TESTUSDT"};
  for (std::size_t v = 0; v < kVenues; ++v) {
    instrument.tick[v] = Decimal::parse("0.01");
    instrument.step[v] = Decimal::parse("0.001");
    instrument.min_qty[v] = Decimal::parse("0.001");
    instrument.min_notional[v] = Decimal::parse("5");
  }
  std::string error;
  finalize_instrument(instrument, error);
  return instrument;
}

RuntimeConfig scenario_config() {
  RuntimeConfig config;
  config.basket = "TEST:TESTUSDT:TESTUSDT";
  config.strategy.basis_warmup_ms = 0;
  config.strategy.basis_halflife_ms = 3'600'000;
  config.strategy.cooldown_ms = 0;
  config.strategy.exec_mode = ExecMode::taker_taker;
  config.strategy.trade_notional_raw = 1'000 * kScale;
  config.strategy.min_net_ppm = 1'000;
  config.shadow.latency_mode = LatencyMode::fixed;
  return config;
}

Scenario::Scenario(RuntimeConfig config)
    : engine_(std::make_unique<Engine>(std::vector<Instrument>{test_instrument()}, config.strategy, config.risk,
                                       config.shadow)),
      event_(std::make_unique<DepthEvent>()) {
  for (std::size_t v = 0; v < kVenues; ++v)
    engine_->set_fees(static_cast<Venue>(v), 0, config.shadow.fallback_taker_ppm[v],
                      config.shadow.fallback_maker_ppm[v]);
}

void Scenario::collect() {
  for (const auto& output : engine_->outputs()) outputs_.push_back(output);
  engine_->clear_outputs();
}

void Scenario::quote(Venue venue, std::string_view bid, std::string_view ask, std::string_view size) {
  const auto v = index(venue);
  const Level new_bid{Decimal::parse(bid), Decimal::parse(size)};
  const Level new_ask{Decimal::parse(ask), Decimal::parse(size)};
  auto& e = *event_;
  e = DepthEvent{};
  e.venue = venue;
  e.symbol = 0;
  e.receive_ns = now_ns_;
  e.event_ms = now_ns_ / 1'000'000;
  if (venue == Venue::bybit) {
    e.snapshot = true;
    e.first_id = e.final_id = ++bybit_id_;
    e.bids[0] = new_bid;
    e.asks[0] = new_ask;
    e.bid_count = e.ask_count = 1;
    engine_->on_depth(e);
  } else if (!binance_started_) {
    BookSnapshot snapshot;
    snapshot.venue = Venue::binance;
    snapshot.receive_ns = now_ns_;
    snapshot.last_update_id = binance_id_;
    snapshot.bids = {new_bid};
    snapshot.asks = {new_ask};
    engine_->on_snapshot(snapshot);
    binance_started_ = true;
    e.first_id = e.final_id = ++binance_id_;
    e.previous_final_id = binance_id_ - 1;
    e.bids[0] = new_bid;
    e.asks[0] = new_ask;
    e.bid_count = e.ask_count = 1;
    engine_->on_depth(e);
  } else {
    e.previous_final_id = binance_id_;
    e.first_id = e.final_id = ++binance_id_;
    std::uint16_t b = 0, a = 0;
    if (bid_[v].price != new_bid.price) e.bids[b++] = {bid_[v].price, {}};
    e.bids[b++] = new_bid;
    if (ask_[v].price != new_ask.price) e.asks[a++] = {ask_[v].price, {}};
    e.asks[a++] = new_ask;
    e.bid_count = b;
    e.ask_count = a;
    engine_->on_depth(e);
  }
  bid_[v] = new_bid;
  ask_[v] = new_ask;
  collect();
}

void Scenario::funding(Venue venue, std::string_view rate, std::uint64_t next_ms) {
  FundingEvent event;
  event.venue = venue;
  event.symbol = 0;
  event.receive_ns = now_ns_;
  event.event_ms = now_ns_ / 1'000'000;
  event.next_funding_ms = next_ms;
  event.funding_rate = Decimal::parse(rate);
  event.has_rate = true;
  event.mark = Price{(bid_[index(venue)].price.raw + ask_[index(venue)].price.raw) / 2};
  engine_->on_funding(event);
  collect();
}

void Scenario::advance_ms(std::uint64_t ms) {
  for (std::uint64_t i = 0; i < ms; ++i) {
    now_ns_ += 1'000'000;
    for (std::size_t v = 0; v < kVenues; ++v) {
      if (bid_[v].price.raw == 0) continue;
      FundingEvent heartbeat;
      heartbeat.venue = static_cast<Venue>(v);
      heartbeat.receive_ns = now_ns_;
      heartbeat.event_ms = now_ns_ / 1'000'000;
      engine_->on_funding(heartbeat);
    }
    collect();
  }
}

void Scenario::binance_gap() {
  auto& e = *event_;
  e = DepthEvent{};
  e.venue = Venue::binance;
  e.receive_ns = now_ns_;
  e.previous_final_id = binance_id_ + 5;
  e.first_id = e.final_id = binance_id_ + 6;
  e.bids[0] = bid_[0];
  e.asks[0] = ask_[0];
  e.bid_count = e.ask_count = 1;
  engine_->on_depth(e);
  collect();
}

std::size_t Scenario::count(OutputKind kind) const noexcept {
  return static_cast<std::size_t>(
      std::count_if(outputs_.begin(), outputs_.end(), [kind](const Output& o) { return o.kind == kind; }));
}

const Output* Scenario::last(OutputKind kind) const noexcept {
  for (auto it = outputs_.rbegin(); it != outputs_.rend(); ++it)
    if (it->kind == kind) return &*it;
  return nullptr;
}

void Scenario::clear() noexcept { outputs_.clear(); }

}  // namespace cxa
