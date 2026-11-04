#include "feed/update.hpp"

#include "core/pb.hpp"

namespace arb::feed {

namespace {

using pb::Reader;

bool parse_account(std::string_view s, AccountView& a) {
  // SubscribeUpdateAccount { account = 1 (Info), slot = 2, is_startup = 3 }
  a = AccountView{};
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) {
      // SubscribeUpdateAccountInfo { pubkey 1, lamports 2, owner 3, executable 4, rent_epoch 5, data 6, write_version 7, txn_signature 8 }
      Reader in(r.bytes());
      while (in.next(f, wt)) {
        switch (f) {
          case 1: a.pubkey = in.bytes(); break;
          case 2: a.lamports = in.varint(); break;
          case 3: a.owner = in.bytes(); break;
          case 6: a.data = in.bytes(); break;
          case 7: a.write_version = in.varint(); break;
          default: in.skip(wt);
        }
      }
      if (!in.ok) return false;
    } else if (f == 2 && wt == pb::VARINT) {
      a.slot = r.varint();
    } else if (f == 3 && wt == pb::VARINT) {
      a.is_startup = r.varint() != 0;
    } else {
      r.skip(wt);
    }
  }
  return r.ok && a.pubkey.size() == 32 && a.owner.size() == 32;
}

bool parse_slot(std::string_view s, SlotView& v) {
  // SubscribeUpdateSlot { slot 1, parent 2, status 3, dead_error 4 }
  v = SlotView{};
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::VARINT) v.slot = r.varint();
    else if (f == 2 && wt == pb::VARINT) v.parent = r.varint();
    else if (f == 3 && wt == pb::VARINT) v.status = uint32_t(r.varint());
    else r.skip(wt);
  }
  return r.ok;
}

}  // namespace

bool parse_update(const uint8_t* data, size_t len, UpdateView& out) {
  // SubscribeUpdate { filters 1, account 2, slot 3, transaction 4, block 5, ping 6, block_meta 7, entry 8, pong 9, ... }
  out.kind = UpdateView::None;
  out.n_filters = 0;
  out.created_at_ns = 0;
  Reader r(data, len);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    switch (f) {
      case 1: {
        std::string_view name = r.bytes();
        if (out.n_filters < MAX_FILTERS) out.filters[out.n_filters++] = name;
        break;
      }
      case 2:
        if (wt != pb::LEN || !parse_account(r.bytes(), out.account)) return false;
        out.kind = UpdateView::Account;
        break;
      case 3:
        if (wt != pb::LEN || !parse_slot(r.bytes(), out.slot)) return false;
        out.kind = UpdateView::Slot;
        break;
      case 6:
        r.skip(wt);
        out.kind = UpdateView::Ping;
        break;
      case 11: {  // created_at: google.protobuf.Timestamp { seconds 1, nanos 2 }
        if (wt != pb::LEN) { r.skip(wt); break; }
        Reader ts(r.bytes());
        uint32_t f2, wt2;
        int64_t sec = 0, nanos = 0;
        while (ts.next(f2, wt2)) {
          if (f2 == 1 && wt2 == pb::VARINT) sec = int64_t(ts.varint());
          else if (f2 == 2 && wt2 == pb::VARINT) nanos = int64_t(ts.varint());
          else ts.skip(wt2);
        }
        out.created_at_ns = sec * 1'000'000'000LL + nanos;
        break;
      }
      case 9: {
        Reader in(r.bytes());
        uint32_t f2, wt2;
        while (in.next(f2, wt2)) {
          if (f2 == 1 && wt2 == pb::VARINT) out.pong_id = int32_t(in.varint());
          else in.skip(wt2);
        }
        out.kind = UpdateView::Pong;
        break;
      }
      default:
        r.skip(wt);
        if (out.kind == UpdateView::None) out.kind = UpdateView::Other;
    }
  }
  return r.ok;
}

}  // namespace arb::feed

namespace arb::feed {

std::string encode_account_frame(const Pubkey& key, const Pubkey& owner, const uint8_t* data, size_t len, uint64_t lamports,
                                 uint64_t slot, uint64_t write_version, std::string_view filter, int64_t created_at_ns) {
  pb::Writer info;
  info.bytes(1, std::string_view(reinterpret_cast<const char*>(key.data()), 32));
  info.u64(2, lamports);
  info.bytes(3, std::string_view(reinterpret_cast<const char*>(owner.data()), 32));
  info.bytes(6, std::string_view(reinterpret_cast<const char*>(data), len));
  info.u64(7, write_version);
  pb::Writer acc;
  acc.message(1, info);
  acc.u64(2, slot);
  pb::Writer up;
  up.bytes(1, filter);
  up.message(2, acc);
  if (created_at_ns > 0) {
    pb::Writer ts;
    ts.u64(1, uint64_t(created_at_ns / 1'000'000'000LL));
    ts.u64(2, uint64_t(created_at_ns % 1'000'000'000LL));
    up.message(11, ts);
  }
  return up.out;
}

std::string encode_slot_frame(uint64_t slot, uint32_t status) {
  pb::Writer s;
  s.u64(1, slot);
  if (status) s.u64(3, status);
  pb::Writer up;
  up.bytes(1, "slots");
  up.message(3, s);
  return up.out;
}

}  // namespace arb::feed
