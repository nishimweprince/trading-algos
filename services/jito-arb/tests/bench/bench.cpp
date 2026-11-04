// arb_bench: chrono microbenchmarks for the spec's latency targets (§5, §6.2), single-threaded.
//   CP quote (Raydium v4)                 target < 100 ns
//   Whirlpool quote, <= 5 ticks crossed   target < 2 us
//   pool update -> candidate (engine)     target < 200 us
// Laptop numbers are indicative only; the production numbers are a VM step.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <vector>

#include "app/runtime.hpp"
#include "dex/quoter.hpp"
#include "feed/frames.hpp"
#include "fixture.hpp"

using namespace arb;
using namespace arbtest;
using Clock = std::chrono::steady_clock;

namespace {

volatile uint64_t g_sink = 0;

template <class F>
double ns_per_op(size_t iters, F&& f) {
  for (size_t i = 0; i < iters / 10; ++i) f(i);  // warm-up
  auto t0 = Clock::now();
  for (size_t i = 0; i < iters; ++i) f(i);
  return std::chrono::duration<double, std::nano>(Clock::now() - t0).count() / double(iters);
}

}  // namespace

int main() {
  app::PoolsFile pf;
  std::string err;
  if (!app::from_json(read_file(fixture_path("replay_pools.json")), pf, &err)) {
    std::fprintf(stderr, "bench: %s\n", err.c_str());
    return 1;
  }
  Config cfg;
  app::Runtime rt(cfg, pf);
  // Load the capture's initial state (slot 1000 pushes) only: stop before the injected gaps.
  {
    feed::FrameReader rd;
    rd.open(fixture_path("frames.bin"));
    feed::Frame f;
    while (rd.next(f)) {
      feed::UpdateView v;
      if (feed::parse_update(f.data.data(), f.data.size(), v) && v.kind == feed::UpdateView::Slot && v.slot.slot > 1000) break;
      rt.on_frame(f.data.data(), f.data.size(), f.recv_ns, f.wall_ms);
    }
  }
  StateStore& store = rt.store();
  dex::RaydiumV4Quoter rq;
  dex::WhirlpoolQuoter wq;
  std::vector<std::shared_ptr<const PoolState>> rays, wps;
  for (uint32_t i = 0; i < store.pool_count(); ++i) {
    auto p = store.pool(i);
    if (!p->quotable(2)) continue;
    if (p->kind == DexKind::RaydiumV4 && rq.quote_exact_in(*p, true, 1'000'000)) rays.push_back(p);
    if (p->kind == DexKind::Whirlpool && wq.supports(*p) && wq.quote_exact_in(*p, true, 1'000)) wps.push_back(p);
  }
  std::printf("pools: %zu raydium_v4, %zu whirlpool quotable\n", rays.size(), wps.size());

  double cp = ns_per_op(2'000'000, [&](size_t i) {
    const PoolState& p = *rays[i % rays.size()];
    auto q = rq.quote_exact_in(p, i & 1, p.vault_a_amount / 1000 + i);
    g_sink += q ? q->amount_out : 0;
  });
  std::printf("raydium_v4 quote:            %8.1f ns/op   (target < 100 ns)\n", cp);

  // Whirlpool: per pool, the largest of a few sizes that stays inside the cached arrays.
  struct Case {
    const PoolState* p;
    bool a_to_b;
    uint64_t amount;
  };
  std::vector<Case> cases;
  for (auto& p : wps)
    for (bool d : {true, false})
      for (uint64_t frac : {100'000ULL, 10'000ULL, 1'000ULL}) {
        uint64_t depth = d ? p->vault_a_amount : p->vault_b_amount;
        auto q = wq.quote_exact_in(*p, d, std::max<uint64_t>(1, depth / frac));
        if (q && !q->crosses_boundary) {
          cases.push_back({p.get(), d, std::max<uint64_t>(1, depth / frac)});
          break;
        }
      }
  double wp = cases.empty() ? 0 : ns_per_op(500'000, [&](size_t i) {
    const Case& c = cases[i % cases.size()];
    auto q = wq.quote_exact_in(*c.p, c.a_to_b, c.amount);
    g_sink += q ? q->amount_out : 0;
  });
  std::printf("whirlpool quote (%3zu cases): %8.1f ns/op   (target < 2000 ns)\n", cases.size(), wp);

  // Update -> candidate: replay the injected-gap frames and time each pool update through the engine.
  std::vector<double> lat;
  {
    feed::FrameReader rd;
    rd.open(fixture_path("frames.bin"));
    feed::Frame f;
    bool past = false;
    while (rd.next(f)) {
      feed::UpdateView v;
      bool parsed = feed::parse_update(f.data.data(), f.data.size(), v);
      if (parsed && v.kind == feed::UpdateView::Slot && v.slot.slot > 1000) past = true;
      if (!past) continue;
      auto t0 = Clock::now();
      rt.on_frame(f.data.data(), f.data.size(), f.recv_ns, f.wall_ms);
      if (parsed && v.kind == feed::UpdateView::Account) lat.push_back(std::chrono::duration<double, std::micro>(Clock::now() - t0).count());
    }
  }
  std::sort(lat.begin(), lat.end());
  auto pct = [&](double q) { return lat.empty() ? 0.0 : lat[size_t(q * double(lat.size() - 1))]; };
  std::printf("update -> engine pass (%zu updates, %llu candidates): p50 %.1f us  p99 %.1f us  max %.1f us   (target < 200 us)\n",
              lat.size(), (unsigned long long)rt.engine().counters().candidates, pct(0.5), pct(0.99), lat.empty() ? 0 : lat.back());
  return g_sink == 42 ? 1 : 0;
}
