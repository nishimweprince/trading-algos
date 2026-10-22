#pragma once

#include "hla/hl_sign.hpp"
#include "hla/net.hpp"
#include "hla/spsc.hpp"
#include "hla/types.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

constexpr std::size_t kMaxBatch = 8;

enum class HlCommandKind : std::uint8_t { place, modify, cancel_cloid, schedule_cancel, update_leverage };

// Decision thread -> gateway. Fixed size so it travels through an SPSC ring.
//   place:        orders[0..count)
//   modify:       oids[i] is replaced by orders[i]
//   cancel_cloid: orders[i].asset + orders[i].cloid
struct HlCommand final {
  HlCommandKind kind{HlCommandKind::place};
  std::uint64_t request_id{};
  std::uint8_t count{};
  std::array<OrderSpec, kMaxBatch> orders{};
  std::array<std::uint64_t, kMaxBatch> oids{};
  std::uint64_t time_ms{};  // schedule_cancel; 0 clears
  std::uint32_t leverage{};
  bool is_cross{};
};

enum class HlEventKind : std::uint8_t { ack, order_update, fill, connection };
enum class AckStatus : std::uint8_t { resting, filled, success, error };
enum class OrderStatus : std::uint8_t { open, filled, canceled, rejected, other };

struct AckItem final {
  AckStatus status{AckStatus::error};
  std::uint64_t oid{};
  Quantity filled_size{};
  Price average_price{};
  bool post_only_reject{};  // ALO would have crossed; not an error for a maker
};

// Gateway -> decision thread.
struct HlEvent final {
  HlEventKind kind{HlEventKind::ack};
  std::uint64_t receive_ns{};
  // ack: per-item results in command order; `ok` false means the whole action failed.
  std::uint64_t request_id{};
  bool ok{};
  std::uint8_t count{};
  std::array<AckItem, kMaxBatch> items{};
  // order_update / fill
  std::uint16_t symbol{};
  std::uint64_t oid{};
  OrderStatus order_status{OrderStatus::other};
  Side side{Side::buy};
  Price price{};
  Quantity size{};
  Decimal fee{};
  std::uint64_t tid{};
  std::uint64_t time_ms{};
  bool crossed{};         // our fill was the taker side
  bool forced{};          // ADL or liquidation: needs a human
  // connection
  bool connected{};
  std::array<char, 160> text{};  // error text (ack) or fill direction

  void set_text(std::string_view value) noexcept;
  [[nodiscard]] std::string_view text_view() const noexcept;
};

// {"status":"ok","response":{"type":"order","data":{"statuses":[...]}}} or {"status":"err",...},
// the payload of both the REST /exchange reply and a WS post `action` response.
bool parse_action_payload(std::string_view payload, HlEvent& out);
// A WS frame on channel "post". False when the frame is not a post response.
bool parse_post_frame(std::string_view frame, std::uint64_t& id, HlEvent& out);
// userFills (snapshots are skipped) and orderUpdates frames. Unknown coins are dropped.
bool parse_user_fills(std::string_view frame, std::span<const std::string> coins, std::uint64_t receive_ns,
                      std::vector<HlEvent>& out);
bool parse_order_updates(std::string_view frame, std::span<const std::string> coins, std::uint64_t receive_ns,
                         std::vector<HlEvent>& out);
OrderStatus order_status_from(std::string_view status) noexcept;

Node command_action(const HlCommand& command);

struct GatewayConfig final {
  std::string account;              // master address: fills and order updates are keyed by it
  std::vector<std::string> coins;   // basket order = symbol index
  bool live{};                      // false: dry-run, synthesises acks and sends nothing
  std::uint64_t expires_after_ms{}; // 0: no expiresAfter on actions
  std::uint32_t deadman_s{60};      // scheduleCancel horizon; 0 disables
  std::uint64_t response_timeout_ms{5'000};
  bool spin{true};
};

using HlCommands = SpscRing<HlCommand, 256>;
using HlEvents = SpscRing<HlEvent, 4096>;

class HlGateway final {
 public:
  HlGateway(GatewayConfig config, const Signer& signer, HlCommands& commands, HlEvents& events,
            const std::atomic<std::uint64_t>& engine_heartbeat_ns);
  // Thread body. Returns false only on a pipeline failure (event queue overflow).
  bool run(std::atomic<bool>& stop, std::string& error);
  // Signs and POSTs one action to /exchange; `reply` is the raw response body.
  bool post_rest(const Node& action, std::string& reply, std::string& error);

  std::atomic<std::uint64_t> requests_sent{0};  // HL counts each order in a batch
  std::atomic<bool> connected{false};

 private:
  struct Pending final {
    std::uint64_t id{};
    HlCommandKind kind{};
    std::uint8_t count{};
    std::uint64_t sent_ns{};
  };

  std::uint64_t next_nonce() noexcept;
  bool emit(const HlEvent& event, std::string& error);
  bool handle_command(const HlCommand& command, std::string& error);
  bool handle_frame(std::string_view frame, std::string& error);
  bool simulate(const HlCommand& command, std::string& error);
  bool connect_socket(std::string& error);
  bool deadman(std::uint64_t now_ns, std::string& error);

  GatewayConfig config_;
  const Signer& signer_;
  HlCommands& commands_;
  HlEvents& events_;
  const std::atomic<std::uint64_t>& engine_heartbeat_ns_;
  WebSocket socket_;
  HttpClient http_;
  std::vector<Pending> pending_;
  std::vector<HlEvent> parsed_;
  std::uint64_t last_nonce_{};
  std::uint64_t sim_oid_{1'000'000'000};
  std::uint64_t internal_id_{1ULL << 62};
  std::uint64_t last_deadman_ns_{};
  bool deadman_disabled_{};  // set when HL rejects scheduleCancel (e.g. <$1M volume)
};

}  // namespace hla
