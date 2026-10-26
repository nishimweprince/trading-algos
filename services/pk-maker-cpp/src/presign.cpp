#include "pkm/presign.hpp"

#include <chrono>
#include <cstring>
#include <thread>

#include "pkm/clob.hpp"
#include "pkm/eip712.hpp"
#include "pkm/net.hpp"
#include "pkm/platform.hpp"
#include "pkm/venue.hpp"

namespace pkm::presign {

Presigner::Presigner(const venue::Wallet& wallet, std::string owner, std::int64_t ttl_ms)
    : wallet_(wallet), owner_(std::move(owner)), ttl_ms_(ttl_ms), slots_(std::make_unique<std::array<Slot, kMaxSlots>>()) {
  scratch_.reserve(kBodyMax);
}

Presigner::~Presigner() = default;

void Presigner::want(int slot, std::uint32_t gen, std::int64_t price_cents, std::int64_t shares, std::string_view token,
                     bool neg_risk) noexcept {
  if (slot < 0 || slot >= kMaxSlots || token.size() >= sizeof(Want::token)) return;
  Want& w = (*slots_)[static_cast<std::size_t>(slot)].want;
  if (w.gen == gen && w.price_cents == price_cents && w.shares == shares) return;  // only this thread writes
  const std::uint32_t s = w.seq.load(std::memory_order_relaxed);
  w.seq.store(s + 1, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_release);
  w.gen = gen;
  w.price_cents = price_cents;
  w.shares = shares;
  w.neg_risk = neg_risk;
  std::memcpy(w.token, token.data(), token.size());
  w.token_len = static_cast<std::uint8_t>(token.size());
  w.seq.store(s + 2, std::memory_order_release);
}

bool Presigner::take(int slot, std::uint32_t gen, std::int64_t price_cents, std::int64_t shares, std::string& out) noexcept {
  if (ttl_ms_ <= 0 || slot < 0 || slot >= kMaxSlots) return false;
  const std::int64_t now_ms = net::now_ns() / 1'000'000;
  for (auto& e : (*slots_)[static_cast<std::size_t>(slot)].entries) {
    const std::uint32_t s1 = e.seq.load(std::memory_order_acquire);
    if ((s1 & 1U) != 0 || s1 == 0 || e.consumed.load(std::memory_order_relaxed) == s1) continue;
    if (e.gen != gen || e.price_cents != price_cents || e.shares != shares || now_ms - e.signed_ms > ttl_ms_) continue;
    const std::uint32_t len = e.len;
    if (len == 0 || len > kBodyMax) continue;
    out.assign(e.body, len);
    std::atomic_thread_fence(std::memory_order_acquire);
    if (e.seq.load(std::memory_order_relaxed) != s1) continue;
    e.consumed.store(s1, std::memory_order_relaxed);
    return true;
  }
  return false;
}

void Presigner::sign_into(Entry& entry, const Want& w, std::int64_t price_cents, std::int64_t now_ms) {
  const std::string_view token(w.token, w.token_len);
  Bytes32 id{};
  if (!decimal_to_u256(token, id)) return;
  const auto order = venue::make_order(wallet_, id, price_cents, w.shares, venue::random_salt(),
                                       static_cast<std::uint64_t>(net::now_ns() / 1'000'000));
  const std::string signature = eip712::sign_order(wallet_.signer, order, w.neg_risk);
  clob::write_order_body(scratch_, order, token, signature, owner_, true);
  if (scratch_.size() > kBodyMax) return;
  const std::uint32_t s = entry.seq.load(std::memory_order_relaxed) + 1;
  entry.seq.store(s, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_release);
  entry.gen = w.gen;
  entry.price_cents = price_cents;
  entry.shares = w.shares;
  entry.signed_ms = now_ms;
  std::memcpy(entry.body, scratch_.data(), scratch_.size());
  entry.len = static_cast<std::uint32_t>(scratch_.size());
  entry.seq.store(s + 1, std::memory_order_release);
  signed_.fetch_add(1, std::memory_order_relaxed);
}

bool Presigner::refresh(std::int64_t now_ms) {
  if (ttl_ms_ <= 0) return false;  // pre-signing off: every post is signed inline
  bool any = false;
  for (auto& slot : *slots_) {
    // A consistent copy of the want.
    Want w;
    std::uint32_t s1 = 0;
    for (int tries = 0; tries < 4; ++tries) {
      s1 = slot.want.seq.load(std::memory_order_acquire);
      if ((s1 & 1U) != 0) continue;
      w.gen = slot.want.gen;
      w.price_cents = slot.want.price_cents;
      w.shares = slot.want.shares;
      w.neg_risk = slot.want.neg_risk;
      w.token_len = slot.want.token_len;
      std::memcpy(w.token, slot.want.token, sizeof(w.token));
      std::atomic_thread_fence(std::memory_order_acquire);
      if (slot.want.seq.load(std::memory_order_relaxed) == s1) break;
      s1 = 1;
    }
    if (s1 == 0 || (s1 & 1U) != 0 || w.price_cents <= 0 || w.shares <= 0 || w.token_len == 0) continue;
    // Entries for price − 1, price, price + 1 ticks (within 1..99).
    for (int k = 0; k < kPerSlot; ++k) {
      const std::int64_t px = w.price_cents + (k - 1);
      if (px < 1 || px > 99) continue;
      Entry& e = slot.entries[static_cast<std::size_t>(k)];
      const std::uint32_t es = e.seq.load(std::memory_order_acquire);
      const bool usable = es != 0 && e.consumed.load(std::memory_order_relaxed) != es && e.gen == w.gen &&
                          e.price_cents == px && e.shares == w.shares && now_ms - e.signed_ms < ttl_ms_ / 2;
      if (usable) continue;
      sign_into(e, w, px, now_ms);
      any = true;
    }
  }
  return any;
}

void Presigner::run(const std::atomic<bool>& stop, int core) {
  if (core >= 0) platform::pin_current_thread(static_cast<unsigned>(core));
  while (!stop.load(std::memory_order_relaxed)) {
    if (!refresh(net::now_ns() / 1'000'000)) std::this_thread::sleep_for(std::chrono::microseconds(200));
  }
}

}  // namespace pkm::presign
