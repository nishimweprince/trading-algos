#pragma once
// The send hook (spec §9): a dispatched opportunity becomes the §7.3 transaction (CU limit, Kamino
// borrow, executor execute_route, repay, tip), checked by the sign guard and the §10 limits,
// compiled against the bot's lookup tables (loaded from chain), signed, and handed to the Submitter.
#include <random>

#include <functional>
#include "app/live_send.hpp"
#include "app/runtime.hpp"
#include "obs/latency.hpp"
#include "risk/limits.hpp"
#include "risk/sign_guard.hpp"
#include "submit/jito.hpp"
#include "tx/alt_manager.hpp"
#include "tx/builders.hpp"

namespace arb::app {

/** Keys every route references (programs, authorities, the flash reserves, the executor): the shared table. */
std::vector<Pubkey> shared_table_keys(const Pubkey& wallet, std::span<const flash::FlashReserve> reserves, const Pubkey& executor);
/** The accounts a route through this pool needs from a lookup table (pool, vaults, the wallet's ATAs, arrays). */
std::vector<Pubkey> pool_table_keys(const PoolState& p, const Pubkey& wallet);

/** Placeholder executor id for tests and tools before a program is deployed (never on chain). */
Pubkey placeholder_executor();
/** A fixed, publicly known test keypair (holds nothing): tests, tools, and the recorder's watch address. */
Keypair test_keypair();


struct BuilderStats {
  uint64_t built = 0, signed_ok = 0, too_large = 0, guard_blocked = 0, risk_blocked = 0, build_failed = 0, sent = 0, stale = 0;
  uint64_t not_armed = 0;  // routes skipped: through a pool outside the armed set
  uint64_t leader_skipped = 0;  // signed, not sent: no Jito path reaches the slot's leaders
};

class BundleBuilder {
 public:
  BundleBuilder(const Config& cfg, Runtime& rt, Keypair wallet, Pubkey executor, tx::AltManager& alts,
                tx::BlockhashCache& blockhash, risk::Limits& limits, submit::Submitter& submitter);
  /** The Runtime send hook. */
  void operator()(const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& rec);
  void set_wallet_lamports(uint64_t l) { wallet_lamports_ = l; }
  /** The feed health gate (feed::FeedHealth): while false every candidate is blocked as feed_lag. */
  void set_feed_ok(bool ok) { feed_ok_ = ok; }
  void set_tip_accounts(std::vector<Pubkey> v) { guard_.tip_accounts = std::move(v); }
  /** The pools the bot may trade (null: every pool); a route through any other pool is skipped as not_armed. */
  void set_armed(const ArmedSet* a) { armed_ = a; }
  /** Called for each bundle Jito accepted: (bundle id, signature, route class, wall ms). */
  using SentHook = std::function<void(const std::string&, const Signature&, uint16_t, int64_t)>;
  void set_sent_hook(SentHook h) { sent_hook_ = std::move(h); }
  /** Called for each bundle queued on the router (engine thread): the signed bytes and route class. */
  using QueuedHook = std::function<void(const tx::SignedTx&, uint16_t)>;
  void set_queued_hook(QueuedHook h) { queued_hook_ = std::move(h); }
  const BuilderStats& stats() const { return n_; }
  const tx::SignedTx& last_tx() const { return last_; }
  const risk::SignGuard& guard() const { return guard_; }
  /** Frame received -> bundle signed (everything before the HTTP write), microseconds. */
  obs::LatencyWindow& detect_to_signed_us() { return detect_us_; }
  obs::LatencyWindow& eval_to_signed_us() { return compute_us_; }
  /** Builder entry (after the engine chose the candidate) to signed: instructions, guard, compile, sign. */
  obs::LatencyWindow& build_sign_us() { return build_us_; }
  /** Build steps over 1 h: 0 limits/setup, 1 instructions + guard, 2 blockhash + tables, 3 compile + sign. */
  obs::LatencyWindow& build_step_us(int i) { return step_us_[i]; }

 private:
  const ArmedSet* armed_ = nullptr;
  SentHook sent_hook_;
  QueuedHook queued_hook_;
  const Config& cfg_;
  Runtime& rt_;
  Keypair wallet_;
  Pubkey executor_;
  tx::AltManager& alts_;
  tx::BlockhashCache& blockhash_;
  risk::Limits& limits_;
  submit::Submitter& submitter_;
  risk::SignGuard guard_;
  std::mt19937_64 rng_{0x5EED};
  bool feed_ok_ = true;  // tests have no streams; live sets it every second
  uint64_t wallet_lamports_ = UINT64_MAX;  // unknown until the wallet subscription reports it
  tx::IxList ixs_;
  tx::SignedTx last_;
  BuilderStats n_;
  obs::LatencyWindow detect_us_, compute_us_, build_us_;
  obs::LatencyWindow step_us_[4]{obs::LatencyWindow(3600), obs::LatencyWindow(3600), obs::LatencyWindow(3600), obs::LatencyWindow(3600)};
};

}  // namespace arb::app
