// GBX route: the hand-encoded auth / bundle / connected-leaders messages, the client's auth-send-status loop on a
// mock transport, and tipless route transactions passing the sign guard only where allowed.
#include <sodium.h>

#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>

#include "core/base58.hpp"
#include "core/crypto.hpp"
#include "core/pb.hpp"
#include "risk/sign_guard.hpp"
#include "submit/gbx.hpp"
#include "test.hpp"
#include "tx/builders.hpp"

using namespace arb;

namespace {
Keypair test_id() {
  uint8_t seed[32];
  for (int i = 0; i < 32; ++i) seed[i] = uint8_t(i + 1);
  return Keypair::from_seed(seed);
}
std::string token_msg(const std::string& v, int64_t exp) {
  pb::Writer ts, t;
  ts.u64(1, uint64_t(exp));
  t.bytes(1, v);
  t.message(2, ts);
  return t.out;
}
}  // namespace

TEST(gbx_wire_format_round_trips) {
  Keypair id = test_id();
  // GenerateAuthChallengeRequest {role = SEARCHER (1), pubkey}
  std::string req = submit::gbx_challenge_request(id.pubkey());
  pb::Reader r(req);
  uint32_t f, wt;
  CHECK(r.next(f, wt) && f == 1 && r.varint() == 1);
  CHECK(r.next(f, wt) && f == 2 && r.bytes() == std::string_view(reinterpret_cast<const char*>(id.pubkey().data()), 32));
  pb::Writer cr;
  cr.bytes(1, "abc123");
  std::string ch;
  CHECK(submit::gbx_parse_challenge(cr.out, ch) && ch == "abc123");
  CHECK_EQ(submit::gbx_challenge_message(id.pubkey(), "abc123"), b58(id.pubkey()) + "-abc123");
  // Tokens: access (1) and refresh (2), each {value, expires_at_utc {seconds}}
  pb::Writer tr;
  tr.bytes(1, token_msg("AT", 1'800'000'000));
  tr.bytes(2, token_msg("RT", 1'900'000'000));
  submit::GbxToken a, b;
  CHECK(submit::gbx_parse_tokens(tr.out, a, b) && a.value == "AT" && a.expires_s == 1'800'000'000 && b.value == "RT");
  // SendBundleRequest {bundle = 1 {packets = 3 [{data = 1}], dropping_indices = 4 (packed), target_accounts = 6}}
  std::string body = submit::gbx_bundle_request({"TRIG", "OURS"}, {0}, {id.pubkey()});
  pb::Reader top(body);
  CHECK(top.next(f, wt) && f == 1);
  pb::Reader bundle(top.bytes());
  std::vector<std::string> data;
  std::vector<uint64_t> drop;
  int targets = 0;
  while (bundle.next(f, wt)) {
    if (f == 3) {
      pb::Reader p(bundle.bytes());
      uint32_t g, w;
      while (p.next(g, w)) {
        if (g == 1) data.emplace_back(p.bytes());
        else p.skip(w);
      }
    } else if (f == 4) {
      pb::each_varint(bundle, wt, [&](uint64_t v) { drop.push_back(v); });
    } else if (f == 6) {
      CHECK(bundle.bytes().size() == 32);
      ++targets;
    } else {
      bundle.skip(wt);
    }
  }
  CHECK(data.size() == 2 && data[0] == "TRIG" && data[1] == "OURS" && drop.size() == 1 && drop[0] == 0 && targets == 1);
  // ConnectedLeadersResponse: map<string, SlotList{repeated uint64 slots = 1}>
  pb::Writer sl, sl2, resp;
  {
    pb::Writer packed;
    packed.varint(100);
    packed.varint(101);
    sl.bytes(1, packed.out);
    sl2.u64(1, 205);  // unpacked is legal too
  }
  resp.map_entry(1, "ValA", sl);
  resp.map_entry(1, "ValB", sl2);
  std::map<std::string, std::vector<uint64_t>> m;
  CHECK(submit::gbx_parse_connected_leaders(resp.out, m));
  CHECK(m.size() == 2 && m["ValA"].size() == 2 && m["ValA"][1] == 101 && m["ValB"][0] == 205);
  pb::Writer u;
  u.bytes(1, "uuid-1");
  std::string uuid;
  CHECK(submit::gbx_parse_bundle_uuid(u.out, uuid) && uuid == "uuid-1");
}

namespace {
struct MockGbx final : submit::GbxTransport {
  std::mutex* mu;
  std::vector<std::string>* calls;
  Keypair id;
  int* unauth_once;
  int call(const std::string& method, const std::string& req, std::string& resp, const std::string& bearer, std::string& err) override {
    std::lock_guard<std::mutex> g(*mu);
    calls->push_back(method + "|" + bearer);
    pb::Writer w;
    if (method == "/auth.AuthService/GenerateAuthChallenge") {
      w.bytes(1, "chal");
    } else if (method == "/auth.AuthService/GenerateAuthTokens") {
      // The signature must verify over "<pubkey>-chal".
      pb::Reader r(req);
      uint32_t f, wt;
      std::string_view sig;
      while (r.next(f, wt)) {
        if (f == 3) sig = r.bytes();
        else r.skip(wt);
      }
      const std::string msg = std::string(reinterpret_cast<const char*>(id.pubkey().data()), 32) + "chal";  // variant 1
      if (sig.size() != 64 || crypto_sign_verify_detached(reinterpret_cast<const uint8_t*>(sig.data()), reinterpret_cast<const uint8_t*>(msg.data()),
                                                          msg.size(), id.pubkey().data()) != 0) {
        err = "bad signature";
        return 16;
      }
      w.bytes(1, token_msg("AT", 0));
      w.bytes(2, token_msg("RT", 0));
    } else if (method == "/searcher.SearcherService/GetConnectedLeaders") {
      pb::Writer sl;
      sl.u64(1, 500);
      sl.u64(1, 501);
      w.map_entry(1, "Val", sl);
    } else if (method == "/searcher.SearcherService/SendBundle") {
      if (bearer != "AT") { err = "no token"; return 16; }
      if (*unauth_once) { --*unauth_once; err = "expired"; return 16; }
      w.bytes(1, "u1");
    }
    resp = w.out;
    return 0;
  }
};
bool wait_until(const std::function<bool()>& f) {
  for (int i = 0; i < 600; ++i) {
    if (f()) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  return f();
}
}  // namespace

TEST(gbx_client_authenticates_sends_and_tracks_landing) {
  std::mutex mu;
  std::vector<std::string> calls;
  int unauth_once = 1;  // the first send finds the token expired: the client authenticates again and resends
  Keypair id = test_id();
  std::atomic<uint64_t> slot{500};
  submit::GbxConfig c;
  c.status_poll_ms = 10;
  c.transport = [&] {
    auto m = std::make_unique<MockGbx>();
    m->mu = &mu;
    m->calls = &calls;
    m->id = id;
    m->unauth_once = &unauth_once;
    return std::unique_ptr<submit::GbxTransport>(std::move(m));
  };
  c.statuses = [](const std::vector<std::string>& sigs) {
    std::vector<RpcSigStatus> v(sigs.size());
    for (auto& s : v) { s.found = true; s.slot = 501; }
    return v;
  };
  std::atomic<int> sent_ok{0}, landed{0};
  submit::GbxClient g(c, id, "", &slot,
                      [&](const submit::GbxJob&, int code, const std::string& uuid, const std::string&, int64_t) { if (code == 0 && uuid == "u1") ++sent_ok; },
                      [&](const submit::GbxJob&, submit::GbxOutcome o, uint64_t s) { if (o == submit::GbxOutcome::landed && s == 501) ++landed; });
  CHECK(wait_until([&] { return g.authed() && g.connected_validators() == 1; }));
  CHECK(g.gbx_slot(501) && !g.gbx_slot(502));
  CHECK_EQ(g.coverage_ppm(500, 4), uint32_t(500'000));  // 2 of the next 4 slots
  submit::GbxJob j;
  j.txs = {"T", "O"};
  j.dropping = {0};
  j.sig = "Sig1";
  j.built_slot = 500;
  CHECK(g.send(j));
  CHECK(wait_until([&] { return landed.load() == 1; }));
  CHECK(sent_ok.load() == 1 && g.sent_ok() == 1 && g.rejected() == 0);
  std::lock_guard<std::mutex> l(mu);
  int challenges = 0;
  for (auto& s : calls) challenges += s.rfind("/auth.AuthService/GenerateAuthChallenge", 0) == 0;
  CHECK(challenges == 2);  // once at start, once after the expired token
  int token_calls = 0;
  for (auto& s : calls) token_calls += s.rfind("/auth.AuthService/GenerateAuthTokens", 0) == 0;
  CHECK(token_calls == 3);  // variants 0 and 1 the first time; only the known variant 1 the second
}

TEST(gbx_tipless_transaction_passes_only_a_guard_that_allows_it) {
  tx::IxList l;
  Keypair id = test_id();
  risk::SignGuard g;
  g.wallet = id.pubkey();
  Pubkey exec{};
  exec[0] = 9;
  g.executor = exec;
  tx::set_compute_unit_limit(l, 200'000);
  tx::set_compute_unit_price(l, 1'000);
  CHECK(!g.check(l));  // no tip transfer: refused by default (bundle and Sender paths)
  g.allow_no_tip = true;
  CHECK(bool(g.check(l)));
  tx::system_transfer(l, id.pubkey(), risk::default_tip_accounts()[0], 1'000);
  tx::system_transfer(l, id.pubkey(), risk::default_tip_accounts()[1], 1'000);
  CHECK(!g.check(l));  // two transfers are never allowed
}
