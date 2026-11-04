#pragma once
// The send hook (spec §9): a dispatched opportunity becomes the §7.3 transaction (CU limit, Kamino
// borrow, executor execute_route, repay, tip), checked by the sign guard and the §10 limits,
// compiled against the active lookup tables, signed, and handed to the Submitter. In dry run the
// Submitter holds no client: the signed bundle is recorded in the decision log and nothing is sent.
#include <random>

#include "app/runtime.hpp"
#include "obs/latency.hpp"
#include "risk/limits.hpp"
#include "risk/sign_guard.hpp"
#include "submit/jito.hpp"
#include "tx/alt_manager.hpp"
#include "tx/builders.hpp"

namespace arb::app {

/** Stand-in executor id for dry runs before the program is deployed (never on chain). */
Pubkey dry_run_executor();
/** Dry-run signer when no hot-wallet file is configured: a fixed, publicly known test seed (holds nothing). */
Keypair dry_run_keypair();

/**
 * One lookup table per pool (its accounts and the wallet's ATAs for its mints) plus a shared table
 * (programs, Kamino reserve accounts), all active: the tables discovery would create. Used by replay
 * and dry runs so the full route compiles under 1,232 bytes; the table keys are synthetic.
 */
void seed_virtual_tables(tx::AltManager& alts, const StateStore& store, const Pubkey& wallet,
                         std::span<const flash::FlashReserve> reserves, const Pubkey& executor);

struct BuilderStats {
  uint64_t built = 0, signed_ok = 0, too_large = 0, guard_blocked = 0, risk_blocked = 0, build_failed = 0, dry_run = 0, sent = 0, stale = 0;
};

class BundleBuilder {
 public:
  BundleBuilder(const Config& cfg, Runtime& rt, Keypair wallet, Pubkey executor, tx::AltManager& alts,
                tx::BlockhashCache& blockhash, risk::Limits& limits, submit::Submitter& submitter);
  /** The Runtime send hook. */
  void operator()(const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& rec);
  void set_wallet_lamports(uint64_t l) { wallet_lamports_ = l; }
  /** Seed the virtual lookup tables on first use (until discovery has created the real ones). */
  void set_virtual_tables(bool on) { virtual_tables_ = on; }
  /**
   * Replay without RPC: virtual tables, and sign over a placeholder blockhash (all zeros) stamped at the
   * current slot. Never on a send path: the Submitter is dry run whenever this is used.
   */
  void set_offline(bool on) { virtual_tables_ = placeholder_blockhash_ = on; }
  void set_tip_accounts(std::vector<Pubkey> v) { guard_.tip_accounts = std::move(v); }
  const BuilderStats& stats() const { return n_; }
  const tx::SignedTx& last_tx() const { return last_; }
  const risk::SignGuard& guard() const { return guard_; }
  /** Frame received -> bundle signed (everything before the HTTP write), microseconds. */
  obs::LatencyWindow& detect_to_signed_us() { return detect_us_; }

 private:
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
  uint64_t wallet_lamports_ = UINT64_MAX;  // unknown until the wallet subscription reports it
  bool virtual_tables_ = false, placeholder_blockhash_ = false, seeded_ = false;
  tx::IxList ixs_;
  tx::SignedTx last_;
  BuilderStats n_;
  obs::LatencyWindow detect_us_;
};

}  // namespace arb::app
