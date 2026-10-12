#include "stream/update.hpp"

#include "core/pb.hpp"

namespace pd {

namespace {

using pb::Reader;

void parse_account(std::string_view s, AccountView& a) {
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
          case 8: a.txn_signature = in.bytes(); break;
          default: in.skip(wt);
        }
      }
    } else if (f == 2 && wt == pb::VARINT) {
      a.slot = r.varint();
    } else {
      r.skip(wt);
    }
  }
}

void parse_token_balance(std::string_view s, TokenBalanceView& t) {
  // TokenBalance { account_index 1, mint 2, ui_token_amount 3, owner 4, program_id 5 }
  t = TokenBalanceView{};
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    switch (f) {
      case 1: t.account_index = static_cast<uint32_t>(r.varint()); break;
      case 2: t.mint = r.bytes(); break;
      case 3: {
        // UiTokenAmount { ui_amount 1 (double), decimals 2, amount 3, ui_amount_string 4 }
        Reader in(r.bytes());
        while (in.next(f, wt)) {
          if (f == 3 && wt == pb::LEN) parse_u64(in.bytes(), t.amount);
          else in.skip(wt);
        }
        break;
      }
      case 4: t.owner = r.bytes(); break;
      case 5: t.program_id = r.bytes(); break;
      default: r.skip(wt);
    }
  }
}

void parse_message(std::string_view s, TxView& tx) {
  // Message { header 1, account_keys 2, recent_blockhash 3, instructions 4, versioned 5, lookups 6 }
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 2 && wt == pb::LEN) {
      std::string_view k = r.bytes();
      if (tx.n_keys < MAX_KEYS) tx.keys[tx.n_keys++] = k;
    } else {
      r.skip(wt);
    }
  }
  tx.n_static_keys = tx.n_keys;
}

void parse_meta(std::string_view s, TxView& tx, std::string_view* loaded_w, uint16_t& n_w, std::string_view* loaded_r,
                uint16_t& n_r) {
  // TransactionStatusMeta { err 1, fee 2, pre_balances 3, post_balances 4, log_messages 6,
  //   pre_token_balances 7, post_token_balances 8, loaded_writable 12, loaded_readonly 13 }
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    switch (f) {
      case 1:
        tx.failed = true;
        r.skip(wt);
        break;
      case 2: tx.fee = r.varint(); break;
      case 3:
        pb::each_varint(r, wt, [&](uint64_t v) { if (tx.n_pre_balances < MAX_KEYS) tx.pre_balances[tx.n_pre_balances++] = v; });
        break;
      case 4:
        pb::each_varint(r, wt, [&](uint64_t v) { if (tx.n_post_balances < MAX_KEYS) tx.post_balances[tx.n_post_balances++] = v; });
        break;
      case 6: {
        std::string_view line = r.bytes();
        if (tx.n_logs < MAX_LOGS) tx.logs[tx.n_logs++] = line;
        else tx.logs_truncated = true;
        break;
      }
      case 7:
        if (tx.n_pre_tokens < MAX_TOKEN_BALANCES) parse_token_balance(r.bytes(), tx.pre_tokens[tx.n_pre_tokens++]);
        else r.skip(wt);
        break;
      case 8:
        if (tx.n_post_tokens < MAX_TOKEN_BALANCES) parse_token_balance(r.bytes(), tx.post_tokens[tx.n_post_tokens++]);
        else r.skip(wt);
        break;
      case 12:
        if (n_w < MAX_KEYS) loaded_w[n_w++] = r.bytes(); else r.skip(wt);
        break;
      case 13:
        if (n_r < MAX_KEYS) loaded_r[n_r++] = r.bytes(); else r.skip(wt);
        break;
      default: r.skip(wt);
    }
  }
}

void parse_transaction(std::string_view s, TxView& tx) {
  // SubscribeUpdateTransaction { transaction 1 (Info), slot 2 }
  // Info { signature 1, is_vote 2, transaction 3, meta 4, index 5 }
  tx.slot = 0;
  tx.signature = {};
  tx.failed = tx.has_meta = tx.logs_truncated = false;
  tx.fee = 0;
  tx.n_keys = tx.n_static_keys = tx.n_pre_balances = tx.n_post_balances = tx.n_logs = 0;
  tx.n_pre_tokens = tx.n_post_tokens = 0;
  std::string_view loaded_w[MAX_KEYS], loaded_r[MAX_KEYS];
  uint16_t n_w = 0, n_r = 0;
  Reader r(s);
  uint32_t f, wt;
  while (r.next(f, wt)) {
    if (f == 1 && wt == pb::LEN) {
      Reader in(r.bytes());
      while (in.next(f, wt)) {
        switch (f) {
          case 1: tx.signature = in.bytes(); break;
          case 3: {
            // Transaction { signatures 1, message 2 }
            Reader t(in.bytes());
            while (t.next(f, wt)) {
              if (f == 2 && wt == pb::LEN) parse_message(t.bytes(), tx);
              else t.skip(wt);
            }
            break;
          }
          case 4:
            tx.has_meta = true;
            parse_meta(in.bytes(), tx, loaded_w, n_w, loaded_r, n_r);
            break;
          default: in.skip(wt);
        }
      }
    } else if (f == 2 && wt == pb::VARINT) {
      tx.slot = r.varint();
    } else {
      r.skip(wt);
    }
  }
  // Full key list = static keys, then loaded writable, then loaded readonly (balance index order).
  for (uint16_t i = 0; i < n_w && tx.n_keys < MAX_KEYS; ++i) tx.keys[tx.n_keys++] = loaded_w[i];
  for (uint16_t i = 0; i < n_r && tx.n_keys < MAX_KEYS; ++i) tx.keys[tx.n_keys++] = loaded_r[i];
}

}  // namespace

bool parse_update(const uint8_t* data, size_t len, UpdateView& out) {
  out.kind = UpdateView::None;
  out.n_filters = 0;
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
        out.kind = UpdateView::Account;
        parse_account(r.bytes(), out.account);
        break;
      case 3: {
        out.kind = UpdateView::Slot;
        out.slot = SlotView{};
        Reader in(r.bytes());
        while (in.next(f, wt)) {
          if (f == 1) out.slot.slot = in.varint();
          else if (f == 3) out.slot.status = static_cast<uint32_t>(in.varint());
          else in.skip(wt);
        }
        break;
      }
      case 4:
        out.kind = UpdateView::Transaction;
        parse_transaction(r.bytes(), out.tx);
        break;
      case 6: out.kind = UpdateView::Ping; r.skip(wt); break;
      case 9: out.kind = UpdateView::Pong; r.skip(wt); break;
      default:
        if (out.kind == UpdateView::None && (f == 5 || f == 7 || f == 8 || f == 10)) out.kind = UpdateView::Other;
        r.skip(wt);
    }
  }
  return r.ok;
}

}  // namespace pd
