// End-to-end logic-thread tests over synthetic frames: create -> band -> fire ->
// migrate -> buy packet -> wallet fill -> mark -> exit -> sell fill -> close.
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
#include "sol/lighthouse.hpp"
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

std::vector<uint8_t> curve_data(uint64_t vt, uint64_t vs, uint64_t real, bool complete, const Pubkey& creator) {
  std::vector<uint8_t> d(151, 0);
  std::memcpy(d.data(), disc::BONDING_CURVE, 8);
  wr_u64(d.data() + 8, vt);
  wr_u64(d.data() + 16, vs);
  wr_u64(d.data() + 24, real);
  wr_u64(d.data() + 32, complete ? 0 : 50'000'000'000ULL);
  wr_u64(d.data() + 40, 1'000'000'000'000'000ULL);
  d[48] = complete ? 1 : 0;
  std::memcpy(d.data() + 49, creator.data(), 32);
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
    decode_global_config(gc.data(), gc.size(), amm);
    decode_fee_config(fc.data(), fc.size(), amm);
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

  void band(uint64_t real_tokens) {
    // vT 280e12 / vS 80e9: completion cost of `real` = 1e12 is ~0.29 SOL.
    feed(account_frame("band0", curve, prog::PUMP_FUN, curve_data(280'000'000'000'000ULL, 80'000'000'000ULL, real_tokens, false, creator), slot));
  }

  void pool_push() {
    feed(account_frame("watch", pool, prog::PUMP_SWAP, b64bytes(golden()["accounts"]["pool"]), slot));
  }

  /** The pool push usually lands a few ms before the migrate tx; `with_pool` sends it first. */
  void migrate(uint64_t base, uint64_t quote, uint8_t sig_seed = 2, bool with_pool = true) {
    if (with_pool) pool_push();
    TxSpec t;
    t.filters = {"migrate"};
    t.sig = make_sig(sig_seed);
    t.slot = ++slot;
    t.keys = {prog::MIGRATION_AUTHORITY};
    std::string pf = b58s(prog::PUMP_FUN);
    t.logs = {"Program " + pf + " invoke [1]", "Program log: Instruction: MigrateV2", "Program " + pf + " success"};
    t.pre_tokens = {{mint_s, curve_s, t22_s, base}};
    t.post_tokens = {{mint_s, pool_s, t22_s, base}, {wsol_s, pool_s, token_s, quote}};
    feed(tx_frame(t));
  }

  void wallet_fill(const Signature& sig, uint64_t pre, uint64_t post, uint64_t tok_pre, uint64_t tok_post, bool failed = false) {
    TxSpec t;
    t.filters = {"wallet"};
    t.sig = sig;
    t.slot = ++slot;
    t.failed = failed;
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

bool verify_packet(const Packet& pk, const Pubkey& signer) {
  std::vector<uint8_t> tx;
  if (!b64_decode(std::string_view(pk.b64, pk.b64_len), tx)) return false;
  if (tx.size() > PACKET_DATA_SIZE || tx.size() < 65 || tx[0] != 1) return false;
  if (std::memcmp(tx.data() + 1, pk.sig.data(), 64) != 0) return false;
  return crypto_sign_ed25519_verify_detached(tx.data() + 1, tx.data() + 65, tx.size() - 65, signer.data()) == 0;
}

}  // namespace

TEST(engine_full_round_trip) {
  Rig r;
  r.create();
  r.band(1'000'000'000'000ULL);
  const CurveSlot* c = r.eng->curve(r.curve);
  CHECK(c != nullptr);
  if (!c) return;
  CHECK(c->mint_known);
  CHECK(c->fired);
  CHECK(c->keys_ready);
  CHECK(c->fee == TransferFee::None);  // proven by the create logs
  CHECK(c->keys.pool == r.pool);
  CHECK(r.sh->watch_cmds.size() == 4);  // pool, both vaults, and the pool's txs (rival profiling)

  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Buy);
  CHECK(verify_packet(pk, r.kp.pubkey()));
  const Position& p = r.eng->position(0);
  CHECK(p.state == PosState::BuySent);
  CHECK_EQ(p.size_lamports, uint64_t(150'000'000));  // 15 % of 1 SOL
  CHECK(r.eng->reserved_lamports() > p.size_lamports);
  auto es = r.events();
  CHECK(find(es, Ev::Decision, "buy") != nullptr);
  CHECK(find(es, Ev::Order) != nullptr);

  // Fill: 0.16 SOL out, expected_base tokens in.
  uint64_t base = p.expected_base;
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, base);
  CHECK(r.eng->position(0).state == PosState::Open);
  CHECK_EQ(r.eng->position(0).base_amount, base);
  CHECK(r.eng->position(0).entry_price > 0);
  CHECK_EQ(r.eng->reserved_lamports(), uint64_t(0));
  CHECK_EQ(r.eng->wallet_lamports(), uint64_t(840'000'000));
  es = r.events();
  CHECK(find(es, Ev::PositionOpen) != nullptr);

  // Quote vault triples -> far above +50 % -> TP1 full exit.
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(3 * 67'410'000'000ULL), r.slot));
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(sell.side == Side::Sell);
  CHECK(verify_packet(sell, r.kp.pubkey()));
  CHECK(r.eng->position(0).state == PosState::SellSent);
  es = r.events();
  CHECK(find(es, Ev::Order, "TAKE_PROFIT_1") != nullptr);

  r.wallet_fill(sell.sig, 840'000'000, 1'100'000'000, base, 0);
  CHECK(r.eng->position(0).state == PosState::Empty);
  es = r.events();
  const Event* close = find(es, Ev::PositionClose);
  CHECK(close != nullptr);
  if (close) {
    CHECK_EQ(std::string(close->text), std::string("TAKE_PROFIT_1"));
    CHECK_EQ(close->i[0], int64_t(1'100'000'000 - 840'000'000) - int64_t(160'000'000));
  }
  // Watches released by the position; the curve still holds its own.
  CHECK(r.eng->curve(r.curve) != nullptr);
}

TEST(engine_pool_push_triggers_first) {
  Rig r;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.pool_push();
  r.feed(account_frame("watch", r.base_vault, prog::TOKEN_2022, token_account(206'900'000'000'000ULL), r.slot));
  Packet pk;
  CHECK(!r.pop_packet(pk));  // quote vault still missing
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'410'000'000ULL), r.slot));
  CHECK(r.pop_packet(pk));
  auto es = r.events();
  const Event* d = find(es, Ev::Decision, "buy");
  CHECK(d != nullptr);
  if (d) CHECK_EQ(d->i[2], int64_t(1));  // TriggerSrc::PoolPush
  // The migrate tx arriving after must not trigger a second buy.
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  CHECK(!r.pop_packet(pk));
}

TEST(engine_stale_pool_vetoes) {
  {
    Rig r;  // migrate seen, but the pool push arrives past max_migrate_age_slots
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL, 2, /*with_pool=*/false);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    r.slot += uint64_t(r.cfg.screen.max_migrate_age_slots) + 10;
    r.feed(slot_frame(r.slot));
    r.pool_push();
    r.feed(account_frame("watch", r.base_vault, prog::TOKEN_2022, token_account(206'900'000'000'000ULL), r.slot));
    r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'410'000'000ULL), r.slot));
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "stale_pool") != nullptr);
  }
  {
    Rig r;  // never seen incomplete and never seen migrating: a pre-graduated pool is stale
    r.create();
    r.feed(account_frame("band0", r.curve, prog::PUMP_FUN,
                         curve_data(280'000'000'000'000ULL, 80'000'000'000ULL, 206'900'000'000'000ULL, true, r.creator),
                         r.slot));
    r.pool_push();
    r.feed(account_frame("watch", r.base_vault, prog::TOKEN_2022, token_account(206'900'000'000'000ULL), r.slot));
    r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'410'000'000ULL), r.slot));
    Packet pk;
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "stale_pool") != nullptr);
  }
}

TEST(engine_migrate_waits_for_pool_push) {
  {
    Rig r;  // pool push a moment after the migrate tx: priced on the pool's own virtual reserve
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL, 2, /*with_pool=*/false);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    r.pool_push();
    CHECK(r.pop_packet(pk));
    const Event* d = find(r.events(), Ev::Decision, "buy");
    CHECK(d != nullptr);
    if (d) CHECK(std::abs(d->d[4] - 17.58449277) < 1e-6);
  }
  {
    Rig r;  // no pool push at all: fall back to the default after pool_push_wait_ms
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL, 2, /*with_pool=*/false);
    Packet pk;
    r.eng->poll(g_mono);
    CHECK(!r.pop_packet(pk));
    uint64_t saved = g_mono;
    g_mono += uint64_t(r.cfg.racer.pool_push_wait_ms + 1) * 1'000'000ULL;
    r.eng->poll(g_mono);
    CHECK(r.pop_packet(pk));
    const Event* d = find(r.events(), Ev::Decision, "buy");
    CHECK(d != nullptr);
    if (d) CHECK_EQ(d->i[2], int64_t(2));  // PoolWaitTimeout
    g_mono = saved;
  }
}

TEST(engine_vetoes) {
  {
    Rig r;  // pool pumped inside the migrate slot
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(150'000'000'000'000ULL, 150'000'000'000ULL);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "pool_above_max") != nullptr);
  }
  {
    Rig r;  // admitted but never cheap enough to fire
    r.create();
    r.band(9'000'000'000'000ULL);  // ~2.7 SOL to complete: admitted, not fired
    const CurveSlot* c = r.eng->curve(r.curve);
    CHECK(c && !c->fired);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "not_fired") != nullptr);
  }
  {
    Rig r;  // kill file
    r.sh->kill_file = true;
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "kill") != nullptr);
  }
  {
    Rig r;  // T22 mint whose fee was never proven
    TxSpec t;
    r.band(1'000'000'000'000ULL);  // no create seen: resolve goes to aux, then aux says T22 + unknown fee
    AuxReq req;
    CHECK(r.sh->aux_reqs.try_pop(req));
    CHECK(req.op == AuxOp::ResolveCurve);
    AuxResp a;
    a.op = AuxOp::ResolveCurve;
    a.ok = true;
    a.curve = r.curve;
    a.mint = r.mint;
    a.token_program = prog::TOKEN_2022;
    a.mint_info.fee = TransferFee::Unknown;
    r.sh->aux_resps.push(a);
    r.eng->poll(g_mono);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
    Packet pk;
    CHECK(!r.pop_packet(pk));
    CHECK(find(r.events(), Ev::Decision, "t22_fee_unverified") != nullptr);
  }
}

TEST(engine_failed_buy_releases_reservation) {
  Rig r;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  Signature sig = r.eng->position(0).buy_sig;
  r.wallet_fill(sig, 1'000'000'000, 999'990'000, 0, 0, /*failed=*/true);
  CHECK(r.eng->position(0).state == PosState::Empty);
  CHECK_EQ(r.eng->reserved_lamports(), uint64_t(0));
  const Event* c = find(r.events(), Ev::PositionClose, "buy_failed");
  CHECK(c != nullptr);
}

TEST(engine_time_stop_and_exit_retries) {
  Rig r;
  // Isolate TIME_STOP from the faster stall / dead-money / dump cuts.
  r.cfg.exits.stall_enabled = false;
  r.cfg.exits.dead_money_enabled = false;
  r.cfg.exits.liquidity_dump_pct = 0;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, p.expected_base);
  CHECK(p.state == PosState::Open);
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));  // nothing to do yet
  g_wall += 11 * 60'000;     // past the time stop
  r.eng->poll(g_mono);
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Sell);
  CHECK(find(r.events(), Ev::Order, "TIME_STOP") != nullptr);
  // Unconfirmed: retries on the exit timer with the next slippage tier, then latches the kill.
  for (int i = 1; i < r.cfg.exits.max_exit_attempts; ++i) {
    g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
    r.eng->poll(g_mono);
    CHECK(r.pop_packet(pk));
  }
  g_mono += uint64_t(r.cfg.exits.exit_retry_ms + 1) * 1'000'000ULL;
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));
  CHECK(r.eng->killed());
  g_wall -= 11 * 60'000;
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

void open_position(Rig& r) {
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, p.expected_base);
  CHECK(p.state == PosState::Open);
}
}  // namespace

TEST(engine_rate_limited_ack_keeps_state) {
  Rig r;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
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

  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, p.expected_base);
  CHECK(p.state == PosState::Open);
  g_wall += 11 * 60'000;  // time stop
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(r.sh->send_results.push(ack(sell.sig, Side::Sell, 0, 429, false, true)));
  r.eng->poll(g_mono);
  CHECK(p.state == PosState::SellSent);
  CHECK_EQ(p.sell_attempts, 0);
  CHECK(!r.pop_packet(pk));
  CHECK(!r.eng->killed());
  g_wall -= 11 * 60'000;
}

// 2026-10-13: a definitive send rejection (e.g. "no sender connection") re-sent at
// once, and a sell re-sent inside drain_queues() was newer than poll's `now`, so the
// unsigned age underflowed and the exit timer re-sent it again: 6 attempts in ~1 ms.
TEST(engine_rejected_sell_resends_once) {
  Rig r;
  open_position(r);
  const Position& p = r.eng->position(0);
  g_wall += 11 * 60'000;
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
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
  g_wall -= 11 * 60'000;
}

TEST(engine_fill_records_landing) {
  Rig r;
  r.feed(slot_frame(r.slot));  // processed tip = trigger slot
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  CHECK_EQ(p.order_slot, uint64_t(100));
  uint64_t land = 102;
  r.feed(slot_frame(land, 3));  // first shred of the landing slot
  CHECK_EQ(r.eng->last_slot(), uint64_t(100));
  g_mono += 40'000'000;
  r.feed(slot_frame(land, 4));  // completed
  // Two other swaps write the quote vault, then ours, then one more.
  Signature other1 = make_sig(40), other2 = make_sig(41), other3 = make_sig(42);
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'500'000'000ULL), land, &other1));
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'600'000'000ULL), land, &other2));
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'700'000'000ULL), land, &p.buy_sig));
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'800'000'000ULL), land, &other3));
  CHECK_EQ(p.writes_ahead, int64_t(2));
  g_mono += 10'000'000;
  r.events();
  TxSpec t;
  t.filters = {"wallet"};
  t.sig = p.buy_sig;
  t.slot = land;
  t.index = 7;
  t.keys = {r.kp.pubkey()};
  t.pre = {1'000'000'000};
  t.post = {840'000'000};
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
  CHECK_EQ(f->d[1], 2.0);
  CHECK_EQ(f->d[5], 1.0);
}

// 2026-10-13: another signer on the bot's wallet sold each position seconds after the
// buy and closed the token account; exits then failed (AccountNotInitialized) to the kill.
TEST(engine_follows_external_sell) {
  Rig r;
  open_position(r);
  const Position& p = r.eng->position(0);
  uint64_t base = p.base_amount;
  r.events();
  // A close-account-style tx touching nothing we hold changes nothing.
  r.wallet_fill(make_sig(50), 840'000'000, 842'000'000, 0, 0);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.base_amount, base);
  // Someone else sells a third.
  r.wallet_fill(make_sig(51), 842'000'000, 892'000'000, base, base - base / 3);
  CHECK(p.state == PosState::Open);
  CHECK_EQ(p.base_amount, base - base / 3);
  CHECK_EQ(p.sol_received, int64_t(50'000'000));
  CHECK(find(r.events(), Ev::PositionUpdate) != nullptr);
  // Then the rest: the position closes without a single sell of ours.
  r.wallet_fill(make_sig(52), 892'000'000, 992'000'000, base - base / 3, 0);
  CHECK(p.state == PosState::Empty);
  const auto es = r.events();
  const Event* c = find(es, Ev::PositionClose, "external_sell");
  CHECK(c != nullptr);
  if (c) CHECK_EQ(c->i[3], int64_t(150'000'000));
  Packet pk;
  CHECK(!r.pop_packet(pk));
  CHECK(!r.eng->killed());
}

TEST(engine_external_sell_while_selling) {
  Rig r;
  open_position(r);
  const Position& p = r.eng->position(0);
  uint64_t base = p.base_amount;
  g_wall += 11 * 60'000;  // time stop
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(p.state == PosState::SellSent);
  r.wallet_fill(make_sig(53), 840'000'000, 990'000'000, base, 0);
  CHECK(p.state == PosState::Empty);
  // Our own sell then fails on-chain: ignored, no retry, no kill.
  r.wallet_fill(sell.sig, 990'000'000, 989'600'000, 0, 0, /*failed=*/true);
  Packet pk;
  CHECK(!r.pop_packet(pk));
  CHECK(!r.eng->killed());
  g_wall -= 11 * 60'000;
}

TEST(engine_sell_429_holds_exit_timer) {
  Rig r;
  open_position(r);
  const Position& p = r.eng->position(0);
  g_wall += 11 * 60'000;
  r.eng->poll(g_mono);
  Packet sell;
  CHECK(r.pop_packet(sell));
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
  g_wall -= 11 * 60'000;
}

TEST(exit_fsm_matches_engine_ts) {
  Config c;
  Config::Exits x = c.exits;
  ExitState s;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 0.5, 1000, x).trigger == ExitTrigger::StopLoss);  // hard stop 22 %
  // +50 % is below TP1 (55 %) so TP0 banks half.
  ExitDecision d0 = evaluate_exit(s, 1.5, 1000, x);
  CHECK(d0.trigger == ExitTrigger::TakeProfit0);
  CHECK_EQ(d0.sell_fraction, 0.5);
  CHECK(evaluate_exit(s, 1.6, 1000, x).trigger == ExitTrigger::TakeProfit1);
  x.stall_enabled = false;
  x.dead_money_enabled = false;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 1.2, int64_t(x.time_stop_minutes * 60'000), x).trigger == ExitTrigger::TimeStop);

  x.hard_stop_pct = 20;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 0.8, 1000, x).trigger == ExitTrigger::StopLoss);

  x.trailing_arm_pct = 20;
  x.tp1_pct = 1000;
  x.tp0_enabled = false;
  exit_init(s, 1.0, 0, x);
  exit_observe(s, 1.3, 500, x);
  CHECK(s.trailing_armed);
  CHECK(evaluate_exit(s, 1.3 * 0.87, 1000, x).trigger == ExitTrigger::TrailingStop);  // 12 % gap

  x = c.exits;
  x.stall_enabled = false;
  x.dead_money_enabled = false;
  x.liquidity_dump_pct = 0;
  exit_init(s, 1.0, 0, x);
  ExitDecision d = evaluate_exit(s, 1.25, 1000, x);
  CHECK(d.trigger == ExitTrigger::TakeProfit0);
  CHECK_EQ(d.sell_fraction, 0.5);
  exit_on_fill(s, ExitTrigger::TakeProfit0, x);
  CHECK(s.tp0_done);
  CHECK_EQ(s.stop_price, 1.05);  // tp0_move_stop_to_pct = 5

  // Quote vault dump: peak 100, last 84 → −16 % ≤ −15 %.
  exit_init(s, 1.0, 0, x);
  x.liquidity_dump_pct = 15;
  exit_observe_quote(s, 100);
  exit_observe_quote(s, 84);
  CHECK(evaluate_exit(s, 1.0, 1000, x).trigger == ExitTrigger::LiquidityDump);

  // Stall: no new high for stall_sec after stall_arm_sec.
  x.liquidity_dump_pct = 0;
  x.stall_enabled = true;
  x.stall_arm_sec = 45;
  x.stall_sec = 45;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 1.0, 44'000, x).trigger == ExitTrigger::None);
  CHECK(evaluate_exit(s, 1.0, 90'000, x).trigger == ExitTrigger::Stall);
}

TEST(engine_liquidity_dump_sells) {
  Rig r;
  r.cfg.exits.stall_enabled = false;
  r.cfg.exits.dead_money_enabled = false;
  r.cfg.exits.hard_stop_pct = 100;  // dump must beat a disabled stop
  r.cfg.exits.tp0_enabled = false;
  r.cfg.exits.tp1_pct = 1000;
  r.cfg.exits.trailing_arm_pct = 1000;
  open_position(r);
  const Position& p = r.eng->position(0);
  // Peak at the migrate quote, then dump 20 % of the vault SOL.
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(67'410'000'000ULL), r.slot));
  Packet pk;
  CHECK(!r.pop_packet(pk));
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(uint64_t(67'410'000'000ULL * 0.80)), r.slot));
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Sell);
  CHECK(find(r.events(), Ev::Order, "LIQUIDITY_DUMP") != nullptr);
  CHECK(p.state == PosState::SellSent);
}

TEST(engine_stall_sells) {
  Rig r;
  r.cfg.exits.dead_money_enabled = false;
  r.cfg.exits.liquidity_dump_pct = 0;
  r.cfg.exits.tp0_enabled = false;
  r.cfg.exits.tp1_pct = 1000;
  r.cfg.exits.trailing_arm_pct = 1000;
  r.cfg.exits.stall_enabled = true;
  r.cfg.exits.stall_arm_sec = 45;
  r.cfg.exits.stall_sec = 45;
  open_position(r);
  Packet pk;
  g_wall += 44'000;
  r.eng->poll(g_mono);
  CHECK(!r.pop_packet(pk));
  g_wall += 46'000;  // past arm + stall with no new high
  r.eng->poll(g_mono);
  CHECK(r.pop_packet(pk));
  CHECK(pk.side == Side::Sell);
  CHECK(find(r.events(), Ev::Order, "STALL") != nullptr);
  g_wall -= 90'000;
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
  struct Want {
    LeaderClass cls;
    uint64_t cu_price;
  };
  // estimate 1.2M; Agave beats the rivals' 3M; BAM and Firedancer bid the estimate (no rival price for them).
  for (Want w : {Want{LeaderClass::Agave, 3'000'000}, Want{LeaderClass::Bam, 1'200'000}, Want{LeaderClass::Firedancer, 1'200'000}}) {
    Rig r;
    auto table = uniform_table(w.cls, leader);
    r.sh->leaders.store(table.get());
    publish_routes(r, 1'200'000, 3'000'000);
    r.create();
    r.band(1'000'000'000'000ULL);
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
    Packet pk;
    CHECK(r.pop_packet(pk));
    CHECK(!pk.jito);  // jito.enabled is off; BAM never routes through the block engine
    auto es = r.events();
    const Event* rt = route_for(es, Side::Buy);
    CHECK(rt != nullptr);
    if (!rt) continue;
    CHECK_EQ(rt->i[0], int64_t(w.cls));
    CHECK_EQ(rt->i[1], int64_t(w.cu_price));
    CHECK_EQ(rt->i[2], int64_t(r.cfg.entry.cu_limit));  // no measured samples yet
    CHECK_EQ(rt->i[4], int64_t(1));                     // the vault guard
    CHECK_EQ(rt->i[7], int64_t(1));
    CHECK(rt->a == leader);
    CHECK(rt->sig == pk.sig);
    const Event* o = find(es, Ev::Order);
    CHECK(o && o->i[4] == int64_t(w.cu_price));
    r.sh->leaders.store(nullptr);
  }
}

TEST(engine_bam_exit_bids_high) {
  // BAM buys at the floor here, yet the take-profit sell still pays estimate x exit multiplier.
  Rig r;
  r.cfg.routing.bam.cu_price_mode = "floor";
  auto table = uniform_table(LeaderClass::Bam, parse_pubkey("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ"));
  r.sh->leaders.store(table.get());
  publish_routes(r, 1'200'000);
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, p.expected_base);
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(3 * 67'410'000'000ULL), r.slot));
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(sell.side == Side::Sell);
  CHECK(!sell.jito);
  auto es = r.events();
  const Event* buy = route_for(es, Side::Buy);
  const Event* rt = route_for(es, Side::Sell);
  CHECK(buy && buy->i[1] == int64_t(r.cfg.fees.cu_price_floor));
  CHECK(rt != nullptr);
  if (rt) {
    CHECK_EQ(rt->i[0], int64_t(LeaderClass::Bam));
    CHECK_EQ(rt->i[1], int64_t(1'200'000 * r.cfg.exits.exit_priority_multiplier));
  }
  r.sh->leaders.store(nullptr);
}

TEST(engine_routing_fallbacks) {
  // No leader table: Agave. No policy yet: the legacy single price, unrouted.
  Rig r;
  r.sh->cu_price.store(700'000);
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
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

TEST(engine_guard_revert_is_beaten) {
  Pubkey rival = parse_pubkey("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta");
  for (int which = 0; which < 2; ++which) {  // 0: vault guard, 1: rival guard
    Rig r;
    RivalSet rs;
    rs.wallets[0] = rival;
    rs.n = 1;
    r.sh->rivals.store(rs);
    r.create();
    r.band(1'000'000'000'000ULL);
    const CurveSlot* c = r.eng->curve(r.curve);
    CHECK(c && c->n_rivals == 1);
    if (c) CHECK(c->rival_atas[0] == ata(rival, r.mint, prog::TOKEN_2022));
    r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
    Packet pk;
    CHECK(r.pop_packet(pk));
    const Position& p = r.eng->position(0);
    CHECK_EQ(int(p.guards.count()), 2);
    CHECK_EQ(int(p.guard_ix0), 2);
    CHECK_EQ(p.guards.max_quote_vault, uint64_t(67'410'000'000ULL + 67'410'000'000ULL / 10'000 * 50));
    r.events();
    TxSpec t;
    t.filters = {"wallet"};
    t.sig = pk.sig;
    t.slot = ++r.slot;
    t.failed = true;
    t.err = custom_ix_error(uint8_t(2 + which), lighthouse::ERR_ASSERTION_FAILED);
    t.keys = {r.kp.pubkey()};
    t.pre = {1'000'000'000};
    t.post = {1'000'000'000 - 400'000};
    r.feed(tx_frame(t));
    CHECK(p.state == PosState::Empty);
    CHECK(!r.eng->killed());
    CHECK_EQ(r.eng->reserved_lamports(), uint64_t(0));
    auto es = r.events();
    const Event* f = find(es, Ev::Fill, which ? "guard_rival" : "guard_vault");
    CHECK(f != nullptr);
    if (f) CHECK(which ? f->c == rival : is_zero(f->c));
    CHECK(find(es, Ev::PositionClose, which ? "beaten_rival" : "beaten_vault") != nullptr);
  }
}

TEST(engine_sheds_rival_guards_that_dont_fit) {
  // No tips in the test config: the widest buy + vault guard has room for exactly one rival guard.
  Rig r;
  RivalSet rs;
  for (uint8_t i = 0; i < 3; ++i) rs.wallets[rs.n++] = Keypair::from_seed(std::vector<uint8_t>(32, uint8_t(90 + i)).data()).pubkey();
  r.cfg.lighthouse.max_rivals = 3;
  r.sh->rivals.store(rs);
  r.create();
  r.band(1'000'000'000'000ULL);
  const CurveSlot* c = r.eng->curve(r.curve);
  CHECK(c && c->n_rivals == 1);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  CHECK(verify_packet(pk, r.kp.pubkey()));
  CHECK_EQ(int(r.eng->position(0).guards.count()), 2);
}

TEST(engine_measures_buy_cu) {
  Rig r;
  r.cfg.routing.cu_samples_min = 1;
  publish_routes(r, 1'000'000);
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  TxSpec t;
  t.filters = {"wallet"};
  t.sig = pk.sig;
  t.slot = ++r.slot;
  t.keys = {r.kp.pubkey()};
  t.pre = {1'000'000'000};
  t.post = {840'000'000};
  t.post_tokens = {{r.mint_s, r.user_s, r.t22_s, p.expected_base}};
  t.cu_consumed = 100'000;
  r.feed(tx_frame(t));
  CHECK_EQ(r.eng->measured_cu_limit(), uint32_t(115'000));  // + cu_limit_margin_pct
}

TEST(engine_records_rival_txs) {
  Rig r;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
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
  int n = 0;
  for (auto& e : es) n += e.type == Ev::RivalTx;
  CHECK_EQ(n, 1);
  const Event* e = find(es, Ev::RivalTx);
  CHECK(e != nullptr);
  if (!e) return;
  CHECK(e->a == rival);
  CHECK(e->b == r.pool);
  CHECK(e->c == tip);
  CHECK_EQ(e->i[0], int64_t(7));
  CHECK_EQ(e->i[1], int64_t(4'000'000));
  CHECK_EQ(e->i[2], int64_t(150'000));
  CHECK_EQ(e->i[3], int64_t(2'000'000));
  CHECK_EQ(e->i[4], int64_t(1'000'000'000));
  CHECK_EQ(e->i[6], int64_t(0));
  CHECK_EQ(e->i[7], int64_t(90'000));
  CHECK_EQ(e->d[0], 3.0);  // an unlisted tip account: "other"
}

namespace {

/** Buy, fill, take-profit sell, and have that sell fail on chain: with max_exit_attempts = 1 the exits are spent. */
Packet exhaust_exits(Rig& r) {
  r.cfg.exits.max_exit_attempts = 1;
  r.create();
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  const Position& p = r.eng->position(0);
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, p.expected_base);
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(3 * 67'410'000'000ULL), r.slot));
  Packet sell;
  CHECK(r.pop_packet(sell));
  CHECK(sell.side == Side::Sell);
  r.wallet_fill(sell.sig, 840'000'000, 839'990'000, p.base_amount, p.base_amount, true);
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

  // No route: retried 5 s later, as a new attempt.
  AuxResp no;
  no.op = AuxOp::JupiterSell;
  no.pos = q.pos;
  no.mint = q.mint;
  CHECK(r.sh->aux_resps.push(no));
  r.eng->poll(g_mono);
  CHECK(!pop_jupiter_req(r, q));
  g_mono += 5'000'000'001ULL;
  r.eng->poll(g_mono);
  CHECK(pop_jupiter_req(r, q));
  CHECK_EQ(int(p.jupiter_attempts), 2);

  // Sent: the stream's fill of that signature closes the position.
  AuxResp ok = no;
  ok.ok = true;
  ok.sig = make_sig(77);
  ok.out_lamports = 250'000'000;
  CHECK(r.sh->aux_resps.push(ok));
  r.eng->poll(g_mono);
  CHECK(find(r.events(), Ev::Order, "jupiter TAKE_PROFIT_1") != nullptr);
  r.wallet_fill(ok.sig, 839'990'000, 1'089'000'000, p.base_amount, 0);
  CHECK(p.state == PosState::Empty);
  auto es = r.events();
  CHECK(find(es, Ev::PositionClose, "jupiter_exit") != nullptr);
}

TEST(engine_jupiter_gives_up_after_max_attempts) {
  Rig r;
  r.cfg.jupiter.enabled = true;
  r.cfg.jupiter.api_key = "test";
  r.cfg.jupiter.max_attempts = 1;
  exhaust_exits(r);
  const Position& p = r.eng->position(0);
  AuxReq q;
  CHECK(pop_jupiter_req(r, q));
  // Sent, then failed on chain: no attempt left, so the operator sells it.
  AuxResp ok;
  ok.op = AuxOp::JupiterSell;
  ok.pos = q.pos;
  ok.mint = q.mint;
  ok.ok = true;
  ok.sig = make_sig(78);
  CHECK(r.sh->aux_resps.push(ok));
  r.eng->poll(g_mono);
  r.wallet_fill(ok.sig, 839'990'000, 839'980'000, p.base_amount, p.base_amount, true);
  r.eng->poll(g_mono);
  CHECK(p.state == PosState::Open);
  CHECK(!p.jupiter_pending);
  CHECK(!pop_jupiter_req(r, q));
}

TEST(engine_exhausted_exits_without_jupiter_hand_off) {
  Rig r;  // no JUPITER_API_KEY: jupiter.enabled stays as load_config leaves it, off
  r.cfg.jupiter.enabled = false;
  exhaust_exits(r);
  const Position& p = r.eng->position(0);
  CHECK(r.eng->killed());
  CHECK(p.state == PosState::Open);
  AuxReq q;
  CHECK(!pop_jupiter_req(r, q));
}

// Not an assertion of speed (CI boxes vary): prints the in-process trigger latency,
// migrate frame in -> signed packet queued, on the real clocks.
TEST(bench_trigger_latency) {
  std::vector<double> us;
  for (int i = 0; i < 40; ++i) {
    Rig r;
    r.eng->set_clocks(&pd::wall_ms, &pd::now_ns);
    BlockhashSnap bh;
    r.sh->blockhash.load(bh);
    bh.fetched_ns = pd::now_ns();
    r.sh->blockhash.store(bh);
    r.create();
    r.band(1'000'000'000'000ULL);
    r.pool_push();
    TxSpec t;
    t.filters = {"migrate"};
    t.sig = make_sig(uint8_t(9 + i));
    t.keys = {prog::MIGRATION_AUTHORITY};
    std::string pf = b58(prog::PUMP_FUN);
    t.logs = {"Program " + pf + " invoke [1]", "Program log: Instruction: MigrateV2", "Program " + pf + " success"};
    t.pre_tokens = {{r.mint_s, r.curve_s, r.t22_s, 206'900'000'000'000ULL}};
    t.post_tokens = {{r.mint_s, r.pool_s, r.t22_s, 206'900'000'000'000ULL}, {r.wsol_s, r.pool_s, r.token_s, 67'410'000'000ULL}};
    std::string frame = tx_frame(t);
    uint64_t t0 = pd::now_ns();
    r.eng->on_frame(reinterpret_cast<const uint8_t*>(frame.data()), frame.size(), t0);
    Packet pk;
    if (!r.pop_packet(pk)) { CHECK(false); return; }
    us.push_back(double(pk.t_signed_ns - t0) / 1000.0);
  }
  std::sort(us.begin(), us.end());
  std::printf("  trigger latency (frame -> signed packet queued): p50 %.1f us, p90 %.1f us, max %.1f us\n",
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
  r.band(1'000'000'000'000ULL);
  r.migrate(206'900'000'000'000ULL, 67'410'000'000ULL);
  Packet pk;
  CHECK(r.pop_packet(pk));
  flush();
  auto pending = w.open_positions();  // in flight: restorable as pending (entry 0)
  CHECK_EQ(pending.size(), size_t(1));
  if (!pending.empty()) {
    CHECK(pending[0].mint == r.mint);
    CHECK_EQ(pending[0].entry_price, 0.0);
    CHECK(pending[0].token_program == prog::TOKEN_2022);
  }
  const Position& p = r.eng->position(0);
  uint64_t base = p.expected_base;
  r.wallet_fill(p.buy_sig, 1'000'000'000, 840'000'000, 0, base);
  flush();
  auto open = w.open_positions();
  CHECK_EQ(open.size(), size_t(1));
  if (!open.empty()) {
    CHECK_EQ(open[0].base_amount, base);
    CHECK(open[0].entry_price > 0);
    CHECK(open[0].creator == r.creator);
  }
  r.feed(account_frame("watch", r.quote_vault, prog::TOKEN, token_account(3 * 67'410'000'000ULL), r.slot));
  Packet sell;
  CHECK(r.pop_packet(sell));
  r.wallet_fill(sell.sig, 840'000'000, 1'100'000'000, base, 0);
  flush();
  CHECK(w.open_positions().empty());
  if (std::getenv("PD_KEEP_TEST_DB")) std::printf("  kept %s\n", r.cfg.storage.db_path.c_str());
  else std::filesystem::remove_all(dir);
}
