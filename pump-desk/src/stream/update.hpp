#pragma once
// Zero-copy views over a geyser.SubscribeUpdate frame (proto/geyser.proto,
// proto/solana-storage.proto). Every string_view points into the ingest ring
// slot; the parse fills a preallocated UpdateView and never allocates.
#include <cstdint>
#include <string_view>

#include "core/types.hpp"

namespace pd {

constexpr size_t MAX_FILTERS = 8;
constexpr size_t MAX_LOGS = 384;
constexpr size_t MAX_TOKEN_BALANCES = 96;
constexpr size_t MAX_KEYS = 128;

struct AccountView {
  std::string_view pubkey;  // 32 bytes
  std::string_view owner;   // 32 bytes
  std::string_view data;
  std::string_view txn_signature;  // 64 bytes or empty
  uint64_t lamports = 0;
  uint64_t slot = 0;
};

struct TokenBalanceView {
  uint32_t account_index = 0;
  std::string_view mint;        // base58
  std::string_view owner;       // base58
  std::string_view program_id;  // base58
  uint64_t amount = 0;          // raw units (ui_token_amount.amount)
};

struct TxView {
  uint64_t slot = 0;
  int64_t index = -1;          // position in the block (Info.index); -1 when absent
  std::string_view signature;  // 64 bytes
  bool failed = false;         // meta.err present
  bool has_meta = false;
  uint64_t fee = 0;
  // message.account_keys then meta.loaded_writable / loaded_readonly addresses (the full key list)
  std::string_view keys[MAX_KEYS];
  uint16_t n_static_keys = 0;
  uint16_t n_keys = 0;
  uint64_t pre_balances[MAX_KEYS];
  uint64_t post_balances[MAX_KEYS];
  uint16_t n_pre_balances = 0, n_post_balances = 0;
  std::string_view logs[MAX_LOGS];
  uint16_t n_logs = 0;
  bool logs_truncated = false;  // ran past MAX_LOGS
  TokenBalanceView pre_tokens[MAX_TOKEN_BALANCES];
  TokenBalanceView post_tokens[MAX_TOKEN_BALANCES];
  uint16_t n_pre_tokens = 0, n_post_tokens = 0;

  int key_index(const Pubkey& k) const {
    for (uint16_t i = 0; i < n_keys; ++i)
      if (keys[i].size() == 32 && key_eq(reinterpret_cast<const uint8_t*>(keys[i].data()), k)) return i;
    return -1;
  }
};

struct SlotView {
  uint64_t slot = 0;
  uint32_t status = 0;
};

struct UpdateView {
  enum Kind : uint8_t { None, Account, Slot, Transaction, Ping, Pong, Other } kind = None;
  std::string_view filters[MAX_FILTERS];
  uint8_t n_filters = 0;
  AccountView account;
  TxView tx;
  SlotView slot;

  bool has_filter(std::string_view name) const {
    for (uint8_t i = 0; i < n_filters; ++i)
      if (filters[i] == name) return true;
    return false;
  }
  bool has_filter_prefix(std::string_view prefix) const {
    for (uint8_t i = 0; i < n_filters; ++i)
      if (filters[i].substr(0, prefix.size()) == prefix) return true;
    return false;
  }
};

/** Parse one frame. False on malformed protobuf (counted and dropped by the caller). */
bool parse_update(const uint8_t* data, size_t len, UpdateView& out);

/** Decimal u64 (token amounts arrive as strings). */
inline bool parse_u64(std::string_view s, uint64_t& v) {
  if (s.empty() || s.size() > 20) return false;
  uint64_t r = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return false;
    uint64_t next = r * 10 + uint64_t(c - '0');
    if (next < r) return false;
    r = next;
  }
  v = r;
  return true;
}

}  // namespace pd
