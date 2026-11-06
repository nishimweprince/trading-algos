#include "app/pools_file.hpp"

#include <simdjson.h>

#include <cstdio>

#include "core/base58.hpp"

namespace arb::app {

std::string to_json(const PoolsFile& f) {
  std::string s = "{\"pools\":[";
  char buf[64];
  for (size_t i = 0; i < f.pools.size(); ++i) {
    const PoolDef& d = f.pools[i];
    if (i) s += ",";
    s += "{\"kind\":\"" + std::string(dex_name(d.kind)) + "\",\"address\":\"" + b58(d.address) + "\",\"mint_a\":\"" + b58(d.mint_a) +
         "\",\"mint_b\":\"" + b58(d.mint_b) + "\",\"vault_a\":\"" + b58(d.vault_a) + "\",\"vault_b\":\"" + b58(d.vault_b) + "\"";
    std::snprintf(buf, sizeof buf, ",\"tvl_sol\":%.3f", i < f.tvl_sol.size() ? f.tvl_sol[i] : 0.0);
    s += buf;
    if (!is_zero(d.config)) s += ",\"config\":\"" + b58(d.config) + "\"";
    s += ",\"tick_arrays\":[";
    for (size_t j = 0; j < d.tick_arrays.size(); ++j) {
      if (j) s += ",";
      s += "[" + std::to_string(d.tick_arrays[j].first) + ",\"" + b58(d.tick_arrays[j].second) + "\"]";
    }
    s += "]}";
  }
  s += "],\"reserves\":[";
  for (size_t i = 0; i < f.reserves.size(); ++i) s += (i ? ",\"" : "\"") + b58(f.reserves[i]) + "\"";
  return s + "]}\n";
}

bool from_json(const std::string& body, PoolsFile& out, std::string* err) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root;
  if (p.parse(ps).get(root) != simdjson::SUCCESS) {
    if (err) *err = "pools file: bad JSON";
    return false;
  }
  auto key = [](simdjson::dom::element e, const char* f, Pubkey& k) {
    std::string_view s;
    return e[f].get(s) == simdjson::SUCCESS && b58_decode32(s, k);
  };
  simdjson::dom::array pools;
  if (root["pools"].get(pools) == simdjson::SUCCESS) {
    for (auto e : pools) {
      PoolDef d;
      std::string_view kind;
      if (e["kind"].get(kind) != simdjson::SUCCESS) continue;
      auto k = dex_kind_from_name(kind);
      if (!k || !dex_program(*k)) continue;  // a kind this build does not support
      d.kind = *k;
      if (!key(e, "address", d.address) || !key(e, "mint_a", d.mint_a) || !key(e, "mint_b", d.mint_b) ||
          !key(e, "vault_a", d.vault_a) || !key(e, "vault_b", d.vault_b)) {
        if (err) *err = "pools file: bad pool entry";
        return false;
      }
      if (std::string_view cs; e["config"].get(cs) == simdjson::SUCCESS && !b58_decode32(cs, d.config)) {
        if (err) *err = "pools file: bad config key";
        return false;
      }
      simdjson::dom::array ta;
      if (e["tick_arrays"].get(ta) == simdjson::SUCCESS) {
        for (auto t : ta) {
          int64_t start = 0;
          std::string_view pk;
          Pubkey k;
          if (t.at(0).get(start) == simdjson::SUCCESS && t.at(1).get(pk) == simdjson::SUCCESS && b58_decode32(pk, k))
            d.tick_arrays.emplace_back(int32_t(start), k);
        }
      }
      double tvl = 0;
      if (e["tvl_sol"].get(tvl) != simdjson::SUCCESS) tvl = 0;
      out.pools.push_back(std::move(d));
      out.tvl_sol.push_back(tvl);
    }
  }
  simdjson::dom::array res;
  if (root["reserves"].get(res) == simdjson::SUCCESS) {
    for (auto e : res) {
      std::string_view s;
      Pubkey k;
      if (e.get(s) == simdjson::SUCCESS && b58_decode32(s, k)) out.reserves.push_back(k);
    }
  }
  return true;
}

}  // namespace arb::app
