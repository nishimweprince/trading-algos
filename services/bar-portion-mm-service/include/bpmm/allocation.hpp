#pragma once

#include <cstddef>
#include <cstdint>

namespace bpmm::allocation {
void begin() noexcept;
std::uint64_t end() noexcept;
void record() noexcept;
}  // namespace bpmm::allocation
