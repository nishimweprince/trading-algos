#pragma once
// Zero-copy view over a geyser.SubscribeUpdate frame (proto/geyser.proto): the
// account and slot updates this bot subscribes to. Every string_view points into
// the frame; parsing never allocates.
#include <cstdint>
#include <string>
#include <string_view>

#include "core/types.hpp"

namespace arb::feed {

constexpr size_t MAX_FILTERS = 8;

struct AccountView {
  std::string_view pubkey;  // 32 bytes
  std::string_view owner;   // 32 bytes
  std::string_view data;
  uint64_t lamports = 0;
  uint64_t write_version = 0;
  uint64_t slot = 0;
  bool is_startup = false;
  bool has_bank = false;  // Alpenglow / Agave 4.3: the bank (candidate block) the update came from
  uint64_t bank_id = 0;   // node-local counter: compare only within one connection
};

struct SlotView {
  uint64_t slot = 0;
  uint64_t parent = 0;
  uint32_t status = 0;  // SlotStatus: 0 processed, 1 confirmed, 2 finalized, 3 first shred, 4 completed, 5 created bank, 6 dead
  bool has_bank = false;  // absent for first-shred and completed
  uint64_t bank_id = 0;
};

struct UpdateView {
  enum Kind : uint8_t { None, Account, Slot, Ping, Pong, Other } kind = None;
  std::string_view filters[MAX_FILTERS];
  uint8_t n_filters = 0;
  AccountView account;
  SlotView slot;
  int32_t pong_id = 0;
  int64_t created_at_ns = 0;  // SubscribeUpdate.created_at (when the server produced it), epoch ns; 0 if absent
};

/** Parse one frame. False on malformed protobuf (counted and dropped by the caller). */
bool parse_update(const uint8_t* data, size_t len, UpdateView& out);

}  // namespace arb::feed

namespace arb::feed {
/** Encode a SubscribeUpdate account frame (synthetic frames for tests and fixture replays). */
std::string encode_account_frame(const Pubkey& key, const Pubkey& owner, const uint8_t* data, size_t len, uint64_t lamports,
                                 uint64_t slot, uint64_t write_version, std::string_view filter = "accounts",
                                 int64_t created_at_ns = 0, const uint64_t* bank_id = nullptr);
/** Encode a SubscribeUpdate slot frame. */
std::string encode_slot_frame(uint64_t slot, uint32_t status = 0, const uint64_t* bank_id = nullptr);
}  // namespace arb::feed
