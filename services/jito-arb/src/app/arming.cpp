#include "app/arming.hpp"

#include <simdjson.h>

#include <fstream>
#include <set>
#include <sstream>

#include "app/bundle_builder.hpp"
#include "core/base58.hpp"
#include "core/programs.hpp"
#include "obs/log.hpp"
#include "tx/builders.hpp"

namespace arb::app {

std::vector<AtaNeed> needed_atas(const StateStore& store, const Pubkey& wallet) {
  std::vector<AtaNeed> out;
  std::set<Pubkey> seen;
  for (uint32_t i = 0; i < store.pool_count(); ++i) {
    auto p = store.pool(i);
    if (!p) continue;
    for (const Pubkey& m : {p->mint_a, p->mint_b}) {
      const auto& info = m == p->mint_a ? p->mint_a_info : p->mint_b_info;
      const Pubkey tp = info && info->token_program == prog::TOKEN_2022 ? prog::TOKEN_2022 : prog::TOKEN;
      Pubkey a = tx::wallet_ata(wallet, m, *p);
      if (seen.insert(a).second) out.push_back({a, m, tp});
    }
  }
  return out;
}

std::vector<Pubkey> needed_table_keys(const StateStore& store, const Pubkey& wallet, std::span<const flash::FlashReserve> reserves,
                                      const std::optional<Pubkey>& executor) {
  std::vector<Pubkey> out;
  std::set<Pubkey> seen{wallet};  // the signer is always a static key
  auto add = [&](const Pubkey& k) {
    if (!is_zero(k) && seen.insert(k).second) out.push_back(k);
  };
  // The executor id is unknown until it is deployed: leave it out rather than table a placeholder.
  Pubkey placeholder = dry_run_executor();
  for (const Pubkey& k : shared_table_keys(wallet, reserves, executor.value_or(placeholder)))
    if (k != placeholder) add(k);
  for (uint32_t i = 0; i < store.pool_count(); ++i)
    if (auto p = store.pool(i))
      for (const Pubkey& k : pool_table_keys(*p, wallet)) add(k);
  return out;
}

std::vector<tx::IxList> ata_txs(const std::vector<AtaNeed>& atas, const Pubkey& wallet, size_t per_tx) {
  std::vector<tx::IxList> out;
  for (size_t i = 0; i < atas.size(); i += per_tx) {
    tx::IxList l;
    for (size_t j = i; j < std::min(atas.size(), i + per_tx); ++j)
      // Associated Token Account CreateIdempotent (1): payer, ata, owner, mint, system, token program.
      l.add(prog::ATA).acc(wallet, true, true).rw(atas[j].ata).ro(wallet).ro(atas[j].mint).ro(prog::SYSTEM).ro(atas[j].token_program).u8(1);
    out.push_back(l);
  }
  return out;
}

void add_compute_budget(std::vector<tx::IxList>& txs, uint32_t cu_limit, uint64_t cu_price_micro_lamports) {
  for (tx::IxList& l : txs) {
    tx::IxList b;
    tx::set_compute_unit_limit(b, cu_limit);
    if (cu_price_micro_lamports) tx::set_compute_unit_price(b, cu_price_micro_lamports);
    for (uint8_t i = 0; i < l.n; ++i) b.ixs[b.n++] = l.ixs[i];
    l = b;
  }
}

bool setup_programs_allowed(const tx::IxList& ixs) {
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const Pubkey& p = ixs.ixs[i].program;
    if (p != prog::COMPUTE_BUDGET && p != prog::ATA && p != prog::ALT && p != prog::PUMPSWAP) return false;
  }
  return true;
}

std::string tables_to_json(const std::vector<Pubkey>& tables) {
  std::string s = "{\"tables\":[";
  for (size_t i = 0; i < tables.size(); ++i) s += (i ? ",\"" : "\"") + b58(tables[i]) + "\"";
  return s + "]}\n";
}

bool tables_from_json(const std::string& body, std::vector<Pubkey>& out, std::string* err) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::array arr;
  if (p.parse(ps)["tables"].get(arr) != simdjson::SUCCESS) {
    if (err) *err = "no \"tables\" array";
    return false;
  }
  std::vector<Pubkey> v;
  for (auto e : arr) {
    std::string_view s;
    Pubkey k;
    if (e.get(s) != simdjson::SUCCESS || !b58_decode32(s, k)) {
      if (err) *err = "bad table address";
      return false;
    }
    v.push_back(k);
  }
  out = std::move(v);
  return true;
}

size_t load_tables(Rpc& rpc, const std::string& path, tx::AltManager& alts, std::vector<Pubkey>* loaded) {
  std::ifstream f(path);
  if (!f) return 0;
  std::stringstream ss;
  ss << f.rdbuf();
  std::vector<Pubkey> keys;
  std::string err;
  if (!tables_from_json(ss.str(), keys, &err)) throw std::runtime_error(path + ": " + err);
  size_t n = 0;
  for (size_t i = 0; i < keys.size(); i += 100) {
    std::vector<Pubkey> batch(keys.begin() + long(i), keys.begin() + long(std::min(keys.size(), i + 100)));
    auto accs = rpc.multiple_accounts(batch, "confirmed");
    for (size_t j = 0; j < batch.size() && j < accs.size(); ++j) {
      if (!accs[j]) {
        LOG_WARN("lookup table %s not found on chain: skipped", b58(batch[j]).c_str());
        continue;
      }
      auto a = tx::decode_lookup_table(accs[j]->data.data(), accs[j]->data.size(), accs[j]->owner);
      if (!a) {
        LOG_WARN("lookup table %s: %s", b58(batch[j]).c_str(), a.error().what);
        continue;
      }
      if (a->deactivation_slot != UINT64_MAX) {
        LOG_WARN("lookup table %s is deactivated: skipped", b58(batch[j]).c_str());
        continue;
      }
      alts.upsert(batch[j], *a);
      if (loaded) loaded->push_back(batch[j]);
      ++n;
    }
  }
  return n;
}

}  // namespace arb::app
