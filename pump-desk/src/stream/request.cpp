#include "stream/request.hpp"

#include "core/base58.hpp"
#include "core/pb.hpp"
#include "sol/constants.hpp"
#include "sol/layouts.hpp"

namespace pd {

namespace {

pb::Writer memcmp_filter(uint64_t offset, const uint8_t* bytes, size_t n) {
  // SubscribeRequestFilterAccountsFilter { memcmp = 1 { offset = 1, base58 = 3 } }
  pb::Writer m;
  m.u64(1, offset);
  m.bytes(3, b58(bytes, n));
  pb::Writer f;
  f.message(1, m);
  return f;
}

template <class Include, class Required>
pb::Writer tx_filter(bool failed_set, bool failed, const Include& include, const Required& required) {
  // SubscribeRequestFilterTransactions { vote 1, failed 2, account_include 3, account_required 6 }
  pb::Writer t;
  t.boolean(1, false);
  if (failed_set) t.boolean(2, failed);
  for (const Pubkey& k : include) t.bytes(3, b58(k));
  for (const Pubkey& k : required) t.bytes(6, b58(k));
  return t;
}

using Keys = std::initializer_list<Pubkey>;

}  // namespace

std::string encode_subscribe_request(const SubscriptionSpec& spec) {
  pb::Writer req;
  // accounts (1): band filters over near-complete bonding curves.
  for (int v = 0; v < spec.band_values; ++v) {
    pb::Writer a;
    a.bytes(3, b58(prog::PUMP_FUN));  // owner
    a.message(4, memcmp_filter(0, disc::BONDING_CURVE, 8));
    const uint8_t top[3] = {static_cast<uint8_t>(v), 0, 0};
    a.message(4, memcmp_filter(CURVE_REAL_TOKEN_TOP, top, 3));
    req.map_entry(1, "band" + std::to_string(v), a);
  }
  if (!spec.watched.empty()) {
    pb::Writer a;
    for (const Pubkey& k : spec.watched) a.bytes(2, b58(k));  // account
    req.map_entry(1, "watch", a);
  }
  // slots (2): heartbeat at the subscription commitment, plus the inter-slot
  // statuses (first shred, completed) that time how late a fill is seen.
  {
    pb::Writer s;
    s.boolean(1, true);
    s.boolean(2, true);
    req.map_entry(2, "heartbeat", s);
  }
  // transactions (3)
  req.map_entry(3, "migrate", tx_filter(false, false, Keys{}, Keys{prog::PUMP_FUN, prog::MIGRATION_AUTHORITY}));
  req.map_entry(3, "creates", tx_filter(true, false, Keys{}, Keys{prog::PUMP_FUN, prog::MINT_AUTHORITY}));
  if (!is_zero(spec.wallet)) req.map_entry(3, "wallet", tx_filter(false, false, Keys{spec.wallet}, Keys{}));
  // Failed txs too: a rival's reverted attempt still shows how it paid.
  if (!spec.pool_txs.empty()) req.map_entry(3, "pools", tx_filter(false, false, spec.pool_txs, Keys{}));
  // commitment (6) = PROCESSED (0); optional field, so presence is explicit.
  req.u64(6, 0);
  return req.out;
}

std::string encode_ping_request(int32_t id) {
  pb::Writer ping;
  ping.u64(1, static_cast<uint64_t>(static_cast<uint32_t>(id)));
  pb::Writer req;
  req.message(9, ping);
  return req.out;
}

}  // namespace pd
