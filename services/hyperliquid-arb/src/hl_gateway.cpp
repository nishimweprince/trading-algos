#include "hla/hl_gateway.hpp"

#include "hla/hl_parser.hpp"
#include "hla/json.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <thread>

namespace hla {
namespace {

constexpr std::string_view kExchangeUrl = "https://api.hyperliquid.xyz/exchange";
constexpr std::uint64_t kPingNs = 30'000'000'000ULL;
constexpr std::uint64_t kReconnectNs = 1'000'000'000ULL;
constexpr std::uint64_t kEngineAliveNs = 2'000'000'000ULL;

AckItem parse_status(std::string_view item, HlEvent& out) {
  AckItem ack;
  if (item.starts_with('"')) {
    ack.status = AckStatus::success;
    return ack;
  }
  if (item.find("\"resting\"") != std::string_view::npos) {
    ack.status = AckStatus::resting;
    json::u64(item, "\"oid\"", ack.oid);
  } else if (item.find("\"filled\"") != std::string_view::npos) {
    ack.status = AckStatus::filled;
    json::u64(item, "\"oid\"", ack.oid);
    json::decimal(item, "\"totalSz\"", ack.filled_size);
    json::decimal(item, "\"avgPx\"", ack.average_price);
  } else {
    ack.status = AckStatus::error;
    const auto text = json::scalar(item, "\"error\"");
    ack.post_only_reject = text.find("Post only") != std::string_view::npos;
    if (out.text[0] == '\0') out.set_text(text);
  }
  return ack;
}

}  // namespace

void HlEvent::set_text(std::string_view value) noexcept {
  const auto n = std::min(value.size(), text.size() - 1);
  for (std::size_t i = 0; i < n; ++i) {
    const char c = value[i];  // journal-safe: no quotes, backslashes or control characters
    text[i] = (c == '"' || c == '\\' || static_cast<unsigned char>(c) < 0x20) ? '\'' : c;
  }
  text[n] = '\0';
}

std::string_view HlEvent::text_view() const noexcept { return std::string_view(text.data()); }

OrderStatus order_status_from(std::string_view status) noexcept {
  if (status == "open" || status == "triggered") return OrderStatus::open;
  if (status == "filled") return OrderStatus::filled;
  if (status.ends_with("anceled") || status == "scheduledCancel") return OrderStatus::canceled;
  if (status.ends_with("ejected")) return OrderStatus::rejected;
  return OrderStatus::other;
}

bool parse_action_payload(std::string_view payload, HlEvent& out) {
  out.kind = HlEventKind::ack;
  out.count = 0;
  out.text[0] = '\0';
  const auto status = json::scalar(payload, "\"status\"");
  if (status == "err") {
    out.ok = false;
    out.set_text(json::scalar(payload, "\"response\""));
    return true;
  }
  if (status != "ok") return false;
  out.ok = true;
  const auto statuses = json::value_after(payload, "\"statuses\"");
  if (statuses.empty()) return true;  // scheduleCancel / updateLeverage: {"type":"default"}
  for (const auto item : json::array_items(statuses)) {
    if (out.count >= kMaxBatch) break;
    out.items[out.count++] = parse_status(item, out);
  }
  return true;
}

bool parse_post_frame(std::string_view frame, std::uint64_t& id, HlEvent& out) {
  if (json::scalar(frame, "\"channel\"") != "post" || !json::u64(frame, "\"id\"", id)) return false;
  const auto response = frame.substr(std::min(frame.size(), frame.find("\"response\"")));
  const auto type = json::scalar(response, "\"type\"");
  out.kind = HlEventKind::ack;
  out.request_id = id;
  if (type == "action") return parse_action_payload(json::value_after(response, "\"payload\""), out);
  out.ok = false;
  out.count = 0;
  out.set_text(type == "error" ? json::scalar(response, "\"payload\"") : std::string_view("unexpected post response"));
  return true;
}

bool parse_user_fills(std::string_view frame, std::span<const std::string> coins, std::uint64_t receive_ns,
                      std::vector<HlEvent>& out) {
  out.clear();
  if (json::scalar(frame, "\"channel\"") != "userFills") return false;
  bool snapshot = false;
  if (json::boolean(frame, "\"isSnapshot\"", snapshot) && snapshot) return true;  // history, not new fills
  for (const auto item : json::array_items(json::value_after(frame, "\"fills\""))) {
    const auto symbol = symbol_index(coins, json::scalar(item, "\"coin\""));
    if (symbol < 0) continue;
    HlEvent event;
    event.kind = HlEventKind::fill;
    event.receive_ns = receive_ns;
    event.symbol = static_cast<std::uint16_t>(symbol);
    const auto side = json::scalar(item, "\"side\"");
    if ((side != "B" && side != "A") || !json::decimal(item, "\"px\"", event.price) ||
        !json::decimal(item, "\"sz\"", event.size) || !json::u64(item, "\"oid\"", event.oid))
      return false;
    event.side = side == "B" ? Side::buy : Side::sell;
    json::decimal(item, "\"fee\"", event.fee);
    json::u64(item, "\"tid\"", event.tid);
    json::u64(item, "\"time\"", event.time_ms);
    json::boolean(item, "\"crossed\"", event.crossed);
    const auto dir = json::scalar(item, "\"dir\"");
    event.forced = dir.find("Auto-Deleveraging") != std::string_view::npos ||
                   dir.find("Liquidat") != std::string_view::npos ||
                   item.find("\"liquidation\"") != std::string_view::npos;
    event.set_text(dir);
    out.push_back(event);
  }
  return true;
}

bool parse_order_updates(std::string_view frame, std::span<const std::string> coins, std::uint64_t receive_ns,
                         std::vector<HlEvent>& out) {
  out.clear();
  if (json::scalar(frame, "\"channel\"") != "orderUpdates") return false;
  for (const auto item : json::array_items(json::value_after(frame, "\"data\""))) {
    const auto symbol = symbol_index(coins, json::scalar(item, "\"coin\""));
    if (symbol < 0) continue;
    HlEvent event;
    event.kind = HlEventKind::order_update;
    event.receive_ns = receive_ns;
    event.symbol = static_cast<std::uint16_t>(symbol);
    if (!json::u64(item, "\"oid\"", event.oid)) return false;
    // "status" follows the nested order object, which has no status key of its own.
    const auto order_end = item.find('}');
    const auto status = json::scalar(item.substr(order_end == std::string_view::npos ? 0 : order_end), "\"status\"");
    event.order_status = order_status_from(status);
    event.set_text(status);
    out.push_back(event);
  }
  return true;
}

Node command_action(const HlCommand& command) {
  const auto n = std::min<std::size_t>(command.count, kMaxBatch);
  switch (command.kind) {
    case HlCommandKind::place: return order_action(std::span(command.orders.data(), n));
    case HlCommandKind::modify: {
      std::vector<ModifySpec> modifies;
      for (std::size_t i = 0; i < n; ++i) modifies.push_back({command.oids[i], command.orders[i]});
      return modify_action(modifies);
    }
    case HlCommandKind::cancel_cloid: {
      std::vector<CancelCloidSpec> cancels;
      for (std::size_t i = 0; i < n; ++i)
        cancels.push_back({command.orders[i].asset, command.orders[i].cloid.value_or(Cloid{})});
      return cancel_by_cloid_action(cancels);
    }
    case HlCommandKind::schedule_cancel:
      return schedule_cancel_action(command.time_ms == 0 ? std::nullopt : std::optional(command.time_ms));
    case HlCommandKind::update_leverage:
      return update_leverage_action(command.orders[0].asset, command.is_cross, command.leverage);
  }
  return {};
}

HlGateway::HlGateway(GatewayConfig config, const Signer& signer, HlCommands& commands, HlEvents& events,
                     const std::atomic<std::uint64_t>& engine_heartbeat_ns)
    : config_(std::move(config)), signer_(signer), commands_(commands), events_(events),
      engine_heartbeat_ns_(engine_heartbeat_ns) {}

std::uint64_t HlGateway::next_nonce() noexcept {
  last_nonce_ = std::max(last_nonce_ + 1, wall_ns() / 1'000'000);
  return last_nonce_;
}

bool HlGateway::emit(const HlEvent& event, std::string& error) {
  if (events_.try_push(event)) return true;
  error = "gateway event queue overflow";
  return false;
}

bool HlGateway::post_rest(const Node& action, std::string& reply, std::string& error) {
  const auto nonce = next_nonce();
  const auto expires = config_.expires_after_ms == 0 ? 0 : nonce + config_.expires_after_ms;
  const auto body = exchange_payload(action, nonce, signer_.sign_action(action, nonce, expires), expires);
  const auto result = http_.post(std::string(kExchangeUrl), body, {"Content-Type: application/json"});
  reply = result.body;
  if (!result.ok) {
    error = "hyperliquid exchange: " + result.error;
    return false;
  }
  return true;
}

bool HlGateway::connect_socket(std::string& error) {
  if (!socket_.connect(std::string(kHyperliquidWs), error)) return false;
  const auto subscribe = [&](std::string_view type) {
    return socket_.send_text(R"({"method":"subscribe","subscription":{"type":")" + std::string(type) +
                                 R"(","user":")" + config_.account + "\"}}",
                             error);
  };
  if (!subscribe("orderUpdates") || !subscribe("userFills")) return false;
  connected.store(true);
  return true;
}

bool HlGateway::simulate(const HlCommand& command, std::string& error) {
  HlEvent ack;
  ack.kind = HlEventKind::ack;
  ack.receive_ns = wall_ns();
  ack.request_id = command.request_id;
  ack.ok = true;
  if (command.kind == HlCommandKind::place || command.kind == HlCommandKind::modify ||
      command.kind == HlCommandKind::cancel_cloid) {
    ack.count = command.count;
    for (std::size_t i = 0; i < command.count; ++i) {
      ack.items[i].status = command.kind == HlCommandKind::cancel_cloid ? AckStatus::success : AckStatus::resting;
      ack.items[i].oid = command.kind == HlCommandKind::cancel_cloid ? 0 : ++sim_oid_;
    }
  }
  return emit(ack, error);
}

bool HlGateway::handle_command(const HlCommand& command, std::string& error) {
  if (!config_.live) return simulate(command, error);
  const auto action = command_action(command);
  const auto nonce = next_nonce();
  const auto expires = config_.expires_after_ms == 0 ? 0 : nonce + config_.expires_after_ms;
  const auto payload = exchange_payload(action, nonce, signer_.sign_action(action, nonce, expires), expires);
  requests_sent.fetch_add(std::max<std::uint64_t>(command.count, 1));
  if (connected.load()) {
    const auto message = "{\"method\":\"post\",\"id\":" + std::to_string(command.request_id) +
                         ",\"request\":{\"type\":\"action\",\"payload\":" + payload + "}}";
    std::string send_error;
    if (socket_.send_text(message, send_error)) {
      pending_.push_back({command.request_id, command.kind, command.count, steady_ns()});
      return true;
    }
    connected.store(false);
    HlEvent down;
    down.kind = HlEventKind::connection;
    down.receive_ns = wall_ns();
    down.set_text(send_error);
    if (!emit(down, error)) return false;
  }
  // Socket down: the same signed payload over REST, synchronously.
  HlEvent ack;
  ack.kind = HlEventKind::ack;
  ack.request_id = command.request_id;
  const auto result = http_.post(std::string(kExchangeUrl), payload, {"Content-Type: application/json"});
  ack.receive_ns = wall_ns();
  if (!result.ok || !parse_action_payload(result.body, ack)) {
    ack.ok = false;
    ack.count = 0;
    ack.set_text(result.ok ? std::string_view("unparsed exchange reply") : std::string_view(result.error));
  }
  return emit(ack, error);
}

bool HlGateway::handle_frame(std::string_view frame, std::string& error) {
  const auto receive_ns = wall_ns();
  const auto channel = json::scalar(frame, "\"channel\"");
  if (channel == "post") {
    HlEvent ack;
    std::uint64_t id = 0;
    if (!parse_post_frame(frame, id, ack)) return true;
    ack.receive_ns = receive_ns;
    HlCommandKind kind{};
    bool known = false;
    for (const auto& p : pending_)
      if (p.id == id) {
        kind = p.kind;
        known = true;
        break;
      }
    std::erase_if(pending_, [&](const Pending& p) { return p.id == id; });
    // New accounts cannot arm scheduleCancel until $1M volume; stop retrying so we don't burn the budget.
    if (known && kind == HlCommandKind::schedule_cancel && !ack.ok) {
      const auto text = ack.text_view();
      if (text.find("scheduled cancel") != std::string_view::npos ||
          text.find("enough volume traded") != std::string_view::npos) {
        deadman_disabled_ = true;
        config_.deadman_s = 0;
      }
    }
    return emit(ack, error);
  }
  if (channel == "userFills" || channel == "orderUpdates") {
    const bool parsed = channel == "userFills" ? parse_user_fills(frame, config_.coins, receive_ns, parsed_)
                                               : parse_order_updates(frame, config_.coins, receive_ns, parsed_);
    if (!parsed) {
      // A malformed private frame may hide a fill: surface it so the engine halts.
      HlEvent bad;
      bad.kind = HlEventKind::connection;
      bad.receive_ns = receive_ns;
      bad.connected = connected.load();
      bad.set_text("malformed private frame");
      return emit(bad, error);
    }
    for (const auto& event : parsed_)
      if (!emit(event, error)) return false;
    return true;
  }
  if (channel == "error") {
    HlEvent ack;
    ack.kind = HlEventKind::ack;
    ack.receive_ns = receive_ns;
    ack.ok = false;
    ack.set_text(json::scalar(frame, "\"data\""));
    return emit(ack, error);
  }
  return true;  // subscriptionResponse, pong
}

bool HlGateway::deadman(std::uint64_t now_ns, std::string& error) {
  if (deadman_disabled_ || config_.deadman_s == 0 ||
      now_ns - last_deadman_ns_ < config_.deadman_s * 500'000'000ULL)
    return true;
  // Refresh only while the decision thread is alive; a hung engine lets the cancel fire.
  if (now_ns - engine_heartbeat_ns_.load(std::memory_order_relaxed) > kEngineAliveNs) return true;
  last_deadman_ns_ = now_ns;
  HlCommand command;
  command.kind = HlCommandKind::schedule_cancel;
  command.request_id = internal_id_++;
  command.time_ms = wall_ns() / 1'000'000 + config_.deadman_s * 1'000ULL;
  return handle_command(command, error);
}

bool HlGateway::run(std::atomic<bool>& stop, std::string& error) {
  std::string socket_error;
  if (config_.live && !connect_socket(socket_error)) {
    error = socket_error;
    return false;
  }
  HlEvent up;
  up.kind = HlEventKind::connection;
  up.receive_ns = wall_ns();
  up.connected = true;
  if (!emit(up, error)) return false;
  auto last_ping = steady_ns();
  auto last_reconnect = steady_ns();
  std::string frame;
  while (!stop.load(std::memory_order_relaxed)) {
    bool busy = false;
    HlCommand command;
    while (commands_.try_pop(command)) {
      busy = true;
      if (!handle_command(command, error)) return false;
    }
    if (!config_.live) {
      if (!busy) std::this_thread::sleep_for(std::chrono::microseconds(200));
      continue;
    }
    const auto now = steady_ns();
    if (connected.load()) {
      for (;;) {
        const auto status = socket_.receive(frame, 0, socket_error);
        if (status == WebSocket::Status::timeout) break;
        if (status != WebSocket::Status::frame) {
          connected.store(false);
          HlEvent down;
          down.kind = HlEventKind::connection;
          down.receive_ns = wall_ns();
          down.set_text(socket_error);
          if (!emit(down, error)) return false;
          break;
        }
        busy = true;
        if (!handle_frame(frame, error)) return false;
      }
      if (connected.load() && now - last_ping >= kPingNs) {
        last_ping = now;
        if (!socket_.send_text(R"({"method":"ping"})", socket_error)) connected.store(false);
      }
    } else if (now - last_reconnect >= kReconnectNs) {
      last_reconnect = now;
      if (connect_socket(socket_error)) {
        HlEvent back;
        back.kind = HlEventKind::connection;
        back.receive_ns = wall_ns();
        back.connected = true;
        if (!emit(back, error)) return false;
      }
    }
    // A post without a response is reported once; its outcome is reconciled from order updates.
    for (auto it = pending_.begin(); it != pending_.end();) {
      if (now - it->sent_ns < config_.response_timeout_ms * 1'000'000ULL) { ++it; continue; }
      HlEvent timeout;
      timeout.kind = HlEventKind::ack;
      timeout.receive_ns = wall_ns();
      timeout.request_id = it->id;
      timeout.ok = false;
      timeout.set_text("post response timeout");
      it = pending_.erase(it);
      if (!emit(timeout, error)) return false;
    }
    if (!deadman(steady_ns(), error)) return false;
    if (!busy) {
      if (config_.spin) {
        std::this_thread::yield();
      } else {
        const auto socket = socket_.native_socket();
        platform::wait_readable(&socket, 1, 1);
      }
    }
  }
  return true;
}

}  // namespace hla
