// Phase 3 (offline): executor instruction layout, the §7.3 route, sign guard, limits and kill switch,
// denylist, lookup-table activation, blockhash age, Jito request/response handling, the dry-run gate,
// the bundle tracker, and a dry-run replay that signs bundles without any network send.
#include <sodium.h>
#include <unistd.h>

#include <chrono>
#include <cstring>

#include "app/bundle_builder.hpp"
#include "feed/frames.hpp"
#include "core/programs.hpp"
#include "fixture.hpp"
#include "risk/limits.hpp"
#include "risk/sign_guard.hpp"
#include "submit/conn.hpp"
#include "submit/jito.hpp"
#include "test.hpp"
#include "tx/alt_manager.hpp"
#include "tx/builders.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;
using namespace arbtest;

namespace {

std::vector<uint8_t> unhex(std::string_view h) {
  std::vector<uint8_t> v;
  for (size_t i = 0; i + 1 < h.size(); i += 2) v.push_back(uint8_t(std::stoi(std::string(h.substr(i, 2)), nullptr, 16)));
  return v;
}

/** A Runtime over the replay fixture, loaded to the end of the capture. */
struct Loaded {
  app::PoolsFile pf;
  Config cfg;
  std::unique_ptr<app::Runtime> rt;
  Loaded() {
    std::string err;
    app::from_json(read_file(fixture_path("replay_pools.json")), pf, &err);
    rt = std::make_unique<app::Runtime>(cfg, pf);
  }
};

/** A quotable 2-hop wSOL cycle (Raydium pool and its synthetic twin) after the initial load. */
const engine::Cycle* raydium_pair(app::Runtime& rt) {
  for (auto& c : rt.cycles().all()) {
    if (c.n != 2 || c.base != mint::WSOL) continue;
    auto a = rt.store().pool(c.legs[0].pool), b = rt.store().pool(c.legs[1].pool);
    if (a->kind == DexKind::RaydiumV4 && b->kind == DexKind::RaydiumV4 && a->quotable(2) && b->quotable(2)) return &c;
  }
  return nullptr;
}

class MockHttp final : public submit::HttpPost {
 public:
  explicit MockHttp(int* calls, int status = 200, std::string body = "{\"jsonrpc\":\"2.0\",\"result\":\"bid123\",\"id\":1}")
      : calls_(calls), status_(status), body_(std::move(body)) {}
  int post(std::string_view path, std::string_view body, std::string& out) override {
    ++*calls_;
    last_path = std::string(path);
    last_body = std::string(body);
    out = body_;
    return status_;
  }
  std::string last_path, last_body;

 private:
  int* calls_;
  int status_;
  std::string body_;
};

}  // namespace

TEST(execute_route_layout_matches_shared_fixture) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(read_file(fixture_path("execute_route.json")));
  size_t n = 0;
  for (auto c : simdjson::dom::array(p.parse(ps)["cases"])) {
    tx::ExecuteRouteArgs a;
    a.amount_in = uint64_t(c["amount_in"]);
    a.min_out = uint64_t(c["min_out"]);
    a.num_legs = uint8_t(uint64_t(c["num_legs"]));
    size_t i = 0;
    for (auto l : simdjson::dom::array(c["legs"])) {
      simdjson::dom::array t = l;
      uint8_t v[4];
      size_t j = 0;
      for (auto x : t) v[j++] = uint8_t(uint64_t(x));
      a.legs[i++] = tx::LegSpec{v[0], v[1], v[2], v[3]};
    }
    uint8_t out[tx::EXECUTE_ROUTE_DATA_LEN];
    size_t len = tx::encode_execute_route(a, out);
    auto want = unhex(std::string_view(c["hex"]));
    CHECK(len == want.size() && std::memcmp(out, want.data(), len) == 0);
    ++n;
  }
  CHECK_EQ(n, size_t(3));
}

TEST(route_tx_layout_accounts_and_borrow_index) {
  Loaded L;
  CHECK(L.rt->replay_file(fixture_path("frames.bin")));
  const engine::Cycle* c = raydium_pair(*L.rt);
  CHECK(c != nullptr);
  if (!c) return;
  std::shared_ptr<const PoolState> legs[2] = {L.rt->store().pool(c->legs[0].pool), L.rt->store().pool(c->legs[1].pool)};
  Keypair kp = app::dry_run_keypair();
  Pubkey exec = app::dry_run_executor();
  const flash::FlashReserve* r = L.rt->engine().flash_reserve(mint::WSOL);
  CHECK(r != nullptr && r->enabled);
  tx::RouteTxParams p;
  p.wallet = kp.pubkey();
  p.executor = exec;
  p.tip_account = risk::default_tip_accounts()[0];
  p.amount_in = 1'000'000'000;
  p.min_out = 1'000'100'000;
  p.tip_lamports = 50'000;
  p.cu_limit = 200'000;
  p.reserve = r;
  tx::IxList l;
  auto lay = tx::build_route_ixs(l, p, *c, legs);
  CHECK(lay.has_value());
  CHECK_EQ(l.n, uint8_t(5));  // limit, borrow, execute, repay, tip
  CHECK(lay->borrow_index == 1 && lay->execute_index == 2 && lay->repay_index == 3 && lay->tip_index == 4);
  const tx::Ix& repay = l.ixs[3];
  CHECK_EQ(repay.data[repay.data_len - 1], uint8_t(1));  // borrow_instruction_index = absolute position
  // With a CU price the borrow moves to index 2 and the repay follows it.
  p.cu_price_micro_lamports = 1000;
  lay = tx::build_route_ixs(l, p, *c, legs);
  CHECK(lay->borrow_index == 2 && l.ixs[4].data[l.ixs[4].data_len - 1] == 2);
  // Kamino rule: borrow and repay carry the identical account list, and amounts match.
  const tx::Ix& b = l.ixs[2];
  const tx::Ix& rp = l.ixs[4];
  CHECK(b.n_accounts == rp.n_accounts);
  for (uint8_t i = 0; i < b.n_accounts; ++i) CHECK(b.accounts[i].key == rp.accounts[i].key);
  CHECK(std::memcmp(b.data + 8, rp.data + 8, 8) == 0);
  // Executor accounts: signer, base ATA, one intermediate ATA, then per leg [program, swap accounts].
  const tx::Ix& ex = l.ixs[3];
  CHECK(ex.program == exec);
  CHECK(ex.accounts[0].key == kp.pubkey() && ex.accounts[0].signer);
  CHECK(ex.accounts[1].key == ata(kp.pubkey(), mint::WSOL, prog::TOKEN));
  CHECK(ex.accounts[2].key == ata(kp.pubkey(), c->legs[0].mint_out, prog::TOKEN));
  tx::ExecuteRouteArgs args;
  std::memcpy(&args.amount_in, ex.data + 1, 8);
  CHECK_EQ(args.amount_in, p.amount_in);
  uint8_t n_legs = ex.data[17];
  CHECK_EQ(n_legs, uint8_t(2));
  size_t fixed = 3;
  for (int i = 0; i < 2; ++i) {
    uint8_t dex = ex.data[18 + 4 * i], dir = ex.data[19 + 4 * i], start = ex.data[20 + 4 * i], len = ex.data[21 + 4 * i];
    CHECK(dex == uint8_t(DexKind::RaydiumV4) && dir == uint8_t(c->legs[i].a_to_b));
    CHECK(ex.accounts[fixed + start].key == prog::RAYDIUM_V4);
    tx::dexix::UserAccounts ua{kp.pubkey(), tx::wallet_ata(kp.pubkey(), legs[i]->mint_a, *legs[i]),
                               tx::wallet_ata(kp.pubkey(), legs[i]->mint_b, *legs[i])};
    auto metas = tx::dexix::swap_accounts(*legs[i], c->legs[i].a_to_b, ua);
    CHECK_EQ(size_t(len), metas.size() + 1);
    for (size_t k = 0; k < metas.size(); ++k) CHECK(ex.accounts[fixed + start + 1 + k].key == metas[k].key);
  }
  CHECK(fixed + ex.data[20 + 4] + ex.data[21 + 4] == ex.n_accounts);
  // Own-capital mode: no borrow or repay.
  p.reserve = nullptr;
  p.cu_price_micro_lamports = 0;
  lay = tx::build_route_ixs(l, p, *c, legs);
  CHECK(lay.has_value() && l.n == 3 && lay->borrow_index == 0xff);
  // A disabled reserve refuses to build.
  flash::FlashReserve off = *r;
  off.enabled = false;
  p.reserve = &off;
  CHECK(!tx::build_route_ixs(l, p, *c, legs).has_value());
}

TEST(sign_guard_allowlist_and_tip_rules) {
  Keypair kp = app::dry_run_keypair();
  risk::SignGuard g;
  g.wallet = kp.pubkey();
  g.executor = app::dry_run_executor();
  g.tip_accounts = risk::default_tip_accounts();
  g.tip_max_lamports = 1'000'000;
  auto base = [&] {
    tx::IxList l;
    tx::set_compute_unit_limit(l, 100'000);
    l.add(g.executor).acc(kp.pubkey(), true, false).u8(0);
    tx::system_transfer(l, kp.pubkey(), g.tip_accounts[3], 5000);
    return l;
  };
  CHECK(g.check(base()).has_value());
  tx::IxList two = base();
  tx::system_transfer(two, kp.pubkey(), g.tip_accounts[1], 5000);
  CHECK(!g.check(two).has_value());  // exactly one transfer
  tx::IxList stranger = base();
  stranger.ixs[2].accounts[1].key = mint::USDC;
  CHECK(!g.check(stranger).has_value());  // not a tip account
  tx::IxList big = base();
  uint64_t lots = 2'000'000;
  std::memcpy(big.ixs[2].data + 4, &lots, 8);
  CHECK(!g.check(big).has_value());  // above tip_max
  tx::IxList dex = base();
  dex.add(prog::RAYDIUM_V4).u8(16);
  CHECK(!g.check(dex).has_value());  // DEX programs are only reached by CPI
  tx::IxList other_signer = base();
  other_signer.ixs[1].accounts[0].key = mint::USDC;
  CHECK(!g.check(other_signer).has_value());
  tx::IxList none;
  tx::set_compute_unit_limit(none, 1);
  CHECK(!g.check(none).has_value());  // no tip transfer
  risk::SignGuard unset = g;
  unset.executor = Pubkey{};
  CHECK(!unset.check(base()).has_value());  // an unset executor id never passes
}

TEST(limits_kill_switch_and_denylist) {
  risk::LimitsConfig lc;
  lc.max_consecutive_rejects = 3;
  lc.reject_pause_s = 300;
  lc.quote_mismatch_min_samples = 10;
  risk::Limits l(lc);
  const int64_t day = 1'793'491'200'000LL;
  uint64_t float_ok = 1'000'000'000;
  CHECK(l.check(day, float_ok, 10, 0) == risk::Block::none);
  l.set_kill(true);
  CHECK(l.check(day, float_ok, 10, 0) == risk::Block::kill_switch);
  l.set_kill(false);
  CHECK(l.check(day, 100'000'000, 10, 0) == risk::Block::sol_float_low);
  CHECK(l.check(day, float_ok, 900, 0) == risk::Block::feed_lag);
  l.record_fee(day, 150'000'000);
  l.record_fee(day + 1000, 60'000'000);
  CHECK(l.check(day + 2000, float_ok, 10, 0) == risk::Block::daily_fee_cap);
  CHECK(l.check(day + 86'400'000LL, float_ok, 10, 0) == risk::Block::none);  // a new UTC day resets the cap
  const int64_t day2 = day + 86'400'000LL;  // past the fee cap tripped above
  for (int i = 0; i < 3; ++i) l.record_submit(day2, true);
  CHECK(l.check(day2 + 1000, float_ok, 10, 0) == risk::Block::reject_pause);
  CHECK(l.check(day2 + 301'000, float_ok, 10, 0) == risk::Block::none);
  for (int i = 0; i < 9; ++i) l.record_sim(1, i == 0);
  CHECK(!l.dex_disabled(1));  // under min samples
  l.record_sim(1, false);
  CHECK(l.dex_disabled(1));  // 1/10 = 10% > 2%
  CHECK(l.check(day2 + 301'000, float_ok, 10, uint8_t(1u << 1)) == risk::Block::dex_disabled);
  CHECK(l.check(day2 + 301'000, float_ok, 10, uint8_t(1u << 0)) == risk::Block::none);
  CHECK_EQ(l.sweep_excess(3'000'000'000ULL), uint64_t(1'000'000'000ULL));
  // The KILL file is honoured by the bundle builder (checked per send); see dry_run_replay_* below.
  risk::Denylist d(86400);
  CHECK(d.load(read_file(std::string(ARB_SOURCE_DIR) + "/config/denylist.json")));
  CHECK(d.load("{\"mints\":[\"" + b58(mint::USDT) + "\"],\"pools\":[]}"));
  CHECK(d.contains(mint::USDT, day));
  d.add(mint::USDC, day);
  CHECK(d.contains(mint::USDC, day + 1000) && !d.contains(mint::USDC, day + 86'400'001LL));
  CHECK(!d.load("{\"mints\":[\"not-a-key\"]}"));
}

TEST(alt_activation_and_extend_plan) {
  Keypair kp = app::dry_run_keypair();
  tx::AltManager m(2);
  tx::AltAccount a;
  a.has_authority = true;
  a.authority = kp.pubkey();
  for (uint8_t i = 1; i <= 10; ++i) a.addresses.push_back(Pubkey{i});
  a.last_extended_slot = 100;
  a.last_extended_start = 6;  // entries 6..9 were added at slot 100
  m.upsert(Pubkey{200}, a);
  Pubkey early[2] = {Pubkey{1}, Pubkey{6}}, late[1] = {Pubkey{9}};
  CHECK(m.ready(early, 101) && !m.ready(late, 101) && !m.ready(late, 102));
  CHECK(m.ready(late, 103));  // past last_extended_slot + 2
  CHECK_EQ(m.active(101)[0].addresses.size(), size_t(6));
  CHECK_EQ(m.active(103)[0].addresses.size(), size_t(10));
  std::vector<Pubkey> want;
  for (uint8_t i = 5; i < 60; ++i) want.push_back(Pubkey{i});
  auto miss = m.missing(want);
  CHECK_EQ(miss.size(), size_t(49));  // 5..59, of which 5..10 are present
  std::vector<Pubkey> created;
  auto plan = m.plan_extend(want, kp.pubkey(), kp.pubkey(), 5000, &created);
  CHECK_EQ(plan.size(), size_t(3));  // 20 + 20 + 9 into the existing table (room 246)
  CHECK(created.empty());
  size_t added = 0;
  for (auto& l : plan) {
    CHECK(l.n == 1 && l.ixs[0].program == prog::ALT);
    uint64_t n;
    std::memcpy(&n, l.ixs[0].data + 4, 8);
    added += n;
  }
  CHECK_EQ(added, size_t(49));
  tx::AltManager empty;
  plan = empty.plan_extend(want, kp.pubkey(), kp.pubkey(), 5000, &created);
  CHECK(created.size() == 1 && plan[0].n == 2);  // create + first extend in one transaction
  CHECK(created[0] == tx::lookup_table_address(kp.pubkey(), 5000));
  tx::BlockhashCache bc;
  CHECK(!bc.get(10).has_value());
  bc.set(Hash{7}, 1000, 0);
  CHECK(bc.get(1060).has_value() && !bc.get(1061).has_value());
  CHECK(!bc.needs_refresh(1999) && bc.needs_refresh(2000));
}

TEST(jito_bodies_parsers_and_429_backoff) {
  std::string tx[2] = {"AAA", "BBB"};
  CHECK_EQ(submit::send_bundle_body(tx),
           std::string("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"sendBundle\",\"params\":[[\"AAA\",\"BBB\"],{\"encoding\":\"base64\"}]}"));
  std::string ids[6] = {"a", "b", "c", "d", "e", "f"};
  CHECK_EQ(submit::statuses_body("getInflightBundleStatuses", ids),
           std::string("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getInflightBundleStatuses\",\"params\":[[\"a\",\"b\",\"c\",\"d\",\"e\"]]}"));
  CHECK(*submit::parse_send_bundle("{\"jsonrpc\":\"2.0\",\"result\":\"2id3\",\"id\":1}") == "2id3");
  std::string msg;
  CHECK(!submit::parse_send_bundle("{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32602,\"message\":\"bundle contains an already processed transaction\"},\"id\":1}", &msg));
  CHECK(msg.find("already processed") != std::string::npos);
  auto st = submit::parse_inflight(
      "{\"jsonrpc\":\"2.0\",\"result\":{\"context\":{\"slot\":280999028},\"value\":[{\"bundle_id\":\"x1\",\"status\":\"Landed\",\"landed_slot\":280999027},"
      "{\"bundle_id\":\"x2\",\"status\":\"Pending\",\"landed_slot\":null},{\"bundle_id\":\"x3\",\"status\":\"Invalid\",\"landed_slot\":null}]},\"id\":1}");
  CHECK(st.size() == 3 && st[0].status == submit::Inflight::Landed && st[0].landed_slot == 280999027);
  CHECK(st[1].status == submit::Inflight::Pending && st[2].status == submit::Inflight::Invalid);
  auto tips = submit::parse_tip_accounts("{\"jsonrpc\":\"2.0\",\"result\":[\"96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5\",\"bad\"],\"id\":1}");
  CHECK(tips.size() == 1 && tips[0] == risk::default_tip_accounts()[0]);
  CHECK_EQ(submit::parse_tip_floor_p25("[{\"time\":\"2024-09-01T12:58:00Z\",\"landed_tips_25th_percentile\":6.001000000000001e-06,"
                                       "\"landed_tips_50th_percentile\":1e-05}]"),
           uint64_t(6001));  // 6.001e-06 SOL
  // Fan-out to two regions; a 429 backs that region off for 1 s.
  int calls_fra = 0, calls_ams = 0;
  submit::JitoClient c({"frankfurt", "amsterdam"}, "https://%s.mainnet.block-engine.jito.wtf", [&](const std::string& url) {
    if (url.find("frankfurt") != std::string::npos) return std::unique_ptr<submit::HttpPost>(new MockHttp(&calls_fra));
    return std::unique_ptr<submit::HttpPost>(new MockHttp(&calls_ams, 429, "{}"));
  });
  auto rr = c.send_bundle(tx, 10'000);
  CHECK(rr.size() == 2 && rr[0].bundle_id == "bid123" && rr[1].http_status == 429);
  rr = c.send_bundle(tx, 10'500);
  CHECK(rr[1].backed_off && calls_ams == 1 && calls_fra == 2);
  rr = c.send_bundle(tx, 11'001);
  CHECK(!rr[1].backed_off && calls_ams == 2);
}

TEST(dry_run_gate_never_reaches_send_bundle) {
  int calls = 0;
  auto make = [&] {
    return std::make_unique<submit::JitoClient>(std::vector<std::string>{"frankfurt"}, "https://%s.x",
                                                [&](const std::string&) { return std::unique_ptr<submit::HttpPost>(new MockHttp(&calls)); });
  };
  tx::SignedTx t;
  t.len = 100;
  submit::Submitter dry(true, make());
  CHECK(dry.dry_run());
  for (int i = 0; i < 10; ++i) CHECK(dry.submit(t, 5, 5, 0) == submit::SubmitOutcome::DryRun);
  CHECK_EQ(calls, 0);
  submit::Submitter live(false, make());
  CHECK(!live.dry_run());
  CHECK(live.submit(t, 5, 6, 0) == submit::SubmitOutcome::Stale);  // the slot passed: never a retry
  CHECK_EQ(calls, 0);
  CHECK(live.submit(t, 5, 5, 0) == submit::SubmitOutcome::Sent);
  CHECK_EQ(calls, 1);
}

TEST(bundle_tracker_outcomes) {
  std::vector<std::pair<std::string, submit::BundleTracker::Result>> done;
  submit::BundleTracker tr([&](const std::string& id, uint16_t, submit::BundleTracker::Result r, uint64_t) { done.emplace_back(id, r); });
  Signature s1{}, s2{};
  s1[0] = 1;
  s2[0] = 2;
  for (int i = 0; i < 7; ++i) tr.track("b" + std::to_string(i), i == 1 ? s1 : i == 2 ? s2 : Signature{}, 0x0201, 0);
  auto due = tr.due(600);
  CHECK_EQ(due.size(), size_t(5));
  CHECK(tr.due(700).size() == 2 && tr.due(800).empty());
  tr.on_statuses({{"b0", submit::Inflight::Landed, 99}, {"b3", submit::Inflight::Failed, 0}, {"b4", submit::Inflight::Pending, 0}}, 900);
  tr.on_signature_seen(s1, 100);
  CHECK_EQ(done.size(), size_t(3));
  CHECK(done[0].second == submit::BundleTracker::Result::Landed && done[1].second == submit::BundleTracker::Result::Rejected);
  CHECK(done[2].first == "b1" && done[2].second == submit::BundleTracker::Result::Landed);
  tr.expire(30'000);
  CHECK_EQ(tr.pending(), size_t(0));
  CHECK_EQ(done.size(), size_t(7));
  CHECK(done.back().second == submit::BundleTracker::Result::NotLanded);
}

TEST(dry_run_replay_signs_bundles_with_zero_sends) {
  Loaded L;
  CHECK(L.cfg.mode.dry_run);
  Keypair kp = app::dry_run_keypair();
  tx::AltManager alts;
  tx::BlockhashCache bh;
  risk::Limits limits(risk::limits_config(L.cfg));
  int calls = 0;
  submit::Submitter sub(true, std::make_unique<submit::JitoClient>(
                                  std::vector<std::string>{"frankfurt"}, "https://%s.x",
                                  [&](const std::string&) { return std::unique_ptr<submit::HttpPost>(new MockHttp(&calls)); }));
  app::BundleBuilder bb(L.cfg, *L.rt, kp, app::dry_run_executor(), alts, bh, limits, sub);
  bb.set_offline(true);
  std::vector<obs::DecisionRecord> recs;
  std::vector<std::vector<uint8_t>> txs;
  L.rt->set_send_hook([&](const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& r) {
    bb(o, c, r);
    recs.push_back(r);
    txs.emplace_back(bb.last_tx().bytes, bb.last_tx().bytes + bb.last_tx().len);
  });
  CHECK(L.rt->replay_file(fixture_path("frames.bin")));
  const auto& s = bb.stats();
  std::printf("    dry run: built=%llu signed=%llu dry_run=%llu too_large=%llu guard=%llu failed=%llu\n", (unsigned long long)s.built,
              (unsigned long long)s.signed_ok, (unsigned long long)s.dry_run, (unsigned long long)s.too_large,
              (unsigned long long)s.guard_blocked, (unsigned long long)s.build_failed);
  CHECK(s.built >= 40 && s.dry_run == s.built && s.sent == 0);
  CHECK_EQ(calls, 0);
  for (size_t i = 0; i < recs.size(); ++i) {
    CHECK(recs[i].action == uint8_t(obs::Action::DryRun));
    const std::vector<uint8_t>& t = txs[i];
    CHECK(t.size() <= tx::PACKET_DATA_SIZE && t[0] == 1);
    // The signature in the record is a valid ed25519 signature by the wallet over the message.
    CHECK(std::memcmp(recs[i].signature, t.data() + 1, 64) == 0);
    CHECK(crypto_sign_verify_detached(t.data() + 1, t.data() + 65, t.size() - 65, kp.pubkey().data()) == 0);
  }
  // KILL file: every later send is blocked (feeds keep running).
  Loaded K;
  char dir[] = "/tmp/arb_kill_XXXXXX";
  CHECK(mkdtemp(dir) != nullptr);
  K.cfg.mode.kill_file = std::string(dir) + "/KILL";
  std::fclose(std::fopen(K.cfg.mode.kill_file.c_str(), "w"));
  K.rt = std::make_unique<app::Runtime>(K.cfg, K.pf);
  tx::AltManager alts2;
  app::BundleBuilder bk(K.cfg, *K.rt, kp, app::dry_run_executor(), alts2, bh, limits, sub);
  bk.set_offline(true);
  K.rt->set_send_hook([&](const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& r) { bk(o, c, r); });
  CHECK(K.rt->replay_file(fixture_path("frames.bin")));
  CHECK(bk.stats().built > 0 && bk.stats().risk_blocked == bk.stats().built && bk.stats().signed_ok == 0);
  std::remove(K.cfg.mode.kill_file.c_str());
  rmdir(dir);
}

TEST(http_parser_incremental_chunked_and_pipelined) {
  std::string chunked = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n7\r\n, world\r\n0\r\n\r\n";
  std::string plain = "HTTP/1.1 429 Too Many\r\nContent-Length: 2\r\n\r\n{}";
  std::string both = chunked + plain;
  for (size_t step : {size_t(1), size_t(3), size_t(7), both.size()}) {
    HttpResponseParser p;
    bool done = false;
    size_t i = 0;
    for (; i < both.size() && !done; i += step) done = p.feed(both.data() + i, std::min(step, both.size() - i));
    CHECK(done && p.status() == 200 && p.body() == "hello, world");
    // The rest (pipelined) arrives later or is already buffered.
    bool second = p.next();
    for (; i < both.size() && !second; i += step) second = p.feed(both.data() + i, std::min(step, both.size() - i));
    CHECK(second && p.status() == 429 && p.body() == "{}");
  }
  // A large chunked body fed in 16 KB reads must parse in linear time (it was quadratic).
  std::string chunk(1 << 16, 'x');
  char hdr[16];
  std::snprintf(hdr, sizeof hdr, "%zx\r\n", chunk.size());
  std::string big = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
  for (int i = 0; i < 1024; ++i) big += hdr + chunk + "\r\n";  // 64 MB
  big += "0\r\n\r\n";
  auto t0 = std::chrono::steady_clock::now();
  HttpResponseParser p;
  bool done = false;
  for (size_t i = 0; i < big.size() && !done; i += 16384) done = p.feed(big.data() + i, std::min<size_t>(16384, big.size() - i));
  double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  CHECK(done && p.body().size() == size_t(1024) << 16);
  std::printf("    64 MB chunked body parsed in %.3f s\n", secs);
  CHECK(secs < 2.0);
}

TEST(cp_executor_route_ranges_directions_and_token_programs) {
  for (bool forward : {false, true}) for (bool pump_first : {false, true}) {
    auto cp = std::make_shared<PoolState>(); auto pump = std::make_shared<PoolState>();
    cp->kind = DexKind::Cpmm; pump->kind = DexKind::PumpSwap;
    cp->mint_a = pump->mint_a = mint::WSOL; cp->mint_b = pump->mint_b = mint::USDC;
    cp->cpmm.token_0_program = prog::TOKEN; cp->cpmm.token_1_program = prog::TOKEN_2022;
    auto mint_info = std::make_shared<state::Mint>(); mint_info->token_program = prog::TOKEN_2022;
    cp->mint_b_info = pump->mint_b_info = mint_info;
    engine::Cycle c; c.n = 2; c.base = forward ? mint::WSOL : mint::USDC;
    c.legs[0].a_to_b = forward; c.legs[1].a_to_b = !forward;
    c.legs[0].mint_out = forward ? mint::USDC : mint::WSOL;
    c.legs[1].mint_out = c.base;
    std::shared_ptr<const PoolState> legs[2] = {pump_first ? pump : cp, pump_first ? cp : pump};
    auto wallet = app::dry_run_keypair().pubkey();
    tx::IxList list;
    auto args = tx::execute_route_ix(list, app::dry_run_executor(), wallet, c, legs, 123456789, 123456790);
    CHECK(args.has_value()); if (!args) continue;
    CHECK_EQ(list.n, uint8_t(1)); const auto& ix = list.ixs[0];
    CHECK_EQ(ix.n_accounts, uint8_t(35)); CHECK_EQ(args->num_legs, uint8_t(2));
    CHECK_EQ(args->amount_in, uint64_t(123456789)); CHECK_EQ(args->min_out, uint64_t(123456790));
    CHECK(ix.accounts[0].key == wallet && ix.accounts[0].signer);
    CHECK(ix.accounts[1].key == ata(wallet, c.base, forward ? prog::TOKEN : prog::TOKEN_2022));
    CHECK(ix.accounts[2].key == ata(wallet, c.legs[0].mint_out, forward ? prog::TOKEN_2022 : prog::TOKEN));
    size_t offset = 3;
    for (int i = 0; i < 2; ++i) {
      const auto& spec = args->legs[i];
      CHECK_EQ(spec.dex, uint8_t(legs[i]->kind)); CHECK_EQ(spec.a_to_b, uint8_t(c.legs[i].a_to_b));
      CHECK_EQ(size_t(spec.acct_start), offset - 3);
      CHECK_EQ(spec.acct_len, uint8_t(legs[i]->kind == DexKind::Cpmm ? 14 : 18));
      CHECK(ix.accounts[offset].key == (legs[i]->kind == DexKind::Cpmm ? prog::RAYDIUM_CPMM : prog::PUMPSWAP));
      offset += spec.acct_len;
    }
    CHECK_EQ(offset, size_t(ix.n_accounts));
    CHECK_EQ(ix.data[18], args->legs[0].dex); CHECK_EQ(ix.data[22], args->legs[1].dex);
  }
}

TEST(runtime_batched_frames_price_each_changed_pool_once) {
  // The same capture, replayed per frame and in one batch: the batch reaches the same store state and prices
  // each changed pool at most once (live.cpp under a backlog), never more updates than the per-frame path.
  Loaded a, b;
  CHECK(a.rt->replay_file(fixture_path("frames.bin")));
  feed::FrameReader rd;
  CHECK(rd.open(fixture_path("frames.bin")));
  feed::Frame f;
  size_t frames = 0;
  while (rd.next(f)) { b.rt->apply_frame(f.data.data(), f.data.size(), f.recv_ns, f.wall_ms); ++frames; }
  size_t priced = b.rt->evaluate_changed();
  CHECK(frames > 0);
  CHECK(priced > 0);
  CHECK(priced <= b.rt->store().pool_count());
  CHECK(b.rt->stats().updates <= a.rt->stats().updates);
  CHECK_EQ(b.rt->stats().frames, a.rt->stats().frames);
  for (uint32_t id = 0; id < a.rt->store().pool_count(); ++id) {
    auto pa = a.rt->store().pool(id), pb = b.rt->store().pool(id);
    CHECK_EQ(pa ? pa->seq : 0, pb ? pb->seq : 0);
  }
  CHECK_EQ(b.rt->evaluate_changed(), size_t(0));  // nothing left after the batch
}

TEST(armed_set_counts_in_dry_run_and_skips_when_enforced) {
  app::ArmedSet none;
  std::string err;
  CHECK(!none.load_json("{\"pools\":[\"not base58!\"]}", &err) && !err.empty() && none.size() == 0);
  CHECK(!none.load_json("[]", &err));
  Pubkey a{};
  a[0] = 7;
  app::ArmedSet one;
  CHECK(one.load_json(app::armed_to_json({a})) && one.contains(a) && one.size() == 1);

  // The replay's routes go through fixture pools, none of which is `a`: every candidate is outside the set.
  for (bool enforce : {false, true}) {
    Loaded L;
    Keypair kp = app::dry_run_keypair();
    tx::AltManager alts;
    tx::BlockhashCache bh;
    risk::Limits limits(risk::limits_config(L.cfg));
    submit::Submitter sub(true, nullptr);
    app::BundleBuilder bb(L.cfg, *L.rt, kp, app::dry_run_executor(), alts, bh, limits, sub);
    bb.set_offline(true);
    bb.set_armed(&one, enforce);
    std::vector<obs::DecisionRecord> recs;
    L.rt->set_send_hook([&](const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& r) {
      bb(o, c, r);
      recs.push_back(r);
    });
    CHECK(L.rt->replay_file(fixture_path("frames.bin")));
    const auto& s = bb.stats();
    CHECK(s.built > 0 && s.not_armed == s.built);
    if (enforce) {
      CHECK(s.signed_ok == 0 && s.dry_run == 0);
      for (auto& r : recs) CHECK(r.action == uint8_t(obs::Action::Skipped) && r.reason == uint8_t(engine::Reason::not_armed));
    } else {
      CHECK(s.dry_run == s.built);  // dry run keeps measuring every pool
    }
  }
  CHECK(std::string(engine::reason_name(engine::Reason::not_armed)) == "not_armed");
}

TEST(landing_watcher_reports_jito_verdicts_off_thread) {
  int calls = 0;
  std::string body = "{\"jsonrpc\":\"2.0\",\"result\":{\"context\":{\"slot\":1},\"value\":["
                     "{\"bundle_id\":\"b0\",\"status\":\"Landed\",\"landed_slot\":77},"
                     "{\"bundle_id\":\"b1\",\"status\":\"Failed\",\"landed_slot\":null},"
                     "{\"bundle_id\":\"b2\",\"status\":\"Pending\",\"landed_slot\":null}]},\"id\":1}";
  auto client = std::make_unique<submit::JitoClient>(
      std::vector<std::string>{"frankfurt"}, "https://%s.x",
      [&](const std::string&) { return std::unique_ptr<submit::HttpPost>(new MockHttp(&calls, 200, body)); });
  app::LandingWatcher w(std::move(client), 500, 30'000);  // not started: poll_once is driven by hand
  Signature s3{};
  s3[0] = 3;
  w.track("b0", Signature{}, 0x0201, 0);
  w.track("b1", Signature{}, 0x0201, 0);
  w.track("b2", Signature{}, 0x0301, 0);
  w.track("b3", s3, 0x0301, 0);
  CHECK_EQ(w.pending(), size_t(4));
  w.poll_once(100);  // nothing is due before poll_ms
  CHECK_EQ(calls, 0);
  w.signature_seen(s3, 80);  // the wallet stream saw b3 land before any poll
  w.poll_once(600);
  CHECK_EQ(calls, 1);
  auto done = w.drain();
  CHECK_EQ(done.size(), size_t(3));
  std::map<std::string, app::LandingWatcher::Done> by;
  for (auto& d : done) by[d.bundle_id] = d;
  CHECK(by["b0"].result == submit::BundleTracker::Result::Landed && by["b0"].landed_slot == 77 && by["b0"].route_class == 0x0201);
  CHECK(by["b1"].result == submit::BundleTracker::Result::Rejected);
  CHECK(by["b3"].result == submit::BundleTracker::Result::Landed && by["b3"].landed_slot == 80);
  CHECK_EQ(w.pending(), size_t(1));
  w.poll_once(31'000);  // b2 never landed
  done = w.drain();
  CHECK(done.size() == 1 && done[0].bundle_id == "b2" && done[0].result == submit::BundleTracker::Result::NotLanded);
  CHECK_EQ(w.pending(), size_t(0));
  CHECK(w.drain().empty());
}
