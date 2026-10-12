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
  CHECK(r.sh->watch_cmds.size() == 3);

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
  g_wall += 11 * 60'000;     // past the 10-minute time stop
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

TEST(exit_fsm_matches_engine_ts) {
  Config c;
  Config::Exits x = c.exits;
  ExitState s;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 0.5, 1000, x).trigger == ExitTrigger::None);  // hard stop 100 % = off
  CHECK(evaluate_exit(s, 1.5, 1000, x).trigger == ExitTrigger::TakeProfit1);
  CHECK(evaluate_exit(s, 1.2, 10 * 60'000, x).trigger == ExitTrigger::TimeStop);
  x.hard_stop_pct = 20;
  exit_init(s, 1.0, 0, x);
  CHECK(evaluate_exit(s, 0.8, 1000, x).trigger == ExitTrigger::StopLoss);
  x.trailing_arm_pct = 20;
  x.tp1_pct = 1000;
  exit_init(s, 1.0, 0, x);
  exit_observe(s, 1.3, x);
  CHECK(s.trailing_armed);
  CHECK(evaluate_exit(s, 1.3 * 0.91, 1000, x).trigger == ExitTrigger::TrailingStop);
  x.tp0_enabled = true;
  x.tp0_pct = 22;
  exit_init(s, 1.0, 0, x);
  ExitDecision d = evaluate_exit(s, 1.25, 1000, x);
  CHECK(d.trigger == ExitTrigger::TakeProfit0);
  CHECK_EQ(d.sell_fraction, 0.5);
  exit_on_fill(s, ExitTrigger::TakeProfit0, x);
  CHECK(s.tp0_done);
  CHECK_EQ(s.stop_price, 1.08);
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
