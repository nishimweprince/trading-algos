// Preconfirmations: notification and transaction parsing, the subscribe body, credit billing, pool matching through
// lookup tables, the Sender Max tip rule, and speculative snapshots that never change the live store.
#include <cstring>
#include <string>
#include <vector>

#include "app/preconf_sim.hpp"
#include "core/base58.hpp"
#include "core/programs.hpp"
#include "feed/preconf.hpp"
#include "risk/sender_budget.hpp"
#include "state/state_store.hpp"
#include "test.hpp"

using namespace arb;

namespace {

Pubkey key(uint8_t b) {
  Pubkey k{};
  k.fill(b);
  return k;
}
void put_key(std::string& s, const Pubkey& k) { s.append(reinterpret_cast<const char*>(k.data()), 32); }

// A legacy/v0 transaction: 1 signature, header, the static keys, blockhash, one instruction, v0 lookups.
std::string legacy_or_v0(bool v0, const std::vector<Pubkey>& keys, const Pubkey& table, std::vector<uint8_t> w, std::vector<uint8_t> r) {
  std::string t;
  t += char(1);
  t += std::string(64, char(0x5a));
  if (v0) t += char(0x80);
  t += char(1); t += char(0); t += char(1);
  t += char(keys.size());
  for (auto& k : keys) put_key(t, k);
  t += std::string(32, char(7));
  t += char(1);                         // one instruction
  t += char(0); t += char(1); t += char(0); t += char(2); t += char(9); t += char(9);  // prog 0, 1 account [0], 2 data bytes
  if (v0) {
    t += char(1);
    put_key(t, table);
    t += char(w.size()); for (auto x : w) t += char(x);
    t += char(r.size()); for (auto x : r) t += char(x);
  }
  return t;
}

// A v1 transaction (SIMD-0385): version, header, config mask, lifetime, counts, keys, config values, ix headers,
// payloads, then the signatures.
std::string v1(const std::vector<Pubkey>& keys) {
  std::string t;
  t += char(0x81);
  t += char(1); t += char(0); t += char(1);
  uint32_t mask = 0b100;  // compute-unit limit only: one 4-byte value
  t.append(reinterpret_cast<const char*>(&mask), 4);
  t += std::string(32, char(3));
  t += char(1);           // one instruction
  t += char(keys.size());
  for (auto& k : keys) put_key(t, k);
  t += std::string(4, char(0));
  t += char(0); t += char(1); t += char(3); t += char(0);  // prog 0, 1 account, 3 data bytes
  t += char(0); t += std::string(3, char(4));
  t += std::string(64, char(0x6b));
  return t;
}

}  // namespace

TEST(preconf_parses_notifications_and_all_transaction_versions) {
  std::string tx = legacy_or_v0(false, {key(1), key(2)}, {}, {}, {});
  std::string note;
  note += char(1);
  uint64_t slot = 471300000, idx = 7;
  note.append(reinterpret_cast<const char*>(&slot), 8);
  note.append(reinterpret_cast<const char*>(&idx), 8);
  note += char(1);
  note += tx;
  feed::PreconfTx p;
  CHECK(feed::parse_preconf(note, p));
  CHECK(p.slot == slot && p.tx_index == 7 && p.status == 1 && p.tx == tx);
  std::string bad = note;
  bad[0] = 2;
  CHECK(!feed::parse_preconf(bad, p));
  CHECK(!feed::parse_preconf(note.substr(0, 40), p));

  feed::TxKeys k;
  CHECK(feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(tx.data()), tx.size(), k));
  CHECK(k.version == 0xff && k.statics.size() == 2 && k.statics[1] == key(2) && k.lookups.empty() && k.sig[0] == 0x5a);
  std::string t0 = legacy_or_v0(true, {key(1)}, key(9), {0, 2}, {1});
  CHECK(feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(t0.data()), t0.size(), k));
  CHECK(k.version == 0 && k.lookups.size() == 1 && k.lookups[0].table == key(9) && k.lookups[0].writable.size() == 2 &&
        k.lookups[0].readonly[0] == 1);
  std::string t1 = v1({key(5), key(6), key(7)});
  CHECK(feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(t1.data()), t1.size(), k));
  CHECK(k.version == 1 && k.statics.size() == 3 && k.statics[2] == key(7) && k.sig[0] == 0x6b);
  CHECK(!feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(t1.data()), t1.size() - 1, k));  // truncated signature

  std::string body = feed::preconf_subscribe_body({"A1", "B2"}, false, {"fra"});
  CHECK_EQ(body, std::string(R"({"jsonrpc":"2.0","id":1,"method":"preconfSubscribe","params":[{"accountInclude":["A1","B2"],"failed":false,"regionInclude":["fra"]}]})"));
}

TEST(preconf_credit_guard_bills_per_slot_and_caps_the_day) {
  feed::CreditGuard g(9'500);
  const int64_t t = 1'795'000'000'000;
  for (int i = 0; i < 3; ++i) g.on_message(100, t);
  CHECK_EQ(g.today(t), uint64_t(300));  // a slot with any message costs at least 300
  for (int i = 0; i < 45; ++i) g.on_message(101, t);
  CHECK_EQ(g.today(t), uint64_t(300 + 450));
  g.add(2, t);  // simulations / table reads
  CHECK_EQ(g.today(t), uint64_t(752));
  for (uint64_t s = 120; s < 148; ++s) g.on_message(s, t);  // older slots fold into the finalized total
  CHECK_EQ(g.today(t), uint64_t(752 + 28 * 300));
  CHECK(!g.blocked(t));  // 9,152 + 300 <= 9,500
  g.on_message(151, t);
  CHECK(g.blocked(t));   // 9,452 + 300 > 9,500
  CHECK(!g.blocked(t + 86'400'000) && g.today(t + 86'400'000) == 0);
}

TEST(preconf_matches_pools_through_lookup_tables) {
  std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_of{{key(10), 3}, {key(11), 4}};
  std::unordered_map<Pubkey, std::vector<Pubkey>, PubkeyHash> tables;
  std::string t0 = legacy_or_v0(true, {key(1)}, key(9), {0}, {2});
  feed::TxKeys k;
  CHECK(feed::parse_tx_keys(reinterpret_cast<const uint8_t*>(t0.data()), t0.size(), k));
  std::vector<Pubkey> missing;
  CHECK(app::touched_pools(k, pool_of, tables, &missing).empty() && missing.size() == 1 && missing[0] == key(9));
  tables[key(9)] = {key(10), key(1), key(11)};
  missing.clear();
  auto pools = app::touched_pools(k, pool_of, tables, &missing);
  CHECK(pools.size() == 2 && pools[0] == 3 && pools[1] == 4 && missing.empty());
  tables[key(9)] = {key(10)};  // index 2 past the end: the table was extended since we read it
  pools = app::touched_pools(k, pool_of, tables, &missing);
  CHECK(pools.size() == 1 && missing.size() == 1);
}

TEST(sender_max_tip_is_at_least_the_minimum_and_a_share_of_gross) {
  risk::SenderFeeConfig c;
  c.tip_lamports = 1'000'000;
  c.tip_frac_ppm = 200'000;
  c.max_tip_share_ppm = 500'000;
  CHECK(!risk::sender_fee(1'999'999, 150'000, c).ok);  // 0.001 SOL would be over half of gross
  auto a = risk::sender_fee(2'000'000, 150'000, c);
  CHECK(a.ok && a.tip_lamports == 1'000'000);           // 20% of gross is below the minimum: the minimum
  auto b = risk::sender_fee(20'000'000, 150'000, c);
  CHECK(b.ok && b.tip_lamports == 4'000'000);           // 20% of gross
  CHECK(b.fee_lamports <= 2'000'000);                   // a failure still costs at most 10% of gross
  risk::SenderFeeConfig plain;                          // Sender (swqos): unchanged
  CHECK(risk::sender_fee(372'000, 150'000, plain).tip_lamports == 10'000);
}

TEST(speculative_snapshot_never_changes_the_live_store) {
  StateStore st;
  PoolDef d;
  d.kind = DexKind::RaydiumV4;
  d.address = key(20);
  d.mint_a = key(21);
  d.mint_b = key(22);
  d.vault_a = key(23);
  d.vault_b = key(24);
  uint32_t id = st.add_pool(d);
  auto token = [&](const Pubkey& mint, uint64_t amount) {
    std::vector<uint8_t> v(165, 0);
    std::memcpy(v.data(), mint.data(), 32);
    std::memcpy(v.data() + 64, &amount, 8);
    v[108] = 1;
    return v;
  };
  auto va = token(d.mint_a, 1'000), vb = token(d.mint_b, 2'000);
  AccountUpdate ua{d.vault_a, prog::TOKEN, va.data(), va.size(), 50, 1, false, false};
  AccountUpdate ub{d.vault_b, prog::TOKEN, vb.data(), vb.size(), 50, 2, false, false};
  CHECK(st.apply(ua) && st.apply(ub));
  auto live = st.pool(id);
  CHECK(live && live->vault_a_amount == 1'000 && live->vault_b_amount == 2'000);
  auto va2 = token(d.mint_a, 5'555);
  AccountUpdate spec{d.vault_a, prog::TOKEN, va2.data(), va2.size(), 51, 0, false, false};
  std::vector<AccountUpdate> ups{spec};
  auto s = st.speculate(id, ups);
  CHECK(s && s->vault_a_amount == 5'555 && s->vault_b_amount == 2'000 && s->vault_a_slot == 51);
  // The published snapshot and the entries behind it are unchanged: a real update builds from the live values.
  CHECK(st.pool(id) == live && st.pool(id)->vault_a_amount == 1'000);
  auto vb2 = token(d.mint_b, 2'100);
  AccountUpdate ub2{d.vault_b, prog::TOKEN, vb2.data(), vb2.size(), 52, 3, false, false};
  CHECK(st.apply(ub2));
  CHECK(st.pool(id)->vault_a_amount == 1'000 && st.pool(id)->vault_b_amount == 2'100);
  // Accounts of other pools are ignored; nothing of this pool -> no snapshot.
  auto other = token(key(30), 1);
  AccountUpdate foreign{key(31), prog::TOKEN, other.data(), other.size(), 53, 0, false, false};
  std::vector<AccountUpdate> f{foreign};
  CHECK(!st.speculate(id, f));
  auto keys = st.keys_of_pool(id);
  CHECK(std::find(keys.begin(), keys.end(), d.vault_a) != keys.end() && std::find(keys.begin(), keys.end(), d.address) != keys.end());
}
