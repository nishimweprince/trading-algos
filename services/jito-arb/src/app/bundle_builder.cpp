#include "app/bundle_builder.hpp"

#include <unistd.h>

#include <cstdint>
#include <cstring>
#include <set>
#include <stdexcept>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/programs.hpp"
#include "core/sys.hpp"
#include "obs/log.hpp"
#include "tx/dex_ix.hpp"

namespace arb::app {

Pubkey placeholder_executor() {
  Pubkey k{};
  std::memcpy(k.data(), "jito-arb dry-run executor (none)", 32);
  return k;
}

Keypair test_keypair() {
  uint8_t seed[32];
  std::memcpy(seed, "jito-arb dry run only: no funds!", 32);
  return Keypair::from_seed(seed);
}

std::vector<Pubkey> shared_table_keys(const Pubkey& wallet, std::span<const flash::FlashReserve> reserves, const Pubkey& executor) {
  std::vector<Pubkey> v;
  for (const Pubkey& k : {prog::TOKEN, prog::TOKEN_2022, prog::KLEND, prog::SYSVAR_INSTRUCTIONS, prog::RAYDIUM_V4, prog::WHIRLPOOL,
                          prog::RAYDIUM_CPMM, prog::PUMPSWAP, prog::RAYDIUM_CLMM, prog::MEMO, prog::SYSTEM, tx::dexix::raydium_authority(), tx::dexix::cpmm_authority(),
                          tx::dexix::pump_global_config(), tx::dexix::pump_fee_config(), tx::dexix::pump_event_authority(),
                          tx::dexix::pump_user_volume_accumulator(wallet), executor})
    v.push_back(k);
  for (const flash::FlashReserve& r : reserves)
    for (const Pubkey& k : {r.reserve, r.lending_market, r.lending_market_authority, r.liquidity_mint, r.supply_vault, r.fee_vault})
      v.push_back(k);
  return v;
}

std::vector<Pubkey> pool_table_keys(const PoolState& p, const Pubkey& wallet) {
  std::vector<Pubkey> v{p.address, p.vault_a, p.vault_b, tx::wallet_ata(wallet, p.mint_a, p), tx::wallet_ata(wallet, p.mint_b, p)};
  switch (p.kind) {
    case DexKind::Dlmm:
      v.push_back(prog::METEORA_DLMM);
      v.push_back(p.dlmm.oracle);
      v.push_back(state::dlmm_bitmap_pda(p.address));
      v.push_back(p.mint_a); v.push_back(p.mint_b);
      for (auto& slot : p.dlmm_arrays) v.push_back(slot.key);
      break;
    case DexKind::Whirlpool:
      v.push_back(tx::dexix::whirlpool_oracle(p.address));
      for (uint8_t t = 0; t < p.n_ticks; ++t) v.push_back(p.ticks[t].key);
      break;
    case DexKind::Clmm:
      v.push_back(p.clmm.amm_config);
      v.push_back(p.clmm.observation_key);
      v.push_back(state::clmm_bitmap_extension_pda(p.address));
      v.push_back(p.mint_a);
      v.push_back(p.mint_b);
      for (uint8_t t = 0; t < p.n_ticks; ++t) v.push_back(p.ticks[t].key);
      break;
    case DexKind::PumpSwap:
      v.push_back(p.mint_a);
      v.push_back(p.mint_b);
      break;
    case DexKind::Cpmm:
      v.push_back(p.cpmm.amm_config);
      v.push_back(p.cpmm.observation_key);
      v.push_back(p.mint_a);
      v.push_back(p.mint_b);
      break;
    case DexKind::RaydiumV4:
      break;
    default:
      throw std::invalid_argument(std::string("lookup table keys: unsupported dex ") + dex_name(p.kind));
  }
  return v;
}

BundleBuilder::BundleBuilder(const Config& cfg, Runtime& rt, Keypair wallet, Pubkey executor, tx::AltManager& alts,
                             tx::BlockhashCache& blockhash, risk::Limits& limits, submit::Submitter& submitter)
    : cfg_(cfg), rt_(rt), wallet_(wallet), executor_(executor), alts_(alts), blockhash_(blockhash), limits_(limits),
      submitter_(submitter) {
  guard_.wallet = wallet_.pubkey();
  guard_.executor = executor_;
  guard_.tip_accounts = risk::default_tip_accounts();
  guard_.tip_max_lamports = cfg.jito.tip_max_lamports;
}

void BundleBuilder::add_plain_path(std::string name, submit::SenderClient* s, risk::SenderBudget* b, risk::SenderFeeConfig fc,
                                   std::vector<Pubkey> tip_accounts, uint64_t tip_max, bool for_preconf, bool preconf_fallback) {
  if (!s) return;
  PlainPath p;
  p.name = std::move(name);
  p.for_preconf = for_preconf;
  p.preconf_fallback = preconf_fallback;
  p.client = s;
  p.fee = fc;
  p.guard = guard_;
  p.guard.tip_accounts = std::move(tip_accounts);
  p.guard.tip_max_lamports = tip_max;
  plain_.push_back(std::move(p));
  sender_budget_ = b;
}

bool BundleBuilder::send_plain(const engine::Opportunity& o, const engine::Cycle& c,
                               std::span<const std::shared_ptr<const PoolState>> legs, const tx::RouteTxParams& bp, uint64_t slot,
                               int64_t wall) {
  // Routes whose bid this opportunity can pay; one is chosen at random so they can be compared like for like.
  PlainPath* eligible[4];
  risk::SenderFee fees[4];
  int n = 0;
  const bool spec = rt_.speculative();
  // A preconfirmed trigger is already scheduled by the leader, so a plain transaction sent now lands after it.
  // Preconf candidates use the preconf route (Sender Max) when they can pay its tip, else a fallback route (Sender).
  auto collect = [&](auto&& want) {
    for (PlainPath& p : plain_) {
      if (n == 4) break;
      if (!want(p)) continue;
      risk::SenderFee f = risk::sender_fee(o.pr.gross_lamports, bp.cu_limit, p.fee);
      if (f.ok) {
        eligible[n] = &p;
        fees[n++] = f;
      }
    }
  };
  if (spec) {
    collect([](const PlainPath& p) { return p.for_preconf; });
    if (n == 0) collect([](const PlainPath& p) { return p.preconf_fallback; });
  } else {
    collect([](const PlainPath& p) { return !p.for_preconf; });
  }
  if (n == 0) {
    ++n_.sender_fee_skip;
    return false;
  }
  const int pick = n == 1 ? 0 : int(rng_() % uint64_t(n));
  PlainPath& path = *eligible[pick];
  const risk::SenderFee fee = fees[pick];
  if (sender_budget_ && sender_budget_->check(wall, fee.fee_lamports) != risk::SenderBlock::none) {
    ++n_.sender_budget_blocked;
    return false;
  }
  tx::RouteTxParams p = bp;
  const auto& tips = path.guard.tip_accounts;
  p.tip_account = tips[size_t(rng_() % tips.size())];
  p.tip_lamports = fee.tip_lamports;
  p.cu_price_micro_lamports = fee.cu_price;
  // The bundle's min_out reserves its Jito tip (about half of gross). A plain transaction never pays that tip, so its
  // guard reserves only what it does pay, its own tip and fee, plus the same profit floor: a smaller edge that is
  // still profitable no longer fails MinOutNotMet.
  if (const engine::BasePrice* price = rt_.engine().base_price(c.base); price && price->known) {
    uint64_t bundle_guard = price->from_lamports_ceil(o.pr.tip_lamports);
    uint64_t plain_guard = price->from_lamports_ceil(p.tip_lamports + fee.fee_lamports);
    if (bp.min_out > bundle_guard && plain_guard < bundle_guard) p.min_out = bp.min_out - bundle_guard + plain_guard;
  }
  if (!tx::build_route_ixs(sender_ixs_, p, c, legs) || !path.guard.check(sender_ixs_)) {
    ++n_.sender_build_failed;
    return false;
  }
  auto bh = blockhash_.get(slot);
  if (!bh) {
    ++n_.sender_build_failed;
    return false;
  }
  std::vector<tx::LookupTable> tables = alts_.active(slot);
  Pubkey statics[1] = {p.tip_account};
  if (!tx::build_tx(wallet_.pubkey(), &wallet_, sender_ixs_, *bh, tables, last_sender_, statics)) {
    ++n_.sender_build_failed;
    return false;
  }
  submit::SenderJob j;
  j.path = path.name;
  if (spec && backrun_ && rt_.trigger_tx()) {
    const std::string& t = *rt_.trigger_tx();
    j.prefix_b64.push_back(b64(reinterpret_cast<const uint8_t*>(t.data()), t.size()));
  }
  j.tx_b64 = b64(last_sender_.bytes, last_sender_.len);
  j.sig = b58(last_sender_.sig);
  j.built_slot = slot;
  j.fee_lamports = fee.fee_lamports;
  j.cu_price = fee.cu_price;
  j.cu_limit = p.cu_limit;
  j.tip_lamports = p.tip_lamports;
  j.route_class = c.route_class;
  j.wall_ms = wall;
  last_plain_min_out_ = p.min_out;
  std::string sig = j.sig;
  if (!path.client->send(std::move(j))) {
    ++n_.sender_queue_full;
    return false;
  }
  if (sender_budget_) sender_budget_->on_send(sig, fee.fee_lamports, wall);
  ++n_.sender_queued;
  ++plain_by_path_[path.name];
  if (spec) LOG_INFO("preconf: %s plain send %s queued (gross %llu, tip %llu, fee %llu)", path.name.c_str(), sig.substr(0, 12).c_str(),
                     (unsigned long long)o.pr.gross_lamports, (unsigned long long)p.tip_lamports, (unsigned long long)fee.fee_lamports);
  return true;
}

void BundleBuilder::operator()(const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& rec) {
  auto skip = [&](engine::Reason r) {
    rec.action = uint8_t(obs::Action::Skipped);
    rec.reason = uint8_t(r);
  };
  ++n_.built;
  if (rt_.speculative()) ++n_.spec_built;
  const uint64_t t_enter = now_ns();
  uint8_t dex_mask = 0;
  std::shared_ptr<const PoolState> legs[engine::MAX_LEGS];
  for (int i = 0; i < c.n; ++i) {
    legs[i] = rt_.store().pool(c.legs[i].pool);
    dex_mask |= uint8_t(1u << uint8_t(legs[i]->kind));
  }
  if (armed_) {
    bool all = true;
    for (int i = 0; i < c.n; ++i) all &= armed_->contains(legs[i]->address);
    if (!all) {
      ++n_.not_armed;
      return skip(engine::Reason::not_armed);
    }
  }
  uint64_t slot = std::max(o.slot, rt_.clock().slot.load(std::memory_order_relaxed));
  // §10 limits: kill switch, fee cap, float, reject pause, feed lag, disabled quoters.
  limits_.set_kill(::access(cfg_.mode.kill_file.c_str(), F_OK) == 0 || limits_.killed());
  // Feed age: the newest account update (tests have none and pass 0); a failed health gate counts as
  // infinitely stale, so the feed_lag block covers flapping streams too.
  int64_t feed_age_ms = 0;
  if (!feed_ok_) feed_age_ms = INT64_MAX;
  else if (uint64_t lu = rt_.last_update_ns(); lu) feed_age_ms = int64_t((now_ns() - std::min(now_ns(), lu)) / 1'000'000);
  risk::Block blk = limits_.check(rec.wall_ms, wallet_lamports_, feed_age_ms, dex_mask);
  if (blk != risk::Block::none) {
    ++n_.risk_blocked;
    return skip(engine::Reason::risk_block);
  }
  tx::RouteTxParams p;
  p.wallet = wallet_.pubkey();
  p.executor = executor_;
  p.tip_account = guard_.tip_accounts[size_t(rng_() % guard_.tip_accounts.size())];  // random per transaction (§9)
  p.amount_in = o.x;
  p.min_out = o.pr.min_out;
  p.tip_lamports = o.pr.tip_lamports;
  p.cu_limit = o.pr.cu_limit;
  p.cu_price_micro_lamports = cfg_.engine.cu_price_micro_lamports;
  p.reserve = rt_.engine().flash_mode() ? rt_.engine().flash_reserve(c.base) : nullptr;
  if (rt_.engine().flash_mode() && !p.reserve) {
    ++n_.build_failed;
    return skip(engine::Reason::flash_disabled);
  }
  const uint64_t t_ixs = now_ns();
  auto lay = tx::build_route_ixs(ixs_, p, c, std::span<const std::shared_ptr<const PoolState>>(legs, c.n));
  if (!lay) {
    ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  if (auto g = guard_.check(ixs_); !g) {
    ++n_.guard_blocked;
    return skip(engine::Reason::sign_guard);
  }
  const uint64_t t_guarded = now_ns();
  auto bh = blockhash_.get(slot);
  if (!bh) {
    ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  std::vector<tx::LookupTable> tables = alts_.active(slot);
  Pubkey statics[1] = {p.tip_account};
  const uint64_t t_compile = now_ns();
  auto t = tx::build_tx(wallet_.pubkey(), &wallet_, ixs_, *bh, tables, last_, statics);
  if (!t) {
    if (t.error().code == Errc::tx_too_large) ++n_.too_large;
    else ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  ++n_.signed_ok;
  if (uint64_t t0 = rt_.frame_recv_ns(); t0) {
    uint64_t t1 = now_ns();
    build_us_.add(t1, int64_t(t1 - t_enter) / 1000);
    // Where the build time goes, over an hour (bundles are rare): limits and setup, instructions and guard,
    // blockhash and tables, compile and sign.
    step_us_[0].add(t1, int64_t(t_ixs - t_enter) / 1000);
    step_us_[1].add(t1, int64_t(t_guarded - t_ixs) / 1000);
    step_us_[2].add(t1, int64_t(t_compile - t_guarded) / 1000);
    step_us_[3].add(t1, int64_t(t1 - t_compile) / 1000);
    detect_us_.add(t1, int64_t(t1 - std::min(t1, t0)) / 1000);
    // The part spent pricing this pool and building the bundle; the rest is frames queued ahead of it.
    if (uint64_t e = rt_.eval_start_ns(); e && e >= t0) compute_us_.add(t1, int64_t(t1 - std::min(t1, e)) / 1000);
  }
  std::memcpy(rec.signature, last_.sig.data(), 64);
  std::vector<submit::RegionResult> regions;
  const auto leg_span = std::span<const std::shared_ptr<const PoolState>>(legs, c.n);
  auto plain = [&]() { return !plain_.empty() && send_plain(o, c, leg_span, p, slot, rec.wall_ms); };
  // A preconfirmation-triggered candidate goes out as a back-run: [trigger, ours] in one bundle, so ours runs right
  // after the transaction that made the edge, or not at all (an already-executed trigger fails the whole bundle).
  std::vector<std::string> trigger_prefix;
  if (backrun_ && rt_.speculative() && rt_.trigger_tx()) {
    const std::string& t = *rt_.trigger_tx();
    trigger_prefix.push_back(b64(reinterpret_cast<const uint8_t*>(t.data()), t.size()));
  }
  const std::vector<std::string>* prefix = trigger_prefix.empty() ? nullptr : &trigger_prefix;
  if (rt_.speculative())
    LOG_INFO("preconf: trigger %s (slot %llu): %s %s, gross %llu", rt_.trigger_sig().substr(0, 12).c_str(), (unsigned long long)slot,
             prefix ? "back-run bundle" : "bundle", b58(last_.sig).substr(0, 12).c_str(), (unsigned long long)o.pr.gross_lamports);
  switch (submitter_.submit(last_, slot, rt_.clock().slot.load(std::memory_order_relaxed), rec.wall_ms, &regions, c.route_class, prefix)) {
    case submit::SubmitOutcome::Queued:
      // Leader-aware routing: queued on the region senders; acceptance and bundle ids arrive through the router.
      ++n_.sent;
      if (rt_.speculative()) ++n_.spec_sent;
      rec.action = uint8_t(obs::Action::Sent);
      if (queued_hook_) queued_hook_(last_, c.route_class);
      plain();
      break;
    case submit::SubmitOutcome::SkippedLeader:
      // No Jito path reaches these leaders, but a plain transaction does.
      ++n_.leader_skipped;
      if (plain()) {
        rec.action = uint8_t(obs::Action::Sent);
        std::memcpy(rec.signature, last_sender_.sig.data(), 64);
      } else {
        skip(engine::Reason::leader_skipped);
      }
      break;
    case submit::SubmitOutcome::Sent:
      ++n_.sent;
      rec.action = uint8_t(obs::Action::Sent);
      // Every region returns the same id for the same bundle: track the first accepted one.
      if (sent_hook_)
        for (const auto& r : regions)
          if (!r.bundle_id.empty()) {
            sent_hook_(r.bundle_id, last_.sig, c.route_class, rec.wall_ms);
            break;
          }
      plain();
      break;
    case submit::SubmitOutcome::Stale:
      ++n_.stale;
      skip(engine::Reason::send_lag);
      break;
    case submit::SubmitOutcome::NoRegion:
      skip(engine::Reason::risk_block);
      break;
  }
}

}  // namespace arb::app
