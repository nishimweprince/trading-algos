#pragma once

// Minimal key-search JSON readers for the fixed-shape exchange payloads this service
// consumes (same approach as bar-portion-mm). Keys are passed with their quotes,
// e.g. "\"u\"", so "\"u\"" never matches "\"pu\"".

#include "cxa/types.hpp"

#include <array>
#include <charconv>
#include <cstdint>
#include <span>
#include <string_view>

namespace cxa::json {

inline std::string_view value_after(std::string_view text, std::string_view key,
                                    std::size_t start = 0) noexcept {
  const auto pos = text.find(key, start);
  if (pos == std::string_view::npos) return {};
  auto value = text.substr(pos + key.size());
  while (!value.empty() && (value.front() == ' ' || value.front() == ':')) value.remove_prefix(1);
  return value;
}

// Unquoted or quoted scalar following `key`.
inline std::string_view scalar(std::string_view text, std::string_view key) noexcept {
  auto value = value_after(text, key);
  if (value.empty()) return {};
  if (value.front() == '"') {
    value.remove_prefix(1);
    const auto end = value.find('"');
    return end == std::string_view::npos ? std::string_view{} : value.substr(0, end);
  }
  const auto end = value.find_first_of(",}] \n");
  return end == std::string_view::npos ? value : value.substr(0, end);
}

inline bool u64(std::string_view text, std::string_view key, std::uint64_t& out) noexcept {
  const auto value = scalar(text, key);
  if (value.empty()) return false;
  const auto [ptr, ec] = std::from_chars(value.data(), value.data() + value.size(), out);
  return ec == std::errc{} && ptr == value.data() + value.size();
}

inline bool i64(std::string_view text, std::string_view key, std::int64_t& out) noexcept {
  const auto value = scalar(text, key);
  if (value.empty()) return false;
  const auto [ptr, ec] = std::from_chars(value.data(), value.data() + value.size(), out);
  return ec == std::errc{} && ptr == value.data() + value.size();
}

inline bool decimal(std::string_view text, std::string_view key, Decimal& out) noexcept {
  const auto value = scalar(text, key);
  if (value.empty()) return false;
  try {
    out = Decimal::parse(value);
    return true;
  } catch (...) {
    return false;
  }
}

inline bool boolean(std::string_view text, std::string_view key, bool& out) noexcept {
  const auto value = value_after(text, key);
  if (value.starts_with("true")) { out = true; return true; }
  if (value.starts_with("false")) { out = false; return true; }
  return false;
}

// Rate such as "0.0005" or "-0.000212" in ppm.
inline bool rate_ppm(std::string_view text, std::string_view key, std::int64_t& out) noexcept {
  Decimal rate;
  if (!decimal(text, key, rate)) return false;
  out = static_cast<std::int64_t>((static_cast<__int128>(rate.raw) * kPpm) / kScale);
  return true;
}

// [["price","qty"],...] following `key`. Returns false on malformed input or overflow.
template <std::size_t N>
bool levels(std::string_view text, std::string_view key, std::array<Level, N>& output,
            std::uint16_t& count) noexcept {
  auto value = value_after(text, key);
  if (value.empty() || value.front() != '[') return false;
  value.remove_prefix(1);
  count = 0;
  while (!value.empty() && value.front() != ']') {
    const auto price_start = value.find('"');
    if (price_start == std::string_view::npos) return false;
    value.remove_prefix(price_start + 1);
    const auto price_end = value.find('"');
    if (price_end == std::string_view::npos) return false;
    const auto price = value.substr(0, price_end);
    value.remove_prefix(price_end + 1);
    const auto qty_start = value.find('"');
    if (qty_start == std::string_view::npos) return false;
    value.remove_prefix(qty_start + 1);
    const auto qty_end = value.find('"');
    if (qty_end == std::string_view::npos) return false;
    const auto quantity = value.substr(0, qty_end);
    value.remove_prefix(qty_end + 1);
    if (count >= N) return false;
    try {
      output[count++] = {Decimal::parse(price), Decimal::parse(quantity)};
    } catch (...) {
      return false;
    }
    const auto close = value.find(']');
    if (close == std::string_view::npos) return false;
    value.remove_prefix(close + 1);
    while (!value.empty() && (value.front() == ',' || value.front() == ' ')) value.remove_prefix(1);
  }
  return true;
}

// The `{...}` object that contains position `pos` (no string-aware brace matching;
// exchange metadata objects contain no braces inside strings).
inline std::string_view enclosing_object(std::string_view text, std::size_t pos) noexcept {
  const auto open = text.rfind('{', pos);
  const auto close = text.find('}', pos);
  if (open == std::string_view::npos || close == std::string_view::npos) return {};
  return text.substr(open, close - open + 1);
}

}  // namespace cxa::json
