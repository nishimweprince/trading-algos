#include "sol/tx.hpp"

#include "sol/constants.hpp"

namespace pd {

namespace {

struct KeyMeta {
  Pubkey key;
  bool signer, writable;
};

size_t shortvec(uint8_t* out, size_t v) {
  size_t n = 0;
  for (;;) {
    uint8_t b = v & 0x7f;
    v >>= 7;
    if (v == 0) { out[n++] = b; return n; }
    out[n++] = b | 0x80;
  }
}

int find_or_insert(KeyMeta* metas, size_t& n, const Pubkey& k) {
  for (size_t i = 0; i < n; ++i)
    if (metas[i].key == k) return static_cast<int>(i);
  if (n >= MAX_TX_KEYS) return -1;
  metas[n] = KeyMeta{k, false, false};
  return static_cast<int>(n++);
}

}  // namespace

size_t compile_message_v0(const Pubkey& payer, const IxList& ixs, const Hash& blockhash, uint8_t* out, size_t cap) {
  // CompiledKeys.compile: payer first, then per ix the program, then its accounts,
  // in first-seen order; flags OR together.
  KeyMeta metas[MAX_TX_KEYS];
  size_t n = 0;
  int p = find_or_insert(metas, n, payer);
  metas[p].signer = metas[p].writable = true;
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const Ix& x = ixs.ixs[i];
    if (find_or_insert(metas, n, x.program) < 0) return 0;
    for (uint8_t a = 0; a < x.n_accounts; ++a) {
      int k = find_or_insert(metas, n, x.accounts[a].key);
      if (k < 0) return 0;
      metas[k].signer |= x.accounts[a].signer;
      metas[k].writable |= x.accounts[a].writable;
    }
  }
  // getMessageComponents: writable signers, readonly signers, writable non-signers, readonly non-signers.
  Pubkey ordered[MAX_TX_KEYS];
  size_t m = 0;
  uint8_t n_ro_signed = 0, n_ro_unsigned = 0, n_signers = 0;
  for (int pass = 0; pass < 4; ++pass) {
    bool want_signer = pass < 2;
    bool want_writable = (pass % 2) == 0;
    for (size_t i = 0; i < n; ++i) {
      if (metas[i].signer != want_signer || metas[i].writable != want_writable) continue;
      ordered[m++] = metas[i].key;
      if (want_signer) ++n_signers;
      if (want_signer && !want_writable) ++n_ro_signed;
      if (!want_signer && !want_writable) ++n_ro_unsigned;
    }
  }
  auto index_of = [&](const Pubkey& k) -> uint8_t {
    for (size_t i = 0; i < m; ++i)
      if (ordered[i] == k) return static_cast<uint8_t>(i);
    return 0xff;  // unreachable: every key was inserted above
  };

  // Worst case is far below the local buffer; check against `cap` as we go.
  uint8_t buf[4096];
  size_t o = 0;
  buf[o++] = 0x80;  // v0 prefix
  buf[o++] = n_signers;
  buf[o++] = n_ro_signed;
  buf[o++] = n_ro_unsigned;
  o += shortvec(buf + o, m);
  for (size_t i = 0; i < m; ++i) { std::memcpy(buf + o, ordered[i].data(), 32); o += 32; }
  std::memcpy(buf + o, blockhash.data(), 32);
  o += 32;
  o += shortvec(buf + o, ixs.n);
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const Ix& x = ixs.ixs[i];
    buf[o++] = index_of(x.program);
    o += shortvec(buf + o, x.n_accounts);
    for (uint8_t a = 0; a < x.n_accounts; ++a) buf[o++] = index_of(x.accounts[a].key);
    o += shortvec(buf + o, x.data_len);
    std::memcpy(buf + o, x.data, x.data_len);
    o += x.data_len;
  }
  buf[o++] = 0;  // no address table lookups
  if (o > cap) return 0;
  std::memcpy(out, buf, o);
  return o;
}

bool build_signed_tx(const Keypair& payer, const IxList& ixs, const Hash& blockhash, SignedTx& out) {
  // Wire form: shortvec(1) | signature | message.
  out.bytes[0] = 1;
  size_t msg_len = compile_message_v0(payer.pubkey(), ixs, blockhash, out.bytes + 65, sizeof(out.bytes) - 65);
  if (msg_len == 0 || 65 + msg_len > PACKET_DATA_SIZE) return false;
  payer.sign(out.bytes + 65, msg_len, out.bytes + 1);
  std::memcpy(out.sig.data(), out.bytes + 1, 64);
  out.len = static_cast<uint16_t>(65 + msg_len);
  return true;
}

namespace ix {

void cu_limit(IxList& l, uint32_t units) { l.add(prog::COMPUTE_BUDGET).u8(2).u32(units); }

void cu_price(IxList& l, uint64_t micro_lamports) { l.add(prog::COMPUTE_BUDGET).u8(3).u64(micro_lamports); }

void transfer(IxList& l, const Pubkey& from, const Pubkey& to, uint64_t lamports) {
  l.add(prog::SYSTEM).acc(from, true, true).rw(to).u32(2).u64(lamports);
}

void ata_idempotent(IxList& l, const Pubkey& payer, const Pubkey& ata_key, const Pubkey& owner, const Pubkey& mint,
                    const Pubkey& token_program) {
  l.add(prog::ATA)
      .acc(payer, true, true)
      .rw(ata_key)
      .ro(owner)
      .ro(mint)
      .ro(prog::SYSTEM)
      .ro(token_program)
      .u8(1);
}

void sync_native(IxList& l, const Pubkey& account) { l.add(prog::TOKEN).rw(account).u8(17); }

void close_account(IxList& l, const Pubkey& account, const Pubkey& dest, const Pubkey& owner, const Pubkey& token_program) {
  l.add(token_program).rw(account).rw(dest).acc(owner, true, false).u8(9);
}

}  // namespace ix

}  // namespace pd
