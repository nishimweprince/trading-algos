#include "bpmm/allocation.hpp"

#include <atomic>
#include <cstdlib>
#include <new>

namespace {
std::atomic<bool> tracking{false};
std::atomic<std::uint64_t> allocations{0};
}

namespace bpmm::allocation {
void begin() noexcept { allocations.store(0, std::memory_order_relaxed); tracking.store(true, std::memory_order_release); }
std::uint64_t end() noexcept { tracking.store(false, std::memory_order_release); return allocations.load(std::memory_order_relaxed); }
void record() noexcept { if (tracking.load(std::memory_order_acquire)) allocations.fetch_add(1, std::memory_order_relaxed); }
}  // namespace bpmm::allocation

void* operator new(std::size_t size) {
  bpmm::allocation::record();
  if (void* value = std::malloc(size)) return value;
  throw std::bad_alloc();
}

void* operator new[](std::size_t size) {
  bpmm::allocation::record();
  if (void* value = std::malloc(size)) return value;
  throw std::bad_alloc();
}

void operator delete(void* ptr) noexcept { std::free(ptr); }
void operator delete[](void* ptr) noexcept { std::free(ptr); }
void operator delete(void* ptr, std::size_t) noexcept { std::free(ptr); }
void operator delete[](void* ptr, std::size_t) noexcept { std::free(ptr); }

void* operator new(std::size_t size, std::align_val_t alignment) {
  bpmm::allocation::record();
  const auto align = static_cast<std::size_t>(alignment);
  const auto rounded = ((size + align - 1) / align) * align;
  if (void* value = std::aligned_alloc(align, rounded)) return value;
  throw std::bad_alloc();
}

void* operator new[](std::size_t size, std::align_val_t alignment) {
  return ::operator new(size, alignment);
}

void operator delete(void* ptr, std::align_val_t) noexcept { std::free(ptr); }
void operator delete[](void* ptr, std::align_val_t) noexcept { std::free(ptr); }
void operator delete(void* ptr, std::size_t, std::align_val_t) noexcept { std::free(ptr); }
void operator delete[](void* ptr, std::size_t, std::align_val_t) noexcept { std::free(ptr); }
