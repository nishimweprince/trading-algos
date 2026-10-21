// End-to-end logic-thread tests over synthetic frames, curve-in, migrate-out:
// create -> band (entry range) -> curve buy packet -> wallet fill -> curve complete ->
// pool push + migrate -> slot timeout / take-profit -> PumpSwap sell fill -> close.
#include <sodium.h>

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <memory>
#include <unistd.h>

#include "core/base64.hpp"
#include "core/sys.hpp"
#include "aux/writer.hpp"
#include "fixture.hpp"
#include "frames.hpp"
#include "logic/engine.hpp"
#include "logic/exit_fsm.hpp"
#include "aux/aux.hpp"
#include "sol/constants.hpp"
#include "test.hpp"

using namespace pd;
using namespace pdtest;

namespace {

int64_t g_wall = 1'700'000'000'000;
uint64_t g_mono = 1'000'000'000'000;
int64_t test_wall() { return g_wall; }
uint64_t test_mono() { return g_mono; }

std::string b58s(const Pubkey& k) { return b58(k); }

constexpr uint64_t STD_GAP = 279'900'000'000'000ULL;  // virtual - real tokens on a standard curve
constexpr uint64_t NEAR = 9'000'000'000'000ULL;       // ~3.6 SOL to complete: inside the 4 SOL entry range
constexpr uint64_t ADMIT = 11'500'000'000'000ULL;     // ~4.6 SOL: admitted (<= 5), outside the entry range

/** A standard pump.fun curve with `real` tokens left (k = 30 SOL x 1.073B tokens). */
std::vector<uint8_t> curve_data(uint64_t real, bool complete, const Pubkey& creator, bool mayhem = false,
                                const Pubkey* quote_mint = nullptr) {
  uint64_t vt = real + STD_GAP;
  uint64_t vs = uint64_t(30e9 * 1.073e15 / double(vt));
  std::vector<uint8_t> d(151, 0);
  std::memcpy(d.data(), disc::BONDING_CURVE, 8);
  wr_u64(d.data() + 8, vt);
  wr_u64(d.data() + 16, vs);
  wr_u64(d.data() + 24, complete ? 0 : real);
  wr_u64(d.data() + 32, complete ? 85'000'000'000ULL : vs - 30'000'000'000ULL);
  wr_u64(d.data() + 40, 1'000'000'000'000'000ULL);
  d[48] = complete ? 1 : 0;
  std::memcpy(d.data() + 49, creator.data(), 32);
  d[81] = mayhem ? 1 : 0;
  if (quote_mint) std::memcpy(d.data() + 83, quote_mint->data(), 32);
  return d;
}

std::vector<uint8_t> token_account(uint64_t amount) {
  std::vector<uint8_t> d(165, 0);
  wr_u64(d.data() + 64, amount);
  return d;
}

struct Rig {
  Config cfg;
  std::unique_ptr<Shared> sh{new Shared};
  Keypair kp;
  std::unique_ptr<Engine> eng;
  Pubkey mint, curve, creator, pool, base_vault, quote_vault;
  std::string mint_s, curve_s, pool_s, t22_s, token_s, wsol_s, user_s;
  uint64_t slot = 100;

  Rig() {
    auto& g = golden();
    AmmConfig amm;
    auto gc = b64bytes(g["accounts"]["globalConfig"]);
    auto fc = b64bytes(g["accounts"]["feeConfig"]);
    auto pg = b64bytes(curve_golden()["accounts"]["pumpGlobal"]);
    decode_global_config(gc.data(), gc.size(), amm);
    decode_fee_config(fc.data(), fc.size(), amm);
    decode_pump_global(pg.data(), pg.size(), amm);
    amm.valid = true;
    sh->amm.store(amm);
    BlockhashSnap bh;
    bh.hash = key(g["blockhash"]);
    bh.fetched_ns = g_mono;
    sh->blockhash.store(bh);
    auto seed = unhex(std::string_view(g["userSeedHex"]));
    kp = Keypair::from_seed(seed.data());
    eng = std::make_unique<Engine>(cfg, *sh, kp, nullptr);
    eng->set_clocks(&test_wall, &test_mono);
    eng->set_wallet_lamports(1'000'000'000, 1);
    mint = key(g["seedMint"]);
    creator = key(g["pool"]["coinCreator"]);
    curve = pda({std::string_view("bonding-curve"), mint}, prog::PUMP_FUN);
    pool = key(g["seedPool"]);
    mint_s = b58s(mint);
    curve_s = b58s(curve);
    pool_s = b58s(pool);
    t22_s = b58s(prog::TOKEN_2022);
    token_s = b58s(prog::TOKEN);
    wsol_s = b58s(prog::WSOL);
    user_s = b58s(kp.pubkey());
    base_vault = ata(pool, mint, prog::TOKEN_2022);
    quote_vault = ata(pool, prog::WSOL, prog::TOKEN);
  }

  void feed(const std::string& f) { eng->on_frame(reinterpret_cast<const uint8_t*>(f.data()), f.size(), g_mono); }

  void create() {
    TxSpec t;
    t.filters = {"creates"};
    t.sig = make_sig(1);
    t.keys = {kp.pubkey()};
    std::string pf = b58s(prog::PUMP_FUN);
    t.logs = {"Program " + pf + " invoke [1]", "Program log: Instruction: CreateV2", "Program " + t22_s + " invoke [2]",
              "Program log: Instruction: InitializeMint2", "Program " + t22_s + " success", "Program " + pf + " success"};
    t.post_tokens = {{mint_s, curve_s, t22_s, 1'000'000'000'000'000ULL}};
    feed(tx_frame(t));
  }

  void band(uint64_t real_tokens, bool mayhem = false, const Pubkey* quote_mint = nullptr) {
    feed(account_frame("band0", curve, prog::PUMP_FUN, curve_data(real_tokens, false, creator, mayhem, quote_mint), slot));
  }
  void complete() { feed(account_frame("band0", curve, prog::PUMP_FUN, curve_data(0, true, creator), slot)); }

  void pool_push() {
    feed(account_frame("watch", pool, prog::PUMP_SWAP, b64bytes(golden()["accounts"]["pool"]), slot));
  }

  /** The pool push usually lands a few ms before the migrate tx; `with_pool` sends it first. */
  void migrate(uint64_t base, uint64_t quote, uint8_t sig_seed = 2, bool with_pool = true) {
    ++slot;
    if (with_pool) pool_push();
    TxSpec t;
    t.filters = {"migrate"};
    t.sig = make_sig(sig_seed);
    t.slot = slot;
    t.keys = {prog::MIGRATION_AUTHORITY};
    std::string pf = b58s(prog::PUMP_FUN);
    t.logs = {"Program " + pf + " invoke [1]", "Program log: Instruction: MigrateV2", "Program " + pf + " success"};
    t.pre_tokens = {{mint_s, curve_s, t22_s, base}};
    t.post_tokens = {{mint_s, pool_s, t22_s, base}, {wsol_s, pool_s, token_s, quote}};
    feed(tx_frame(t));
  }

  void tip(uint64_t s) { feed(slot_frame(s)); }

  void wallet_fill(const Signature& sig, uint64_t pre, uint64_t post, uint64_t tok_pre, uint64_t tok_post, bool failed = false,
                   const std::string& err = "x") {
    TxSpec t;
    t.filters = {"wallet"};
    t.sig = sig;
    t.slot = ++slot;
    t.failed = failed;
    t.err = err;
    t.keys = {kp.pubkey()};
    t.pre = {pre};
    t.post = {post};
    if (tok_pre) t.pre_tokens = {{mint_s, user_s, t22_s, tok_pre}};
    if (tok_post || tok_pre) t.post_tokens = {{mint_s, user_s, t22_s, tok_post}};
    feed(tx_frame(t));
  }

  bool pop_packet(Packet& p) { return sh->packets.try_pop(p); }

  std::vector<Event> events() {
    std::vector<Event> out;
    Event e;
    while (sh->events.try_pop(e)) out.push_back(e);
    return out;
  }
};

const Event* find(const std::vector<Event>& es, Ev t, const char* text = nullptr) {
  for (auto& e : es)
    if (e.type == t && (!text || std::string(e.text) == text)) return &e;
  return nullptr;
}

int count(const std::vector<Event>& es, Ev t, const char* text = nullptr) {
  int n = 0;
  for (auto& e : es) n += e.type == t && (!text || std::string(e.text) == text);
  return n;
}

bool verify_packet(const Packet& pk, const Pubkey& signer) {
  std::vector<uint8_t> tx;
  if (!b64_decode(std::string_view(pk.b64, pk.b64_len), tx)) return false;
  if (tx.size() > PACKET_DATA_SIZE || tx.size() < 65 || tx[0] != 1) return false;
  if (std::memcmp(tx.data() + 1, pk.sig.data(), 64) != 0) return false;
  return crypto_sign_ed25519_verify_detached(tx.data() + 1, tx.data() + 65, tx.size() - 65, signer.data()) == 0;
}

/** Program id of the swap instruction in a signed packet: is this a curve or a PumpSwap tx? */
bool packet_uses(const Packet& pk, const Pubkey& program) {
  std::vector<uint8_t> tx;
  if (!b64_decode(std::string_view(pk.b64, pk.b64_len), tx)) return false;
  return std::search(tx.begin(), tx.end(), program.begin(), program.end()) != tx.end();
}

constexpr uint64_t BUY_PRE = 1'000'000'000, BUY_POST = 945'000'000;  // 0.05 SOL + fees + rent out

/** Curve buy sent and filled: the position sits in the curve phase. */
void open_on_curve(Rig& r) {
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, BUY_PRE, BUY_POST, 0, p.expected_base);
  CHECK(p.state == PosState::Open);
  CHECK(p.phase == Phase::Curve);
}

/** On the curve, then complete, then migrated: the pool phase, before the timeout. */
void open_in_pool(Rig& r, uint64_t quote = 67'410'000'000ULL) {
  open_on_curve(r);
  r.complete();
  r.migrate(206'900'000'000'000ULL, quote);
  CHECK(r.eng->position(0).phase == Phase::Pool);
}

}  // namespace

TEST(engine_curve_round_trip) {
  Rig r;
  r.create();
  r.band(ADMIT);  // admitted, outside the entry range: no buy yet
  const CurveSlot* c = r.eng->curve(r.curve);
  CHECK(c != nullptr);
  if (!c) return;
  CHECK(c->mint_known);
  CHECK(c->keys_ready);
  CHECK(c->fee == TransferFee::None);  // proven by the create logs
  CHECK(c->keys.pool == r.pool);
  Packet pk;
  CHECK(!r.pop_packet(pk));

  r.band(NEAR);
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Buy);
  CHECK(verify_packet(pk, r.kp.pubkey()));
  CHECK(packet_uses(pk, prog::PUMP_FUN));
  const Position& p = r.eng->position(0);
  CHECK(p.state == PosState::BuySent);
  CHECK(p.phase == Phase::Curve);
  CHECK(p.size_lamports <= 50'000'000 && p.size_lamports > 49'000'000);  // entry.size_sol, fee included
  CHECK_EQ(p.max_quote, p.size_lamports + uint64_t(double(p.size_lamports) * 0.10));
  CHECK(r.sh->watch_cmds.size() == 4);  // pool, both vaults, and the pool's txs
  auto es = r.events();
  CHECK(find(es, Ev::Decision, "buy") != nullptr);
  const Event* o = find(es, Ev::Order, "curve");
  CHECK(o && o->b == r.curve);

  uint64_t base = p.expected_base;
  r.wallet_fill(p.buy_sig, BUY_PRE, BUY_POST, 0, base);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.base_amount, base);
  CHECK(p.entry_price > 0);
  CHECK_EQ(r.eng->reserved_lamports(), uint64_t(0));
  CHECK(find(r.events(), Ev::PositionOpen) != nullptr);

  r.complete();
  CHECK(p.phase == Phase::AwaitMigrate);
  CHECK(!r.pop_packet(pk));  // nothing trades between completion and the migrate

  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  uint64_t migrate_slot = r.slot;
  CHECK(p.phase == Phase::Pool);
  CHECK_EQ(p.migrate_slot, migrate_slot);
  CHECK(p.pool_seen);
  CHECK_EQ(p.base_res, uint64_t(206'900'000'000'000ULL));
  CHECK(!r.pop_packet(pk));  // tp off; the timeout has not come

  r.tip(migrate_slot + uint64_t(r.cfg.exits.timeout_slots) - 1);
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));
  r.tip(migrate_slot + uint64_t(r.cfg.exits.timeout_slots));
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(sell.side == Side::Sell);
  CHECK(verify_packet(sell, r.kp.pubkey()));
  CHECK(packet_uses(sell, prog::PUMP_SWAP));
  CHECK(p.state == PosState::SellSent);
  CHECK(find(r.events(), Ev::Order, "POOL_TIMEOUT") != nullptr);

  r.wallet_fill(sell.sig, BUY_POST, 1'010'000'000, base, 0);
  CHECK(p.state == PosState::Empty);
  es = r.events();
  const Event* close = find(es, Ev::PositionClose, "POOL_TIMEOUT");
  CHECK(close != nullptr);
  if (close) {
    CHECK_EQ(close->i[0], int64_t(1'010'000'000 - BUY_POST) - int64_t(BUY_PRE - BUY_POST));
    CHECK_EQ(close->i[5], int64_t(Phase::Pool));
  }
  CHECK_EQ(r.eng->realized_pnl(), int64_t(10'000'000));
}

TEST(engine_entry_vetoes) {
  struct Case {
    const char* verdict;
    void (*setup)(Rig&);
  };
  const Pubkey usdc = parse_pubkey("EPjFWdd5AufqSSqM2qN1xzybapC8G4wEGGkZwyTDt1v");
  static Pubkey s_usdc;
  s_usdc = usdc;
  for (const char* want : {"mayhem", "quote_not_sol", "blacklist", "kill", "insufficient_balance"}) {
    Rig r;
    std::string w = want;
    if (w == "blacklist") r.cfg.screen.blacklist_creators = {b58(r.creator)};
    if (w == "blacklist") {
      r.eng = std::make_unique<Engine>(r.cfg, *r.sh, r.kp, nullptr);
      r.eng->set_clocks(&test_wall, &test_mono);
      r.eng->set_wallet_lamports(1'000'000'000, 1);
    }
    if (w == "kill") r.sh->kill_file.store(true);
    if (w == "insufficient_balance") r.eng->set_wallet_lamports(10'000'000, 2);
    r.create();
    if (w == "mayhem") r.band(NEAR, true);
    else if (w == "quote_not_sol") r.band(NEAR, false, &s_usdc);
    else r.band(NEAR);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    auto es = r.events();
    if (!find(es, Ev::Decision, want)) {
      std::fprintf(stderr, "  missing veto %s\n", want);
      ++failures();
    }
    CHECK(r.eng->position(0).state == PosState::Empty);
  }
}

TEST(engine_soft_veto_logs_once_and_retries) {
  Rig r;
  r.sh->kill_file.store(true);
  r.create();
  r.band(NEAR);
  r.band(NEAR - 1'000'000'000);
  r.band(NEAR - 2'000'000'000);
  CHECK_EQ(count(r.events(), Ev::Decision, "kill"), 1);  // one row per change, not per update
  r.sh->kill_file.store(false);
  r.band(NEAR - 3'000'000'000);
  Packet pk;
  CHECK(r.pop_packet(pk));  // soft: retried once KILL is gone
  CHECK(find(r.events(), Ev::Decision, "buy") != nullptr);
  r.band(NEAR - 4'000'000'000);
  CHECK(!r.pop_packet(pk));  // traded: one buy per curve
}

TEST(engine_curve_stall_sells_on_curve) {
  Rig r;
  open_on_curve(r);
  const Position& p = r.eng->position(0);
  r.events();
  g_wall += int64_t(r.cfg.curve.max_hold_s * 1000) - 1;
  r.eng->poll(g_mono);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  g_wall += 2;
  r.eng->poll(g_mono);
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Sell);
  CHECK(packet_uses(pk, prog::PUMP_FUN));
  CHECK(!packet_uses(pk, prog::PUMP_SWAP));
  CHECK(p.sell_phase == Phase::Curve);
  CHECK(find(r.events(), Ev::Order, "CURVE_STALL") != nullptr);
  r.wallet_fill(pk.sig, BUY_POST, 990'000'000, p.base_amount, 0);
  CHECK(p.state == PosState::Empty);
  auto es = r.events();
  const Event* c = find(es, Ev::PositionClose, "CURVE_STALL");
  CHECK(c != nullptr);
  if (c) CHECK_EQ(c->i[5], int64_t(Phase::Curve));
  g_wall -= int64_t(r.cfg.curve.max_hold_s * 1000) + 1;
}

TEST(engine_curve_abort_on_selloff) {
  Rig r;
  r.cfg.curve.abort_remaining_sol = 4.5;
  open_on_curve(r);
  r.events();
  r.band(NEAR + 4'000'000'000'000ULL);  // holders dump: ~5.2 SOL to complete again
  Packet pk;
  CHECK(r.pop_packet(pk));
  CHECK(packet_uses(pk, prog::PUMP_FUN));
  CHECK(find(r.events(), Ev::Order, "CURVE_ABORT") != nullptr);
}

TEST(engine_buy_meets_completed_curve) {
  Rig r;
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, BUY_PRE, BUY_PRE - 760'000, 0, 0, true, custom_ix_error(3, 6005));
  CHECK(p.state == PosState::Empty);
  CHECK(!r.eng->killed());
  CHECK_EQ(r.eng->reserved_lamports(), uint64_t(0));
  auto es = r.events();
  CHECK(find(es, Ev::PositionClose, "complete_before_land") != nullptr);
  CHECK(find(es, Ev::Fill, "curve_complete") != nullptr);
}

TEST(engine_curve_sell_meets_completion_then_sells_in_pool) {
  Rig r;
  open_on_curve(r);
  const Position& p = r.eng->position(0);
  g_wall += int64_t(r.cfg.curve.max_hold_s * 1000) + 1;
  r.eng->poll(g_mono);
  Packet stall;
  CHECK(r.pop_packet(stall));
  // The curve completed first: the curve sell fails 6005. No retry, no attempt counted: wait for the migrate.
  r.wallet_fill(stall.sig, BUY_POST, BUY_POST - 760'000, p.base_amount, p.base_amount, true, custom_ix_error(2, 6005));
  CHECK(p.phase == Phase::AwaitMigrate);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.sell_attempts, 0);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  r.tip(r.slot + uint64_t(r.cfg.exits.timeout_slots));
  r.eng->poll(g_mono);
  CHECK(r.pop_packet(pk));
  CHECK(packet_uses(pk, prog::PUMP_SWAP));
  g_wall -= int64_t(r.cfg.curve.max_hold_s * 1000) + 1;
}

TEST(engine_unconfirmed_curve_sell_restarts_after_completion) {
  Rig r;
  open_on_curve(r);
  const Position& p = r.eng->position(0);
  g_wall += int64_t(r.cfg.curve.max_hold_s * 1000) + 1;
  r.eng->poll(g_mono);
  Packet stall;
  CHECK(r.pop_packet(stall));
  r.complete();  // seen before the sell's fate
  CHECK(p.phase == Phase::AwaitMigrate);
  g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
  r.eng->poll(g_mono);
  CHECK(p.state == PosState::Open);  // no curve resend into a completed curve
  Packet pk;
  CHECK(!r.pop_packet(pk));
  g_wall -= int64_t(r.cfg.curve.max_hold_s * 1000) + 1;
}

TEST(engine_pool_take_profit_on_migrate) {
  Rig r;
  r.cfg.exits.tp_pct = 5;
  open_on_curve(r);
  r.complete();
  r.events();
  // The racers' buys are already in the migrate slot's vault: the pool marks far above entry.
  r.migrate(180'000'000'000'000ULL, 95'000'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  CHECK(packet_uses(pk, prog::PUMP_SWAP));
  auto es = r.events();
  const Event* o = find(es, Ev::Order, "POOL_TP");
  CHECK(o != nullptr);
  if (o) CHECK(o->d[1] >= 5.0);
}

TEST(engine_migrate_without_pool_push_uses_default_virtual) {
  Rig r;
  open_on_curve(r);
  r.complete();
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL, 2, /*with_pool=*/false);
  const Position& p = r.eng->position(0);
  CHECK(p.phase == Phase::Pool);
  CHECK(!p.pool_seen);
  CHECK_EQ(p.virtual_quote, r.cfg.curve.default_virtual_quote_lamports);
  CHECK(p.marked);
}

TEST(engine_await_timeout_hands_off) {
  Rig r;
  r.cfg.jupiter.enabled = true;
  r.cfg.jupiter.api_key = "test";
  open_on_curve(r);
  r.complete();
  const Position& p = r.eng->position(0);
  g_wall += int64_t(r.cfg.curve.migrate_wait_s * 1000) + 1;
  r.eng->poll(g_mono);
  CHECK(r.eng->killed());
  CHECK(p.jupiter_pending);
  AuxReq q;
  bool got = false;
  while (r.sh->aux_reqs.try_pop(q)) got = got || q.op == AuxOp::JupiterSell;
  CHECK(got);
  g_wall -= int64_t(r.cfg.curve.migrate_wait_s * 1000) + 1;
}

TEST(engine_max_loss_latches_kill) {
  Rig r;
  r.cfg.risk.max_loss_sol = 0.03;
  open_in_pool(r);
  r.tip(r.slot + 10);
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  // Sold for 0.015 SOL: -0.04 SOL on the round trip.
  r.wallet_fill(sell.sig, BUY_POST, BUY_POST + 15'000'000, r.eng->position(0).base_amount, 0);
  CHECK(r.eng->position(0).state == PosState::Empty);
  CHECK(r.eng->realized_pnl() < -30'000'000);
  CHECK(r.eng->killed());
  auto es = r.events();
  const Event* k = find(es, Ev::Kill);
  CHECK(k && std::string(k->text) == "max_loss");
}

namespace {
// A clock that moves on every read, so "sent after `now` was taken" happens.
uint64_t ticking_mono() { return g_mono += 1000; }

SendResult ack(const Signature& sig, Side side, uint8_t pos, int status, bool definitive, bool rate_limited) {
  SendResult r;
  r.side = side;
  r.pos = pos;
  r.route = Route::Sender;
  r.http_status = status;
  r.definitive = definitive;
  r.rate_limited = rate_limited;
  r.sig = sig;
  return r;
}

/** In the pool, past the timeout: the sell is out. */
Packet pool_sell(Rig& r) {
  open_in_pool(r);
  r.tip(r.slot + 10);
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  return sell;
}
}  // namespace

TEST(engine_rate_limited_ack_keeps_state) {
  Rig r;
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  CHECK(r.sh->send_results.push(ack(p.buy_sig, Side::Buy, 0, 429, false, true)));
  r.eng->poll(g_mono);
  CHECK(p.state == PosState::BuySent);  // a 429 is not a rejection
  CHECK(!r.pop_packet(pk));
  const auto es = r.events();
  const Event* a = find(es, Ev::SendAck);
  CHECK(a != nullptr);
  if (a) CHECK_EQ(a->i[7], int64_t(1));
}

// 2026-10-13: a definitive send rejection (e.g. "no sender connection") re-sent at
// once, and a sell re-sent inside drain_queues() was newer than poll's `now`, so the
// unsigned age underflowed and the exit timer re-sent it again: 6 attempts in ~1 ms.
TEST(engine_rejected_sell_resends_once) {
  Rig r;
  Packet sell = pool_sell(r);
  const Position& p = r.eng->position(0);
  r.events();
  r.eng->set_clocks(&test_wall, &ticking_mono);
  uint64_t now = g_mono;  // taken before the drain, as run() does
  CHECK(r.sh->send_results.push(ack(sell.sig, Side::Sell, 0, 200, true, false)));
  r.eng->poll(now);
  Packet pk;
  CHECK(!r.pop_packet(pk));  // waits for the exit timer
  CHECK(p.state == PosState::SellSent);
  CHECK_EQ(p.sell_attempts, 0);
  g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
  r.eng->poll(g_mono);
  int sent = 0;
  while (r.pop_packet(pk)) {
    ++sent;
    CHECK_EQ(int(pk.attempt), 1);
  }
  CHECK_EQ(sent, 1);
  CHECK_EQ(p.sell_attempts, 1);
  CHECK(!r.eng->killed());
  r.eng->set_clocks(&test_wall, &test_mono);
}

TEST(engine_exit_retries_then_kill) {
  Rig r;
  Packet sell = pool_sell(r);
  (void)sell;
  Packet pk;
  for (int i = 1; i < r.cfg.exits.max_exit_attempts; ++i) {
    g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
    r.eng->poll(g_mono);
    CHECK(r.pop_packet(pk));
  }
  g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));
  CHECK(r.eng->killed());
}

TEST(engine_fill_records_landing) {
  Rig r;
  r.tip(r.slot);  // processed tip = trigger slot
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  CHECK_EQ(p.order_slot, uint64_t(100));
  uint64_t land = 102;
  r.feed(slot_frame(land, 3));  // first shred of the landing slot
  g_mono += 40'000'000;
  r.feed(slot_frame(land, 4));  // completed
  g_mono += 10'000'000;
  r.events();
  TxSpec t;
  t.filters = {"wallet"};
  t.sig = p.buy_sig;
  t.slot = land;
  t.index = 7;
  t.keys = {r.kp.pubkey()};
  t.pre = {BUY_PRE};
  t.post = {BUY_POST};
  t.post_tokens = {{r.mint_s, r.user_s, r.t22_s, p.expected_base}};
  r.feed(tx_frame(t));
  CHECK(p.state == PosState::Open);
  const auto es = r.events();
  const Event* f = find(es, Ev::Fill);
  CHECK(f != nullptr);
  if (!f) return;
  CHECK_EQ(f->i[5], int64_t(7));
  CHECK_EQ(f->i[6], int64_t(2));
  CHECK_EQ(f->i[7], int64_t(50'000'000));  // first shred -> seen
  CHECK(std::fabs(f->d[2] - 10.0) < 1e-9);  // completed -> seen, ms
  CHECK_EQ(f->d[5], 1.0);
}

// 2026-10-13: another signer on the bot's wallet sold each position seconds after the
// buy and closed the token account; exits then failed (AccountNotInitialized) to the kill.
TEST(engine_follows_external_sell) {
  Rig r;
  open_on_curve(r);
  const Position& p = r.eng->position(0);
  uint64_t base = p.base_amount;
  r.events();
  r.wallet_fill(make_sig(50), BUY_POST, BUY_POST + 2'000'000, 0, 0);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.base_amount, base);
  r.wallet_fill(make_sig(51), BUY_POST, BUY_POST + 20'000'000, base, base - base / 3);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.base_amount, base - base / 3);
  CHECK(find(r.events(), Ev::PositionUpdate) != nullptr);
  r.wallet_fill(make_sig(52), BUY_POST, BUY_POST + 30'000'000, base - base / 3, 0);
  CHECK(p.state == PosState::Empty);
  CHECK(find(r.events(), Ev::PositionClose, "external_sell") != nullptr);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  CHECK(!r.eng->killed());
}

TEST(engine_external_sell_while_selling) {
  Rig r;
  Packet sell = pool_sell(r);
  const Position& p = r.eng->position(0);
  uint64_t base = p.base_amount;
  r.wallet_fill(make_sig(53), BUY_POST, 990'000'000, base, 0);
  CHECK(p.state == PosState::Empty);
  // Our own sell then fails on-chain: ignored, no retry, no kill.
  r.wallet_fill(sell.sig, 990'000'000, 989'600'000, 0, 0, /*failed=*/true);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  CHECK(!r.eng->killed());
}

TEST(engine_sell_429_holds_exit_timer) {
  Rig r;
  Packet sell = pool_sell(r);
  const Position& p = r.eng->position(0);
  const uint64_t ms = 1'000'000ULL;
  g_mono += 1000 * ms;  // exec's first 429 resend is due about now
  CHECK(r.sh->send_results.push(ack(sell.sig, Side::Sell, 0, 429, false, true)));
  r.eng->poll(g_mono);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  g_mono += 1000 * ms;  // 2.0 s after the send, 1.0 s after the 429: timer was restarted
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));
  CHECK(p.state == PosState::SellSent);
  g_mono += uint64_t(r.cfg.exits.exit_retry_ms) * ms;
  r.eng->poll(g_mono);
  CHECK(r.pop_packet(pk));
  CHECK_EQ(int(pk.attempt), 1);
}

TEST(exit_rules) {
  Config c;
  CHECK(evaluate_curve_exit(0, 3.0, c.curve) == ExitTrigger::None);
  CHECK(evaluate_curve_exit(c.curve.max_hold_s, 3.0, c.curve) == ExitTrigger::CurveStall);
  c.curve.abort_remaining_sol = 4.5;
  CHECK(evaluate_curve_exit(0, 5.0, c.curve) == ExitTrigger::CurveAbort);
  CHECK(evaluate_pool_exit(50, 100, 100, c.exits) == ExitTrigger::None);  // tp off by default
  CHECK(evaluate_pool_exit(0, 103, 100, c.exits) == ExitTrigger::PoolTimeout);
  c.exits.tp_pct = 5;
  CHECK(evaluate_pool_exit(5, 100, 100, c.exits) == ExitTrigger::PoolTakeProfit);
  CHECK(is_protective(ExitTrigger::PoolTimeout));
  CHECK(!is_protective(ExitTrigger::PoolTakeProfit));
}

namespace {

/** A table that gives every slot in [0, 4096) the same class and leader. */
std::unique_ptr<LeaderTable> uniform_table(LeaderClass c, const Pubkey& leader) {
  auto t = std::make_unique<LeaderTable>();
  t->first_slot = 0;
  t->cls.assign(4096, uint8_t(c));
  t->leader.assign(4096, 1);
  t->identities = {Pubkey{}, leader};
  return t;
}

void publish_routes(Rig& r, uint64_t estimate, uint64_t beat_agave = 0) {
  uint64_t beat[N_LEADER_CLASSES] = {beat_agave, 0, 0};
  r.sh->routes.store(make_route_policy(r.cfg, estimate, beat));
}

const Event* route_for(const std::vector<Event>& es, Side side) {
  for (auto& e : es)
    if (e.type == Ev::Route && e.i[5] == int64_t(side)) return &e;
  return nullptr;
}

}  // namespace

TEST(engine_routes_by_leader_class) {
  Pubkey leader = parse_pubkey("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ");
  for (LeaderClass cls : {LeaderClass::Agave, LeaderClass::Bam, LeaderClass::Firedancer}) {
    Rig r;
    auto table = uniform_table(cls, leader);
    r.sh->leaders.store(table.get());
    publish_routes(r, 1'200'000);
    r.create();
    r.band(NEAR);
    Packet pk;
    CHECK(r.pop_packet(pk));
    CHECK(!pk.jito);
    auto es = r.events();
    const Event* rt = route_for(es, Side::Buy);
    CHECK(rt != nullptr);
    if (rt) {
      CHECK_EQ(rt->i[0], int64_t(cls));
      CHECK_EQ(rt->i[1], int64_t(1'200'000));  // estimate on every class (no beat_rivals by default)
      CHECK_EQ(rt->i[2], int64_t(r.cfg.entry.cu_limit));
      CHECK_EQ(rt->i[7], int64_t(1));
      CHECK(rt->a == leader);
      CHECK(rt->sig == pk.sig);
    }
    r.sh->leaders.store(nullptr);
  }
}

TEST(engine_routing_fallbacks) {
  // No leader table: Agave. No policy yet: the legacy single price, unrouted.
  Rig r;
  r.sh->cu_price.store(700'000);
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  auto es = r.events();
  const Event* rt = route_for(es, Side::Buy);
  CHECK(rt != nullptr);
  if (rt) {
    CHECK_EQ(rt->i[0], int64_t(LeaderClass::Agave));
    CHECK_EQ(rt->i[1], int64_t(700'000));
    CHECK_EQ(rt->i[7], int64_t(0));
    CHECK(is_zero(rt->a));
  }
}

TEST(engine_exits_price_at_exit_multiplier) {
  Rig r;
  r.cfg.exits.exit_priority_multiplier = 1;
  publish_routes(r, 1'200'000);
  Packet sell = pool_sell(r);
  (void)sell;
  auto es = r.events();
  const Event* rt = route_for(es, Side::Sell);
  CHECK(rt != nullptr);
  if (rt) {
    CHECK_EQ(rt->i[1], int64_t(1'200'000));
    CHECK_EQ(rt->i[2], int64_t(r.cfg.exits.cu_limit));
  }
}

TEST(engine_measures_buy_cu) {
  Rig r;
  r.cfg.routing.cu_samples_min = 1;
  publish_routes(r, 1'000'000);
  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  TxSpec t;
  t.filters = {"wallet"};
  t.sig = pk.sig;
  t.slot = ++r.slot;
  t.keys = {r.kp.pubkey()};
  t.pre = {BUY_PRE};
  t.post = {BUY_POST};
  t.post_tokens = {{r.mint_s, r.user_s, r.t22_s, p.expected_base}};
  t.cu_consumed = 90'000;
  r.feed(tx_frame(t));
  CHECK_EQ(r.eng->measured_cu_limit(), uint32_t(103'500));  // + cu_limit_margin_pct
}

TEST(engine_records_rival_txs) {
  Rig r;
  open_in_pool(r);
  r.events();
  uint64_t migrate_slot = r.slot;
  Pubkey rival = parse_pubkey("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta");
  Pubkey tip = parse_pubkey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5");
  auto rival_tx = [&](const Pubkey& signer, uint64_t slot, uint8_t seed) {
    TxSpec t;
    t.filters = {"pools"};
    t.sig = make_sig(seed);
    t.slot = slot;
    t.index = 7;
    t.keys = {signer, r.pool, r.quote_vault, tip, prog::COMPUTE_BUDGET, prog::SYSTEM};
    auto cat = [](std::vector<uint8_t> a, const std::vector<uint8_t>& b) { a.insert(a.end(), b.begin(), b.end()); return a; };
    t.ixs = {{4, {}, cat({2}, le_bytes(150'000, 4))},
             {4, {}, cat({3}, le_bytes(4'000'000, 8))},
             {5, {0, 3}, cat(le_bytes(2, 4), le_bytes(2'000'000, 8))}};
    t.pre_tokens = {{r.wsol_s, r.pool_s, r.token_s, 67'410'000'000ULL}};
    t.post_tokens = {{r.wsol_s, r.pool_s, r.token_s, 68'410'000'000ULL}};
    t.cu_consumed = 90'000;
    r.feed(tx_frame(t));
  };
  rival_tx(rival, migrate_slot, 60);
  rival_tx(r.kp.pubkey(), migrate_slot, 61);                                     // ours: not a rival
  rival_tx(rival, migrate_slot + uint64_t(r.cfg.routing.rival_window_slots) + 1, 62);  // after the window
  auto es = r.events();
  CHECK_EQ(count(es, Ev::RivalTx), 1);
  const Event* e = find(es, Ev::RivalTx);
  CHECK(e != nullptr);
  if (!e) return;
  CHECK(e->a == rival);
  CHECK(e->b == r.pool);
  CHECK_EQ(e->i[4], int64_t(1'000'000'000));  // a buy we sell into
  CHECK_EQ(e->i[6], int64_t(0));
}

namespace {

/** Pool sell that fails on chain with max_exit_attempts = 1: the native exits are spent. */
Packet exhaust_exits(Rig& r) {
  r.cfg.exits.max_exit_attempts = 1;
  Packet sell = pool_sell(r);
  const Position& p = r.eng->position(0);
  r.wallet_fill(sell.sig, BUY_POST, BUY_POST - 10'000, p.base_amount, p.base_amount, true);
  return sell;
}

bool pop_jupiter_req(Rig& r, AuxReq& out) {
  AuxReq q;
  while (r.sh->aux_reqs.try_pop(q))
    if (q.op == AuxOp::JupiterSell) { out = q; return true; }
  return false;
}

}  // namespace

TEST(engine_jupiter_takes_exhausted_exits) {
  Rig r;
  r.cfg.jupiter.enabled = true;
  r.cfg.jupiter.api_key = "test";
  exhaust_exits(r);
  const Position& p = r.eng->position(0);
  CHECK(r.eng->killed());  // entries stop all the same
  CHECK(p.state == PosState::SellSent);
  CHECK(p.jupiter_pending);
  AuxReq q;
  CHECK(pop_jupiter_req(r, q));
  CHECK(q.mint == r.mint);
  CHECK_EQ(q.amount, p.base_amount);
  AuxResp ok;
  ok.op = AuxOp::JupiterSell;
  ok.pos = q.pos;
  ok.mint = q.mint;
  ok.ok = true;
  ok.sig = make_sig(77);
  ok.out_lamports = 50'000'000;
  CHECK(r.sh->aux_resps.push(ok));
  r.eng->poll(g_mono);
  CHECK(find(r.events(), Ev::Order, "jupiter POOL_TIMEOUT") != nullptr);
  r.wallet_fill(ok.sig, BUY_POST, 995'000'000, p.base_amount, 0);
  CHECK(p.state == PosState::Empty);
  CHECK(find(r.events(), Ev::PositionClose, "jupiter_exit") != nullptr);
}

TEST(engine_exhausted_exits_without_jupiter_hand_off) {
  Rig r;
  r.cfg.jupiter.enabled = false;
  exhaust_exits(r);
  const Position& p = r.eng->position(0);
  CHECK(r.eng->killed());
  CHECK(p.state == PosState::Open);
  AuxReq q;
  CHECK(!pop_jupiter_req(r, q));
}

TEST(engine_restores_each_phase) {
  for (Phase ph : {Phase::Curve, Phase::AwaitMigrate, Phase::Pool}) {
    Rig r;
    r.tip(r.slot);
    RestoredPosition rp;
    rp.curve = r.curve;
    rp.mint = r.mint;
    rp.creator = r.creator;
    rp.token_program = prog::TOKEN_2022;
    rp.base_amount = rp.base_bought = 120'000'000'000ULL;
    rp.entry_price = 4.0e-7;
    rp.sol_spent = 50'000'000;
    rp.opened_ms = g_wall;
    rp.phase = ph;
    Curve c;
    auto cd = curve_data(NEAR, false, r.creator);
    decode_curve(cd.data(), cd.size(), c);
    rp.curve_state = c;
    rp.base_res = 206'900'000'000'000ULL;
    rp.quote_res = 67'410'000'000ULL;
    rp.virtual_quote = 17'584'492'770ULL;
    CHECK(r.eng->restore_position(rp));
    const Position& p = r.eng->position(0);
    CHECK(p.phase == ph);
    CHECK(p.marked || ph == Phase::AwaitMigrate);
    r.eng->poll(g_mono);
    Packet pk;
    bool sold = r.pop_packet(pk);
    // Restored in the pool: the timeout is long past, so it sells at once. Elsewhere it waits.
    CHECK_EQ(sold, ph == Phase::Pool);
    if (ph == Phase::Curve) {
      r.complete();
      CHECK(p.phase == Phase::AwaitMigrate);
    }
  }
}

// Not an assertion of speed (CI boxes vary): prints the in-process entry latency,
// curve frame in -> signed curve buy queued, on the real clocks.
TEST(bench_entry_latency) {
  std::vector<double> us;
  for (int i = 0; i < 40; ++i) {
    Rig r;
    r.eng->set_clocks(&pd::wall_ms, &pd::now_ns);
    BlockhashSnap bh;
    r.sh->blockhash.load(bh);
    bh.fetched_ns = pd::now_ns();
    r.sh->blockhash.store(bh);
    r.create();
    r.band(ADMIT);
    std::string frame = account_frame("band0", r.curve, prog::PUMP_FUN, curve_data(NEAR, false, r.creator), r.slot);
    uint64_t t0 = pd::now_ns();
    r.eng->on_frame(reinterpret_cast<const uint8_t*>(frame.data()), frame.size(), t0);
    Packet pk;
    if (!r.pop_packet(pk)) { CHECK(false); return; }
    us.push_back(double(pk.t_signed_ns - t0) / 1000.0);
  }
  std::sort(us.begin(), us.end());
  std::printf("  entry latency (curve frame -> signed buy queued): p50 %.1f us, p90 %.1f us, max %.1f us\n",
              us[us.size() / 2], us[us.size() * 9 / 10], us.back());
}

TEST(writer_persists_positions_for_restore) {
  std::string dir = std::filesystem::temp_directory_path().string() + "/pd_writer_test_" + std::to_string(::getpid());
  std::filesystem::create_directories(dir);
  Rig r;
  r.cfg.storage.db_path = dir + "/t.db";
  r.cfg.storage.jsonl_path = dir + "/t.jsonl";
  Logger log;
  Writer w(r.cfg, *r.sh, log);
  w.open();
  auto flush = [&] { for (auto& e : r.events()) w.write(e); };

  r.create();
  r.band(NEAR);
  Packet pk;
  CHECK(r.pop_packet(pk));
  flush();
  auto pending = w.open_positions();  // in flight: restorable as pending (entry 0)
  CHECK_EQ(pending.size(), size_t(1));
  if (!pending.empty()) {
    CHECK(pending[0].mint == r.mint);
    CHECK_EQ(pending[0].entry_price, 0.0);
    CHECK(pending[0].token_program == prog::TOKEN_2022);
    CHECK(pending[0].creator == r.creator);
  }
  const Position& p = r.eng->position(0);
  uint64_t base = p.expected_base;
  r.wallet_fill(p.buy_sig, BUY_PRE, BUY_POST, 0, base);
  flush();
  auto open = w.open_positions();
  CHECK_EQ(open.size(), size_t(1));
  if (!open.empty()) {
    CHECK_EQ(open[0].base_amount, base);
    CHECK(open[0].entry_price > 0);
    CHECK(open[0].curve == r.curve);
  }
  r.complete();
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  r.tip(r.slot + 10);
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  r.wallet_fill(sell.sig, BUY_POST, 1'000'000'000, base, 0);
  flush();
  CHECK(w.open_positions().empty());
  if (std::getenv("PD_KEEP_TEST_DB")) std::printf("  kept %s\n", r.cfg.storage.db_path.c_str());
  else std::filesystem::remove_all(dir);
}
