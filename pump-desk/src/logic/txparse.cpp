#include "logic/txparse.hpp"

#include <cctype>

#include "core/base58.hpp"
#include "sol/constants.hpp"

namespace pd {

namespace {

KnownB58 make_known() {
  KnownB58 k{};
  k.pump_fun_len = static_cast<uint8_t>(b58_encode(prog::PUMP_FUN.data(), 32, k.pump_fun));
  k.pump_swap_len = static_cast<uint8_t>(b58_encode(prog::PUMP_SWAP.data(), 32, k.pump_swap));
  k.token_2022_len = static_cast<uint8_t>(b58_encode(prog::TOKEN_2022.data(), 32, k.token_2022));
  k.wsol_len = static_cast<uint8_t>(b58_encode(prog::WSOL.data(), 32, k.wsol));
  return k;
}

bool starts_with(std::string_view s, std::string_view p) { return s.substr(0, p.size()) == p; }

/** Case-insensitive substring search. */
bool icontains(std::string_view hay, std::string_view needle) {
  if (needle.size() > hay.size()) return false;
  for (size_t i = 0; i + needle.size() <= hay.size(); ++i) {
    size_t j = 0;
    while (j < needle.size() && std::tolower(static_cast<unsigned char>(hay[i + j])) == needle[j]) ++j;
    if (j == needle.size()) return true;
  }
  return false;
}

std::string_view rtrim(std::string_view s) {
  while (!s.empty() && (s.back() == ' ' || s.back() == '\t' || s.back() == '\r' || s.back() == '\n')) s.remove_suffix(1);
  return s;
}

/** "Program <id> invoke [n]" -> id. */
bool parse_invoke(std::string_view l, std::string_view& id) {
  if (!starts_with(l, "Program ")) return false;
  std::string_view rest = l.substr(8);
  size_t sp = rest.find(' ');
  if (sp == std::string_view::npos) return false;
  if (!starts_with(rest.substr(sp), " invoke [")) return false;
  id = rest.substr(0, sp);
  return true;
}

/** "Program <id> success" / "Program <id> failed: ..." */
bool parse_exit(std::string_view l) {
  if (!starts_with(l, "Program ")) return false;
  std::string_view rest = l.substr(8);
  size_t sp = rest.find(' ');
  if (sp == std::string_view::npos) return false;
  std::string_view tail = rest.substr(sp + 1);
  return starts_with(tail, "success") || starts_with(tail, "failed");
}

/** "Program log: Instruction: <Name>" -> Name (a single word). */
bool parse_ix_name(std::string_view l, std::string_view& name) {
  static constexpr std::string_view P = "Program log: Instruction: ";
  if (!starts_with(l, P)) return false;
  std::string_view n = rtrim(l.substr(P.size()));
  if (n.empty()) return false;
  for (char c : n)
    if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_')) return false;
  name = n;
  return true;
}

bool is_wsol(std::string_view mint) { return mint == known_b58().wsol_sv(); }

}  // namespace

const KnownB58& known_b58() {
  static const KnownB58 k = make_known();
  return k;
}

size_t instruction_trace(const TxView& tx, TraceEntry* out, size_t cap) {
  std::string_view stack[64];
  size_t depth = 0, n = 0;
  for (uint16_t i = 0; i < tx.n_logs; ++i) {
    std::string_view l = tx.logs[i], id, name;
    if (parse_invoke(l, id)) {
      if (depth < 64) stack[depth] = id;
      ++depth;
    } else if (parse_exit(l)) {
      if (depth) --depth;
    } else if (parse_ix_name(l, name) && depth && depth <= 64 && n < cap) {
      out[n++] = TraceEntry{stack[depth - 1], name};
    }
  }
  return n;
}

bool is_migrate_name(std::string_view name) {
  if (name == "Migrate") return true;
  if (!starts_with(name, "MigrateV") || name.size() == 8) return false;
  for (char c : name.substr(8))
    if (!std::isdigit(static_cast<unsigned char>(c))) return false;
  return true;
}

uint64_t sum_tokens(const TokenBalanceView* tb, uint16_t n, std::string_view mint, std::string_view owner) {
  uint64_t s = 0;
  for (uint16_t i = 0; i < n; ++i)
    if (tb[i].mint == mint && tb[i].owner == owner) s += tb[i].amount;
  return s;
}

bool balances_moved(const TxView& tx) {
  auto differs = [&](const TokenBalanceView& b) {
    return sum_tokens(tx.pre_tokens, tx.n_pre_tokens, b.mint, b.owner) !=
           sum_tokens(tx.post_tokens, tx.n_post_tokens, b.mint, b.owner);
  };
  for (uint16_t i = 0; i < tx.n_pre_tokens; ++i)
    if (differs(tx.pre_tokens[i])) return true;
  for (uint16_t i = 0; i < tx.n_post_tokens; ++i)
    if (differs(tx.post_tokens[i])) return true;
  return false;
}

std::string_view largest_holder(const TxView& tx, std::string_view mint) {
  std::string_view best;
  uint64_t best_amt = 0;
  for (uint16_t i = 0; i < tx.n_post_tokens; ++i) {
    const TokenBalanceView& b = tx.post_tokens[i];
    if (b.mint == mint && !b.owner.empty() && b.amount > best_amt) {
      best = b.owner;
      best_amt = b.amount;
    }
  }
  return best;
}

std::string_view token_program_of(const TxView& tx, std::string_view mint) {
  for (uint16_t i = 0; i < tx.n_post_tokens; ++i)
    if (tx.post_tokens[i].mint == mint && !tx.post_tokens[i].program_id.empty()) return tx.post_tokens[i].program_id;
  for (uint16_t i = 0; i < tx.n_pre_tokens; ++i)
    if (tx.pre_tokens[i].mint == mint && !tx.pre_tokens[i].program_id.empty()) return tx.pre_tokens[i].program_id;
  return {};
}

MigrateInfo parse_migrate(const TxView& tx) {
  MigrateInfo m;
  TraceEntry trace[128];
  size_t n = instruction_trace(tx, trace, 128);
  const KnownB58& k = known_b58();
  int migrate_idx = -1;
  for (size_t i = 0; i < n; ++i) {
    if (trace[i].program == k.pump_fun_sv() && is_migrate_name(trace[i].name)) { migrate_idx = int(i); break; }
  }
  if (migrate_idx < 0) {
    // The trace can miss a migrate whose invoke line was truncated; fall back to the log alone.
    for (uint16_t i = 0; i < tx.n_logs && migrate_idx < 0; ++i) {
      std::string_view name;
      if (parse_ix_name(tx.logs[i], name) && is_migrate_name(name)) migrate_idx = int(n);
    }
    if (migrate_idx < 0) return m;
  }
  m.is_migrate = true;
  for (int i = 0; i < migrate_idx && size_t(i) < n; ++i)
    if (trace[i].program == k.pump_fun_sv() && starts_with(trace[i].name, "Buy")) m.curve_buy_before = true;
  for (size_t i = size_t(migrate_idx) + 1; i < n; ++i)
    if (trace[i].program == k.pump_swap_sv() && starts_with(trace[i].name, "Buy")) m.pool_buy_after = true;
  bool already = false;
  for (uint16_t i = 0; i < tx.n_logs && !already; ++i) already = icontains(tx.logs[i], "already migrated");
  m.landed = !tx.failed && !already && balances_moved(tx);
  if (tx.n_keys > 0) m.caller = tx.keys[0];
  auto add_mint = [&](std::string_view mint) {
    if (mint.empty() || is_wsol(mint)) return;
    for (uint8_t i = 0; i < m.n_mints; ++i)
      if (m.mints[i] == mint) return;
    if (m.n_mints < 4) m.mints[m.n_mints++] = mint;
  };
  for (uint16_t i = 0; i < tx.n_pre_tokens; ++i) add_mint(tx.pre_tokens[i].mint);
  for (uint16_t i = 0; i < tx.n_post_tokens; ++i) add_mint(tx.post_tokens[i].mint);
  return m;
}

bool create_logs_prove_no_transfer_fee(const TxView& tx) {
  if (tx.logs_truncated) return false;
  for (uint16_t i = 0; i < tx.n_logs; ++i) {
    if (icontains(tx.logs[i], "log truncated") || icontains(tx.logs[i], "transfer fee") ||
        icontains(tx.logs[i], "transferfee") || icontains(tx.logs[i], "transfer_fee") ||
        icontains(tx.logs[i], "transfer-fee"))
      return false;
  }
  TraceEntry trace[128];
  size_t n = instruction_trace(tx, trace, 128);
  int inits = 0;
  bool by_2022 = false;
  for (size_t i = 0; i < n; ++i) {
    if (trace[i].name == "InitializeMint" || trace[i].name == "InitializeMint2") {
      ++inits;
      by_2022 = trace[i].program == known_b58().token_2022_sv();
    }
  }
  return inits == 1 && by_2022;
}

LaunchInfo parse_launch(const TxView& tx) {
  LaunchInfo L;
  if (tx.failed) return L;
  bool create = false;
  for (uint16_t i = 0; i < tx.n_logs && !create; ++i) {
    std::string_view name;
    if (!parse_ix_name(tx.logs[i], name)) continue;
    if (name == "Create") create = true;
    else if (starts_with(name, "CreateV") && name.size() > 7) {
      create = true;
      for (char c : name.substr(7)) create = create && std::isdigit(static_cast<unsigned char>(c));
    }
  }
  if (!create) return L;
  // New mints: in post balances, not WSOL, absent from pre balances. Exactly one expected.
  std::string_view found;
  int count = 0;
  for (uint16_t i = 0; i < tx.n_post_tokens; ++i) {
    std::string_view mint = tx.post_tokens[i].mint;
    if (mint.empty() || is_wsol(mint) || mint == found) continue;
    bool in_pre = false;
    for (uint16_t j = 0; j < tx.n_pre_tokens && !in_pre; ++j) in_pre = tx.pre_tokens[j].mint == mint;
    if (in_pre) continue;
    found = mint;
    ++count;
  }
  if (count != 1) return L;
  L.mint = found;
  L.curve = largest_holder(tx, found);
  L.token_program = token_program_of(tx, found);
  L.no_transfer_fee = create_logs_prove_no_transfer_fee(tx);
  L.ok = !L.curve.empty();
  return L;
}

namespace {

bool program_is(const TxView& tx, const CompiledIxView& ix, const Pubkey& program) {
  return ix.program < tx.n_static_keys && tx.keys[ix.program].size() == 32 &&
         key_eq(reinterpret_cast<const uint8_t*>(tx.keys[ix.program].data()), program);
}

uint64_t le(std::string_view d, size_t off, size_t n) {
  uint64_t v = 0;
  for (size_t i = 0; i < n; ++i) v |= uint64_t(uint8_t(d[off + i])) << (8 * i);
  return v;
}

}  // namespace

BudgetInfo compute_budget_of(const TxView& tx) {
  BudgetInfo b;
  for (uint8_t i = 0; i < tx.n_ixs; ++i) {
    const CompiledIxView& ix = tx.ixs[i];
    if (ix.data.empty() || !program_is(tx, ix, prog::COMPUTE_BUDGET)) continue;
    // ComputeBudgetInstruction: 2 SetComputeUnitLimit(u32), 3 SetComputeUnitPrice(u64)
    if (uint8_t(ix.data[0]) == 2 && ix.data.size() >= 5) b.cu_limit = uint32_t(le(ix.data, 1, 4));
    else if (uint8_t(ix.data[0]) == 3 && ix.data.size() >= 9) b.cu_price = le(ix.data, 1, 8);
  }
  return b;
}

TipInfo tip_of(const TxView& tx) {
  TipInfo best;
  for (uint8_t i = 0; i < tx.n_ixs; ++i) {
    const CompiledIxView& ix = tx.ixs[i];
    // SystemInstruction::Transfer = u32 2 + u64 lamports; accounts [from, to]
    if (ix.data.size() != 12 || ix.accounts.size() != 2 || le(ix.data, 0, 4) != 2 || !program_is(tx, ix, prog::SYSTEM)) continue;
    if (uint8_t(ix.accounts[0]) != 0) continue;
    uint8_t dst = uint8_t(ix.accounts[1]);
    bool elsewhere = false;
    for (uint8_t j = 0; j < tx.n_ixs && !elsewhere; ++j)
      if (j != i) elsewhere = tx.ixs[j].accounts.find(char(dst)) != std::string_view::npos;
    uint64_t lamports = le(ix.data, 4, 8);
    if (!elsewhere && lamports > best.lamports) best = TipInfo{int(dst), lamports};
  }
  return best;
}

bool custom_instruction_error(const TxView& tx, uint8_t& ix_index, uint32_t& code) {
  // bincode: TransactionError::InstructionError (8) u32, index u8, InstructionError::Custom (25) u32, code u32
  std::string_view e = tx.err;
  if (e.size() < 13 || le(e, 0, 4) != 8 || le(e, 5, 4) != 25) return false;
  ix_index = uint8_t(e[4]);
  code = uint32_t(le(e, 9, 4));
  return true;
}

}  // namespace pd
