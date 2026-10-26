#include "pkm/feeds.hpp"

#include <charconv>

namespace pkm::feeds {
namespace {

void append_list(std::string& out, const std::vector<std::string>& items) {
  out.push_back('[');
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i > 0) out.push_back(',');
    json::append_string(out, items[i]);
  }
  out.push_back(']');
}


// Kalshi levels: ["0.5300","5.00"] pairs (dollars) or [53, 5] (cents).
template <typename F>
void kalshi_levels(const json::Value& msg, bool yes, F&& put) {
  const auto fp = msg[yes ? "yes_dollars_fp" : "no_dollars_fp"];
  const auto dollars = fp ? fp : msg[yes ? "yes_dollars" : "no_dollars"];
  if (dollars.is_array()) {
    for (const auto pair : dollars) put(pair.at(0).number_or(-1), pair.at(1).number_or(0));
    return;
  }
  for (const auto pair : msg[yes ? "yes" : "no"])
    put(static_cast<double>(static_cast<std::int64_t>(pair.at(0).number_or(-100))) / 100.0, pair.at(1).number_or(0));
}

std::int64_t days_from_civil(std::int64_t y, unsigned m, unsigned d) noexcept {
  y -= m <= 2;
  const std::int64_t era = (y >= 0 ? y : y - 399) / 400;
  const auto yoe = static_cast<unsigned>(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + static_cast<std::int64_t>(doe) - 719468;
}

bool digits(std::string_view s, std::size_t at, std::size_t n, int& out) noexcept {
  if (at + n > s.size()) return false;
  out = 0;
  for (std::size_t i = at; i < at + n; ++i) {
    if (s[i] < '0' || s[i] > '9') return false;
    out = out * 10 + (s[i] - '0');
  }
  return true;
}

}  // namespace

bool iso_to_unix(std::string_view s, std::int64_t& out) noexcept {
  int y = 0, mo = 0, d = 0, h = 0, mi = 0, se = 0;
  if (!digits(s, 0, 4, y) || s.size() < 19 || s[4] != '-' || !digits(s, 5, 2, mo) || s[7] != '-' || !digits(s, 8, 2, d) ||
      (s[10] != 'T' && s[10] != ' ') || !digits(s, 11, 2, h) || s[13] != ':' || !digits(s, 14, 2, mi) || s[16] != ':' ||
      !digits(s, 17, 2, se))
    return false;
  std::size_t at = 19;
  if (at < s.size() && s[at] == '.')
    for (++at; at < s.size() && s[at] >= '0' && s[at] <= '9';) ++at;
  std::int64_t offset = 0;
  if (at < s.size() && (s[at] == '+' || s[at] == '-')) {
    int oh = 0, om = 0;
    if (!digits(s, at + 1, 2, oh)) return false;
    const std::size_t m_at = (at + 3 < s.size() && s[at + 3] == ':') ? at + 4 : at + 3;
    if (!digits(s, m_at, 2, om)) om = 0;
    offset = (oh * 3600 + om * 60) * (s[at] == '+' ? 1 : -1);
  } else if (at < s.size() && s[at] != 'Z') {
    return false;
  }
  out = days_from_civil(y, static_cast<unsigned>(mo), static_cast<unsigned>(d)) * 86400 + h * 3600 + mi * 60 + se - offset;
  return true;
}

void KalshiSession::reset() noexcept {
  sid_ = -1;
  subscribe_in_flight_ = false;
  seq_.clear();
  subscribed_.clear();
  pending_add_.clear();
  last_error.clear();
}

std::vector<std::string> KalshiSession::sync(const std::set<std::string>& wanted) {
  std::vector<std::string> out;
  std::vector<std::string> add;
  std::vector<std::string> remove;
  for (const auto& t : wanted)
    if (!subscribed_.contains(t) && !pending_add_.contains(t)) add.push_back(t);
  for (const auto& t : subscribed_)
    if (!wanted.contains(t)) remove.push_back(t);
  if (!add.empty()) {
    if (sid_ < 0 && subscribe_in_flight_) {
      // One sid only: add once the first subscribe is acknowledged.
      pending_add_.insert(add.begin(), add.end());
    } else {
      std::string body = "{\"id\":" + std::to_string(next_id());
      if (sid_ < 0) {
        subscribe_in_flight_ = true;
        body += ",\"cmd\":\"subscribe\",\"params\":{\"channels\":[\"orderbook_delta\"],\"market_tickers\":";
        append_list(body, add);
        body += "}}";
      } else {
        body += ",\"cmd\":\"update_subscription\",\"params\":{\"sids\":[" + std::to_string(sid_) + "],\"market_tickers\":";
        append_list(body, add);
        body += ",\"action\":\"add_markets\"}}";
      }
      out.push_back(std::move(body));
      subscribed_.insert(add.begin(), add.end());
    }
  }
  if (!remove.empty() && sid_ >= 0) {
    std::string body = "{\"id\":" + std::to_string(next_id()) + ",\"cmd\":\"update_subscription\",\"params\":{\"sids\":[" +
                       std::to_string(sid_) + "],\"market_tickers\":";
    append_list(body, remove);
    body += ",\"action\":\"delete_markets\"}}";
    out.push_back(std::move(body));
    for (const auto& t : remove) subscribed_.erase(t);
  }
  return out;
}

KalshiSession::Verdict KalshiSession::on_frame(const json::Value& root, std::vector<std::string>& to_send) {
  const auto kind = root["type"].raw();
  if (kind == "subscribed") {
    const std::int64_t sid = root["msg"]["sid"].integer_or(-1);
    if (sid >= 0) sid_ = sid;
    subscribe_in_flight_ = false;
    if (!pending_add_.empty()) {
      std::set<std::string> wanted = subscribed_;
      wanted.insert(pending_add_.begin(), pending_add_.end());
      pending_add_.clear();
      for (auto& m : sync(wanted)) to_send.push_back(std::move(m));
    }
  } else if (kind == "error") {
    last_error = "kalshi error: " + root["msg"]["msg"].string();
    if (last_error.size() <= 14) last_error = "kalshi error: " + std::to_string(root["msg"]["code"].integer_or(0));
    // A refused update leaves the subscription unknown: start over.
    return Verdict::error;
  }
  const auto seq = root["seq"];
  const auto sid = root["sid"];
  if (seq.is_number() && sid.is_number()) {
    const std::int64_t s = seq.integer_or(0);
    const std::int64_t id = sid.integer_or(0);
    if (const auto it = seq_.find(id); it != seq_.end() && s != it->second + 1) {
      last_error = "kalshi seq gap on sid " + std::to_string(id) + ": expected " + std::to_string(it->second + 1) +
                   ", got " + std::to_string(s);
      return Verdict::gap;
    }
    seq_[id] = s;
  }
  return Verdict::ok;
}

int apply_kalshi(const json::Value& root, engine::Engine& engine, std::int64_t recv_ns) {
  const auto kind = root["type"].raw();
  const bool snapshot = kind == "orderbook_snapshot";
  if (!snapshot && kind != "orderbook_delta") return -1;
  const auto msg = root["msg"];
  const int coin = engine.coin_for_ticker(msg["market_ticker"].raw());
  if (coin < 0) return -1;
  Book& book = engine.kalshi_book(coin);
  if (snapshot) {
    book.begin_snapshot(recv_ns);
    kalshi_levels(msg, true, [&](double p, double s) {
      if (p >= 0 && s > 0) book.set(Side::bid, px_key(p), s, recv_ns);
    });
    kalshi_levels(msg, false, [&](double p, double s) {
      if (p >= 0 && s > 0) book.set(Side::ask, px_key(1.0 - p), s, recv_ns);
    });
    return coin;
  }
  if (!book.has_snapshot()) return -1;
  double price = 0;
  if (const auto pd = msg["price_dollars"]; pd) {
    if (!pd.number(price)) return -1;
  } else if (const auto pc = msg["price"]; pc) {
    if (!pc.number(price)) return -1;
    price /= 100.0;
  } else {
    return -1;
  }
  double delta = 0;
  const auto dfp = msg["delta_fp"];
  if (!(dfp ? dfp : msg["delta"]).number(delta)) return -1;
  if (msg["side"].raw() == "yes") book.add(Side::bid, px_key(price), delta, recv_ns);
  else book.add(Side::ask, px_key(1.0 - price), delta, recv_ns);
  return coin;
}

namespace {

void apply_poly_one(const json::Value& msg, engine::Engine& engine, std::int64_t recv_ns, std::vector<char>& touched,
                    std::vector<Resolution>* resolved) {
  if (!msg.is_object()) return;
  std::string_view kind = msg["event_type"].raw();
  if (kind.empty() && msg["bids"]) kind = "book";
  if (kind == "book") {
    const auto [coin, up] = engine.coin_for_token(msg["asset_id"].raw());
    if (coin < 0) return;
    Book& b = engine.poly_book(coin, up);
    b.begin_snapshot(recv_ns);
    for (const auto l : msg["bids"]) {
      const double s = l["size"].number_or(0);
      if (s > 0) b.set(Side::bid, px_key(l["price"].number_or(0)), s, recv_ns);
    }
    for (const auto l : msg["asks"]) {
      const double s = l["size"].number_or(0);
      if (s > 0) b.set(Side::ask, px_key(l["price"].number_or(0)), s, recv_ns);
    }
    touched[static_cast<std::size_t>(coin)] = 1;
  } else if (kind == "price_change") {
    for (const auto ch : msg["price_changes"]) {
      const auto [coin, up] = engine.coin_for_token(ch["asset_id"].raw());
      if (coin < 0) continue;
      Book& b = engine.poly_book(coin, up);
      if (!b.has_snapshot()) continue;
      double price = 0;
      double size = 0;
      if (!ch["price"].number(price) || !ch["size"].number(size)) continue;
      b.set(ch["side"].raw() == "BUY" ? Side::bid : Side::ask, px_key(price), size, recv_ns);
      touched[static_cast<std::size_t>(coin)] = 1;
    }
  } else if (kind == "market_resolved" && resolved != nullptr) {
    resolved->push_back({msg["market"].string(), msg["winning_asset_id"].string()});
  }
}

}  // namespace

void apply_poly(const json::Value& root, engine::Engine& engine, std::int64_t recv_ns, std::vector<char>& touched,
                std::vector<Resolution>* resolved) {
  if (touched.size() < engine.coins()) touched.assign(engine.coins(), 0);
  if (root.is_array()) {
    for (const auto m : root) apply_poly_one(m, engine, recv_ns, touched, resolved);
  } else {
    apply_poly_one(root, engine, recv_ns, touched, resolved);
  }
}

std::string poly_subscribe(const std::vector<std::string>& tokens, bool initial) {
  std::string out = "{\"assets_ids\":";
  append_list(out, tokens);
  out += initial ? ",\"type\":\"market\"" : ",\"operation\":\"subscribe\"";
  out += ",\"custom_feature_enabled\":true}";
  return out;
}

std::string poly_unsubscribe(const std::vector<std::string>& tokens) {
  std::string out = "{\"assets_ids\":";
  append_list(out, tokens);
  out += ",\"operation\":\"unsubscribe\",\"custom_feature_enabled\":true}";
  return out;
}

std::string user_subscribe(std::string_view api_key, std::string_view secret_b64, std::string_view passphrase,
                           const std::vector<std::string>& condition_ids) {
  std::string out = "{\"auth\":{\"apiKey\":";
  json::append_string(out, api_key);
  out += ",\"secret\":";
  json::append_string(out, secret_b64);
  out += ",\"passphrase\":";
  json::append_string(out, passphrase);
  out += "},\"markets\":";
  append_list(out, condition_ids);
  out += ",\"type\":\"user\"}";
  return out;
}

namespace {

void parse_user_one(const json::Value& m, std::vector<OrderUpdate>& out) {
  const auto kind = m["event_type"].raw();
  if (kind == "order") {
    OrderUpdate u;
    u.id = m["id"].string();
    if (u.id.empty()) return;
    u.has_matched = m["size_matched"].number(u.matched);
    double original = 0;
    const bool has_original = m["original_size"].number(original);
    const auto type = m["type"].raw();
    u.done = type == "CANCELLATION" || (u.has_matched && has_original && original > 0 && u.matched >= original - 1e-9);
    out.push_back(std::move(u));
  } else if (kind == "trade") {
    // Per-trade amounts, not totals: a hint to read those orders now.
    for (const auto mo : m["maker_orders"]) {
      OrderUpdate u;
      u.id = mo["order_id"].string();
      if (!u.id.empty()) out.push_back(std::move(u));
    }
    OrderUpdate taker;
    taker.id = m["taker_order_id"].string();
    if (!taker.id.empty()) out.push_back(std::move(taker));
  }
}

}  // namespace

void parse_user(const json::Value& root, std::vector<OrderUpdate>& out) {
  if (root.is_array()) {
    for (const auto m : root) parse_user_one(m, out);
  } else if (root.is_object()) {
    parse_user_one(root, out);
  }
}

}  // namespace pkm::feeds
