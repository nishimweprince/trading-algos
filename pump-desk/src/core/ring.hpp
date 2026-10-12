#pragma once
// Lock-free queues between pinned threads. All storage is allocated once at
// construction; push/pop never touch the heap.
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <new>
#include <stdexcept>

namespace pd {

constexpr size_t CACHE_LINE = 64;

inline void cpu_relax() {
#if defined(__x86_64__)
  __builtin_ia32_pause();
#elif defined(__aarch64__)
  asm volatile("yield");
#endif
}

/**
 * Single-producer single-consumer queue of fixed-size records. The producer
 * claims a slot in place, fills it and publishes; the consumer peeks in place
 * and pops. N must be a power of two.
 */
template <typename T, size_t N>
class Spsc {
  static_assert((N & (N - 1)) == 0, "N must be a power of two");

 public:
  Spsc() : slots_(new T[N]) {}

  /** Producer: a slot to fill, or nullptr when full. */
  T* claim() {
    uint64_t h = head_.load(std::memory_order_relaxed);
    if (h - tail_cache_ >= N) {
      tail_cache_ = tail_.load(std::memory_order_acquire);
      if (h - tail_cache_ >= N) return nullptr;
    }
    return &slots_[h & (N - 1)];
  }
  void publish() { head_.store(head_.load(std::memory_order_relaxed) + 1, std::memory_order_release); }

  bool push(const T& v) {
    T* s = claim();
    if (!s) return false;
    *s = v;
    publish();
    return true;
  }

  /** Consumer: the oldest record, or nullptr when empty. */
  T* peek() {
    uint64_t t = tail_.load(std::memory_order_relaxed);
    if (t >= head_cache_) {
      head_cache_ = head_.load(std::memory_order_acquire);
      if (t >= head_cache_) return nullptr;
    }
    return &slots_[t & (N - 1)];
  }
  void pop() { tail_.store(tail_.load(std::memory_order_relaxed) + 1, std::memory_order_release); }

  bool try_pop(T& out) {
    T* s = peek();
    if (!s) return false;
    out = *s;
    pop();
    return true;
  }

  size_t size() const { return head_.load(std::memory_order_acquire) - tail_.load(std::memory_order_acquire); }

 private:
  std::unique_ptr<T[]> slots_;
  alignas(CACHE_LINE) std::atomic<uint64_t> head_{0};
  alignas(CACHE_LINE) uint64_t tail_cache_ = 0;  // producer-local
  alignas(CACHE_LINE) std::atomic<uint64_t> tail_{0};
  alignas(CACHE_LINE) uint64_t head_cache_ = 0;  // consumer-local
};

/**
 * Disruptor-style frame ring: one producer (ingest) writes raw gRPC frames
 * into fixed slots; one gating consumer (logic) must see every frame and
 * holds the producer back when it lags; any number of lossy readers (the
 * capture recorder) copy frames out and detect overwrites by sequence, so a
 * slow disk never stalls the trading path.
 */
class FrameRing {
 public:
  struct alignas(CACHE_LINE) Header {
    std::atomic<uint64_t> seq{0};  // sequence + 1 once the slot holds frame `sequence`
    uint64_t recv_ns = 0;
    uint32_t len = 0;
  };

  FrameRing(size_t slots, size_t slot_bytes)
      : n_(slots), mask_(slots - 1), slot_bytes_(slot_bytes),
        headers_(new Header[slots]), data_(new uint8_t[slots * slot_bytes]) {
    if ((slots & (slots - 1)) != 0) throw std::invalid_argument("FrameRing slots must be a power of two");
    // Touch every page now so the first live frames don't fault.
    std::memset(data_.get(), 0, slots * slot_bytes);
  }

  size_t slot_bytes() const { return slot_bytes_; }

  /** Producer: claim the next slot (spins while the gating consumer is a full ring behind). */
  uint8_t* claim(uint64_t& seq_out) {
    uint64_t s = cursor_.load(std::memory_order_relaxed);
    while (s - gate_.load(std::memory_order_acquire) >= n_) cpu_relax();
    seq_out = s;
    return data_.get() + (s & mask_) * slot_bytes_;
  }
  void publish(uint64_t seq, uint32_t len, uint64_t recv_ns) {
    Header& h = headers_[seq & mask_];
    h.len = len;
    h.recv_ns = recv_ns;
    h.seq.store(seq + 1, std::memory_order_release);
    cursor_.store(seq + 1, std::memory_order_release);
  }

  /** Gating consumer: next frame if available. */
  bool peek(uint64_t seq, const uint8_t*& data, uint32_t& len, uint64_t& recv_ns) const {
    if (seq >= cursor_.load(std::memory_order_acquire)) return false;
    const Header& h = headers_[seq & mask_];
    data = data_.get() + (seq & mask_) * slot_bytes_;
    len = h.len;
    recv_ns = h.recv_ns;
    return true;
  }
  void release(uint64_t next_seq) { gate_.store(next_seq, std::memory_order_release); }

  /**
   * Lossy reader: copy frame `seq` into `out`. Returns 1 = copied, 0 = not yet
   * written, -1 = overwritten (the reader fell more than a ring behind).
   */
  int copy_out(uint64_t seq, uint8_t* out, uint32_t& len, uint64_t& recv_ns) const {
    uint64_t cur = cursor_.load(std::memory_order_acquire);
    if (seq >= cur) return 0;
    if (cur - seq > n_) return -1;
    const Header& h = headers_[seq & mask_];
    if (h.seq.load(std::memory_order_acquire) != seq + 1) return -1;
    len = h.len;
    recv_ns = h.recv_ns;
    std::memcpy(out, data_.get() + (seq & mask_) * slot_bytes_, len);
    std::atomic_thread_fence(std::memory_order_acquire);
    return h.seq.load(std::memory_order_relaxed) == seq + 1 ? 1 : -1;
  }
  uint64_t cursor() const { return cursor_.load(std::memory_order_acquire); }

 private:
  const size_t n_, mask_, slot_bytes_;
  std::unique_ptr<Header[]> headers_;
  std::unique_ptr<uint8_t[]> data_;
  alignas(CACHE_LINE) std::atomic<uint64_t> cursor_{0};
  alignas(CACHE_LINE) std::atomic<uint64_t> gate_{0};
};

/** Seqlock for a POD snapshot written by one thread and read by others without locks. */
template <typename T>
class Seqlock {
 public:
  void store(const T& v) {
    uint64_t s = seq_.load(std::memory_order_relaxed);
    seq_.store(s + 1, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_release);
    std::memcpy(static_cast<void*>(&value_), &v, sizeof(T));
    std::atomic_thread_fence(std::memory_order_release);
    seq_.store(s + 2, std::memory_order_release);
  }
  /** Copy out a consistent snapshot. Returns the version (0 = never stored). */
  uint64_t load(T& out) const {
    for (;;) {
      uint64_t s1 = seq_.load(std::memory_order_acquire);
      if (s1 & 1) { cpu_relax(); continue; }
      std::memcpy(static_cast<void*>(&out), &value_, sizeof(T));
      std::atomic_thread_fence(std::memory_order_acquire);
      if (seq_.load(std::memory_order_relaxed) == s1) return s1 / 2;
    }
  }
  uint64_t version() const { return seq_.load(std::memory_order_acquire) / 2; }

 private:
  alignas(CACHE_LINE) std::atomic<uint64_t> seq_{0};
  T value_{};
};

}  // namespace pd
