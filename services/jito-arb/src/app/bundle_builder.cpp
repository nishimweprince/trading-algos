#include "app/bundle_builder.hpp"

#include "app/bundle_sim.hpp"

#include <unistd.h>

#include <cstring>
#include <set>

#include "core/base58.hpp"
#include "core/programs.hpp"
#include "core/sys.hpp"
#include "tx/dex_ix.hpp"

namespace arb::app {

Pubkey dry_run_executor() {
  Pubkey k{};
  std::memcpy(k.data(), "jito-arb dry-run executor (none)", 32);
  return k;
}

Keypair dry_run_keypair() {
  uint8_t seed[32];
  std::memcpy(seed, "jito-arb dry run only: no funds!", 32);
  return Keypair::from_seed(seed);
}

void seed_virtual_tables(tx::AltManager& alts, const StateStore& store, const Pubkey& wallet,
                         std::span<const flash::FlashReserve> reserves, const Pubkey& executor) {
  auto table_key = [](uint32_t n) {
    Pubkey k{};
    std::memcpy(k.data(), "virtual-alt", 11);
    std::memcpy(k.data() + 28, &n, 4);
    return k;
  };
  tx::AltAccount shared;
  shared.has_authority = true;
  shared.authority = wallet;
  std::set<Pubkey> seen;
  auto add = [&](tx::AltAccount& a, const Pubkey& k) {
    if (k == wallet || a.addresses.size() >= tx::ALT_MAX_ADDRESSES) return;
    a.addresses.push_back(k);
  };
  for (const Pubkey& k : {prog::TOKEN, prog::TOKEN_2022, prog::KLEND, prog::SYSVAR_INSTRUCTIONS, prog::RAYDIUM_V4, prog::WHIRLPOOL,
                          prog::RAYDIUM_CPMM, tx::dexix::raydium_authority(), tx::dexix::cpmm_authority(), executor})
    add(shared, k);
  for (const flash::FlashReserve& r : reserves)
    for (const Pubkey& k : {r.reserve, r.lending_market, r.lending_market_authority, r.liquidity_mint, r.supply_vault, r.fee_vault})
      add(shared, k);
  alts.upsert(table_key(0), shared);
  for (uint32_t i = 0; i < store.pool_count(); ++i) {
    auto p = store.pool(i);
    if (!p) continue;
    tx::AltAccount a;
    a.has_authority = true;
    a.authority = wallet;
    add(a, p->address);
    add(a, p->vault_a);
    add(a, p->vault_b);
    add(a, tx::wallet_ata(wallet, p->mint_a, *p));
    add(a, tx::wallet_ata(wallet, p->mint_b, *p));
    switch (p->kind) {
      case DexKind::Whirlpool:
        add(a, tx::dexix::whirlpool_oracle(p->address));
        for (uint8_t t = 0; t < p->n_ticks; ++t) add(a, p->ticks[t].key);
        break;
      case DexKind::Cpmm:
        add(a, p->cpmm.amm_config);
        add(a, p->cpmm.observation_key);
        add(a, p->mint_a);
        add(a, p->mint_b);
        break;
      default:
        break;
    }
    alts.upsert(table_key(i + 1), a);
  }
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

void BundleBuilder::operator()(const engine::Opportunity& o, const engine::Cycle& c, obs::DecisionRecord& rec) {
  auto skip = [&](engine::Reason r) {
    rec.action = uint8_t(obs::Action::Skipped);
    rec.reason = uint8_t(r);
  };
  ++n_.built;
  if (virtual_tables_ && !seeded_) {
    std::vector<flash::FlashReserve> rs;
    for (const std::string& m : cfg_.engine.base_mints)
      if (auto r = rt_.engine().flash_reserve(parse_pubkey(m))) rs.push_back(*r);
    seed_virtual_tables(alts_, rt_.store(), wallet_.pubkey(), rs, executor_);
    seeded_ = true;
  }
  uint8_t dex_mask = 0;
  std::shared_ptr<const PoolState> legs[engine::MAX_LEGS];
  for (int i = 0; i < c.n; ++i) {
    legs[i] = rt_.store().pool(c.legs[i].pool);
    dex_mask |= uint8_t(1u << uint8_t(legs[i]->kind));
  }
  uint64_t slot = std::max(o.slot, rt_.clock().slot.load(std::memory_order_relaxed));
  // §10 limits: kill switch, fee cap, float, reject pause, feed lag, disabled quoters.
  limits_.set_kill(::access(cfg_.mode.kill_file.c_str(), F_OK) == 0 || limits_.killed());
  risk::Block blk = limits_.check(rec.wall_ms, wallet_lamports_, 0, dex_mask);
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
  auto lay = tx::build_route_ixs(ixs_, p, c, std::span<const std::shared_ptr<const PoolState>>(legs, c.n));
  if (!lay) {
    ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  if (auto g = guard_.check(ixs_); !g) {
    ++n_.guard_blocked;
    return skip(engine::Reason::sign_guard);
  }
  if (placeholder_blockhash_) blockhash_.set(Hash{}, slot, rec.wall_ms);
  auto bh = blockhash_.get(slot);
  if (!bh) {
    ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  auto tables = alts_.active(slot);
  Pubkey statics[1] = {p.tip_account};
  auto t = tx::build_tx(wallet_.pubkey(), &wallet_, ixs_, *bh, tables, last_, statics);
  if (!t) {
    if (t.error().code == Errc::tx_too_large) ++n_.too_large;
    else ++n_.build_failed;
    return skip(engine::Reason::tx_build_failed);
  }
  ++n_.signed_ok;
  if (uint64_t t0 = rt_.frame_recv_ns(); t0) {
    uint64_t t1 = now_ns();
    detect_us_.add(t1, int64_t(t1 - std::min(t1, t0)) / 1000);
  }
  std::memcpy(rec.signature, last_.sig.data(), 64);
  switch (submitter_.submit(last_, slot, rt_.clock().slot.load(std::memory_order_relaxed), rec.wall_ms)) {
    case submit::SubmitOutcome::DryRun:
      ++n_.dry_run;
      rec.action = uint8_t(obs::Action::DryRun);
      if (sim_) {
        SimJob j;
        j.o = o;
        j.c = c;
        for (int i = 0; i < c.n; ++i) j.legs[size_t(i)] = legs[i];
        j.cand = rec;
        sim_->submit(std::move(j));
      }
      break;
    case submit::SubmitOutcome::Sent:
      ++n_.sent;
      rec.action = uint8_t(obs::Action::Sent);
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
