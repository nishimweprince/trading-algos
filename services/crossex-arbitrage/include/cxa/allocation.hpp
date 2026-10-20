#pragma once

#include <cstddef>
#include <cstdint>

namespace cxa::allocation {
void begin() noexcept;
std::uint64_t end() noexcept;
void record() noexcept;
}  // namespace cxa::allocation
