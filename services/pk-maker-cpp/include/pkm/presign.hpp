#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

#include "pkm/crypto.hpp"

namespace pkm::venue {
struct Wallet;
}

// Orders signed ahead of the decision. The hot thread publishes what each slot
// (coin × side) wants; the signer thread keeps a signed body ready for that
// price and a tick either side. Every handoff is a seqlock, so neither thread
// ever waits on the other. Each body is used once (its salt is unique).
namespace pkm::presign {

inline constexpr int kMaxSlots = 32;
inline constexpr int kPerSlot = 3;
inline constexpr std::size_t kBodyMax = 2048;

class Presigner final {
 public:
  Presigner(const venue::Wallet& wallet, std::string owner, std::int64_t ttl_ms);
  ~Presigner();
  Presigner(const Presigner&) = delete;
  Presigner& operator=(const Presigner&) = delete;

  // Hot thread: slot `slot` wants `shares` at `price_cents` of `token` (window
  // generation `gen`); price 0 clears it.
  void want(int slot, std::uint32_t gen, std::int64_t price_cents, std::int64_t shares, std::string_view token,
            bool neg_risk) noexcept;
  // Hot thread: a fresh body for exactly this order, copied into `out`.
  bool take(int slot, std::uint32_t gen, std::int64_t price_cents, std::int64_t shares, std::string& out) noexcept;

  // Signer thread: one pass over every slot; true if it signed anything.
  bool refresh(std::int64_t now_ms);
  void run(const std::atomic<bool>& stop, int core);

  [[nodiscard]] std::uint64_t signed_count() const noexcept { return signed_.load(std::memory_order_relaxed); }

 private:
  struct Want {
    std::atomic<std::uint32_t> seq{0};
    std::uint32_t gen = 0;
    std::int64_t price_cents = 0;
    std::int64_t shares = 0;
    bool neg_risk = false;
    char token[96] = {};
    std::uint8_t token_len = 0;
  };
  struct Entry {
    std::atomic<std::uint32_t> seq{0};
    std::atomic<std::uint32_t> consumed{0};  // = seq once taken
    std::uint32_t gen = 0;
    std::int64_t price_cents = 0;
    std::int64_t shares = 0;
    std::int64_t signed_ms = 0;
    std::uint32_t len = 0;
    char body[kBodyMax] = {};
  };
  struct alignas(64) Slot {
    Want want;
    std::array<Entry, kPerSlot> entries;
  };

  void sign_into(Entry& entry, const Want& w, std::int64_t price_cents, std::int64_t now_ms);

  const venue::Wallet& wallet_;
  std::string owner_;
  std::int64_t ttl_ms_;
  std::unique_ptr<std::array<Slot, kMaxSlots>> slots_;
  std::atomic<std::uint64_t> signed_{0};
  std::string scratch_;
};

}  // namespace pkm::presign
