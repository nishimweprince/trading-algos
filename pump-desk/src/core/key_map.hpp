#pragma once
// Fixed-capacity open-addressed hash map keyed by Pubkey. Linear probing with
// backward-shift deletion (no tombstones), all storage allocated up front.
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>

#include "core/types.hpp"

namespace pd {

template <typename V>
class KeyMap {
 public:
  explicit KeyMap(size_t capacity_pow2)
      : cap_(capacity_pow2), mask_(capacity_pow2 - 1), keys_(new Pubkey[capacity_pow2]),
        vals_(new V[capacity_pow2]), used_(new uint8_t[capacity_pow2]()) {
    if ((cap_ & mask_) != 0) throw std::invalid_argument("KeyMap capacity must be a power of two");
  }

  size_t size() const { return size_; }
  size_t capacity() const { return cap_; }
  /** Callers keep the load under ~75 %; insert refuses beyond that. */
  bool full() const { return size_ * 4 >= cap_ * 3; }

  V* find(const Pubkey& k) {
    size_t i = key_hash(k) & mask_;
    while (used_[i]) {
      if (keys_[i] == k) return &vals_[i];
      i = (i + 1) & mask_;
    }
    return nullptr;
  }
  const V* find(const Pubkey& k) const { return const_cast<KeyMap*>(this)->find(k); }

  /** Insert or return the existing value. nullptr when full. `created` reports which. */
  V* upsert(const Pubkey& k, bool* created = nullptr) {
    size_t i = key_hash(k) & mask_;
    while (used_[i]) {
      if (keys_[i] == k) {
        if (created) *created = false;
        return &vals_[i];
      }
      i = (i + 1) & mask_;
    }
    if (full()) return nullptr;
    used_[i] = 1;
    keys_[i] = k;
    vals_[i] = V{};
    ++size_;
    if (created) *created = true;
    return &vals_[i];
  }

  bool erase(const Pubkey& k) {
    size_t i = key_hash(k) & mask_;
    while (used_[i]) {
      if (keys_[i] == k) break;
      i = (i + 1) & mask_;
    }
    if (!used_[i]) return false;
    // Backward-shift: pull later entries of the same cluster into the hole.
    size_t hole = i;
    size_t j = (i + 1) & mask_;
    while (used_[j]) {
      size_t home = key_hash(keys_[j]) & mask_;
      // Movable when its home is not cyclically inside (hole, j].
      if (((j - home) & mask_) >= ((j - hole) & mask_)) {
        keys_[hole] = keys_[j];
        vals_[hole] = vals_[j];
        hole = j;
      }
      j = (j + 1) & mask_;
    }
    used_[hole] = 0;
    --size_;
    return true;
  }

  template <typename F>
  void for_each(F&& f) {
    for (size_t i = 0; i < cap_; ++i)
      if (used_[i]) f(keys_[i], vals_[i]);
  }

  void clear() {
    for (size_t i = 0; i < cap_; ++i) used_[i] = 0;
    size_ = 0;
  }

 private:
  size_t cap_, mask_;
  std::unique_ptr<Pubkey[]> keys_;
  std::unique_ptr<V[]> vals_;
  std::unique_ptr<uint8_t[]> used_;
  size_t size_ = 0;
};

}  // namespace pd
