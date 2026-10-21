#include "hla/engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <random>
#include <cstdio>
#include <sstream>

namespace hla {
namespace {

constexpr std::uint64_t kMs = 1'000'000ULL;
constexpr std::uint64_t kSecond = 1'000'000'000ULL;
constexpr std::uint64_t kMismatchGraceNs = 5 * kSecond;
constexpr std::uint64_t kHlMismatchGraceNs = 10 * kSecond;
constexpr std::uint64_t kDayNs = 86'400 * kSecond;
constexpr double kMinNotionalUsd = 10.0;
constexpr std::size_t kFlattenSide = 2;

double mid(double bid, double ask) noexcept { return (bid + ask) / 2; }
double mid(const Level& bid, const Level& ask) noexcept { return (bid.price.as_double() + ask.price.as_double()) / 2; }
double ppm_of(double part, double whole) noexcept { return whole == 0 ? 0 : part / whole * 1e6; }

bool contains(std::string_view text, std::string_view needle) noexcept {
  return text.find(needle) != std::string_view::npos;
}

}  // namespace

std::string_view block_name(Block block) noexcept {
  switch (block) {
    case Block::none: return "none";
    case Block::no_spec: return "no_spec";
    case Block::no_mt5_tick: return "no_mt5_tick";
    case Block::mt5_stale: return "mt5_stale";
    case Block::mt5_closed: return "mt5_closed";
    case Block::mt5_spread: return "mt5_spread";
    case Block::hl_stale: return "hl_stale";
    case Block::no_hl_book: return "no_hl_book";
    case Block::basis_cold: return "basis_cold";
    case Block::rollover: return "rollover";
    case Block::mark_oracle: return "mark_oracle";
    case Block::fair_gap: return "fair_gap";
    case Block::basis_large: return "basis_large";
    case Block::size: return "size";
    case Block::count: break;
  }
  return "unknown";
}

std::string_view halt_name(Halt halt) noexcept {
  switch (halt) {
    case Halt::none: return "none";
    case Halt::killed: return "killed";
    case Halt::emergency: return "emergency";
    case Halt::bridge_stale: return "bridge_stale";
    case Halt::gateway_down: return "gateway_down";
    case Halt::mt5_account: return "mt5_account";
    case Halt::aux_stale: return "aux_stale";
    case Halt::daily_loss: return "daily_loss";
    case Halt::budget: return "budget";
    case Halt::margin: return "margin";
    case Halt::hedge_unknown: return "hedge_unknown";
    case Halt::position_mismatch: return "position_mismatch";
    case Halt::forced_fill: return "forced_fill";
    case Halt::count: break;
  }
  return "unknown";
}

Engine::Engine(const Config& config, std::vector<PairSetup> pairs, EngineIo& io, bool simulated_mt5)
    : config_(config), pairs_(std::move(pairs)), io_(io), simulated_mt5_(simulated_mt5), state_(pairs_.size()),
      cloid_session_(static_cast<std::uint64_t>(std::chrono::system_clock::now().time_since_epoch().count()) ^
                     (static_cast<std::uint64_t>(std::random_device{}()) << 32)) {
  flights_.reserve(256);
}

// ------------------------------------------------------------------ helpers

void Engine::journal(std::string_view line) { io_.journal(line); }

Cloid Engine::next_cloid() noexcept {
  Cloid cloid{};
  const auto counter = ++cloid_counter_;
  for (int i = 0; i < 8; ++i) {
    cloid[static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(cloid_session_ >> (56 - 8 * i));
    cloid[static_cast<std::size_t>(8 + i)] = static_cast<std::uint8_t>(counter >> (56 - 8 * i));
  }
  return cloid;
}

double Engine::units_per_lot_step(std::size_t s) const noexcept {
  const auto& spec = state_[s].spec;
  return spec.volume_step * spec.value_per_point() / pairs_[s].ratio;
}

double Engine::inventory_steps(std::size_t s) const noexcept {
  const auto per_step = units_per_lot_step(s);
  return per_step > 0 ? state_[s].hl_units.as_double() / per_step : 0;
}

double Engine::unhedged_lots(std::size_t s) const noexcept {
  const auto vpp = state_[s].spec.value_per_point();
  if (vpp <= 0) return state_[s].hl_units.raw == 0 ? state_[s].mt5_lots : 1e9;
  return state_[s].mt5_lots + state_[s].hl_units.as_double() * pairs_[s].ratio / vpp;
}

double Engine::fair(std::size_t s) const noexcept {
  const auto& st = state_[s];
  if (st.mt5_tick_ns == 0 || !st.basis_seeded) return 0;
  return pairs_[s].ratio * mid(st.mt5_bid, st.mt5_ask) + st.basis;
}

void Engine::seed_positions(std::size_t s, Decimal hl_units, double mt5_lots) {
  state_[s].hl_units = hl_units;
  state_[s].mt5_lots = mt5_lots;
}

bool Engine::send(HlCommand& command, std::uint16_t symbol, std::span<const std::uint8_t> sides) {
  command.request_id = next_request_id_++;
  if (!io_.hl_command(command)) {
    failed_ = true;
    journal(R"({"k":"error","what":"hl command queue full"})");
    return false;
  }
  Flight flight;
  flight.request_id = command.request_id;
  flight.kind = command.kind;
  flight.symbol = symbol;
  flight.count = command.count;
  std::copy(sides.begin(), sides.end(), flight.sides.begin());
  flights_.push_back(flight);
  requests_sent_ += std::max<std::uint64_t>(command.count, 1);
  return true;
}

// ------------------------------------------------------------------ market data

void Engine::update_basis(std::size_t s, std::uint64_t now_ns) {
  auto& st = state_[s];
  if (st.mt5_tick_ns == 0 || st.hl_bid.price.raw <= 0 || st.hl_ask.price.raw <= 0) return;
  const auto raw = mid(st.hl_bid, st.hl_ask) - pairs_[s].ratio * mid(st.mt5_bid, st.mt5_ask);
  if (!st.basis_seeded) {
    st.basis = raw;
    st.basis_seeded = true;
    st.basis_start_ns = now_ns;
  } else if (now_ns > st.basis_last_ns) {
    const auto dt = static_cast<double>(now_ns - st.basis_last_ns) / static_cast<double>(kSecond);
    const auto alpha = 1.0 - std::exp(-dt / std::max(config_.basis_tau_s, 1e-3));
    st.basis += alpha * (raw - st.basis);
  }
  st.basis_last_ns = now_ns;
}

void Engine::on_hl_frame(const HlFrame& frame, std::uint64_t now_ns) {
  if (frame.kind == HlFrameKind::bbo) {
    const auto s = frame.bbo.symbol;
    if (s >= state_.size()) return;
    auto& st = state_[s];
    st.hl_bid = frame.bbo.has_bid ? frame.bbo.bid : Level{};
    st.hl_ask = frame.bbo.has_ask ? frame.bbo.ask : Level{};
    st.hl_bbo_ns = now_ns;
    update_basis(s, now_ns);
    requote(s, now_ns);
  } else if (frame.kind == HlFrameKind::ctx) {
    const auto s = frame.ctx.symbol;
    if (s >= state_.size()) return;
    auto& st = state_[s];
    st.mark = frame.ctx.mark;
    st.oracle = frame.ctx.oracle;
    st.funding = frame.ctx.funding;
    st.hl_ctx_ns = now_ns;
  }
}

void Engine::on_mt5_tick(const bridge::Tick& tick, std::uint64_t now_ns) {
  if (tick.symbol >= state_.size() || !(tick.bid > 0) || !(tick.ask >= tick.bid)) return;
  auto& st = state_[tick.symbol];
  st.mt5_bid = tick.bid;
  st.mt5_ask = tick.ask;
  st.mt5_tick_ns = now_ns;
  update_basis(tick.symbol, now_ns);
  requote(tick.symbol, now_ns);
}

void Engine::on_mt5_state(std::size_t s, const Mt5Spec& spec) {
  if (s < state_.size()) state_[s].spec = spec;
}

void Engine::on_mt5_account(const Mt5Account& account, std::uint64_t publisher_ns, std::uint64_t executor_ns,
                            std::uint64_t now_ns) {
  const auto previous_scan = account_.positions_ns;
  account_ = account;
  publisher_ns_ = publisher_ns;
  executor_ns_ = executor_ns;
  if (simulated_mt5_ || account.positions_ns == 0 || account.positions_ns == previous_scan) return;
  // The executor's position scan is the broker's truth; our running total must agree.
  for (std::size_t s = 0; s < state_.size(); ++s) {
    auto& st = state_[s];
    const auto step = st.spec.volume_step > 0 ? st.spec.volume_step : 0.01;
    const bool settling = st.hedge.request_id != 0 || now_ns - st.last_hedge_ns < 2 * kSecond;
    if (settling || std::abs(account.positions[s] - st.mt5_lots) < step / 2) {
      st.mt5_mismatch_since_ns = 0;
      continue;
    }
    if (st.mt5_mismatch_since_ns == 0) st.mt5_mismatch_since_ns = now_ns;
    if (now_ns - st.mt5_mismatch_since_ns >= kMismatchGraceNs) {
      std::snprintf(line_.data(), line_.size(), "mt5 position %s: broker %.4f vs engine %.4f",
                    pairs_[s].mt5_symbol.c_str(), account.positions[s], st.mt5_lots);
      set_halt(Halt::position_mismatch, line_.data(), now_ns);
    }
  }
}

void Engine::on_aux(const AuxReport& report, std::uint64_t now_ns) {
  aux_ = report;
  requests_at_aux_ = requests_sent_;
  if (!report.ok || simulated_mt5_) return;  // dry-run fills are simulated: nothing to reconcile
  for (std::size_t s = 0; s < state_.size(); ++s) {
    auto& st = state_[s];
    const auto step = pairs_[s].hl.step.raw;
    // Fills can lag the clearinghouse by a moment; only a persistent gap counts.
    if (std::abs(report.hl_positions[s].raw - st.hl_units.raw) < std::max<std::int64_t>(step, 1)) {
      st.hl_mismatch_since_ns = 0;
      continue;
    }
    if (st.hl_mismatch_since_ns == 0) st.hl_mismatch_since_ns = now_ns;
    if (now_ns - st.hl_mismatch_since_ns >= kHlMismatchGraceNs) {
      std::snprintf(line_.data(), line_.size(), "hl position %s: clearinghouse %.6f vs engine %.6f",
                    pairs_[s].hl.coin.c_str(), report.hl_positions[s].as_double(), st.hl_units.as_double());
      set_halt(Halt::position_mismatch, line_.data(), now_ns);
    }
  }
}

void Engine::on_kill_file(bool present, std::uint64_t now_ns) {
  if (!present || killed_) return;
  killed_ = true;
  journal(R"({"k":"halt","halt":"killed","detail":"kill file present"})");
  pull_all(now_ns, "kill file");
}

// ------------------------------------------------------------------ quoting

Block Engine::evaluate(std::size_t s, std::uint64_t now_ns) const {
  const auto& st = state_[s];
  if (!st.spec.valid || st.spec.value_per_point() <= 0 || st.spec.volume_step <= 0) return Block::no_spec;
  if (st.mt5_tick_ns == 0) return Block::no_mt5_tick;
  if (now_ns - st.mt5_tick_ns > config_.mt5_tick_stale_ms * kMs) return Block::mt5_stale;
  if (!st.spec.tradeable()) return Block::mt5_closed;
  const auto mt5_mid = mid(st.mt5_bid, st.mt5_ask);
  if (ppm_of(st.mt5_ask - st.mt5_bid, mt5_mid) > static_cast<double>(config_.max_mt5_spread_ppm))
    return Block::mt5_spread;
  if (st.hl_ctx_ns == 0 || now_ns - st.hl_ctx_ns > config_.hl_stale_ms * kMs) return Block::hl_stale;
  if (st.hl_bid.price.raw <= 0 || st.hl_ask.price.raw <= 0) return Block::no_hl_book;
  if (!st.basis_seeded || now_ns - st.basis_start_ns < config_.basis_warmup_s * kSecond) return Block::basis_cold;
  const auto minute = static_cast<int>((now_ns / (60 * kSecond)) % 1440);
  for (const auto& [start, end] : config_.rollover_windows)
    if (minute >= start && minute < end) return Block::rollover;
  if (st.oracle.raw > 0 &&
      std::abs(ppm_of(st.mark.as_double() - st.oracle.as_double(), st.oracle.as_double())) >
          static_cast<double>(config_.max_mark_oracle_ppm))
    return Block::mark_oracle;
  const auto f = fair(s);
  if (f > 0 && std::abs(ppm_of(st.basis, f)) > static_cast<double>(config_.max_basis_ppm)) return Block::basis_large;
  if (f <= 0 || std::abs(ppm_of(mid(st.hl_bid, st.hl_ask) - f, f)) > static_cast<double>(config_.max_fair_gap_ppm))
    return Block::fair_gap;
  const auto units = static_cast<double>(config_.quote_lots) * units_per_lot_step(s);
  if (units < pairs_[s].hl.step.as_double() || units * f < kMinNotionalUsd) return Block::size;
  return Block::none;
}

std::array<Engine::Desired, 2> Engine::desired_quotes(std::size_t s, std::uint64_t now_ns) {
  std::array<Desired, 2> out{};
  auto& st = state_[s];
  st.block = evaluate(s, now_ns);
  if (st.block != Block::none || halted() || soft_halt_ != Halt::none || killed_) return out;
  const auto& pair = pairs_[s];
  const auto f = fair(s);
  const auto mt5_half_spread_ppm = ppm_of(st.mt5_ask - st.mt5_bid, mid(st.mt5_bid, st.mt5_ask)) / 2;
  const auto half_spread_ppm = static_cast<double>(pair.hl_maker_ppm + config_.mt5_commission_ppm +
                                                   config_.adverse_ppm + config_.edge_ppm) +
                               mt5_half_spread_ppm;
  const auto inventory = inventory_steps(s);
  const auto skewed = f * (1.0 - inventory * static_cast<double>(config_.skew_ppm) / 1e6);
  const auto tick = hl_tick(Price::from_double(f), pair.hl.size_decimals);
  auto bid = round_down(Price::from_double(skewed * (1.0 - half_spread_ppm / 1e6)), tick);
  auto ask = round_up(Price::from_double(skewed * (1.0 + half_spread_ppm / 1e6)), tick);
  // ALO must not cross: rest one tick inside the far side instead.
  if (bid >= st.hl_ask.price) bid = Price{st.hl_ask.price.raw - tick.raw};
  if (ask <= st.hl_bid.price) ask = Price{st.hl_bid.price.raw + tick.raw};
  const auto size = round_down(
      Quantity::from_double(static_cast<double>(config_.quote_lots) * units_per_lot_step(s)), pair.hl.step);

  double gross = 0;
  for (std::size_t i = 0; i < state_.size(); ++i) {
    const auto& other = state_[i];
    if (other.hl_bid.price.raw > 0 && other.hl_ask.price.raw > 0)
      gross += std::abs(other.hl_units.as_double()) * mid(other.hl_bid, other.hl_ask);
  }
  const bool room = gross + size.as_double() * f <= config_.max_gross_notional_usd;
  const auto lots = static_cast<double>(config_.quote_lots);
  const auto max_lots = static_cast<double>(config_.max_inventory_lots);
  const bool long_side = inventory < 0;   // buying reduces a short
  const bool short_side = inventory > 0;  // selling reduces a long
  out[0] = {inventory + lots <= max_lots + 1e-9 && (room || long_side) && bid.raw > 0, bid, size};
  out[1] = {inventory - lots >= -max_lots - 1e-9 && (room || short_side), ask, size};
  return out;
}

void Engine::cancel_side(std::size_t s, std::size_t side, std::uint64_t) {
  auto& q = state_[s].quotes[side];
  HlCommand command;
  command.kind = HlCommandKind::cancel_cloid;
  command.count = 1;
  command.orders[0].asset = pairs_[s].hl.asset_id;
  command.orders[0].cloid = q.cloid;
  const std::array<std::uint8_t, 1> sides{static_cast<std::uint8_t>(side)};
  if (send(command, static_cast<std::uint16_t>(s), sides)) q.state = QuoteState::canceling;
}

void Engine::requote(std::size_t s, std::uint64_t now_ns) {
  auto& st = state_[s];
  const auto desired = desired_quotes(s, now_ns);
  const auto& pair = pairs_[s];
  HlCommand place;
  place.kind = HlCommandKind::place;
  HlCommand modify;
  modify.kind = HlCommandKind::modify;
  std::array<std::uint8_t, kMaxBatch> place_sides{};
  std::array<std::uint8_t, kMaxBatch> modify_sides{};
  const bool may_modify = now_ns - st.last_requote_ns >= config_.min_requote_ms * kMs;
  for (std::size_t side = 0; side < 2; ++side) {
    auto& q = st.quotes[side];
    const auto& want = desired[side];
    if (q.state == QuoteState::placing || q.state == QuoteState::modifying || q.state == QuoteState::canceling)
      continue;
    if (q.state == QuoteState::resting && !want.active) {
      cancel_side(s, side, now_ns);
      continue;
    }
    if (!want.active) continue;
    OrderSpec order{pair.hl.asset_id, side == 0, want.price, want.size, false, Tif::alo, next_cloid()};
    if (q.state == QuoteState::idle) {
      if (now_ns < st.next_place_ns) continue;
      q.pending_price = want.price;
      q.pending_size = want.size;
      q.pending_cloid = *order.cloid;
      q.cloid = *order.cloid;
      place_sides[place.count] = static_cast<std::uint8_t>(side);
      place.orders[place.count++] = order;
      continue;
    }
    const auto tick = hl_tick(q.price, pair.hl.size_decimals);
    const auto threshold = std::max(tick.raw, apply_ppm(q.price.raw, config_.requote_ppm));
    if (!may_modify || q.oid == 0 || (abs64(want.price.raw - q.price.raw) < threshold && want.size == q.size)) continue;
    q.pending_price = want.price;
    q.pending_size = want.size;
    q.pending_cloid = *order.cloid;
    modify_sides[modify.count] = static_cast<std::uint8_t>(side);
    modify.oids[modify.count] = q.oid;
    modify.orders[modify.count++] = order;
  }
  if (place.count > 0 && send(place, static_cast<std::uint16_t>(s), std::span(place_sides.data(), place.count))) {
    for (std::size_t i = 0; i < place.count; ++i) st.quotes[place_sides[i]].state = QuoteState::placing;
    st.last_requote_ns = now_ns;
  }
  if (modify.count > 0 && send(modify, static_cast<std::uint16_t>(s), std::span(modify_sides.data(), modify.count))) {
    for (std::size_t i = 0; i < modify.count; ++i) st.quotes[modify_sides[i]].state = QuoteState::modifying;
    st.last_requote_ns = now_ns;
  }
}

void Engine::pull_all(std::uint64_t now_ns, std::string_view reason) {
  std::snprintf(line_.data(), line_.size(), R"({"k":"pull","reason":"%.*s"})", static_cast<int>(reason.size()),
                reason.data());
  journal(line_.data());
  for (std::size_t s = 0; s < state_.size(); ++s)
    for (std::size_t side = 0; side < 2; ++side)
      if (state_[s].quotes[side].state == QuoteState::resting) cancel_side(s, side, now_ns);
}

// ------------------------------------------------------------------ HL events

void Engine::on_ack(const HlEvent& event, std::uint64_t now_ns) {
  const auto it = std::find_if(flights_.begin(), flights_.end(),
                               [&](const Flight& f) { return f.request_id == event.request_id; });
  if (it == flights_.end()) {
    if (!event.ok) {
      std::snprintf(line_.data(), line_.size(), R"({"k":"ack","request":%llu,"ok":false,"error":"%s"})",
                    static_cast<unsigned long long>(event.request_id), event.text.data());
      journal(line_.data());
    }
    return;
  }
  const auto flight = *it;
  flights_.erase(it);
  const auto s = flight.symbol;
  auto& st = state_[s];
  const auto text = event.text_view();
  const bool timeout = !event.ok && contains(text, "timeout");
  std::vector<std::pair<Cloid, std::uint8_t>> orphaned;  // state unknown: cancel by cloid
  bool place_rejected = false;
  bool post_only_rejected = false;
  for (std::size_t i = 0; i < flight.count; ++i) {
    const auto side = flight.sides[i];
    const AckItem item = i < event.count ? event.items[i] : AckItem{};
    if (side == kFlattenSide) {
      std::snprintf(line_.data(), line_.size(),
                    R"({"k":"flatten_ack","coin":"%s","ok":%s,"status":%d,"filled":%.8f,"px":%.8f,"error":"%s"})",
                    pairs_[s].hl.coin.c_str(), event.ok ? "true" : "false", static_cast<int>(item.status),
                    item.filled_size.as_double(), item.average_price.as_double(), event.text.data());
      journal(line_.data());
      continue;
    }
    auto& q = st.quotes[side];
    switch (flight.kind) {
      case HlCommandKind::place:
        if (event.ok && item.status == AckStatus::resting) {
          q.state = QuoteState::resting;
          q.oid = item.oid;
          q.price = q.pending_price;
          q.size = q.pending_size;
          q.cloid = q.pending_cloid;
        } else if (timeout) {
          q.cloid = q.pending_cloid;
          orphaned.emplace_back(q.cloid, side);
          q.state = QuoteState::canceling;
        } else {
          // Back off so a persistent reject (margin, rate limit) never loops at the timer rate.
          q.state = QuoteState::idle;
          q.oid = 0;
          (event.ok && item.post_only_reject ? post_only_rejected : place_rejected) = true;
        }
        if (q.state == QuoteState::resting) st.place_backoff_ms = 0;
        break;
      case HlCommandKind::modify:
        if (event.ok && item.status == AckStatus::resting) {
          q.state = QuoteState::resting;
          q.oid = item.oid;
          q.price = q.pending_price;
          q.size = q.pending_size;
          q.cloid = q.pending_cloid;
        } else if (event.ok && (contains(text, "canceled or filled") || contains(text, "Cannot modify"))) {
          q.state = QuoteState::idle;  // the order is gone; fills arrive on userFills
          q.oid = 0;
        } else {
          // Old order may still rest, new one may exist: cancel both by cloid.
          orphaned.emplace_back(q.cloid, side);
          if (timeout || !event.ok) orphaned.emplace_back(q.pending_cloid, side);
          q.state = QuoteState::canceling;
        }
        break;
      case HlCommandKind::cancel_cloid:
        if ((event.ok && item.status == AckStatus::success) ||
            contains(text, "never placed, already canceled, or filled")) {
          q.state = QuoteState::idle;
          q.oid = 0;
        } else {
          q.state = QuoteState::resting;  // retried by the next requote or pull
        }
        break;
      default:
        break;
    }
  }
  // Back off once per ack so a persistent reject (margin, rate limit) never loops at the timer rate.
  if (place_rejected) {
    st.place_backoff_ms = std::clamp<std::uint64_t>(st.place_backoff_ms * 2, 1'000, 30'000);
    st.next_place_ns = now_ns + st.place_backoff_ms * kMs;
  } else if (post_only_rejected) {
    st.next_place_ns = std::max(st.next_place_ns, now_ns + 200 * kMs);
  }
  if (!event.ok || (event.count > 0 && event.items[0].status == AckStatus::error && !event.items[0].post_only_reject)) {
    std::snprintf(line_.data(), line_.size(),
                  R"({"k":"ack","request":%llu,"coin":"%s","kind":%d,"ok":%s,"error":"%s"})",
                  static_cast<unsigned long long>(event.request_id), pairs_[s].hl.coin.c_str(),
                  static_cast<int>(flight.kind), event.ok ? "true" : "false", event.text.data());
    journal(line_.data());
  }
  if (!orphaned.empty()) {
    HlCommand command;
    command.kind = HlCommandKind::cancel_cloid;
    std::array<std::uint8_t, kMaxBatch> sides{};
    for (const auto& [cloid, side] : orphaned) {
      if (command.count >= kMaxBatch) break;
      command.orders[command.count].asset = pairs_[s].hl.asset_id;
      command.orders[command.count].cloid = cloid;
      sides[command.count++] = side;
    }
    send(command, s, std::span(sides.data(), command.count));
  }
  requote(s, now_ns);
}

void Engine::on_fill(const HlEvent& event, std::uint64_t now_ns) {
  const auto s = event.symbol;
  if (s >= state_.size()) return;
  auto& st = state_[s];
  const auto signed_size = event.side == Side::buy ? event.size.raw : -event.size.raw;
  st.hl_units = Decimal{st.hl_units.raw + signed_size};
  for (auto& q : st.quotes) {
    if (q.oid == 0 || q.oid != event.oid) continue;
    q.size = Quantity{q.size.raw - event.size.raw};
    if (q.size.raw <= 0 && q.state == QuoteState::resting) {
      q.state = QuoteState::idle;
      q.oid = 0;
    }
  }
  if (st.first_unhedged_ns == 0) st.first_unhedged_ns = event.receive_ns;
  std::snprintf(line_.data(), line_.size(),
                R"({"k":"fill","t":%llu,"coin":"%s","side":"%s","px":%.8f,"sz":%.8f,"fee":%.8f,"oid":%llu,)"
                R"("tid":%llu,"crossed":%s,"dir":"%s","fair":%.8f,"mt5_bid":%.8f,"mt5_ask":%.8f,"hl_pos":%.8f})",
                static_cast<unsigned long long>(event.receive_ns), pairs_[s].hl.coin.c_str(),
                event.side == Side::buy ? "B" : "A", event.price.as_double(), event.size.as_double(),
                event.fee.as_double(), static_cast<unsigned long long>(event.oid),
                static_cast<unsigned long long>(event.tid), event.crossed ? "true" : "false", event.text.data(),
                fair(s), st.mt5_bid, st.mt5_ask, st.hl_units.as_double());
  journal(line_.data());
  hedge(s, now_ns);
  if (event.forced) set_halt(Halt::forced_fill, "ADL or liquidation fill", now_ns);
  requote(s, now_ns);
}

void Engine::on_hl_event(const HlEvent& event, std::uint64_t now_ns) {
  switch (event.kind) {
    case HlEventKind::ack: on_ack(event, now_ns); return;
    case HlEventKind::fill: on_fill(event, now_ns); return;
    case HlEventKind::order_update: {
      if (event.symbol >= state_.size()) return;
      if (event.order_status == OrderStatus::open || event.order_status == OrderStatus::other) return;
      for (auto& q : state_[event.symbol].quotes) {
        if (q.oid != event.oid || (q.state != QuoteState::resting && q.state != QuoteState::canceling)) continue;
        q.state = QuoteState::idle;
        q.oid = 0;
      }
      requote(event.symbol, now_ns);
      return;
    }
    case HlEventKind::connection:
      gateway_up_ = event.connected;
      std::snprintf(line_.data(), line_.size(), R"({"k":"gateway","connected":%s,"detail":"%s"})",
                    event.connected ? "true" : "false", event.text.data());
      journal(line_.data());
      if (contains(event.text_view(), "malformed private frame"))
        set_halt(Halt::position_mismatch, "malformed private frame (possible lost fill)", now_ns);
      return;
  }
}

// ------------------------------------------------------------------ hedging

void Engine::hedge(std::size_t s, std::uint64_t now_ns) {
  auto& st = state_[s];
  if (st.hedge.request_id != 0) return;
  const auto& spec = st.spec;
  const auto vpp = spec.value_per_point();
  if (!spec.valid || vpp <= 0 || spec.volume_step <= 0) return;  // timer escalates if exposure persists
  const auto need = -(st.hl_units.as_double() * pairs_[s].ratio / vpp) - st.mt5_lots;
  const auto steps = std::llround(need / spec.volume_step);
  auto lots = static_cast<double>(steps) * spec.volume_step;
  if (steps == 0 || std::abs(lots) < spec.volume_min - 1e-12) {
    st.first_unhedged_ns = 0;  // within one lot step: as hedged as the broker allows
    return;
  }
  if (!spec.tradeable() || (!simulated_mt5_ && soft_halt_ == Halt::bridge_stale)) return;
  if (spec.volume_max > 0) lots = std::clamp(lots, -spec.volume_max, spec.volume_max);
  bridge::OrderRequest order{};
  order.request_id = next_request_id_++;
  order.symbol = static_cast<std::uint32_t>(s);
  order.side = lots > 0 ? bridge::buy : bridge::sell;
  order.volume = std::abs(lots);
  order.deviation_points = config_.hedge_deviation_points;
  order.submit_ns = now_ns;
  order.expire_ns = now_ns + config_.mt5_order_max_age_ms * kMs;
  if (!io_.mt5_order(order)) {
    failed_ = true;
    emergency(s, "mt5 order ring full", now_ns);
    return;
  }
  const auto attempts = st.hedge.attempts;
  st.hedge = {order.request_id, lots, now_ns, account_.positions[s], attempts};
  st.last_hedge_ns = now_ns;
  std::snprintf(line_.data(), line_.size(),
                R"({"k":"hedge","t":%llu,"symbol":"%s","request":%llu,"lots":%.4f,"attempt":%d,"mt5_bid":%.8f,"mt5_ask":%.8f})",
                static_cast<unsigned long long>(now_ns), pairs_[s].mt5_symbol.c_str(),
                static_cast<unsigned long long>(order.request_id), lots, attempts + 1, st.mt5_bid, st.mt5_ask);
  journal(line_.data());
}

void Engine::hedge_result(std::size_t s, double filled_lots, std::uint64_t now_ns) {
  auto& st = state_[s];
  st.mt5_lots += filled_lots;
  st.hedge = {};
  st.last_hedge_ns = now_ns;
  hedge(s, now_ns);  // a partial fill sends the remainder
  if (st.hedge.request_id == 0 && std::abs(unhedged_lots(s)) < st.spec.volume_step / 2) st.first_unhedged_ns = 0;
}

void Engine::on_mt5_txn(const bridge::Txn& txn, std::uint64_t now_ns) {
  if (txn.symbol >= state_.size()) return;
  auto& st = state_[txn.symbol];
  const bool ours = st.hedge.request_id == txn.request_id;
  const auto sign = txn.side == bridge::buy ? 1.0 : -1.0;
  std::snprintf(line_.data(), line_.size(),
                R"({"k":"txn","t":%llu,"symbol":"%s","request":%llu,"status":%u,"retcode":%u,"lots":%.4f,)"
                R"("px":%.8f,"commission":%.4f,"deal":%llu,"hedge_ms":%.3f,"order_ms":%.3f,"ours":%s})",
                static_cast<unsigned long long>(now_ns), pairs_[txn.symbol].mt5_symbol.c_str(),
                static_cast<unsigned long long>(txn.request_id), txn.status, txn.retcode, sign * txn.volume,
                txn.price, txn.commission, static_cast<unsigned long long>(txn.deal),
                st.first_unhedged_ns == 0 ? 0.0 : static_cast<double>(txn.publish_ns - st.first_unhedged_ns) / 1e6,
                ours ? static_cast<double>(txn.publish_ns - st.hedge.sent_ns) / 1e6 : 0.0, ours ? "true" : "false");
  journal(line_.data());
  if (!ours) return;
  if (txn.status == bridge::filled || txn.status == bridge::partial) {
    st.hedge.attempts = 0;
    hedge_result(txn.symbol, sign * txn.volume, now_ns);
    return;
  }
  const auto attempts = st.hedge.attempts + 1;
  st.hedge = {};
  st.hedge.attempts = attempts;
  if (attempts >= 2) {
    emergency(txn.symbol, "mt5 hedge rejected twice", now_ns);
    return;
  }
  hedge(txn.symbol, now_ns);
}

void Engine::emergency(std::size_t s, std::string_view reason, std::uint64_t now_ns) {
  auto& st = state_[s];
  std::snprintf(line_.data(), line_.size(), "%s: %.*s", pairs_[s].hl.coin.c_str(), static_cast<int>(reason.size()),
                reason.data());
  const std::string detail(line_.data());
  set_halt(Halt::emergency, detail, now_ns);
  if (st.flattened) return;
  st.flattened = true;
  // Close the HL units not covered by MT5 with one IOC inside a slippage cap.
  const auto vpp = st.spec.value_per_point();
  const auto covered = vpp > 0 ? -st.mt5_lots * vpp / pairs_[s].ratio : 0.0;
  const auto residual = st.hl_units.as_double() - covered;
  const auto& hl = pairs_[s].hl;
  const auto qty = round_down(Quantity::from_double(std::abs(residual)), hl.step);
  if (qty.raw <= 0) return;
  const bool buy = residual < 0;
  const auto slip = static_cast<double>(config_.flatten_slippage_ppm) / 1e6;
  auto reference = buy ? st.hl_ask.price.as_double() : st.hl_bid.price.as_double();
  if (reference <= 0) reference = st.mark.as_double();
  if (reference <= 0) return;
  const auto limit = Price::from_double(reference * (buy ? 1 + slip : 1 - slip));
  const auto tick = hl_tick(limit, hl.size_decimals);
  HlCommand command;
  command.kind = HlCommandKind::place;
  command.count = 1;
  command.orders[0] = {hl.asset_id, buy, buy ? round_up(limit, tick) : round_down(limit, tick), qty,
                       (buy && st.hl_units.raw < 0) || (!buy && st.hl_units.raw > 0), Tif::ioc, next_cloid()};
  const std::array<std::uint8_t, 1> sides{static_cast<std::uint8_t>(kFlattenSide)};
  send(command, static_cast<std::uint16_t>(s), sides);
  std::snprintf(line_.data(), line_.size(),
                R"({"k":"flatten","coin":"%s","side":"%s","sz":%.8f,"limit":%.8f,"reason":"%s"})", hl.coin.c_str(),
                buy ? "B" : "A", qty.as_double(), command.orders[0].price.as_double(), detail.c_str());
  journal(line_.data());
}

// ------------------------------------------------------------------ risk

void Engine::set_halt(Halt halt, std::string_view detail, std::uint64_t now_ns) {
  if (halt_ == halt) return;  // already halted for this reason: quotes are being pulled
  if (halt_ == Halt::none) halt_ = halt;
  // `detail` may point into line_; copy it, and keep it JSON-safe.
  std::string text(detail);
  for (auto& c : text)
    if (c == '"' || c == '\\' || static_cast<unsigned char>(c) < 0x20) c = '\'';
  std::snprintf(line_.data(), line_.size(), R"({"k":"halt","halt":"%.*s","detail":"%s"})",
                static_cast<int>(halt_name(halt).size()), halt_name(halt).data(), text.c_str());
  journal(line_.data());
  io_.kill(std::string(halt_name(halt)) + ": " + text);
  pull_all(now_ns, halt_name(halt));
}

void Engine::check_global(std::uint64_t now_ns) {
  const auto stale = config_.mt5_heartbeat_stale_ms * kMs;
  Halt soft = Halt::none;
  if (!gateway_up_) {
    soft = Halt::gateway_down;
  } else if (now_ns - publisher_ns_ > stale || (!simulated_mt5_ && now_ns - executor_ns_ > stale)) {
    soft = Halt::bridge_stale;
  } else if (!account_.valid || !account_.connected || (!simulated_mt5_ && !account_.trade_allowed) ||
             (!config_.mt5_logins.empty() &&
              std::find(config_.mt5_logins.begin(), config_.mt5_logins.end(), account_.login) ==
                  config_.mt5_logins.end())) {
    soft = Halt::mt5_account;
  } else if (!aux_.ok || now_ns - aux_.receive_ns > config_.aux_stale_ms * kMs) {
    soft = Halt::aux_stale;
  } else if (aux_.has_rate_limit &&
             aux_.requests_remaining - static_cast<std::int64_t>(requests_sent_ - requests_at_aux_) <
                 config_.min_request_budget) {
    soft = Halt::budget;
  } else if (aux_.hl_min_withdrawable < config_.hl_min_margin_usd ||
             account_.margin_free < config_.mt5_min_margin_usd) {
    soft = Halt::margin;
  }
  const auto escalate = std::max(3 * config_.hedge_timeout_ms, config_.mt5_order_max_age_ms + 1'000) * kMs;
  for (std::size_t s = 0; s < state_.size(); ++s) {
    auto& st = state_[s];
    if (st.hedge.request_id != 0 && now_ns - st.hedge.sent_ns > config_.hedge_timeout_ms * kMs) {
      // No result yet: the broker's position scan can prove the fill.
      if (!simulated_mt5_ && account_.positions_ns > st.hedge.sent_ns &&
          std::abs(account_.positions[s] - st.hedge.position_before - st.hedge.lots) < st.spec.volume_step / 2) {
        std::snprintf(line_.data(), line_.size(), R"({"k":"hedge_inferred","symbol":"%s","request":%llu})",
                      pairs_[s].mt5_symbol.c_str(), static_cast<unsigned long long>(st.hedge.request_id));
        journal(line_.data());
        hedge_result(s, st.hedge.lots, now_ns);
        continue;
      }
      if (soft == Halt::none) soft = Halt::hedge_unknown;
      if (now_ns - st.hedge.sent_ns > escalate) emergency(s, "mt5 hedge result missing", now_ns);
    } else if (st.hedge.request_id == 0 && st.first_unhedged_ns != 0 && now_ns - st.first_unhedged_ns > escalate &&
               std::abs(unhedged_lots(s)) >= std::max(st.spec.volume_step, 1e-9) / 2) {
      emergency(s, "unhedged exposure could not be hedged", now_ns);
    }
  }
  if (soft != soft_halt_) {
    std::snprintf(line_.data(), line_.size(), R"({"k":"soft_halt","from":"%.*s","to":"%.*s"})",
                  static_cast<int>(halt_name(soft_halt_).size()), halt_name(soft_halt_).data(),
                  static_cast<int>(halt_name(soft).size()), halt_name(soft).data());
    journal(line_.data());
    soft_halt_ = soft;
    if (soft != Halt::none) pull_all(now_ns, halt_name(soft));
  }
  // Daily loss on combined equity, re-based at each UTC midnight.
  if (aux_.ok && account_.valid) {
    const auto equity = aux_.hl_account_value + account_.equity;
    if (day_start_ns_ == 0 || now_ns / kDayNs != day_start_ns_ / kDayNs) {
      day_start_ns_ = now_ns;
      day_start_equity_ = equity;
    } else if (halt_ == Halt::none && day_start_equity_ - equity > config_.max_daily_loss_usd) {
      std::snprintf(line_.data(), line_.size(), "equity %.2f from day start %.2f", equity, day_start_equity_);
      set_halt(Halt::daily_loss, line_.data(), now_ns);
    }
  }
}

void Engine::on_timer(std::uint64_t now_ns) {
  check_global(now_ns);
  for (std::size_t s = 0; s < state_.size(); ++s) requote(s, now_ns);
  if (now_ns - last_snapshot_ns_ >= kSecond) {
    last_snapshot_ns_ = now_ns;
    journal_snapshot(now_ns);
  }
}

void Engine::journal_snapshot(std::uint64_t now_ns) {
  for (std::size_t s = 0; s < state_.size(); ++s) {
    const auto& st = state_[s];
    std::snprintf(line_.data(), line_.size(),
                  R"({"k":"f","t":%llu,"coin":"%s","fair":%.8f,"basis":%.8f,"hl_bid":%.8f,"hl_ask":%.8f,)"
                  R"("mt5_bid":%.8f,"mt5_ask":%.8f,"bid":%.8f,"ask":%.8f,"hl_pos":%.8f,"mt5_lots":%.4f,"block":"%.*s"})",
                  static_cast<unsigned long long>(now_ns), pairs_[s].hl.coin.c_str(), fair(s), st.basis,
                  st.hl_bid.price.as_double(), st.hl_ask.price.as_double(), st.mt5_bid, st.mt5_ask,
                  st.quotes[0].state == QuoteState::resting ? st.quotes[0].price.as_double() : 0.0,
                  st.quotes[1].state == QuoteState::resting ? st.quotes[1].price.as_double() : 0.0,
                  st.hl_units.as_double(), st.mt5_lots, static_cast<int>(block_name(st.block).size()),
                  block_name(st.block).data());
    journal(line_.data());
  }
  if (aux_.ok && account_.valid) {
    std::snprintf(line_.data(), line_.size(),
                  R"({"k":"equity","t":%llu,"hl":%.4f,"mt5":%.4f,"day_start":%.4f,"requests":%llu})",
                  static_cast<unsigned long long>(now_ns), aux_.hl_account_value, account_.equity, day_start_equity_,
                  static_cast<unsigned long long>(requests_sent_));
    journal(line_.data());
  }
}

std::string Engine::status_json(std::uint64_t now_ns) const {
  std::ostringstream out;
  out.precision(10);
  out << "{\"t\":" << now_ns << ",\"live\":" << (config_.live ? "true" : "false") << ",\"halt\":\""
      << halt_name(halt_) << "\",\"soft_halt\":\"" << halt_name(soft_halt_) << "\",\"killed\":"
      << (killed_ ? "true" : "false") << ",\"gateway_up\":" << (gateway_up_ ? "true" : "false")
      << ",\"mt5_login\":" << account_.login << ",\"mt5_equity\":" << account_.equity
      << ",\"hl_account_value\":" << aux_.hl_account_value << ",\"day_start_equity\":" << day_start_equity_
      << ",\"requests_sent\":" << requests_sent_ << ",\"requests_remaining\":" << aux_.requests_remaining
      << ",\"symbols\":[";
  for (std::size_t s = 0; s < state_.size(); ++s) {
    const auto& st = state_[s];
    out << (s == 0 ? "" : ",") << "{\"coin\":\"" << pairs_[s].hl.coin << "\",\"mt5\":\"" << pairs_[s].mt5_symbol
        << "\",\"block\":\"" << block_name(st.block) << "\",\"fair\":" << fair(s) << ",\"hl_bid\":"
        << st.hl_bid.price.as_double() << ",\"hl_ask\":" << st.hl_ask.price.as_double() << ",\"mt5_bid\":"
        << st.mt5_bid << ",\"mt5_ask\":" << st.mt5_ask << ",\"bid\":" << st.quotes[0].price.as_double()
        << ",\"bid_state\":" << static_cast<int>(st.quotes[0].state) << ",\"ask\":" << st.quotes[1].price.as_double()
        << ",\"ask_state\":" << static_cast<int>(st.quotes[1].state) << ",\"hl_pos\":" << st.hl_units.as_double()
        << ",\"mt5_lots\":" << st.mt5_lots << ",\"unhedged_lots\":" << unhedged_lots(s)
        << ",\"hedge_in_flight\":" << (st.hedge.request_id != 0 ? "true" : "false") << "}";
  }
  out << "]}";
  return out.str();
}

}  // namespace hla
