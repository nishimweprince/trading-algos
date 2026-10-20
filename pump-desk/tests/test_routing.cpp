// Leader classification, route policy, rivals file, Lighthouse bytes (lighthouse-sdk-legacy,
// tools/fixtures/lighthouse.mjs) and the tx fields rival profiling reads.
#include "aux/aux.hpp"
#include "aux/leaders.hpp"
#include "fixture.hpp"
#include "frames.hpp"
#include "logic/txparse.hpp"
#include "sol/constants.hpp"
#include "sol/lighthouse.hpp"
#include "sol/swap.hpp"
#include "stream/update.hpp"
#include "test.hpp"

using namespace pd;
using namespace pdtest;

namespace {

simdjson::dom::element& lighthouse_golden() {
  static simdjson::dom::parser parser;
  static simdjson::dom::element root = parser.load(std::string(PD_FIXTURE_DIR) + "/lighthouse.json");
  return root;
}

const char* SCHEDULE = R"({"jsonrpc":"2.0","id":1,"result":{
  "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE":[0,1,2,3],
  "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ":[4,5,6,7],
  "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta":[8,9,10,11]}})";
const char* NODES = R"({"jsonrpc":"2.0","id":1,"result":[
  {"pubkey":"4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE","version":"2.3.13"},
  {"pubkey":"D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ","version":"0.708.20306"},
  {"pubkey":"9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta","version":"2.3.13"}]})";

}  // namespace

TEST(lighthouse_golden_bytes) {
  int n = 0;
  for (auto c : lighthouse_golden()["cases"].get_array()) {
    ++n;
    IxList l;
    Pubkey target = key(c["target"]);
    auto op = lighthouse::IntOp(uint8_t(int64_t(c["op"])));
    uint64_t value = std::stoull(str(c["value"]));
    if (str(c["kind"]) == "token_amount") lighthouse::assert_token_amount(l, target, op, value);
    else lighthouse::assert_lamports(l, target, op, value);
    CHECK_EQ(int(l.n), 1);
    const Ix& x = l.ixs[0];
    CHECK_EQ(b58(x.program), str(c["program"]));
    CHECK_EQ(hex(x.data, x.data_len), str(c["data"]));
    auto keys = c["keys"].get_array();
    CHECK_EQ(size_t(x.n_accounts), keys.size());
    size_t a = 0;
    for (auto k : keys) {
      if (a >= x.n_accounts) break;
      const AccountMeta& m = x.accounts[a++];
      CHECK_EQ(b58(m.key), str(k["pubkey"]));
      CHECK_EQ(m.signer, bool(k["signer"]));
      CHECK_EQ(m.writable, bool(k["writable"]));
    }
  }
  CHECK(n >= 4);
  CHECK_EQ(b58(prog::LIGHTHOUSE), str(lighthouse_golden()["cases"].at(0)["program"]));
}

TEST(buy_guard_layout_and_size) {
  auto& g = golden();
  AmmConfig cfg;
  auto gc = b64bytes(g["accounts"]["globalConfig"]);
  auto fc = b64bytes(g["accounts"]["feeConfig"]);
  CHECK(decode_global_config(gc.data(), gc.size(), cfg));
  CHECK(decode_fee_config(fc.data(), fc.size(), cfg));
  auto pl = b64bytes(g["accounts"]["pool"]);
  Pool pool;
  CHECK(decode_pool(pl.data(), pl.size(), pool));
  auto seed = unhex(std::string_view(g["userSeedHex"]));
  Keypair kp = Keypair::from_seed(seed.data());
  UserKeys u;
  derive_user_keys(kp.pubkey(), u);
  SwapKeys k;
  // Cashback + Token-2022: the widest buy account list.
  derive_swap_keys(u, key(g["seedMint"]), prog::TOKEN_2022, pool.coin_creator, true, false, k);
  TxParams p;
  p.cu_limit = 250000;
  p.cu_price = 1234567;
  p.tips[0] = Tip{key(g["tip"]), 5000};
  p.tips[1] = Tip{parse_pubkey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"), 10000};
  p.n_tips = 2;
  BuyGuards guards;
  guards.vault = true;
  guards.max_quote_vault = 67'747'050'000ULL;
  for (size_t i = 0; i < MAX_RIVALS; ++i) {
    Keypair r = Keypair::from_seed(std::vector<uint8_t>(32, uint8_t(40 + i)).data());
    guards.rival_atas[guards.n_rivals++] = ata(r.pubkey(), k.mint, prog::TOKEN_2022);
  }
  IxList l;
  buy_ixs(l, u, k, cfg, p, 30'000'000, 40'000'000, &guards);
  CHECK(l.n <= MAX_IXS);
  CHECK_EQ(int(budget_ix_count(p)), 2);
  CHECK(l.ixs[2].program == prog::LIGHTHOUSE);
  CHECK(l.ixs[2].accounts[0].key == k.pool_quote_vault);
  CHECK(l.ixs[2].data[0] == lighthouse::ASSERT_TOKEN_ACCOUNT);
  for (size_t i = 0; i < MAX_RIVALS; ++i) {
    CHECK(l.ixs[3 + i].program == prog::LIGHTHOUSE);
    CHECK(l.ixs[3 + i].accounts[0].key == guards.rival_atas[i]);
    CHECK(l.ixs[3 + i].data[0] == lighthouse::ASSERT_ACCOUNT_INFO);
  }
  CHECK(l.ixs[3 + MAX_RIVALS].program == prog::ATA);  // the unguarded buy resumes here
  const int guarded_n = l.n;
  // Every guard costs a key and an instruction (~48 bytes): the widest buy has room for the vault
  // guard with one tip, and nothing more. The engine sheds rival guards that don't fit.
  SignedTx tx;
  CHECK(!build_signed_tx(kp, l, key(g["blockhash"]), tx));
  guards.n_rivals = 0;
  p.n_tips = 1;
  buy_ixs(l, u, k, cfg, p, 30'000'000, 40'000'000, &guards);
  CHECK(build_signed_tx(kp, l, key(g["blockhash"]), tx));
  std::printf("  widest buy + vault guard + 1 tip: %u of %zu bytes\n", unsigned(tx.len), PACKET_DATA_SIZE);
  p.n_tips = 2;

  // No guards: identical to the unguarded list (golden_swap_messages covers its bytes).
  IxList plain, none;
  buy_ixs(plain, u, k, cfg, p, 30'000'000, 40'000'000);
  BuyGuards off;
  buy_ixs(none, u, k, cfg, p, 30'000'000, 40'000'000, &off);
  CHECK_EQ(int(plain.n), int(none.n));
  CHECK_EQ(int(plain.n) + 1 + int(MAX_RIVALS), guarded_n);
}

TEST(leaders_classify_and_table) {
  CHECK(classify_version("0.708.20306") == LeaderClass::Firedancer);
  CHECK(classify_version("0.503.20214") == LeaderClass::Firedancer);
  CHECK(classify_version("2.3.13") == LeaderClass::Agave);
  CHECK(classify_version("") == LeaderClass::Agave);
  auto versions = parse_cluster_versions(NODES);
  CHECK_EQ(versions.size(), size_t(3));
  auto bam = parse_bam_list(R"(["9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta"])");
  CHECK_EQ(bam.size(), size_t(1));
  CHECK_EQ(parse_bam_list(R"({"validators":[{"identity":"a"},"b"]})").size(), size_t(2));
  // A BAM identity is Bam even on a 0.x client; an unknown identity is Agave.
  CHECK(classify_leader("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", versions, bam) == LeaderClass::Bam);
  CHECK(classify_leader("unknown", versions, bam) == LeaderClass::Agave);

  Schedule s = parse_leader_schedule(SCHEDULE);
  CHECK_EQ(s.size(), size_t(3));
  auto t = build_leader_table(1000, 12, {s, s}, versions, bam);
  CHECK(t->covers(1000));
  CHECK(t->covers(1023));
  CHECK(!t->covers(1024));
  CHECK(!t->covers(999));
  CHECK(t->class_at(1002) == LeaderClass::Agave);
  CHECK(t->class_at(1005) == LeaderClass::Firedancer);
  CHECK(t->class_at(1010) == LeaderClass::Bam);
  CHECK(t->class_at(1017) == LeaderClass::Firedancer);  // second epoch, slot index 5
  CHECK(t->class_at(5000) == LeaderClass::Agave);       // outside: the legacy class
  CHECK(t->leader_at(5000) == nullptr);
  const Pubkey* l = t->leader_at(1005);
  CHECK(l != nullptr);
  if (l) CHECK_EQ(b58(*l), std::string("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ"));
}

TEST(route_policy_modes) {
  Config c;
  c.fees.cu_price_floor = 100'000;
  c.fees.cu_price_cap = 5'000'000;
  c.sender.tip_lamports = 5000;
  c.routing.bam.sender_tip_lamports = 7000;
  c.routing.firedancer.cu_limit = 120'000;
  uint64_t beat[N_LEADER_CLASSES] = {3'000'000, 9'000'000, 9'000'000};
  RoutePolicy rp = make_route_policy(c, 1'000'000, beat);
  const RouteProfile& agave = rp.by_class[size_t(LeaderClass::Agave)];
  const RouteProfile& bam = rp.by_class[size_t(LeaderClass::Bam)];
  const RouteProfile& fd = rp.by_class[size_t(LeaderClass::Firedancer)];
  CHECK_EQ(agave.cu_price, uint64_t(3'000'000));  // beat_rivals: the rivals' price beats the estimate
  CHECK_EQ(bam.cu_price, uint64_t(5'000'000));    // beat_rivals too: bid high inside the BAM auction, capped
  CHECK_EQ(fd.cu_price, uint64_t(5'000'000));     // beat_rivals, capped
  CHECK_EQ(agave.cu_limit, uint32_t(0));          // measured
  CHECK_EQ(fd.cu_limit, uint32_t(120'000));
  CHECK_EQ(agave.sender_tip, uint64_t(5000));
  CHECK_EQ(bam.sender_tip, uint64_t(7000));
  c.routing.bam.cu_price_mode = "floor";  // arrival only
  CHECK_EQ(make_route_policy(c, 1'000'000, beat).by_class[size_t(LeaderClass::Bam)].cu_price, uint64_t(100'000));
  CHECK(!agave.jito && !bam.jito && !fd.jito);
  // Exits: estimate x exit multiplier on every class, whatever the buy mode.
  for (const RouteProfile* x : {&agave, &bam, &fd}) CHECK_EQ(x->exit_cu_price, uint64_t(1'000'000 * c.exits.exit_priority_multiplier));
  c.routing.bam.exit_cu_price_mode = "beat_rivals";
  CHECK_EQ(make_route_policy(c, 1'000'000, beat).by_class[size_t(LeaderClass::Bam)].exit_cu_price, uint64_t(5'000'000));
  // Without a rival price, beat_rivals is the estimate; the estimate falls back to the default.
  uint64_t none[N_LEADER_CLASSES] = {0, 0, 0};
  c.routing.agave.cu_price_multiplier = 2;
  RoutePolicy rp2 = make_route_policy(c, 0, none);
  CHECK_EQ(rp2.by_class[0].cu_price, uint64_t(double(c.fees.cu_price_default) * 2));
  c.routing.agave.jito = true;  // needs jito.enabled
  CHECK(!make_route_policy(c, 0, none).by_class[0].jito);
  c.jito.enabled = true;
  CHECK(make_route_policy(c, 0, none).by_class[0].jito);
}

TEST(rivals_file_parse) {
  Pubkey self = parse_pubkey("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE");
  RivalsFile r = parse_rivals(R"({"guard":["4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
      "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ","not-a-key","9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
      "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn"],
      "beat_cu_price":{"agave":2500001,"firedancer":800001}})",
                              self, 2);
  CHECK_EQ(int(r.set.n), 2);  // self and the bad key skipped, then capped at max_rivals
  CHECK_EQ(b58(r.set.wallets[0]), std::string("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ"));
  CHECK_EQ(b58(r.set.wallets[1]), std::string("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta"));
  CHECK_EQ(r.beat_cu_price[0], uint64_t(2'500'001));
  CHECK_EQ(r.beat_cu_price[1], uint64_t(0));
  CHECK_EQ(r.beat_cu_price[2], uint64_t(800'001));
  CHECK_EQ(int(parse_rivals("{}", self, 3).set.n), 0);
}

TEST(tx_budget_tip_and_error_fields) {
  Pubkey payer = parse_pubkey("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ");
  Pubkey wsol_ata = parse_pubkey("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta");
  Pubkey tip = parse_pubkey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5");
  TxSpec t;
  t.filters = {"pools"};
  t.sig = make_sig(77);
  t.keys = {payer, wsol_ata, tip, prog::COMPUTE_BUDGET, prog::SYSTEM, prog::TOKEN};
  auto cat = [](std::vector<uint8_t> a, const std::vector<uint8_t>& b) { a.insert(a.end(), b.begin(), b.end()); return a; };
  t.ixs = {
      {3, {}, cat({2}, le_bytes(180'000, 4))},
      {3, {}, cat({3}, le_bytes(2'500'000, 8))},
      {4, {0, 1}, cat(le_bytes(2, 4), le_bytes(900'000'000, 8))},  // WSOL wrap: the token program touches it next
      {5, {1}, {17}},                                               // sync_native
      {4, {0, 2}, cat(le_bytes(2, 4), le_bytes(1'000'000, 8))},     // the tip
  };
  t.failed = true;
  t.err = custom_ix_error(3, 6001);
  t.cu_consumed = 41'234;
  std::string f = tx_frame(t);
  UpdateView u;
  CHECK(parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size(), u));
  CHECK_EQ(int(u.tx.n_ixs), 5);
  CHECK_EQ(u.tx.cu_consumed, uint64_t(41'234));
  BudgetInfo b = compute_budget_of(u.tx);
  CHECK_EQ(b.cu_limit, uint32_t(180'000));
  CHECK_EQ(b.cu_price, uint64_t(2'500'000));
  TipInfo ti = tip_of(u.tx);
  CHECK_EQ(ti.dst, 2);
  CHECK_EQ(ti.lamports, uint64_t(1'000'000));
  uint8_t ix = 0;
  uint32_t code = 0;
  CHECK(custom_instruction_error(u.tx, ix, code));
  CHECK_EQ(int(ix), 3);
  CHECK_EQ(code, lighthouse::ERR_ASSERTION_FAILED);
  t.err = "x";
  f = tx_frame(t);
  CHECK(parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size(), u));
  CHECK(!custom_instruction_error(u.tx, ix, code));
}
