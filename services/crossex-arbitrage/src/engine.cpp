#include "cxa/engine.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace cxa {
namespace {

constexpr std::int64_t kNoGap = std::numeric_limits<std::int64_t>::min();
constexpr std::uint64_t kMsNs = 1'000'000;

Price widen(Price price, Side side, std::int64_t ppm, Price tick) noexcept {
  if (price.raw <= 0) return {};
  if (side == Side::buy) return round_down(Price{price.raw + apply_ppm(price.raw, ppm)}, tick);
  return round_up(Price{price.raw - apply_ppm(price.raw, ppm)}, tick);
}

std::int64_t rate_ppm(Decimal rate) noexcept { return rate.raw / (kScale / kPpm); }

SweepResult maker_join(Price price, Quantity quantity) noexcept {
  SweepResult result;
  if (price.raw <= 0 || quantity.raw <= 0) return result;
  result.filled = quantity;
  result.worst = price;
  result.vwap = price;
  result.notional_raw = mul_scaled(price.raw, quantity.raw);
  return result;
}

}  // namespace

std::string_view exit_reason_name(ExitReason reason) noexcept {
  switch (reason) {
    case ExitReason::none: return "none";
    case ExitReason::converged: return "converged";
    case ExitReason::max_hold: return "max_hold";
    case ExitReason::funding: return "funding";
    case ExitReason::stop: return "stop";
    case ExitReason::kill: return "kill";
    case ExitReason::shutdown: return "shutdown";
    case ExitReason::legged_out: return "legged_out";
    case ExitReason::missed: return "missed";
  }
  return "unknown";
}

std::string_view risk_code_name(RiskCode code) noexcept {
  switch (code) {
    case RiskCode::ok: return "ok";
    case RiskCode::halted: return "halted";
    case RiskCode::symbol_disabled: return "symbol_disabled";
    case RiskCode::feed_stale: return "feed_stale";
    case RiskCode::book_stale: return "book_stale";
    case RiskCode::fees_unknown: return "fees_unknown";
    case RiskCode::funding_unknown: return "funding_unknown";
    case RiskCode::basis_cold: return "basis_cold";
    case RiskCode::max_pairs: return "max_pairs";
    case RiskCode::notional_limit: return "notional_limit";
    case RiskCode::cooldown: return "cooldown";
    case RiskCode::below_minimum: return "below_minimum";
    case RiskCode::count: break;
  }
  return "unknown";
}

Engine::Engine(std::vector<Instrument> instruments, StrategyConfig strategy, RiskLimits risk,
               ShadowConfig shadow)
    : instruments_(std::move(instruments)), strategy_(strategy), limits_(risk), shadow_(shadow) {
  for (std::size_t v = 0; v < kVenues; ++v) {
    latency_[v] = LatencyModel(shadow_.latency_mode, shadow_.fixed_one_way_ns[v],
                               shadow_.engine_penalty_ns + shadow_.venue_penalty_ns[v], shadow_.seed + v);
  }
  for (auto& row : maker_ppm_) row.fill(-1);
}

Output* Engine::push_output(OutputKind kind) noexcept {
  if (output_count_ == outputs_.size()) {
    metrics_.output_overflow = true;
    return nullptr;
  }
  auto& output = outputs_[output_count_++];
  output.kind = kind;
  return &output;
}

void Engine::advance(std::uint64_t now_ns) noexcept {
  now_ns_ = std::max(now_ns_, now_ns);
  process_arrivals();
  process_resting_makers();
}

void Engine::set_fees(Venue venue, std::uint16_t symbol, std::int64_t taker_ppm,
                      std::int64_t maker_ppm) noexcept {
  if (symbol >= instruments_.size()) return;
  taker_ppm_[index(venue)][symbol] = taker_ppm;
  maker_ppm_[index(venue)][symbol] = maker_ppm;
}

void Engine::set_taker_fee(Venue venue, std::uint16_t symbol, std::int64_t taker_ppm) noexcept {
  if (symbol >= instruments_.size()) return;
  taker_ppm_[index(venue)][symbol] = taker_ppm;
  if (maker_ppm_[index(venue)][symbol] < 0) maker_ppm_[index(venue)][symbol] = taker_ppm;
}

void Engine::add_latency_sample(Venue venue, std::uint64_t rtt_ns) noexcept {
  latency_[index(venue)].add_rtt_sample(rtt_ns);
}

// ---------------------------------------------------------------- market data

void Engine::on_depth(const DepthEvent& event) noexcept {
  advance(event.receive_ns);
  if (event.symbol >= instruments_.size()) return;
  const auto v = index(event.venue);
  last_receive_ns_[v] = std::max(last_receive_ns_[v], event.receive_ns);
  ++metrics_.depth_events[v];
  auto& book = books_[v][event.symbol];
  const auto before = book.state();
  const auto ok = event.venue == Venue::binance ? book.apply_binance(event) : book.apply_bybit(event);
  if (!ok && book.state() == BookState::gap && before != BookState::gap) on_gap(event.venue, event.symbol);
  process_resting_makers();
  if (book.verified()) evaluate(event.symbol);
}

bool Engine::on_snapshot(const BookSnapshot& snapshot) noexcept {
  advance(snapshot.receive_ns);
  if (snapshot.symbol >= instruments_.size()) return false;
  auto& book = books_[index(snapshot.venue)][snapshot.symbol];
  const auto verified = snapshot.venue != Venue::binance;
  if (!book.apply_snapshot(snapshot.last_update_id, snapshot.bids, snapshot.asks, snapshot.receive_ns, verified)) {
    on_gap(snapshot.venue, snapshot.symbol);
    return false;
  }
  process_resting_makers();
  return true;
}

void Engine::on_feed_gap(Venue venue, std::uint16_t symbol, std::uint64_t receive_ns) noexcept {
  advance(receive_ns);
  if (symbol >= instruments_.size()) return;
  auto& book = books_[index(venue)][symbol];
  if (book.state() == BookState::gap) return;
  book.mark_gap();
  on_gap(venue, symbol);
}

void Engine::on_gap(Venue venue, std::size_t symbol) noexcept {
  ++metrics_.gaps[index(venue)];
  close_episodes(symbol);
  if (auto* output = push_output(OutputKind::resync)) {
    output->venue = venue;
    output->symbol = static_cast<std::uint16_t>(symbol);
  }
}

void Engine::on_funding(const FundingEvent& event) noexcept {
  advance(event.receive_ns);
  if (event.symbol >= instruments_.size()) return;
  const auto v = index(event.venue);
  last_receive_ns_[v] = std::max(last_receive_ns_[v], event.receive_ns);
  ++metrics_.funding_events;
  auto& state = funding_[v][event.symbol];
  if (event.next_funding_ms > 0) {
    if (state.next_ms > 0 && event.next_funding_ms > state.next_ms && event.event_ms >= state.next_ms) {
      auto& pair = pairs_[event.symbol];
      const auto qty = pair.qty_raw[v];
      if (qty != 0 && state.known) {
        const auto mark = state.mark.raw > 0 ? state.mark : books_[v][event.symbol].mid();
        const auto payment = mul_scaled(mul_scaled(qty, mark.raw), state.rate.raw);
        pair.funding_raw -= payment;
        ++metrics_.funding_settlements;
      }
    }
    state.next_ms = event.next_funding_ms;
  }
  if (event.mark.raw > 0) state.mark = event.mark;
  if (event.has_rate) {
    state.rate = event.funding_rate;
    state.known = true;
  }
}

// ---------------------------------------------------------------- opportunity

std::int64_t Engine::mid_basis_ppm(std::size_t symbol) const noexcept {
  const auto b = books_[index(Venue::binance)][symbol].mid().raw;
  const auto y = books_[1][symbol].mid().raw;
  return ratio_ppm(y - b, (y + b) / 2);
}

std::int64_t Engine::basis_mean_ppm(std::size_t symbol) const noexcept {
  return static_cast<std::int64_t>(std::llround(basis_[symbol].mean_ppm));
}

bool Engine::basis_warm(std::size_t symbol) const noexcept {
  const auto& basis = basis_[symbol];
  return basis.initialised && basis.last_ms - basis.first_ms >= strategy_.basis_warmup_ms;
}

void Engine::update_basis(std::size_t symbol) noexcept {
  auto& basis = basis_[symbol];
  const auto now_ms = now_ns_ / kMsNs;
  const auto b = static_cast<double>(mid_basis_ppm(symbol));
  if (!basis.initialised) {
    basis = {b, now_ms, now_ms, true};
    return;
  }
  const auto dt = now_ms - basis.last_ms;
  if (dt < strategy_.basis_sample_ms) return;
  const auto alpha = 1.0 - std::exp2(-static_cast<double>(dt) / static_cast<double>(strategy_.basis_halflife_ms));
  basis.mean_ppm += alpha * (b - basis.mean_ppm);
  basis.last_ms = now_ms;
}

Quantity Engine::quantity_for(std::size_t symbol, std::int64_t notional_raw) const noexcept {
  const auto& instrument = instruments_[symbol];
  const auto b = books_[index(Venue::binance)][symbol].mid().raw;
  const auto y = books_[1][symbol].mid().raw;
  const auto mid = (b + y) / 2;
  if (mid <= 0) return {};
  const auto qty = round_down(Quantity{div_scaled(notional_raw, mid)}, instrument.common_step);
  if (qty.raw < instrument.common_min_qty.raw) return {};
  const auto value = mul_scaled(qty.raw, mid);
  for (std::size_t v = 0; v < kVenues; ++v)
    if (value < instrument.min_notional[v].raw) return {};
  return qty;
}

std::int64_t Engine::funding_cost_ppm(std::size_t symbol, Direction dir, std::uint64_t horizon_ms) const noexcept {
  const auto now_ms = now_ns_ / kMsNs;
  const auto settles = [&](Venue venue) {
    const auto& state = funding_[index(venue)][symbol];
    return state.known && state.next_ms >= now_ms && state.next_ms - now_ms <= horizon_ms;
  };
  std::int64_t cost = 0;
  const auto lv = long_of(dir);
  const auto sv = short_of(dir);
  if (settles(lv)) cost += rate_ppm(funding_[index(lv)][symbol].rate);
  if (settles(sv)) cost -= rate_ppm(funding_[index(sv)][symbol].rate);
  return cost;
}

std::int64_t Engine::cycle_fees_ppm(std::size_t symbol, Venue maker_venue) const noexcept {
  const auto hedge = counterpart(maker_venue);
  if (strategy_.exec_mode == ExecMode::maker_taker)
    return 2 * (maker_ppm_[index(maker_venue)][symbol] + taker_ppm_[index(hedge)][symbol]);
  return 2 * (taker_ppm_[0][symbol] + taker_ppm_[1][symbol]);
}

Edge Engine::edge_taker_taker(std::size_t symbol, Direction dir, Quantity quantity) const noexcept {
  Edge edge;
  if (quantity.raw <= 0) return edge;
  const auto& long_book = books_[index(long_of(dir))][symbol];
  const auto& short_book = books_[index(short_of(dir))][symbol];
  if (!long_book.verified() || !short_book.verified()) return edge;
  edge.quantity = quantity;
  edge.buy = long_book.sweep(Side::buy, quantity);
  edge.sell = short_book.sweep(Side::sell, quantity);
  edge.exit_sell = long_book.sweep(Side::sell, quantity);
  edge.exit_buy = short_book.sweep(Side::buy, quantity);
  if (!edge.buy.complete(quantity) || !edge.sell.complete(quantity) || !edge.exit_sell.complete(quantity) ||
      !edge.exit_buy.complete(quantity))
    return edge;
  const auto mid = (long_book.mid().raw + short_book.mid().raw) / 2;
  edge.gap_ppm = ratio_ppm(edge.sell.vwap.raw - edge.buy.vwap.raw, mid);
  edge.exit_gap_ppm = ratio_ppm(edge.exit_buy.vwap.raw - edge.exit_sell.vwap.raw, mid);
  edge.basis_ppm = sign(dir) * mid_basis_ppm(symbol);
  edge.mean_ppm = sign(dir) * basis_mean_ppm(symbol);
  edge.capture_ppm = edge.gap_ppm - edge.mean_ppm - (edge.exit_gap_ppm - edge.basis_ppm);
  edge.fees_ppm = cycle_fees_ppm(symbol, Venue::binance);
  edge.funding_ppm = funding_cost_ppm(symbol, dir, strategy_.expected_hold_ms);
  edge.net_ppm = edge.capture_ppm - edge.fees_ppm - edge.funding_ppm - strategy_.safety_ppm;
  edge.maker_venue = Venue::binance;
  edge.valid = true;
  return edge;
}

Edge Engine::edge_maker_taker(std::size_t symbol, Direction dir, Quantity quantity,
                              Venue maker_venue) const noexcept {
  Edge edge;
  if (quantity.raw <= 0) return edge;
  const auto lv = long_of(dir);
  const auto sv = short_of(dir);
  const auto hedge_venue = counterpart(maker_venue);
  const auto& maker_book = books_[index(maker_venue)][symbol];
  const auto& hedge_book = books_[index(hedge_venue)][symbol];
  if (!maker_book.verified() || !hedge_book.verified()) return edge;
  if (maker_ppm_[index(maker_venue)][symbol] < 0 || taker_ppm_[index(hedge_venue)][symbol] <= 0) return edge;

  const bool maker_is_long = maker_venue == lv;
  const auto join = maker_is_long ? maker_book.best_bid().price : maker_book.best_ask().price;
  if (join.raw <= 0) return edge;

  edge.quantity = quantity;
  if (maker_is_long) {
    edge.buy = maker_join(join, quantity);
    edge.sell = hedge_book.sweep(Side::sell, quantity);
    edge.exit_sell = maker_join(maker_book.best_ask().price, quantity);
    edge.exit_buy = hedge_book.sweep(Side::buy, quantity);
  } else {
    edge.sell = maker_join(join, quantity);
    edge.buy = hedge_book.sweep(Side::buy, quantity);
    edge.exit_buy = maker_join(maker_book.best_bid().price, quantity);
    edge.exit_sell = hedge_book.sweep(Side::sell, quantity);
  }
  if (!edge.buy.complete(quantity) || !edge.sell.complete(quantity) || !edge.exit_sell.complete(quantity) ||
      !edge.exit_buy.complete(quantity) || edge.exit_sell.vwap.raw <= 0 || edge.exit_buy.vwap.raw <= 0)
    return edge;

  const auto mid = (books_[index(lv)][symbol].mid().raw + books_[index(sv)][symbol].mid().raw) / 2;
  edge.gap_ppm = ratio_ppm(edge.sell.vwap.raw - edge.buy.vwap.raw, mid);
  edge.exit_gap_ppm = ratio_ppm(edge.exit_buy.vwap.raw - edge.exit_sell.vwap.raw, mid);
  edge.basis_ppm = sign(dir) * mid_basis_ppm(symbol);
  edge.mean_ppm = sign(dir) * basis_mean_ppm(symbol);
  edge.capture_ppm = edge.gap_ppm - edge.mean_ppm - (edge.exit_gap_ppm - edge.basis_ppm);
  edge.fees_ppm = cycle_fees_ppm(symbol, maker_venue);
  edge.funding_ppm = funding_cost_ppm(symbol, dir, strategy_.expected_hold_ms);
  edge.net_ppm = edge.capture_ppm - edge.fees_ppm - edge.funding_ppm - strategy_.safety_ppm;
  edge.maker_venue = maker_venue;
  edge.valid = true;
  return edge;
}

Edge Engine::edge(std::size_t symbol, Direction dir, Quantity quantity) const noexcept {
  if (strategy_.exec_mode != ExecMode::maker_taker) return edge_taker_taker(symbol, dir, quantity);
  auto best = edge_maker_taker(symbol, dir, quantity, Venue::binance);
  const auto other = edge_maker_taker(symbol, dir, quantity, shadow_.peer);
  if (!best.valid || (other.valid && other.net_ppm > best.net_ppm)) return other;
  return best;
}

Edge Engine::edge(std::size_t symbol, Direction dir, Quantity quantity, Venue maker_venue) const noexcept {
  if (strategy_.exec_mode != ExecMode::maker_taker) return edge_taker_taker(symbol, dir, quantity);
  return edge_maker_taker(symbol, dir, quantity, maker_venue);
}

void Engine::update_census(std::size_t symbol) noexcept {
  constexpr std::array<Direction, 2> dirs{Direction::long_binance, Direction::long_peer};
  for (std::size_t s = 0; s < kCensusSizes; ++s) {
    const auto quantity = quantity_for(symbol, strategy_.census_sizes_raw[s]);
    if (quantity.raw <= 0) continue;
    for (std::size_t d = 0; d < dirs.size(); ++d) {
      const auto& long_book = books_[index(long_of(dirs[d]))][symbol];
      const auto& short_book = books_[index(short_of(dirs[d]))][symbol];
      const auto buy = long_book.sweep(Side::buy, quantity);
      const auto sell = short_book.sweep(Side::sell, quantity);
      auto gap = kNoGap;
      if (buy.complete(quantity) && sell.complete(quantity))
        gap = ratio_ppm(sell.vwap.raw - buy.vwap.raw, (long_book.mid().raw + short_book.mid().raw) / 2);
      for (std::size_t t = 0; t < kCensusThresholds; ++t) {
        auto& episode = episodes_[symbol][s][d][t];
        const auto threshold = strategy_.census_thresholds_ppm[t];
        if (gap != kNoGap && gap >= threshold) {
          if (!episode.active) episode = {true, now_ns_, gap};
          else episode.peak_ppm = std::max(episode.peak_ppm, gap);
        } else if (episode.active) {
          if (auto* output = push_output(OutputKind::census)) {
            output->census = {static_cast<std::uint16_t>(symbol), static_cast<std::uint8_t>(s), dirs[d],
                              threshold, episode.start_ns, now_ns_ - episode.start_ns, episode.peak_ppm};
          }
          ++metrics_.census_episodes;
          episode.active = false;
        }
      }
    }
  }
}

void Engine::close_episodes(std::size_t symbol) noexcept {
  constexpr std::array<Direction, 2> dirs{Direction::long_binance, Direction::long_peer};
  for (std::size_t s = 0; s < kCensusSizes; ++s)
    for (std::size_t d = 0; d < dirs.size(); ++d)
      for (std::size_t t = 0; t < kCensusThresholds; ++t) {
        auto& episode = episodes_[symbol][s][d][t];
        if (!episode.active) continue;
        if (auto* output = push_output(OutputKind::census)) {
          output->census = {static_cast<std::uint16_t>(symbol), static_cast<std::uint8_t>(s), dirs[d],
                            strategy_.census_thresholds_ppm[t], episode.start_ns, now_ns_ - episode.start_ns,
                            episode.peak_ppm};
        }
        ++metrics_.census_episodes;
        episode.active = false;
      }
}

// ---------------------------------------------------------------- decisions

RiskCode Engine::entry_risk(std::size_t symbol, std::int64_t notional_raw) const noexcept {
  if (halted()) return RiskCode::halted;
  if (disabled_[symbol]) return RiskCode::symbol_disabled;
  for (std::size_t v = 0; v < kVenues; ++v) {
    if (now_ns_ - last_receive_ns_[v] > limits_.max_feed_age_ns) return RiskCode::feed_stale;
    if (now_ns_ - books_[v][symbol].last_receive_ns() > limits_.max_book_age_ns) return RiskCode::book_stale;
    if (taker_ppm_[v][symbol] <= 0) return RiskCode::fees_unknown;
    if (strategy_.exec_mode == ExecMode::maker_taker && maker_ppm_[v][symbol] < 0) return RiskCode::fees_unknown;
    if (!funding_[v][symbol].known || funding_[v][symbol].next_ms == 0) return RiskCode::funding_unknown;
  }
  if (!basis_warm(symbol)) return RiskCode::basis_cold;
  if (metrics_.open_pairs >= limits_.max_open_pairs) return RiskCode::max_pairs;
  if (notional_raw > limits_.max_pair_notional_raw) return RiskCode::notional_limit;
  if (now_ns_ < pairs_[symbol].cooldown_until_ns) return RiskCode::cooldown;
  return RiskCode::ok;
}

void Engine::evaluate(std::size_t symbol) noexcept {
  if (!books_[0][symbol].verified() || !books_[1][symbol].verified()) return;
  if (!mid_checked_[symbol]) {
    mid_checked_[symbol] = true;
    if (abs64(mid_basis_ppm(symbol)) > strategy_.max_mid_divergence_ppm) {
      disabled_[symbol] = true;
      if (auto* output = push_output(OutputKind::disabled)) output->symbol = static_cast<std::uint16_t>(symbol);
    }
  }
  if (disabled_[symbol]) return;
  update_basis(symbol);
  update_census(symbol);
  const auto phase = pairs_[symbol].phase;
  if (phase == PairPhase::flat) try_open(symbol);
  else if (phase == PairPhase::opening && strategy_.exec_mode == ExecMode::maker_taker) {
    // Cancel residual maker if live hedge economics no longer clear the threshold.
    const auto& pair = pairs_[symbol];
    const auto qty = quantity_for(symbol, strategy_.trade_notional_raw);
    if (qty.raw > 0) {
      const auto live = edge(symbol, pair.dir, qty, pair.maker_venue);
      if (!live.valid || live.net_ppm < strategy_.min_net_ppm) cancel_resting_maker(symbol);
    }
  } else if (phase == PairPhase::open) check_exit(symbol);
}

void Engine::try_open(std::size_t symbol) noexcept {
  const auto quantity = quantity_for(symbol, strategy_.trade_notional_raw);
  if (quantity.raw <= 0) return;
  auto dir = Direction::long_binance;
  auto best = edge(symbol, dir, quantity);
  const auto other = edge(symbol, Direction::long_peer, quantity);
  if (!best.valid || (other.valid && other.net_ppm > best.net_ppm)) {
    best = other;
    dir = Direction::long_peer;
  }
  if (!best.valid || best.net_ppm < strategy_.min_net_ppm) return;
  const auto notional = mul_scaled(quantity.raw, best.buy.vwap.raw);
  const auto risk = entry_risk(symbol, notional);
  if (risk != RiskCode::ok) {
    ++metrics_.risk[static_cast<std::size_t>(risk)];
    return;
  }
  auto& pair = pairs_[symbol];
  const auto cycle = pair.cycle_id = next_cycle_id_++;
  pair.phase = PairPhase::opening;
  pair.dir = dir;
  pair.qty_raw = {};
  pair.avg_raw = {};
  pair.realized_raw = pair.fees_raw = pair.funding_raw = 0;
  pair.repairs = 0;
  pair.entry_ns = now_ns_;
  pair.entry_gap_ppm = best.gap_ppm;
  pair.entry_mean_ppm = best.mean_ppm;
  pair.expected_capture_ppm = best.capture_ppm;
  pair.expected_net_ppm = best.net_ppm;
  pair.entry_notional_raw = notional;
  pair.last_exit_gap_ppm = best.exit_gap_ppm;
  pair.reason = ExitReason::none;
  pair.opened = false;
  pair.maker_venue = best.maker_venue;
  ++metrics_.open_pairs;
  if (auto* output = push_output(OutputKind::decision)) {
    output->decision = {cycle,          static_cast<std::uint16_t>(symbol), dir,
                        now_ns_,        quantity,                           best.gap_ppm,
                        best.exit_gap_ppm, best.basis_ppm,                  best.mean_ppm,
                        best.capture_ppm,  best.fees_ppm,                   best.funding_ppm,
                        best.net_ppm};
  }

  const auto& instrument = instruments_[symbol];
  const auto lv = long_of(dir);
  const auto sv = short_of(dir);
  if (strategy_.exec_mode == ExecMode::maker_taker) {
    const auto maker = best.maker_venue;
    const auto hedge = counterpart(maker);
    const bool maker_is_long = maker == lv;
    const auto maker_side = maker_is_long ? Side::buy : Side::sell;
    const auto hedge_side = maker_is_long ? Side::sell : Side::buy;
    const auto join = maker_is_long ? best.buy.worst : best.sell.worst;
    submit_leg(symbol, maker, maker_side, LegPurpose::open, quantity, join, LegLiquidity::maker, hedge,
               hedge_side);
    return;
  }

  const auto slack = std::max<std::int64_t>(0, (best.net_ppm - strategy_.min_net_ppm) / 2);
  submit_leg(symbol, lv, Side::buy, LegPurpose::open, quantity,
             widen(best.buy.worst, Side::buy, slack, instrument.tick[index(lv)]));
  submit_leg(symbol, sv, Side::sell, LegPurpose::open, quantity,
             widen(best.sell.worst, Side::sell, slack, instrument.tick[index(sv)]));
}

void Engine::check_exit(std::size_t symbol) noexcept {
  auto& pair = pairs_[symbol];
  const auto quantity = Quantity{abs64(pair.qty_raw[index(long_of(pair.dir))])};
  const auto current = strategy_.exec_mode == ExecMode::maker_taker
                           ? edge(symbol, pair.dir, quantity, pair.maker_venue)
                           : edge(symbol, pair.dir, quantity);
  if (current.valid) pair.last_exit_gap_ppm = current.exit_gap_ppm;
  auto reason = ExitReason::none;
  if (halted()) reason = ExitReason::kill;
  else if (current.valid && current.basis_ppm - pair.entry_mean_ppm <= strategy_.exit_band_ppm)
    reason = ExitReason::converged;
  else if (current.valid && pair.entry_gap_ppm - current.exit_gap_ppm - current.fees_ppm <= -strategy_.pair_stop_ppm)
    reason = ExitReason::stop;
  else if (now_ns_ - pair.entry_ns >= strategy_.max_hold_ms * kMsNs)
    reason = ExitReason::max_hold;
  else if (funding_cost_ppm(symbol, pair.dir, strategy_.funding_guard_ms) > strategy_.funding_tolerance_ppm)
    reason = ExitReason::funding;
  if (reason != ExitReason::none) begin_close(symbol, reason);
}

void Engine::begin_close(std::size_t symbol, ExitReason reason) noexcept {
  auto& pair = pairs_[symbol];
  pair.phase = PairPhase::closing;
  pair.reason = reason;
  const auto& instrument = instruments_[symbol];

  if (strategy_.exec_mode == ExecMode::maker_taker) {
    const auto maker = pair.maker_venue;
    const auto hedge = counterpart(maker);
    const auto maker_qty = pair.qty_raw[index(maker)];
    const auto hedge_qty = pair.qty_raw[index(hedge)];
    if (maker_qty == 0 && hedge_qty == 0) {
      resolve_pair(symbol);
      return;
    }
    // Prefer resting the unwind on the entry maker venue; fall back to both-taker if flat there.
    if (maker_qty != 0) {
      const auto maker_side = maker_qty > 0 ? Side::sell : Side::buy;
      const auto hedge_side = hedge_qty > 0 ? Side::sell : Side::buy;
      const Quantity quantity{abs64(maker_qty)};
      const auto& book = books_[index(maker)][symbol];
      const auto join = maker_side == Side::sell ? book.best_ask().price : book.best_bid().price;
      submit_leg(symbol, maker, maker_side, LegPurpose::close, quantity, join, LegLiquidity::maker, hedge,
                 hedge_qty == 0 ? (maker_side == Side::sell ? Side::buy : Side::sell) : hedge_side);
      return;
    }
  }

  for (std::size_t v = 0; v < kVenues; ++v) {
    const auto qty = pair.qty_raw[v];
    if (qty == 0) continue;
    const auto side = qty > 0 ? Side::sell : Side::buy;
    const Quantity quantity{abs64(qty)};
    const auto& book = books_[v][symbol];
    const auto swept = book.sweep(side, quantity);
    const auto base = swept.complete(quantity) ? swept.worst
                                               : (side == Side::sell ? book.best_bid().price : book.best_ask().price);
    submit_leg(symbol, slot_venue(v, shadow_.peer), side, LegPurpose::close, quantity,
               widen(base, side, strategy_.close_slippage_ppm, instrument.tick[v]));
  }
  if (pair.pending_legs == 0) resolve_pair(symbol);
}

void Engine::submit_leg(std::size_t symbol, Venue venue, Side side, LegPurpose purpose, Quantity quantity,
                        Price limit, LegLiquidity liquidity, Venue hedge_venue, Side hedge_side) noexcept {
  auto slot = std::find_if(legs_.begin(), legs_.end(), [](const PendingLeg& leg) { return !leg.active; });
  if (slot == legs_.end()) {
    loss_halt_ = true;
    return;
  }
  auto& pair = pairs_[symbol];
  LegRecord record;
  record.leg_id = next_leg_id_++;
  record.cycle_id = pair.cycle_id;
  record.symbol = static_cast<std::uint16_t>(symbol);
  record.venue = venue;
  record.side = side;
  record.purpose = purpose;
  record.liquidity = liquidity;
  record.quantity = quantity;
  record.limit = limit;
  record.decision_ns = now_ns_;
  record.arrival_ns = now_ns_ + latency_[index(venue)].draw();
  record.hedge_venue = hedge_venue;
  record.hedge_side = hedge_side;
  *slot = {true, false, record, quantity, 0};
  ++pair.pending_legs;
  ++metrics_.legs;
  if (auto* output = push_output(OutputKind::leg_submit)) output->leg = record;
}

// ---------------------------------------------------------------- shadow fills

void Engine::process_arrivals() noexcept {
  std::array<bool, kMaxSymbols> resolve{};
  bool any = false;
  for (auto& slot : legs_) {
    if (!slot.active || slot.resting || slot.record.arrival_ns > now_ns_) continue;
    auto& record = slot.record;
    if (record.liquidity == LegLiquidity::maker) {
      slot.resting = true;
      slot.remaining = record.quantity;
      slot.expire_ns = record.arrival_ns + strategy_.maker_timeout_ms * kMsNs;
      continue;
    }
    const auto v = index(record.venue);
    const auto& instrument = instruments_[record.symbol];
    const auto& book = books_[v][record.symbol];
    Quantity filled{};
    SweepResult swept{};
    if (book.verified()) {
      const auto available = book.sweep(record.side, record.quantity, record.limit, shadow_.available_ppm);
      filled = round_down(available.filled, instrument.step[v]);
      if (filled.raw > 0) swept = book.sweep(record.side, filled, record.limit, shadow_.available_ppm);
    }
    record.filled = filled;
    record.vwap = swept.vwap;
    record.fee_raw = apply_ppm(swept.notional_raw, taker_ppm_[v][record.symbol]);
    record.fill_ns = record.arrival_ns;
    auto& pair = pairs_[record.symbol];
    if (filled.raw > 0) {
      apply_fill(pair, record.venue, record.side, filled, swept.vwap);
      pair.fees_raw += record.fee_raw;
      if (record.purpose == LegPurpose::open) pair.opened = true;
      ++metrics_.fills;
    }
    if (auto* output = push_output(OutputKind::leg_fill)) output->leg = record;
    slot.active = false;
    if (pair.pending_legs > 0) --pair.pending_legs;
    if (pair.pending_legs == 0) {
      resolve[record.symbol] = true;
      any = true;
    }
  }
  if (!any) return;
  for (std::size_t symbol = 0; symbol < instruments_.size(); ++symbol)
    if (resolve[symbol]) resolve_pair(symbol);
}

void Engine::on_maker_fill(PendingLeg& slot, Quantity filled, Price price) noexcept {
  auto& record = slot.record;
  const auto v = index(record.venue);
  auto& pair = pairs_[record.symbol];
  const auto notional = mul_scaled(price.raw, filled.raw);
  record.filled.raw += filled.raw;
  record.vwap = price;
  record.fee_raw += apply_ppm(notional, maker_ppm_[v][record.symbol]);
  record.fill_ns = now_ns_;
  apply_fill(pair, record.venue, record.side, filled, price);
  pair.fees_raw += apply_ppm(notional, maker_ppm_[v][record.symbol]);
  if (record.purpose == LegPurpose::open) pair.opened = true;
  ++metrics_.fills;
  LegRecord fill = record;
  fill.quantity = filled;
  fill.filled = filled;
  fill.fee_raw = apply_ppm(notional, maker_ppm_[v][record.symbol]);
  if (auto* output = push_output(OutputKind::leg_fill)) output->leg = fill;

  slot.remaining.raw -= filled.raw;
  // Immediate taker hedge for the filled size.
  auto hedge_qty = filled;
  if (record.purpose == LegPurpose::close) {
    const auto available = abs64(pair.qty_raw[index(record.hedge_venue)]);
    hedge_qty = Quantity{std::min(filled.raw, available)};
  }
  if (hedge_qty.raw > 0) {
    const auto& hedge_book = books_[index(record.hedge_venue)][record.symbol];
    const auto& instrument = instruments_[record.symbol];
    const auto swept = hedge_book.sweep(record.hedge_side, hedge_qty);
    const auto base = swept.complete(hedge_qty) ? swept.worst
                                                : (record.hedge_side == Side::sell ? hedge_book.best_bid().price
                                                                                  : hedge_book.best_ask().price);
    submit_leg(record.symbol, record.hedge_venue, record.hedge_side, record.purpose, hedge_qty,
               widen(base, record.hedge_side, strategy_.close_slippage_ppm,
                     instrument.tick[index(record.hedge_venue)]));
  }
  if (slot.remaining.raw <= 0) {
    slot.active = false;
    slot.resting = false;
    if (pair.pending_legs > 0) --pair.pending_legs;
  }
}

void Engine::process_resting_makers() noexcept {
  std::array<bool, kMaxSymbols> resolve{};
  bool any = false;
  for (auto& slot : legs_) {
    if (!slot.active || !slot.resting) continue;
    auto& record = slot.record;
    auto& pair = pairs_[record.symbol];
    const auto& book = books_[index(record.venue)][record.symbol];
    const auto& instrument = instruments_[record.symbol];

    if (now_ns_ >= slot.expire_ns || !book.verified()) {
      slot.active = false;
      slot.resting = false;
      if (pair.pending_legs > 0) --pair.pending_legs;
      resolve[record.symbol] = true;
      any = true;
      continue;
    }

    const bool crossed = record.side == Side::buy ? book.best_ask().price.raw > 0 && book.best_ask().price.raw <= record.limit.raw
                                                  : book.best_bid().price.raw > 0 && book.best_bid().price.raw >= record.limit.raw;
    if (!crossed) continue;

    const auto touch = record.side == Side::buy ? book.best_ask() : book.best_bid();
    const auto available = Quantity{apply_ppm(touch.quantity.raw, shadow_.available_ppm)};
    auto fill = round_down(Quantity{std::min(slot.remaining.raw, available.raw)}, instrument.step[index(record.venue)]);
    if (fill.raw <= 0) continue;
    on_maker_fill(slot, fill, record.limit);
    if (!slot.active) {
      resolve[record.symbol] = true;
      any = true;
    }
  }
  if (!any) return;
  for (std::size_t symbol = 0; symbol < instruments_.size(); ++symbol)
    if (resolve[symbol] && pairs_[symbol].pending_legs == 0) resolve_pair(symbol);
}

void Engine::cancel_resting_maker(std::size_t symbol) noexcept {
  std::array<bool, kMaxSymbols> resolve{};
  bool any = false;
  for (auto& slot : legs_) {
    if (!slot.active || !slot.resting || slot.record.symbol != symbol) continue;
    slot.active = false;
    slot.resting = false;
    auto& pair = pairs_[symbol];
    if (pair.pending_legs > 0) --pair.pending_legs;
    resolve[symbol] = true;
    any = true;
  }
  if (any && pairs_[symbol].pending_legs == 0) resolve_pair(symbol);
}

void Engine::apply_fill(PairState& pair, Venue venue, Side side, Quantity quantity, Price price) noexcept {
  const auto v = index(venue);
  auto& position = pair.qty_raw[v];
  auto& average = pair.avg_raw[v];
  const auto delta = side == Side::buy ? quantity.raw : -quantity.raw;
  if (position == 0 || (position > 0) == (delta > 0)) {
    const auto held = abs64(position);
    const auto total = held + quantity.raw;
    average = static_cast<std::int64_t>(
        (static_cast<__int128>(average) * held + static_cast<__int128>(price.raw) * quantity.raw) / total);
    position += delta;
    return;
  }
  const auto closed = std::min(abs64(position), quantity.raw);
  const auto pnl = mul_scaled(price.raw - average, closed);
  pair.realized_raw += position > 0 ? pnl : -pnl;
  position += delta;
  if (position == 0) average = 0;
  else if ((position > 0) == (delta > 0)) average = price.raw;
}

void Engine::resolve_pair(std::size_t symbol) noexcept {
  auto& pair = pairs_[symbol];
  const auto& instrument = instruments_[symbol];
  const auto qb = pair.qty_raw[0];
  const auto qy = pair.qty_raw[1];
  const auto repair_limit = [&](Venue venue, Side side) {
    const auto& book = books_[index(venue)][symbol];
    const auto base = side == Side::sell ? book.best_bid().price : book.best_ask().price;
    return widen(base, side, strategy_.hedge_slippage_ppm, instrument.tick[index(venue)]);
  };
  if (pair.phase == PairPhase::opening) {
    if (qb == 0 && qy == 0) {
      finish_cycle(symbol, pair.opened ? ExitReason::legged_out : ExitReason::missed);
      return;
    }
    const auto residual = qb + qy;
    if (residual != 0) {
      const auto excess = abs64(qb) > abs64(qy) ? Venue::binance : shadow_.peer;
      const auto repair = round_down(Quantity{abs64(residual)}, instrument.step[index(excess)]);
      if (repair.raw > 0) {
        if (pair.repairs >= strategy_.max_repair_attempts) {
          loss_halt_ = true;
          mark_out(symbol, ExitReason::legged_out);
          return;
        }
        ++pair.repairs;
        ++metrics_.repairs;
        const auto side = pair.qty_raw[index(excess)] > 0 ? Side::sell : Side::buy;
        submit_leg(symbol, excess, side, LegPurpose::repair, repair, repair_limit(excess, side));
        return;
      }
    }
    pair.phase = PairPhase::open;
    pair.entry_notional_raw = mul_scaled(abs64(pair.qty_raw[index(long_of(pair.dir))]),
                                         pair.avg_raw[index(long_of(pair.dir))]);
    return;
  }
  if (pair.phase == PairPhase::closing) {
    if (qb == 0 && qy == 0) {
      finish_cycle(symbol, pair.reason);
      return;
    }
    if (pair.repairs >= strategy_.max_repair_attempts) {
      loss_halt_ = true;
      mark_out(symbol, pair.reason);
      return;
    }
    ++pair.repairs;
    ++metrics_.repairs;
    for (std::size_t v = 0; v < kVenues; ++v) {
      const auto qty = pair.qty_raw[v];
      if (qty == 0) continue;
      const auto venue = slot_venue(v, shadow_.peer);
      const auto side = qty > 0 ? Side::sell : Side::buy;
      submit_leg(symbol, venue, side, LegPurpose::repair, Quantity{abs64(qty)}, repair_limit(venue, side));
    }
  }
}

void Engine::mark_out(std::size_t symbol, ExitReason reason) noexcept {
  auto& pair = pairs_[symbol];
  for (auto& slot : legs_)
    if (slot.active && slot.record.symbol == symbol) {
      slot.active = false;
      slot.resting = false;
    }
  pair.pending_legs = 0;
  for (std::size_t v = 0; v < kVenues; ++v) {
    const auto qty = pair.qty_raw[v];
    if (qty == 0) continue;
    const auto side = qty > 0 ? Side::sell : Side::buy;
    const Quantity quantity{abs64(qty)};
    const auto& book = books_[v][symbol];
    auto swept = book.sweep(side, quantity);
    if (!swept.complete(quantity)) {
      const auto rest = quantity.raw - swept.filled.raw;
      const auto price = swept.worst.raw > 0 ? swept.worst.raw : book.mid().raw;
      swept.notional_raw += mul_scaled(price, rest);
      swept.filled = quantity;
      swept.vwap = Price{div_scaled(swept.notional_raw, quantity.raw)};
    }
    LegRecord record;
    record.leg_id = next_leg_id_++;
    record.cycle_id = pair.cycle_id;
    record.symbol = static_cast<std::uint16_t>(symbol);
    record.venue = slot_venue(v, shadow_.peer);
    record.side = side;
    record.purpose = LegPurpose::close;
    record.quantity = record.filled = quantity;
    record.vwap = swept.vwap;
    record.fee_raw = apply_ppm(swept.notional_raw, taker_ppm_[v][symbol]);
    record.decision_ns = record.arrival_ns = record.fill_ns = now_ns_;
    apply_fill(pair, record.venue, side, quantity, swept.vwap);
    pair.fees_raw += record.fee_raw;
    if (auto* output = push_output(OutputKind::leg_fill)) output->leg = record;
  }
  finish_cycle(symbol, reason);
}

void Engine::finish_cycle(std::size_t symbol, ExitReason reason) noexcept {
  auto& pair = pairs_[symbol];
  CycleRecord cycle;
  cycle.cycle_id = pair.cycle_id;
  cycle.symbol = static_cast<std::uint16_t>(symbol);
  cycle.dir = pair.dir;
  cycle.reason = reason;
  cycle.entry_ns = pair.entry_ns;
  cycle.exit_ns = now_ns_;
  cycle.quantity = Quantity{abs64(pair.qty_raw[index(long_of(pair.dir))])};
  cycle.entry_gap_ppm = pair.entry_gap_ppm;
  cycle.exit_gap_ppm = pair.last_exit_gap_ppm;
  cycle.expected_capture_ppm = pair.expected_capture_ppm;
  cycle.expected_net_ppm = pair.expected_net_ppm;
  cycle.gross_raw = pair.realized_raw;
  cycle.fees_raw = pair.fees_raw;
  cycle.funding_raw = pair.funding_raw;
  cycle.net_raw = pair.realized_raw - pair.fees_raw + pair.funding_raw;
  cycle.notional_raw = pair.entry_notional_raw;
  cycle.repairs = pair.repairs;
  if (auto* output = push_output(OutputKind::cycle)) output->cycle = cycle;
  if (reason == ExitReason::missed) ++metrics_.missed;
  else ++metrics_.cycles;
  metrics_.net_raw += cycle.net_raw;
  metrics_.fees_raw += cycle.fees_raw;
  metrics_.funding_raw += cycle.funding_raw;
  if (metrics_.open_pairs > 0) --metrics_.open_pairs;
  if (metrics_.net_raw <= -limits_.daily_loss_raw) loss_halt_ = true;
  const auto cycle_id = pair.cycle_id;
  pair = PairState{};
  pair.cycle_id = cycle_id;
  pair.cooldown_until_ns = now_ns_ + strategy_.cooldown_ms * kMsNs;
}

// ---------------------------------------------------------------- control

void Engine::kill(std::uint64_t now_ns) noexcept {
  now_ns_ = std::max(now_ns_, now_ns);
  halted_ = true;
  for (std::size_t symbol = 0; symbol < instruments_.size(); ++symbol)
    if (pairs_[symbol].phase == PairPhase::open) begin_close(symbol, ExitReason::kill);
}

void Engine::acknowledge_halt() noexcept { halted_ = false; }

void Engine::liquidate(std::uint64_t now_ns, ExitReason reason) noexcept {
  now_ns_ = std::max(now_ns_, now_ns);
  for (std::size_t symbol = 0; symbol < instruments_.size(); ++symbol) {
    auto& pair = pairs_[symbol];
    if (pair.phase == PairPhase::flat) continue;
    if (pair.reason == ExitReason::none) pair.reason = reason;
    mark_out(symbol, pair.phase == PairPhase::closing ? pair.reason : reason);
  }
}

std::uint64_t Engine::state_hash() const noexcept {
  std::uint64_t hash = 1469598103934665603ULL;
  const auto mix = [&hash](std::uint64_t value) {
    hash ^= value;
    hash *= 1099511628211ULL;
  };
  for (std::size_t symbol = 0; symbol < instruments_.size(); ++symbol) {
    for (std::size_t v = 0; v < kVenues; ++v) mix(books_[v][symbol].state_hash());
    const auto& pair = pairs_[symbol];
    mix(static_cast<std::uint64_t>(pair.phase));
    mix(static_cast<std::uint64_t>(pair.qty_raw[0]));
    mix(static_cast<std::uint64_t>(pair.qty_raw[1]));
    mix(static_cast<std::uint64_t>(pair.realized_raw));
    mix(static_cast<std::uint64_t>(pair.fees_raw));
    mix(static_cast<std::uint64_t>(pair.funding_raw));
    mix(static_cast<std::uint64_t>(basis_mean_ppm(symbol)));
  }
  mix(metrics_.legs);
  mix(metrics_.fills);
  mix(metrics_.cycles);
  mix(metrics_.census_episodes);
  mix(static_cast<std::uint64_t>(metrics_.net_raw));
  return hash;
}

}  // namespace cxa
