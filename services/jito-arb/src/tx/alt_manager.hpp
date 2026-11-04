#pragma once
// Address lookup tables (spec §8) and the recent-blockhash cache (spec §9).
//
// AltManager keeps the bot's tables as decoded from chain. Addresses added by the latest extension
// are usable only once the slot is past last_extended_slot + pending_slots (spec: 2), so a table is
// handed to the compiler with just its active prefix, and a pool stays `pending_alt` until every
// account it needs is active. plan_extend() builds the create/extend instructions for missing keys;
// the discovery job sends them (off the hot path), never the engine.
#include <cstdint>
#include <map>
#include <span>
#include <vector>

#include "core/error.hpp"
#include "tx/builders.hpp"
#include "tx/message_v0.hpp"

namespace arb::tx {

constexpr size_t ALT_MAX_ADDRESSES = 256;
constexpr size_t ALT_EXTEND_CHUNK = 20;  // addresses per extend transaction (fits 1,232 bytes with room)

class AltManager {
 public:
  explicit AltManager(uint64_t pending_slots = 2) : pending_(pending_slots) {}
  void upsert(const Pubkey& table, const AltAccount& a);
  /** Tables with only their active prefix at `slot`, for the compiler. */
  std::vector<LookupTable> active(uint64_t slot) const;
  /** True when every key is an active entry of some table (or is a signer/tip that stays static). */
  bool ready(std::span<const Pubkey> keys, uint64_t slot) const;
  /** Keys not in any table (active or pending). */
  std::vector<Pubkey> missing(std::span<const Pubkey> keys) const;
  /**
   * Instruction lists (one per transaction) that add `keys` to the tables: fill the first table with room,
   * else create a new one at `recent_slot` (its address is returned in `created`).
   */
  std::vector<IxList> plan_extend(std::span<const Pubkey> keys, const Pubkey& authority, const Pubkey& payer,
                                  uint64_t recent_slot, std::vector<Pubkey>* created = nullptr) const;
  size_t tables() const { return tables_.size(); }

 private:
  struct Entry {
    AltAccount acc;
  };
  size_t active_len(const AltAccount& a, uint64_t slot) const;
  uint64_t pending_;
  std::map<Pubkey, Entry> tables_;
};

/** Recent blockhash, refreshed every 2 s off the hot path; refused when older than max_age_slots (60). */
class BlockhashCache {
 public:
  explicit BlockhashCache(uint64_t max_age_slots = 60, int64_t refresh_ms = 2000) : max_age_(max_age_slots), refresh_ms_(refresh_ms) {}
  void set(const Hash& h, uint64_t slot, int64_t now_ms) {
    hash_ = h;
    slot_ = slot;
    at_ms_ = now_ms;
    have_ = true;
  }
  bool needs_refresh(int64_t now_ms) const { return !have_ || now_ms - at_ms_ >= refresh_ms_; }
  Result<Hash> get(uint64_t current_slot) const {
    if (!have_) return fail(Errc::stale, "no blockhash yet");
    if (current_slot > slot_ && current_slot - slot_ > max_age_) return fail(Errc::stale, "blockhash older than 60 slots");
    return hash_;
  }

 private:
  uint64_t max_age_;
  int64_t refresh_ms_;
  Hash hash_{};
  uint64_t slot_ = 0;
  int64_t at_ms_ = 0;
  bool have_ = false;
};

}  // namespace arb::tx
