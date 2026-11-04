#pragma once
// Loads tests/fixtures/accounts.json (real mainnet account bytes) and friends.
#include <simdjson.h>

#include <fstream>
#include <map>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "state/discovery.hpp"

namespace arbtest {

inline std::string read_file(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

inline std::string fixture_path(const char* name) { return std::string(ARB_FIXTURE_DIR) + "/" + name; }

struct FixtureAccount {
  arb::state::RawAccount raw;
  uint64_t slot = 0;
  uint64_t lamports = 0;
  bool exists = false;
};

/** pubkey (base58) -> account; null entries (no account on chain) have exists=false. */
inline const std::map<std::string, FixtureAccount>& fixture_accounts() {
  static std::map<std::string, FixtureAccount> m = [] {
    std::map<std::string, FixtureAccount> out;
    std::string body = read_file(fixture_path("accounts.json"));
    simdjson::dom::parser p;
    simdjson::padded_string ps(body);
    simdjson::dom::object accs = p.parse(ps)["accounts"];
    for (auto kv : accs) {
      FixtureAccount a;
      std::string key(kv.key);
      arb::b58_decode32(key, a.raw.key);
      if (!kv.value.is_null()) {
        a.exists = true;
        std::string_view s;
        if (kv.value["owner"].get(s) == simdjson::SUCCESS) arb::b58_decode32(s, a.raw.owner);
        if (kv.value["data"].get(s) == simdjson::SUCCESS) arb::b64_decode(s, a.raw.data);
        uint64_t v = 0;
        if (kv.value["slot"].get(v) == simdjson::SUCCESS) a.slot = v;
        if (kv.value["lamports"].get(v) == simdjson::SUCCESS) a.lamports = v;
      }
      out[key] = std::move(a);
    }
    return out;
  }();
  return m;
}

inline const FixtureAccount* fixture(const std::string& key) {
  auto& m = fixture_accounts();
  auto it = m.find(key);
  return it == m.end() ? nullptr : &it->second;
}

inline arb::state::AccountLookup fixture_lookup() {
  arb::state::AccountLookup l;
  for (auto& [k, a] : fixture_accounts())
    if (a.exists) l[a.raw.key] = a.raw;
  return l;
}

}  // namespace arbtest
