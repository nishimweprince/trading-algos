#include "tx/message_v0.hpp"

#include <algorithm>

#include "core/compact_u16.hpp"

namespace arb::tx {

namespace {

struct KeyMeta {
  Pubkey key;
  bool signer, writable, invoked;
  bool drained;  // moved into a lookup table (keyMetaMap.delete)
};

int find_or_insert(std::vector<KeyMeta>& metas, const Pubkey& k) {
  for (size_t i = 0; i < metas.size(); ++i)
    if (metas[i].key == k) return static_cast<int>(i);
  if (metas.size() >= MAX_TX_KEYS) return -1;
  metas.push_back(KeyMeta{k, false, false, false, false});
  return static_cast<int>(metas.size() - 1);
}

}  // namespace

Result<size_t> compile_message_v0(const Pubkey& payer, const IxList& ixs, const Hash& blockhash,
                                  std::span<const LookupTable> tables, uint8_t* out, size_t cap,
                                  std::span<const Pubkey> force_static, CompileStats* stats) {
  // CompiledKeys.compile: payer first, then per instruction the program (isInvoked) and its
  // accounts, in first-seen order; signer/writable flags OR together.
  std::vector<KeyMeta> metas;
  metas.reserve(64);
  int p = find_or_insert(metas, payer);
  metas[size_t(p)].signer = metas[size_t(p)].writable = true;
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const Ix& x = ixs.ixs[i];
    int pi = find_or_insert(metas, x.program);
    if (pi < 0) return fail(Errc::too_many_keys, "tx keys");
    metas[size_t(pi)].invoked = true;
    for (uint8_t a = 0; a < x.n_accounts; ++a) {
      int k = find_or_insert(metas, x.accounts[a].key);
      if (k < 0) return fail(Errc::too_many_keys, "tx keys");
      metas[size_t(k)].signer |= x.accounts[a].signer;
      metas[size_t(k)].writable |= x.accounts[a].writable;
    }
  }
  auto pinned = [&](const Pubkey& k) { return std::find(force_static.begin(), force_static.end(), k) != force_static.end(); };

  // extractTableLookup per table: writable drain then readonly drain, each in keyMetaMap order.
  struct Lookup {
    const LookupTable* table;
    std::vector<uint8_t> w_idx, r_idx;
    std::vector<Pubkey> w_keys, r_keys;
  };
  std::vector<Lookup> lookups;
  for (const LookupTable& t : tables) {
    Lookup lk{&t, {}, {}, {}, {}};
    for (int pass = 0; pass < 2; ++pass) {
      bool want_writable = pass == 0;
      for (KeyMeta& m : metas) {
        if (m.drained || m.signer || m.invoked || m.writable != want_writable || pinned(m.key)) continue;
        auto it = std::find(t.addresses.begin(), t.addresses.end(), m.key);
        if (it == t.addresses.end()) continue;
        size_t idx = size_t(it - t.addresses.begin());
        if (idx >= 256) return fail(Errc::too_many_keys, "lookup index >= 256");
        (want_writable ? lk.w_idx : lk.r_idx).push_back(uint8_t(idx));
        (want_writable ? lk.w_keys : lk.r_keys).push_back(m.key);
        m.drained = true;
      }
    }
    if (!lk.w_idx.empty() || !lk.r_idx.empty()) lookups.push_back(std::move(lk));
  }

  // getMessageComponents over the remaining keys: writable signers, readonly signers,
  // writable non-signers, readonly non-signers.
  std::vector<Pubkey> ordered;
  ordered.reserve(metas.size());
  uint8_t n_ro_signed = 0, n_ro_unsigned = 0, n_signers = 0;
  for (int pass = 0; pass < 4; ++pass) {
    bool want_signer = pass < 2;
    bool want_writable = (pass % 2) == 0;
    for (const KeyMeta& m : metas) {
      if (m.drained || m.signer != want_signer || m.writable != want_writable) continue;
      ordered.push_back(m.key);
      if (want_signer) ++n_signers;
      if (want_signer && !want_writable) ++n_ro_signed;
      if (!want_signer && !want_writable) ++n_ro_unsigned;
    }
  }
  // MessageAccountKeys: static keys, then all lookup writable keys, then all lookup readonly keys.
  std::vector<Pubkey> all = ordered;
  size_t n_lw = 0, n_lr = 0;
  for (auto& lk : lookups) { all.insert(all.end(), lk.w_keys.begin(), lk.w_keys.end()); n_lw += lk.w_keys.size(); }
  for (auto& lk : lookups) { all.insert(all.end(), lk.r_keys.begin(), lk.r_keys.end()); n_lr += lk.r_keys.size(); }
  if (all.size() > 256) return fail(Errc::too_many_keys, "more than 256 account keys");
  auto index_of = [&](const Pubkey& k) -> uint8_t {
    for (size_t i = 0; i < all.size(); ++i)
      if (all[i] == k) return static_cast<uint8_t>(i);
    return 0xff;  // unreachable: every key was inserted above
  };

  uint8_t buf[8192];
  size_t o = 0;
  buf[o++] = 0x80;  // v0 prefix
  buf[o++] = n_signers;
  buf[o++] = n_ro_signed;
  buf[o++] = n_ro_unsigned;
  o += compact_u16_encode(uint16_t(ordered.size()), buf + o);
  for (const Pubkey& k : ordered) { std::memcpy(buf + o, k.data(), 32); o += 32; }
  std::memcpy(buf + o, blockhash.data(), 32);
  o += 32;
  o += compact_u16_encode(ixs.n, buf + o);
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const Ix& x = ixs.ixs[i];
    buf[o++] = index_of(x.program);
    o += compact_u16_encode(x.n_accounts, buf + o);
    for (uint8_t a = 0; a < x.n_accounts; ++a) buf[o++] = index_of(x.accounts[a].key);
    o += compact_u16_encode(x.data_len, buf + o);
    std::memcpy(buf + o, x.data, x.data_len);
    o += x.data_len;
  }
  o += compact_u16_encode(uint16_t(lookups.size()), buf + o);
  for (auto& lk : lookups) {
    std::memcpy(buf + o, lk.table->key.data(), 32);
    o += 32;
    o += compact_u16_encode(uint16_t(lk.w_idx.size()), buf + o);
    for (uint8_t v : lk.w_idx) buf[o++] = v;
    o += compact_u16_encode(uint16_t(lk.r_idx.size()), buf + o);
    for (uint8_t v : lk.r_idx) buf[o++] = v;
  }
  if (stats) *stats = CompileStats{ordered.size(), n_lw, n_lr, lookups.size()};
  if (o > cap) return fail(Errc::tx_too_large, "message exceeds buffer");
  std::memcpy(out, buf, o);
  return o;
}

Result<size_t> build_tx(const Pubkey& payer, const Keypair* payer_kp, const IxList& ixs, const Hash& blockhash,
                        std::span<const LookupTable> tables, SignedTx& out, std::span<const Pubkey> force_static) {
  // Wire form: shortvec(num signatures) | signatures | message. The payer is the only signer.
  uint8_t msg[8192];
  auto m = compile_message_v0(payer, ixs, blockhash, tables, msg, sizeof msg, force_static);
  if (!m) return std::unexpected(m.error());
  if (msg[1] != 1) return fail(Errc::sign_guard, "transaction needs a signer other than the payer");
  size_t total = 1 + 64 + *m;
  if (total > PACKET_DATA_SIZE) return fail(Errc::tx_too_large, "tx_too_large");
  out.bytes[0] = 1;
  std::memcpy(out.bytes + 65, msg, *m);
  if (payer_kp) payer_kp->sign(out.bytes + 65, *m, out.bytes + 1);
  else std::memset(out.bytes + 1, 0, 64);
  std::memcpy(out.sig.data(), out.bytes + 1, 64);
  out.len = static_cast<uint16_t>(total);
  return total;
}

}  // namespace arb::tx
