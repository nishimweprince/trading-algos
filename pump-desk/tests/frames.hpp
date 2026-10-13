#pragma once
// Synthetic geyser.SubscribeUpdate frames for engine tests (same field numbers
// the parser reads; see proto/geyser.proto and proto/solana-storage.proto).
#include <string>
#include <vector>

#include "core/base58.hpp"
#include "core/pb.hpp"
#include "core/types.hpp"

namespace pdtest {

struct TokenBal {
  std::string mint, owner, program;
  uint64_t amount;
};

struct TxSpec {
  std::vector<std::string> filters;
  pd::Signature sig{};
  uint64_t slot = 1;
  int64_t index = -1;  // Info.index (position in block); omitted when < 0
  std::vector<pd::Pubkey> keys;
  bool failed = false;
  uint64_t fee = 5000;
  std::vector<uint64_t> pre, post;
  std::vector<std::string> logs;
  std::vector<TokenBal> pre_tokens, post_tokens;
};

inline std::string sv(const uint8_t* p, size_t n) { return std::string(reinterpret_cast<const char*>(p), n); }

inline pd::pb::Writer token_balance(const TokenBal& t, uint32_t idx) {
  pd::pb::Writer ui;
  ui.bytes(3, std::to_string(t.amount));
  pd::pb::Writer w;
  w.u64(1, idx);
  w.bytes(2, t.mint);
  w.message(3, ui);
  w.bytes(4, t.owner);
  w.bytes(5, t.program);
  return w;
}

inline std::string tx_frame(const TxSpec& s) {
  pd::pb::Writer msg;
  for (auto& k : s.keys) msg.bytes(2, sv(k.data(), 32));
  pd::pb::Writer txn;
  txn.bytes(1, sv(s.sig.data(), 64));
  txn.message(2, msg);
  pd::pb::Writer meta;
  if (s.failed) {
    pd::pb::Writer err;
    err.bytes(1, "x");
    meta.message(1, err);
  }
  meta.u64(2, s.fee);
  {
    pd::pb::Writer packed;
    for (auto v : s.pre) packed.varint(v);
    meta.bytes(3, packed.out);
  }
  for (auto v : s.post) meta.u64(4, v);  // unpacked on purpose: both encodings must parse
  for (auto& l : s.logs) meta.bytes(6, l);
  uint32_t i = 0;
  for (auto& t : s.pre_tokens) meta.message(7, token_balance(t, i++));
  i = 0;
  for (auto& t : s.post_tokens) meta.message(8, token_balance(t, i++));
  pd::pb::Writer info;
  info.bytes(1, sv(s.sig.data(), 64));
  info.message(3, txn);
  info.message(4, meta);
  if (s.index >= 0) info.u64(5, uint64_t(s.index));
  pd::pb::Writer upd;
  upd.message(1, info);
  upd.u64(2, s.slot);
  pd::pb::Writer root;
  for (auto& f : s.filters) root.bytes(1, f);
  root.message(4, upd);
  return root.out;
}

inline std::string account_frame(const std::string& filter, const pd::Pubkey& key, const pd::Pubkey& owner,
                                 const std::vector<uint8_t>& data, uint64_t slot, const pd::Signature* sig = nullptr) {
  pd::pb::Writer info;
  info.bytes(1, sv(key.data(), 32));
  info.u64(2, 1'000'000);
  info.bytes(3, sv(owner.data(), 32));
  info.bytes(6, sv(data.data(), data.size()));
  if (sig) info.bytes(8, sv(sig->data(), 64));
  pd::pb::Writer acc;
  acc.message(1, info);
  acc.u64(2, slot);
  pd::pb::Writer root;
  root.bytes(1, filter);
  root.message(2, acc);
  return root.out;
}

/** status: geyser.SlotStatus (0 processed, 3 first shred received, 4 completed). */
inline std::string slot_frame(uint64_t slot, uint32_t status = 0) {
  pd::pb::Writer s;
  s.u64(1, slot);
  if (status) s.u64(3, status);
  pd::pb::Writer root;
  root.bytes(1, "heartbeat");
  root.message(3, s);
  return root.out;
}

inline pd::Signature make_sig(uint8_t seed) {
  pd::Signature s{};
  for (int i = 0; i < 64; ++i) s[i] = uint8_t(seed * 31 + i);
  return s;
}

}  // namespace pdtest
