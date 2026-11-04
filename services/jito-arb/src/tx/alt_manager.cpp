#include "tx/alt_manager.hpp"

#include <algorithm>
#include <set>

namespace arb::tx {

void AltManager::upsert(const Pubkey& table, const AltAccount& a) { tables_[table] = Entry{a}; }

size_t AltManager::active_len(const AltAccount& a, uint64_t slot) const {
  if (a.deactivation_slot != UINT64_MAX) return 0;  // deactivating tables are not used for new transactions
  if (slot > a.last_extended_slot + pending_) return a.addresses.size();
  return std::min<size_t>(a.last_extended_start, a.addresses.size());
}

std::vector<LookupTable> AltManager::active(uint64_t slot) const {
  std::vector<LookupTable> out;
  for (auto& [k, e] : tables_) {
    size_t n = active_len(e.acc, slot);
    if (n == 0) continue;
    LookupTable t;
    t.key = k;
    t.addresses.assign(e.acc.addresses.begin(), e.acc.addresses.begin() + long(n));
    out.push_back(std::move(t));
  }
  return out;
}

bool AltManager::ready(std::span<const Pubkey> keys, uint64_t slot) const {
  std::set<Pubkey> act;
  for (auto& [k, e] : tables_) {
    size_t n = active_len(e.acc, slot);
    act.insert(e.acc.addresses.begin(), e.acc.addresses.begin() + long(n));
  }
  return std::all_of(keys.begin(), keys.end(), [&](const Pubkey& k) { return act.count(k) != 0; });
}

std::vector<Pubkey> AltManager::missing(std::span<const Pubkey> keys) const {
  std::set<Pubkey> all;
  for (auto& [k, e] : tables_) all.insert(e.acc.addresses.begin(), e.acc.addresses.end());
  std::vector<Pubkey> out;
  std::set<Pubkey> seen;
  for (const Pubkey& k : keys)
    if (!all.count(k) && seen.insert(k).second) out.push_back(k);
  return out;
}

std::vector<IxList> AltManager::plan_extend(std::span<const Pubkey> keys, const Pubkey& authority, const Pubkey& payer,
                                            uint64_t recent_slot, std::vector<Pubkey>* created) const {
  std::vector<IxList> txs;
  std::vector<Pubkey> todo = missing(keys);
  size_t i = 0;
  // Fill existing tables owned by `authority` that still have room.
  for (auto& [k, e] : tables_) {
    if (i >= todo.size()) break;
    if (!e.acc.has_authority || e.acc.authority != authority || e.acc.deactivation_slot != UINT64_MAX) continue;
    size_t room = ALT_MAX_ADDRESSES - std::min(ALT_MAX_ADDRESSES, e.acc.addresses.size());
    while (room > 0 && i < todo.size()) {
      size_t n = std::min({room, ALT_EXTEND_CHUNK, todo.size() - i});
      IxList l;
      extend_lookup_table(l, k, authority, payer, std::span<const Pubkey>(todo.data() + i, n));
      txs.push_back(l);
      i += n;
      room -= n;
    }
  }
  // Then new tables: create (in the first transaction) and extend.
  uint64_t slot = recent_slot;
  while (i < todo.size()) {
    IxList l;
    Pubkey table;
    create_lookup_table(l, authority, payer, slot, &table);
    if (created) created->push_back(table);
    size_t room = ALT_MAX_ADDRESSES;
    bool first = true;
    while (room > 0 && i < todo.size()) {
      size_t n = std::min({room, ALT_EXTEND_CHUNK, todo.size() - i});
      if (!first) l.clear();
      extend_lookup_table(l, table, authority, payer, std::span<const Pubkey>(todo.data() + i, n));
      txs.push_back(l);
      first = false;
      i += n;
      room -= n;
    }
    --slot;  // a second new table needs a distinct (still recent) slot
  }
  return txs;
}

}  // namespace arb::tx
