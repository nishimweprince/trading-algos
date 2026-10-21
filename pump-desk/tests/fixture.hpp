#pragma once
// Loads tests/fixtures/golden.json (tools/fixtures/gen.mjs) and curve_golden.json (gen_curve.mjs).
#include <simdjson.h>

#include <cstdlib>
#include <string>
#include <vector>

#include "core/base58.hpp"
#include "core/base64.hpp"

#ifndef PD_FIXTURE_DIR
#define PD_FIXTURE_DIR "tests/fixtures"
#endif

namespace pdtest {

inline simdjson::dom::element& golden() {
  static simdjson::dom::parser parser;
  static simdjson::dom::element root = parser.load(std::string(PD_FIXTURE_DIR) + "/golden.json");
  return root;
}

/** tests/fixtures/curve_golden.json (tools/fixtures/gen_curve.mjs). */
inline simdjson::dom::element& curve_golden() {
  static simdjson::dom::parser parser;
  static simdjson::dom::element root = parser.load(std::string(PD_FIXTURE_DIR) + "/curve_golden.json");
  return root;
}

inline std::string str(simdjson::dom::element e) { return std::string(std::string_view(e)); }

inline std::vector<uint8_t> unhex(std::string_view h) {
  std::vector<uint8_t> out(h.size() / 2);
  for (size_t i = 0; i < out.size(); ++i) out[i] = static_cast<uint8_t>(std::strtoul(std::string(h.substr(2 * i, 2)).c_str(), nullptr, 16));
  return out;
}

inline std::string hex(const uint8_t* p, size_t n) {
  static const char* d = "0123456789abcdef";
  std::string s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; ++i) { s.push_back(d[p[i] >> 4]); s.push_back(d[p[i] & 15]); }
  return s;
}

inline std::vector<uint8_t> b64bytes(simdjson::dom::element e) {
  std::vector<uint8_t> out;
  pd::b64_decode(std::string_view(e), out);
  return out;
}

inline pd::Pubkey key(simdjson::dom::element e) { return pd::parse_pubkey(std::string_view(e)); }

}  // namespace pdtest
