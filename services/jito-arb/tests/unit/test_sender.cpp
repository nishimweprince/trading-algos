// Plain-transaction (Helius Sender) path: the bid, the failed-fee budget, the wire format, and the client's
// send / outcome loop against mocks.
#include <algorithm>
#include <atomic>
#include <functional>
#include <chrono>
#include <mutex>
#include <thread>

#include "core/config.hpp"
#include "risk/sender_budget.hpp"
#include "risk/sign_guard.hpp"
#include "submit/sender.hpp"
#include "test.hpp"

using namespace arb;

TEST(sender_fee_bids_a_share_of_gross) {
  risk::SenderFeeConfig c;  // 10%, tip 10,000, base 5,000, price in [1,000, 5,000,000]
  // The 18 Nov median opportunity: 372k lamports gross, ~150k CU.
  auto f = risk::sender_fee(372'000, 150'000, c);
  CHECK(f.ok);
  CHECK_EQ(f.cu_price, uint64_t(148'000));          // (37,200 - 15,000) * 1e6 / 150,000
  CHECK_EQ(f.fee_lamports, uint64_t(5'000 + 22'200));
  CHECK(f.fee_lamports + c.tip_lamports <= 37'200);  // whole cost of a success within 10% of gross
  // Too small to pay the base fee and tip out of 10%.
  CHECK(!risk::sender_fee(100'000, 150'000, c).ok);
  // A large opportunity is capped at the max price.
  auto big = risk::sender_fee(10'000'000'000ull, 150'000, c);
  CHECK(big.ok && big.cu_price == c.max_cu_price);
  CHECK_EQ(big.fee_lamports, uint64_t(5'000 + 750'000));
  // Rounding up: 1 CU at 1,500 micro-lamports costs 1 lamport, not 0.
  risk::SenderFeeConfig one = c;
  one.base_fee_lamports = 0;
  one.tip_lamports = 0;
  one.fee_frac_ppm = 1'000'000;
  auto r = risk::sender_fee(1, 1, one);
  CHECK(r.ok && r.cu_price == 1'000'000 && r.fee_lamports == 1);
  CHECK(!risk::sender_fee(372'000, 0, c).ok);
}

TEST(sender_fee_keeps_a_large_success_only_tip_out_of_the_fee_share) {
  risk::SenderFeeConfig c;
  c.tip_lamports = 1'000'000;      // Astralane free tier: 0.001 SOL, paid only when it succeeds
  c.max_tip_share_ppm = 500'000;   // ... and only when it is at most half of gross
  CHECK(!risk::sender_fee(1'999'999, 150'000, c).ok);
  auto f = risk::sender_fee(2'000'000, 150'000, c);
  CHECK(f.ok);
  // The failure cost (base + priority) still fits 10% of gross; the tip is not in it.
  CHECK_EQ(f.fee_lamports, uint64_t(5'000 + 195'000));
  CHECK(f.fee_lamports <= 200'000);
  // Without the share rule the same tip would need 10x the gross.
  c.max_tip_share_ppm = 0;
  CHECK(!risk::sender_fee(2'000'000, 150'000, c).ok);
  CHECK(risk::sender_fee(10'100'000, 150'000, c).ok);
  CHECK_EQ(risk::astralane_tip_accounts().size(), size_t(17));
}

TEST(sender_budget_caps_failed_fees_and_pauses_after_a_run) {
  risk::SenderBudgetConfig c;
  c.daily_fail_cap_lamports = 100'000;
  c.max_consecutive_fail = 3;
  c.pause_s = 60;
  risk::SenderBudget b(c);
  const int64_t t = 1'795'000'000'000;  // 2026-11-18
  using R = risk::SenderBudget::Result;
  CHECK(b.check(t, 40'000) == risk::SenderBlock::none);
  b.on_send("a", 40'000, t);
  b.on_send("b", 40'000, t);
  // 80k at risk: a third 40k would exceed the cap before any outcome is known.
  CHECK(b.check(t, 40'000) == risk::SenderBlock::daily_cap);
  CHECK_EQ(b.on_result("a", R::dropped, t), uint64_t(40'000));  // never landed: released, nothing paid
  CHECK_EQ(b.failed_today(t), uint64_t(0));
  CHECK(b.check(t, 40'000) == risk::SenderBlock::none);
  b.on_result("b", R::failed, t);
  CHECK_EQ(b.failed_today(t), uint64_t(40'000));
  CHECK_EQ(b.at_risk(), uint64_t(0));
  CHECK_EQ(b.on_result("b", R::failed, t), uint64_t(0));  // unknown or already resolved: ignored
  // A landing resets the run; three failures in a row pause.
  b.on_send("c", 1, t);
  b.on_result("c", R::landed, t);
  for (const char* s : {"d", "e"}) {
    b.on_send(s, 1, t);
    b.on_result(s, R::failed, t);
  }
  CHECK(b.check(t, 1) == risk::SenderBlock::none);
  b.on_send("f", 1, t);
  b.on_result("f", R::failed, t);
  CHECK(b.check(t + 1, 1) == risk::SenderBlock::paused);
  CHECK(b.check(t + 61'000, 1) == risk::SenderBlock::none);
  // The failed total resets at the UTC day boundary.
  CHECK(b.failed_today(t) > 0);
  CHECK_EQ(b.failed_today(t + 86'400'000), uint64_t(0));
}

TEST(sender_wire_format_and_reply) {
  std::string body = submit::sender_send_body("QUJD", 7);
  CHECK_EQ(body, std::string("{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"sendTransaction\",\"params\":[\"QUJD\",{\"encoding\":\"base64\","
                             "\"skipPreflight\":true,\"maxRetries\":0}]}"));
  CHECK_EQ(submit::sender_bundle_body({"VFJJRw=="}, "QUJD", 3),
           std::string("{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"sendBundle\",\"params\":[[\"VFJJRw==\",\"QUJD\"],{\"encoding\":\"base64\"}]}"));
  std::string sig, err;
  CHECK(submit::parse_sender_reply("{\"jsonrpc\":\"2.0\",\"result\":\"5sig\",\"id\":1}", sig, err) && sig == "5sig" && err.empty());
  CHECK(!submit::parse_sender_reply("{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32602,\"message\":\"missing tip\"},\"id\":1}", sig, err));
  CHECK_EQ(err, std::string("missing tip"));
  CHECK(!submit::parse_sender_reply("<html>", sig, err) && !err.empty());
  CHECK_EQ(risk::sender_tip_accounts().size(), size_t(10));
  for (const Pubkey& k : risk::sender_tip_accounts())
    for (const Pubkey& j : risk::default_tip_accounts()) CHECK(!(k == j));
}

TEST(sender_url_gets_the_key_appended) {
  Config c;
  c.helius.api_key_env = "ARB_TEST_SENDER_KEY";
  ::unsetenv("ARB_TEST_SENDER_KEY");
  CHECK_EQ(resolve_sender_url(c), std::string("http://fra-sender.helius-rpc.com/fast?swqos_only=true"));
  ::setenv("ARB_TEST_SENDER_KEY", "k1", 1);
  CHECK_EQ(resolve_sender_url(c), std::string("http://fra-sender.helius-rpc.com/fast?swqos_only=true&api-key=k1"));
  c.jito.sender_url = "http://x/fast";
  CHECK_EQ(resolve_sender_url(c), std::string("http://x/fast?api-key=k1"));
  ::unsetenv("ARB_TEST_SENDER_KEY");
}

namespace {

class ScriptedHttp final : public submit::HttpPost {
 public:
  ScriptedHttp(std::atomic<int>* posts, std::atomic<int>* gets, int status, std::string reply)
      : posts_(posts), gets_(gets), status_(status), reply_(std::move(reply)) {}
  int post(std::string_view, std::string_view, std::string& out) override {
    ++*posts_;
    out = reply_;
    return status_;
  }
  int get(std::string_view path, std::string& out) override {
    ++*gets_;
    out = "pong";
    return path == "/ping" ? 200 : 404;
  }

 private:
  std::atomic<int>* posts_;
  std::atomic<int>* gets_;
  int status_;
  std::string reply_;
};

bool wait_for(const std::function<bool()>& f, int ms = 3000) {
  for (int i = 0; i < ms / 5; ++i) {
    if (f()) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  return f();
}

}  // namespace

TEST(sender_client_sends_then_resolves_each_outcome) {
  std::atomic<int> posts{0}, gets{0};
  std::atomic<uint64_t> slot{100};
  std::mutex mu;
  std::vector<std::string> sent_ok, outcomes;
  submit::SenderClientConfig c;
  c.url = "http://sender.test/fast";
  c.keepalive_s = 1;
  c.status_poll_ms = 10;
  c.status_timeout_s = 1;
  c.http = [&](const std::string&) {
    return std::unique_ptr<submit::HttpPost>(new ScriptedHttp(&posts, &gets, 200, "{\"jsonrpc\":\"2.0\",\"result\":\"x\",\"id\":1}"));
  };
  // "L" lands, "F" lands and fails, "D" is never seen (dropped after the 1 s timeout).
  c.statuses = [&](const std::vector<std::string>& sigs) {
    std::vector<RpcSigStatus> v(sigs.size());
    for (size_t i = 0; i < sigs.size(); ++i) {
      if (sigs[i] == "L" || sigs[i] == "F") {
        v[i].found = true;
        v[i].slot = 101;
        v[i].failed = sigs[i] == "F";
      }
    }
    return v;
  };
  submit::SenderClient s(
      c, "", &slot,
      [&](const submit::SenderSendResult& r, const submit::SenderJob& j) {
        std::lock_guard<std::mutex> g(mu);
        sent_ok.push_back(j.sig + (r.accepted ? "+" : r.dropped_stale ? "~" : "-"));
      },
      [&](const submit::SenderJob& j, submit::SenderOutcome o, uint64_t) {
        std::lock_guard<std::mutex> g(mu);
        outcomes.push_back(j.sig + ":" + submit::sender_outcome_name(o));
      });
  CHECK(wait_for([&] { return gets.load() >= 1; }));  // the connection is warmed at start
  for (const char* sig : {"L", "F", "D"}) {
    submit::SenderJob j;
    j.sig = sig;
    j.built_slot = 100;
    CHECK(s.send(j));
  }
  submit::SenderJob stale;
  stale.sig = "S";
  stale.built_slot = 90;  // the slot clock is past 91: dropped before sending
  CHECK(s.send(stale));
  CHECK(wait_for([&] {
    std::lock_guard<std::mutex> g(mu);
    return outcomes.size() == 3;
  }));
  CHECK_EQ(posts.load(), 3);
  CHECK(s.accepted() == 3 && s.dropped_stale() == 1 && s.landed() == 1 && s.failed() == 1 && s.dropped() == 1);
  std::lock_guard<std::mutex> g(mu);
  CHECK(sent_ok.size() == 4 && sent_ok[3] == "S~");
  std::sort(outcomes.begin(), outcomes.end());
  CHECK(outcomes[0] == "D:dropped" && outcomes[1] == "F:failed" && outcomes[2] == "L:landed");
}

TEST(sender_client_counts_a_refusal_and_sends_nothing_to_status) {
  std::atomic<int> posts{0}, gets{0}, status_calls{0};
  std::atomic<uint64_t> slot{5};
  submit::SenderClientConfig c;
  c.url = "http://sender.test/fast";
  c.keepalive_s = 0;
  c.status_poll_ms = 10;
  c.http = [&](const std::string&) {
    return std::unique_ptr<submit::HttpPost>(
        new ScriptedHttp(&posts, &gets, 200, "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32602,\"message\":\"no tip\"},\"id\":1}"));
  };
  c.statuses = [&](const std::vector<std::string>& sigs) {
    ++status_calls;
    return std::vector<RpcSigStatus>(sigs.size());
  };
  std::atomic<int> refused{0};
  submit::SenderClient s(c, "", &slot, [&](const submit::SenderSendResult& r, const submit::SenderJob&) {
    if (!r.accepted && r.error == "no tip") ++refused;
  }, nullptr);
  submit::SenderJob j;
  j.sig = "R";
  j.built_slot = 5;
  CHECK(s.send(j));
  CHECK(wait_for([&] { return refused.load() == 1; }));
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  CHECK(s.rejected() == 1 && s.accepted() == 0 && s.pending() == 0 && status_calls.load() == 0 && gets.load() == 0);
}
