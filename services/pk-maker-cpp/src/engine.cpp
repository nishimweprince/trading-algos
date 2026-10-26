#include "pkm/engine.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>

#include "pkm/clob.hpp"
#include "pkm/json.hpp"

namespace pkm::engine {
namespace {

constexpr std::int64_t kNs = 1'000'000'000LL;
constexpr std::int64_t kTrailMs = 5'000;
constexpr std::int64_t kCashHoldMs = 3'000;

double round_dp2(double x) noexcept { return std::nearbyint(x * 100.0) / 100.0; }

std::string fixed(double v, int dp) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%.*f", dp, v);
  return buf;
}

void put_number(std::string& out, double v) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%.10g", v);
  out.append(buf);
}

}  // namespace

std::string window_key(std::string_view asset, std::int64_t open_unix) {
  std::string k(asset);
  k.push_back('-');
  k.append(std::to_string(open_unix));
  return k;
}

std::string utc_day(std::int64_t t_ns) {
  const std::time_t s = static_cast<std::time_t>(t_ns / kNs);
  std::tm tm{};
  gmtime_r(&s, &tm);
  char buf[16];
  std::strftime(buf, sizeof(buf), "%Y-%m-%d", &tm);
  return buf;
}

double floor_tick(double x) noexcept { return round_dp2(std::floor(x / kTick + 1e-6) * kTick); }

std::pair<std::optional<double>, std::optional<double>> desired_px(const Config& cfg, double q, double net,
                                                                   std::optional<double> up_ask,
                                                                   std::optional<double> down_ask) noexcept {
  const double skew = cfg.max_net > 0 ? cfg.skew_k * net / cfg.max_net : 0.0;
  auto clamp = [&cfg](double px, std::optional<double> ask) -> std::optional<double> {
    if (!ask) return std::nullopt;
    if (px >= *ask - 1e-9) px = round_dp2(*ask - kTick);
    if (px < cfg.min_px - 1e-9 || px > cfg.max_px + 1e-9) return std::nullopt;
    return px;
  };
  return {clamp(floor_tick(q - cfg.half_spread - skew), up_ask), clamp(floor_tick(1.0 - (q + cfg.half_spread - skew)), down_ask)};
}

bool opens(double net, bool up) noexcept { return up ? net >= -1e-9 : net <= 1e-9; }

bool first_leg_ok(const Config& cfg, std::int64_t s, double q) noexcept {
  return s < cfg.open_leg_s && q >= cfg.open_min_fair - 1e-9 && q <= cfg.open_max_fair + 1e-9;
}

std::optional<double> pair_cap(const Config& cfg, std::optional<double> px, std::optional<double> opposite_avg) noexcept {
  if (!px) return std::nullopt;
  if (!opposite_avg) return px;
  const double cap = floor_tick(1.0 - *opposite_avg - cfg.pair_margin);
  const double out = std::min(*px, cap);
  if (out < cfg.min_px - 1e-9) return std::nullopt;
  return out;
}

std::optional<double> room(const Config& cfg, double net, bool up) noexcept {
  const double r = up ? cfg.max_net - net : cfg.max_net + net;
  const double size = std::floor(std::min(cfg.shares, r));
  if (size < cfg.min_shares - 1e-9) return std::nullopt;
  return size;
}

Engine::Engine(Config cfg, std::vector<std::string> assets) : cfg_(cfg), bucket_(cfg.max_orders_per_s) {
  coins_.resize(assets.size());
  for (std::size_t i = 0; i < assets.size(); ++i) coins_[i].name = assets[i];
  actions_.reserve(64);
}

int Engine::coin_of(std::string_view asset) const noexcept {
  for (std::size_t i = 0; i < coins_.size(); ++i)
    if (coins_[i].name == asset) return static_cast<int>(i);
  return -1;
}

const Window* Engine::window(int coin) const noexcept {
  if (coin < 0 || static_cast<std::size_t>(coin) >= coins_.size()) return nullptr;
  const auto& w = coins_[static_cast<std::size_t>(coin)].window;
  return w ? &*w : nullptr;
}

int Engine::coin_for_ticker(std::string_view ticker) const noexcept {
  for (std::size_t i = 0; i < coins_.size(); ++i)
    if (coins_[i].window && coins_[i].window->kalshi_ticker == ticker) return static_cast<int>(i);
  return -1;
}

std::pair<int, bool> Engine::coin_for_token(std::string_view token) const noexcept {
  const auto it = owner_.find(token);
  return it == owner_.end() ? std::pair<int, bool>{-1, false} : it->second;
}

void Engine::note(std::string line) { log_.push_back(std::move(line)); }

void Engine::set_window(const Window& w, std::int64_t t_ns) {
  const int i = coin_of(w.asset);
  if (i < 0) return;
  auto& c = coins_[static_cast<std::size_t>(i)];
  if (c.window && *c.window == w) return;
  if (c.window) {
    owner_.erase(c.window->up_token);
    owner_.erase(c.window->down_token);
  }
  owner_[w.up_token] = {i, true};
  owner_[w.down_token] = {i, false};
  c.kbook.invalidate();
  c.upb.invalidate();
  c.downb.invalidate();
  c.trail.clear();
  c.pause_until_ms = 0;
  c.window = w;
  c.key = window_key(w.asset, w.open_unix);
  ++counters_.windows;
  (void)t_ns;
}

void Engine::invalidate_kalshi() noexcept {
  for (auto& c : coins_) c.kbook.invalidate();
}

void Engine::invalidate_poly(int coin) noexcept {
  for (std::size_t i = 0; i < coins_.size(); ++i) {
    if (coin >= 0 && static_cast<std::size_t>(coin) != i) continue;
    coins_[i].upb.invalidate();
    coins_[i].downb.invalidate();
  }
}

std::optional<double> Engine::fair(const Coin& c, std::int64_t now_ms) const noexcept {
  const Quote q = c.kbook.quote();
  if (!q.valid) return std::nullopt;
  const bool fresh = c.kbook.last_update_ns() != 0 && now_ms - c.kbook.last_update_ns() / 1'000'000 <= cfg_.kalshi_max_age_ms;
  if (q.ask >= q.bid && q.ask - q.bid <= cfg_.max_kalshi_spread + 1e-9 && fresh) return (q.ask + q.bid) / 2.0;
  return std::nullopt;
}

void Engine::on_kalshi_update(int coin, std::int64_t t_ns) {
  const auto i = static_cast<std::size_t>(coin);
  auto& c = coins_[i];
  const auto mid = c.kbook.quote().mid();
  if (mid) {
    const double m = *mid;
    const std::int64_t now_ms = t_ns / 1'000'000;
    double moved = 0;
    for (const auto& [ts, v] : c.trail)
      if (ts >= now_ms - 1000) moved = std::max(moved, std::abs(m - v));
    c.trail.emplace_back(now_ms, m);
    while (!c.trail.empty() && c.trail.front().first < now_ms - kTrailMs) c.trail.pop_front();
    if (moved >= cfg_.pause_jump - 1e-9) {
      if (c.pause_until_ms < now_ms) ++counters_.pauses;
      c.pause_until_ms = now_ms + cfg_.pause_ms;
    }
  }
  tick_coin(i, t_ns);
}

void Engine::on_poly_update(int coin, std::int64_t t_ns) { tick_coin(static_cast<std::size_t>(coin), t_ns); }

void Engine::tick(std::int64_t t_ns) {
  for (std::size_t i = 0; i < coins_.size(); ++i) tick_coin(i, t_ns);
}

bool Engine::take_token(std::int64_t now_ms) noexcept {
  const double rate = cfg_.max_orders_per_s;
  if (bucket_ms_ > 0) bucket_ = std::min(rate, bucket_ + static_cast<double>(now_ms - bucket_ms_) / 1000.0 * rate);
  bucket_ms_ = now_ms;
  if (bucket_ >= 1.0) {
    bucket_ -= 1.0;
    return true;
  }
  return false;
}

double Engine::net_of(std::string_view key) const noexcept {
  const auto it = inv_.find(key);
  return it == inv_.end() ? 0.0 : it->second.net();
}

double Engine::open_cost() const {
  double held = 0;
  for (const auto& [k, v] : inv_)
    if (v.settled.empty()) held += v.cost();
  double resting = 0;
  for (const auto& [k, o] : orders_)
    if (!o.closing) resting += o.px * std::max(0.0, o.size - o.matched);
  return held + resting;
}

void Engine::tick_coin(std::size_t i, std::int64_t t_ns) {
  const std::int64_t now_ms = t_ns / 1'000'000;
  const bool is_halted = halted(t_ns);
  const double cost = open_cost();
  auto& c = coins_[i];
  const double net = c.window ? net_of(c.key) : 0.0;
  // Average cost of what this window already holds on each side (pair guard).
  std::array<std::optional<double>, 2> held_avg{};
  if (c.window) {
    if (const auto it = inv_.find(c.key); it != inv_.end()) {
      const auto& v = it->second;
      if (v.up > 1e-9) held_avg[0] = v.up_cost / v.up;
      if (v.down > 1e-9) held_avg[1] = v.down_cost / v.down;
    }
  }
  std::array<std::optional<std::pair<double, double>>, 2> want{};
  std::array<std::optional<std::pair<double, double>>, 2> hint{};
  const auto q = fair(c, now_ms);
  if (stopped_) {
    c.gate = "stopped";
  } else if (is_halted) {
    c.gate = "halted";
  } else if (gates_.kill) {
    c.gate = "kill";
  } else if (!gates_.feeds_ok) {
    c.gate = "feeds stale";
  } else if (!c.window) {
    c.gate = "no window";
  } else {
    const std::int64_t s = t_ns / kNs - c.window->open_unix;
    // What the rule would bid with the books as they are, before the time gates.
    std::array<std::optional<std::pair<double, double>>, 2> rule{};
    bool books_ok = false;
    if (q) {
      const auto ua = c.upb.quote().valid ? c.upb.best_ask() : std::nullopt;
      const auto da = c.downb.quote().valid ? c.downb.best_ask() : std::nullopt;
      books_ok = ua && da;
      if (books_ok) {
        const auto [u, d] = desired_px(cfg_, *q, net, ua, da);
        const std::array<std::optional<double>, 2> pxs{u, d};
        for (std::size_t n = 0; n < 2; ++n) {
          const bool up = n == 0;
          if (opens(net, up) && !first_leg_ok(cfg_, s, *q)) continue;
          const auto px = pair_cap(cfg_, pxs[n], held_avg[1 - n]);
          if (!px) continue;
          const auto size = room(cfg_, net, up);
          if (!size) continue;
          // The resting order's own notional is already in open cost.
          const double mine = c.slots[n].order.empty() ? 0.0 : c.slots[n].px * c.slots[n].remaining;
          if (cost - mine + *px * *size > cfg_.max_open_cost_usd + 1e-9) continue;
          rule[n] = std::pair<double, double>{*px, *size};
        }
      }
    }
    hint = rule;
    if (s < cfg_.entry_early_s) {
      c.gate = "window start";
    } else if (s >= kWindowS - cfg_.stop_s) {
      c.gate = "window end";
      hint = {};
    } else if (now_ms < c.pause_until_ms) {
      c.gate = "pause";
    } else if (!q) {
      c.gate = "no fair";
    } else if (!books_ok) {
      c.gate = "no poly book";
    } else {
      c.gate = "quoting";
      want = rule;
    }
  }
  for (std::size_t n = 0; n < 2; ++n) {
    c.slots[n].want = want[n];
    // The signer prepares for what the slot will want once a pause or the window start ends.
    c.slots[n].hint = want[n] ? want[n] : hint[n];
    step_slot(i, n, want[n], now_ms);
  }
}

void Engine::step_slot(std::size_t i, std::size_t n, std::optional<std::pair<double, double>> want, std::int64_t now_ms) {
  auto& c = coins_[i];
  auto& s = c.slots[n];
  const bool up = n == 0;
  if (s.inflight || (now_ms < s.cooldown_until_ms && s.order.empty())) return;
  if (!s.order.empty()) {
    const auto it = orders_.find(s.order);
    const bool stale_window = it != orders_.end() && (!c.window || it->second.key != c.key);
    const double step = cfg_.requote_cents * kTick;
    bool cancel = false;
    if (!want || stale_window) {
      cancel = true;
    } else {
      const auto [px, size] = *want;
      std::int8_t dir = 0;
      if (s.px > px + 1e-9) dir = 1;
      else if (px - s.px >= step - 1e-9) dir = -1;
      if (dir == 0) {
        s.has_off = false;
      } else if (!s.has_off || s.off_dir != dir) {
        s.has_off = true;
        s.off_dir = dir;
        s.off_since_ms = now_ms;
      }
      const std::int64_t held = s.has_off ? now_ms - s.off_since_ms : 0;
      const bool too_high = dir == 1 && held >= cfg_.adverse_ms;
      const bool raise = dir == -1 && held >= cfg_.min_requote_ms && now_ms - s.last_change_ms >= cfg_.min_requote_ms;
      cancel = s.remaining > size + 1e-9 || too_high || raise;
    }
    if (cancel && now_ms >= s.cooldown_until_ms) {
      s.has_off = false;
      s.inflight = true;
      Action a;
      a.kind = ActionKind::cancel;
      a.coin = static_cast<int>(i);
      a.up = up;
      a.order_id = s.order;
      actions_.push_back(std::move(a));
      ++counters_.cancels;
    }
    return;
  }
  if (!want) return;
  const auto [px, size] = *want;
  if (cash_hint_ && now_ms < cash_hint_->second) {
    if (px * size > cash_hint_->first + 1e-9) {
      ++counters_.cash_waits;
      return;
    }
    cash_hint_->first -= px * size;
  }
  if (!take_token(now_ms)) {
    ++counters_.rate_limited;
    return;
  }
  if (!c.window) return;
  s.inflight = true;
  s.px = px;
  s.remaining = size;
  s.last_change_ms = now_ms;
  Action a;
  a.kind = ActionKind::post;
  a.coin = static_cast<int>(i);
  a.up = up;
  a.px = px;
  a.size = size;
  actions_.push_back(std::move(a));
  ++counters_.posts;
}

Engine::Slot* Engine::slot(int coin, bool up) noexcept {
  if (coin < 0 || static_cast<std::size_t>(coin) >= coins_.size()) return nullptr;
  return &coins_[static_cast<std::size_t>(coin)].slots[up ? 0 : 1];
}

void Engine::on_posted(int coin, bool up, double px, double size, bool ok, std::string_view id_or_error, std::int64_t t_ns) {
  const std::int64_t now_ms = t_ns / 1'000'000;
  Slot* s = slot(coin, up);
  if (s == nullptr) return;
  s->inflight = false;
  const auto& c = coins_[static_cast<std::size_t>(coin)];
  if (ok) {
    s->order = std::string(id_or_error);
    s->last_change_ms = now_ms;
    OrderRec rec;
    rec.id = s->order;
    rec.key = c.window ? c.key : std::string();
    rec.coin = coin;
    rec.up = up;
    rec.px = px;
    rec.size = size;
    rec.posted_ns = t_ns;
    orders_[rec.id] = rec;
    dirty = true;
    return;
  }
  s->order.clear();
  s->cooldown_until_ms = now_ms + cfg_.reject_cooldown_ms;
  double free = 0;
  if (id_or_error.find("cross") != std::string_view::npos) {
    ++counters_.post_only_rejects;
  } else if (clob::free_balance(id_or_error, free)) {
    cash_hint_ = std::pair<double, std::int64_t>{free, now_ms + kCashHoldMs};
    ++counters_.balance_rejects;
  } else {
    ++counters_.post_errors;
  }
  note("reject " + c.name + (up ? " up " : " down ") + fixed(px, 2) + " x" + fixed(size, 0) + ": " + std::string(id_or_error));
}

void Engine::on_cancelled(int coin, bool up, std::string_view order_id, bool ok, std::int64_t t_ns) {
  const std::int64_t now_ms = t_ns / 1'000'000;
  Slot* s = slot(coin, up);
  if (s == nullptr) return;
  s->inflight = false;
  if (ok) {
    if (s->order == order_id) {
      s->order.clear();
      s->last_change_ms = now_ms;
    }
    if (const auto it = orders_.find(order_id); it != orders_.end()) it->second.closing = true;
    return;
  }
  s->cooldown_until_ms = now_ms + cfg_.reject_cooldown_ms;
  ++counters_.cancel_errors;
}

std::vector<std::string> Engine::tracked() const {
  std::vector<std::string> out;
  out.reserve(orders_.size());
  for (const auto& [id, o] : orders_) out.push_back(id);
  return out;
}

void Engine::on_order(std::string_view id, double matched, bool done, std::int64_t t_ns) {
  const auto it = orders_.find(id);
  if (it == orders_.end()) return;
  const OrderRec o = it->second;
  const double delta = matched - o.matched;
  if (delta > 1e-6) {
    book_fill(o, delta, t_ns);
    it->second.matched = matched;
  }
  if (Slot* s = slot(o.coin, o.up); s != nullptr && s->order == id) {
    if (done) {
      s->order.clear();
      s->inflight = false;
      s->last_change_ms = t_ns / 1'000'000;
    } else {
      s->remaining = std::max(0.0, o.size - matched);
    }
  }
  if (done) orders_.erase(it);
  if (delta > 1e-6 || done) dirty = true;
}

void Engine::on_unknown(std::string_view id, std::int64_t t_ns) {
  const auto it = orders_.find(id);
  if (it == orders_.end()) return;
  if (t_ns - it->second.posted_ns > 10 * kNs) {
    ++counters_.orders_unknown;
    note("order " + std::string(id) + " unknown to the venue; closed at " + fixed(it->second.matched, 2) + " matched");
    on_order(id, it->second.matched, true, t_ns);
  }
}

void Engine::book_fill(const OrderRec& o, double shares, std::int64_t t_ns) {
  const auto& c = coins_[static_cast<std::size_t>(o.coin)];
  const auto dash = o.key.rfind('-');
  const std::int64_t open_unix = dash == std::string::npos ? 0 : std::atoll(o.key.c_str() + dash + 1);
  auto [it, inserted] = inv_.try_emplace(o.key);
  auto& inv = it->second;
  if (inserted) {
    inv.asset = c.name;
    inv.open_unix = open_unix;
    if (c.window && c.window->open_unix == open_unix) {
      inv.condition_id = c.window->condition_id;
      inv.up_token = c.window->up_token;
      inv.down_token = c.window->down_token;
    }
  }
  if (o.up) {
    inv.up += shares;
    inv.up_cost += shares * o.px;
  } else {
    inv.down += shares;
    inv.down_cost += shares * o.px;
  }
  ++inv.fills;
  ++counters_.fills;
  const auto q = fair(c, t_ns / 1'000'000);
  std::string line = "fill " + o.key + (o.up ? " up " : " down ") + fixed(shares, 2) + " @ " + fixed(o.px, 2) +
                     " net " + fixed(inv.net(), 2) + " age_ms " + std::to_string((t_ns - o.posted_ns) / 1'000'000);
  if (q) line += " fair " + fixed(*q, 4);
  note(std::move(line));
}

std::vector<Inventory> Engine::unsettled(std::int64_t t_ns) const {
  std::vector<Inventory> out;
  for (const auto& [k, v] : inv_)
    if (v.settled.empty() && t_ns >= v.close_ns() + 30 * kNs) out.push_back(v);
  return out;
}

void Engine::settle(std::string_view asset, std::int64_t open_unix, std::string_view outcome, std::int64_t t_ns) {
  const std::string key = window_key(asset, open_unix);
  // Fills still unconfirmed for this window wait for the order poll.
  for (const auto& [id, o] : orders_)
    if (o.key == key) return;
  const auto it = inv_.find(key);
  if (it == inv_.end() || !it->second.settled.empty()) return;
  auto& v = it->second;
  const double pnl = v.settle_pnl(outcome);
  v.settled = std::string(outcome);
  v.pnl = pnl;
  realised_by_day_[utc_day(t_ns)] += pnl;
  dirty = true;
  note("settle " + key + " " + std::string(outcome) + " up " + fixed(v.up, 2) + "/" + fixed(v.up_cost, 2) + " down " +
       fixed(v.down, 2) + "/" + fixed(v.down_cost, 2) + " pnl " + fixed(pnl, 4) + " today " + fixed(today_realised(t_ns), 4));
  if (today_realised(t_ns) <= -cfg_.daily_loss_usd + 1e-9)
    halt("daily loss " + fixed(today_realised(t_ns), 2) + " reached the " + fixed(cfg_.daily_loss_usd, 2) + " limit", t_ns);
}

std::vector<std::pair<std::string, std::string>> Engine::redeem_due() const {
  std::vector<std::pair<std::string, std::string>> out;
  for (const auto& [k, v] : inv_) {
    if (!v.redeemed.empty() || v.redeem_tries >= 20 || v.condition_id.empty()) continue;
    if ((v.settled == "yes" && v.up > 0) || (v.settled == "no" && v.down > 0)) out.emplace_back(k, v.condition_id);
  }
  return out;
}

void Engine::on_redeemed(std::string_view key, bool ok, std::string_view tx_or_error, std::int64_t t_ns) {
  const auto it = inv_.find(key);
  if (it == inv_.end()) return;
  ++it->second.redeem_tries;
  if (ok) it->second.redeemed = std::string(tx_or_error);
  note(std::string(ok ? "redeemed " : "redeem failed ") + std::string(key) + ": " + std::string(tx_or_error) + " (try " +
       std::to_string(it->second.redeem_tries) + ")");
  dirty = true;
  (void)t_ns;
}

std::map<std::string, double> Engine::held_tokens(std::int64_t t_ns) const {
  std::map<std::string, double> m;
  for (const auto& [k, v] : inv_) {
    if (!v.settled.empty() || t_ns >= v.close_ns()) continue;
    if (!v.up_token.empty()) m[v.up_token] += v.up;
    if (!v.down_token.empty()) m[v.down_token] += v.down;
  }
  return m;
}

void Engine::on_balance(std::string_view token, double wallet, std::int64_t t_ns) {
  const auto held = held_tokens(t_ns);
  const auto h = held.find(std::string(token));
  if (h == held.end()) {
    if (const auto it = drift_.find(token); it != drift_.end()) drift_.erase(it);
    return;
  }
  const double ours = h->second;
  const double d = wallet - ours;
  auto& e = drift_[std::string(token)];
  if (d < -0.5) ++e.first;
  else e.first = 0;
  e.second = d;
  if (std::abs(d) > 0.5) {
    ++counters_.drift_checks;
    note("drift " + std::string(token) + " wallet " + fixed(wallet, 2) + " ours " + fixed(ours, 2) + " streak " +
         std::to_string(e.first));
  }
  if (e.first >= 3)
    halt("inventory drift: wallet " + fixed(wallet, 2) + " < booked " + fixed(ours, 2) + " on " + std::string(token), t_ns);
}

bool Engine::halted(std::int64_t t_ns) const { return halt_ && halt_->day == utc_day(t_ns); }

void Engine::halt(std::string_view reason, std::int64_t t_ns) {
  if (halted(t_ns)) return;
  halt_ = Halt{utc_day(t_ns), std::string(reason), t_ns};
  Action a;
  a.kind = ActionKind::cancel_all;
  a.reason = std::string(reason);
  actions_.push_back(std::move(a));
  dirty = true;
  note("HALT: " + std::string(reason));
}

void Engine::ack(std::int64_t t_ns) {
  const std::string day = utc_day(t_ns);
  const auto it = realised_by_day_.find(day);
  const double r = it == realised_by_day_.end() ? 0.0 : it->second;
  ack_ = std::pair<std::string, double>{day, r};
  halt_.reset();
  dirty = true;
  note("ack: halt cleared, realised today " + fixed(r, 4));
}

double Engine::today_realised(std::int64_t t_ns) const {
  const std::string day = utc_day(t_ns);
  const auto it = realised_by_day_.find(day);
  const double r = it == realised_by_day_.end() ? 0.0 : it->second;
  if (ack_ && ack_->first == day) return r - ack_->second;
  return r;
}

std::optional<std::pair<std::string, double>> Engine::resting(int coin, bool up) const {
  if (coin < 0 || static_cast<std::size_t>(coin) >= coins_.size()) return std::nullopt;
  const auto& s = coins_[static_cast<std::size_t>(coin)].slots[up ? 0 : 1];
  if (s.order.empty()) return std::nullopt;
  return std::pair<std::string, double>{s.order, s.px};
}

std::optional<std::pair<double, double>> Engine::target(int coin, bool up) const noexcept {
  if (coin < 0 || static_cast<std::size_t>(coin) >= coins_.size()) return std::nullopt;
  return coins_[static_cast<std::size_t>(coin)].slots[up ? 0 : 1].hint;
}

std::string Engine::save_json() const {
  std::string out = "{\"inventories\":{";
  bool first = true;
  for (const auto& [k, v] : inv_) {
    if (!first) out.push_back(',');
    first = false;
    json::append_string(out, k);
    out.append(":{\"asset\":");
    json::append_string(out, v.asset);
    out.append(",\"open_unix\":").append(std::to_string(v.open_unix));
    out.append(",\"condition_id\":");
    json::append_string(out, v.condition_id);
    out.append(",\"up_token\":");
    json::append_string(out, v.up_token);
    out.append(",\"down_token\":");
    json::append_string(out, v.down_token);
    out.append(",\"up\":");
    put_number(out, v.up);
    out.append(",\"up_cost\":");
    put_number(out, v.up_cost);
    out.append(",\"down\":");
    put_number(out, v.down);
    out.append(",\"down_cost\":");
    put_number(out, v.down_cost);
    out.append(",\"fills\":").append(std::to_string(v.fills));
    out.append(",\"settled\":");
    json::append_string(out, v.settled);
    out.append(",\"pnl\":");
    put_number(out, v.pnl);
    out.append(",\"redeemed\":");
    json::append_string(out, v.redeemed);
    out.append(",\"redeem_tries\":").append(std::to_string(v.redeem_tries)).append("}");
  }
  out.append("},\"orders\":{");
  first = true;
  for (const auto& [id, o] : orders_) {
    if (!first) out.push_back(',');
    first = false;
    json::append_string(out, id);
    out.append(":{\"key\":");
    json::append_string(out, o.key);
    out.append(",\"asset\":");
    json::append_string(out, coins_[static_cast<std::size_t>(o.coin)].name);
    out.append(o.up ? ",\"up\":true" : ",\"up\":false");
    out.append(",\"px\":");
    put_number(out, o.px);
    out.append(",\"size\":");
    put_number(out, o.size);
    out.append(",\"matched\":");
    put_number(out, o.matched);
    out.append(",\"posted_ns\":").append(std::to_string(o.posted_ns));
    out.append(o.closing ? ",\"closing\":true}" : ",\"closing\":false}");
  }
  out.append("},\"realised_by_day\":{");
  first = true;
  for (const auto& [day, r] : realised_by_day_) {
    if (!first) out.push_back(',');
    first = false;
    json::append_string(out, day);
    out.push_back(':');
    put_number(out, r);
  }
  out.append("}");
  if (halt_) {
    out.append(",\"halt\":{\"day\":");
    json::append_string(out, halt_->day);
    out.append(",\"reason\":");
    json::append_string(out, halt_->reason);
    out.append(",\"t_ns\":").append(std::to_string(halt_->t_ns)).append("}");
  }
  if (ack_) {
    out.append(",\"ack\":{\"day\":");
    json::append_string(out, ack_->first);
    out.append(",\"realised\":");
    put_number(out, ack_->second);
    out.append("}");
  }
  out.append("}\n");
  return out;
}

bool Engine::load_json(std::string_view text, std::string& error) {
  json::Document doc;
  if (!doc.parse(text)) {
    error = "state is not JSON";
    return false;
  }
  const auto root = doc.root();
  inv_.clear();
  orders_.clear();
  realised_by_day_.clear();
  for (const auto v : root["inventories"]) {
    Inventory inv;
    inv.asset = v["asset"].string();
    inv.open_unix = v["open_unix"].integer_or(0);
    inv.condition_id = v["condition_id"].string();
    inv.up_token = v["up_token"].string();
    inv.down_token = v["down_token"].string();
    inv.up = v["up"].number_or(0);
    inv.up_cost = v["up_cost"].number_or(0);
    inv.down = v["down"].number_or(0);
    inv.down_cost = v["down_cost"].number_or(0);
    inv.fills = static_cast<std::uint64_t>(v["fills"].integer_or(0));
    inv.settled = v["settled"].string();
    inv.pnl = v["pnl"].number_or(0);
    inv.redeemed = v["redeemed"].string();
    inv.redeem_tries = static_cast<std::uint32_t>(v["redeem_tries"].integer_or(0));
    inv_[std::string(v.key())] = inv;
  }
  for (const auto o : root["orders"]) {
    OrderRec rec;
    rec.id = std::string(o.key());
    rec.key = o["key"].string();
    rec.coin = coin_of(o["asset"].string());
    if (rec.coin < 0) continue;
    rec.up = o["up"].boolean_or(true);
    rec.px = o["px"].number_or(0);
    rec.size = o["size"].number_or(0);
    rec.matched = o["matched"].number_or(0);
    rec.posted_ns = o["posted_ns"].integer_or(0);
    rec.closing = o["closing"].boolean_or(false);
    orders_[rec.id] = rec;
  }
  for (const auto r : root["realised_by_day"]) realised_by_day_[std::string(r.key())] = r.number_or(0);
  if (const auto h = root["halt"]; h.is_object()) halt_ = Halt{h["day"].string(), h["reason"].string(), h["t_ns"].integer_or(0)};
  if (const auto a = root["ack"]; a.is_object()) ack_ = std::pair<std::string, double>{a["day"].string(), a["realised"].number_or(0)};
  return true;
}

}  // namespace pkm::engine
