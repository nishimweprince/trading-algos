#include "logic/engine.hpp"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/pb.hpp"
#include "core/sys.hpp"
#include "logic/txparse.hpp"
#include "sol/constants.hpp"

namespace pd {

namespace {

constexpr uint8_t ROLE_POOL = 1, ROLE_BASE_VAULT = 2, ROLE_QUOTE_VAULT = 3;
// geyser.SlotStatus
constexpr uint32_t SLOT_PROCESSED = 0, SLOT_FIRST_SHRED_RECEIVED = 3, SLOT_COMPLETED = 4;

uint64_t elapsed(uint64_t now, uint64_t t) { return now > t ? now - t : 0; }
constexpr uint64_t RENT_ATA_2022 = 2'074'080;  // 170-byte Token-2022 ATA (ImmutableOwner)
constexpr uint64_t RENT_ATA_SPL = 2'039'280;   // 165-byte token account
constexpr uint64_t SIGNATURE_FEE = 5'000;
constexpr uint64_t BLOCKHASH_MAX_AGE_NS = 60'000'000'000ULL;
constexpr uint64_t DEFAULT_MINT_SUPPLY = 1'000'000'000'000'000ULL;

size_t pow2_at_least(size_t n) {
  size_t p = 16;
  while (p < n) p <<= 1;
  return p;
}

bool ends_with(std::string_view s, std::string_view suffix) {
  return s.size() >= suffix.size() && s.substr(s.size() - suffix.size()) == suffix;
}

}  // namespace

void B58Key::set(const Pubkey& k) { len = static_cast<uint8_t>(b58_encode(k.data(), 32, s)); }

Engine::Engine(const Config& cfg, Shared& sh, const Keypair& wallet, FrameRing* ring)
    : cfg_(cfg),
      sh_(sh),
      wallet_(wallet),
      ring_(ring),
      curves_(pow2_at_least(size_t(cfg.racer.max_curves) * 2)),
      mint_index_(pow2_at_least(size_t(cfg.racer.max_curves) * 2)),
      launches_(1u << 19),
      watches_(4096),
      blacklist_mints_(pow2_at_least(cfg.screen.blacklist_mints.size() * 2 + 16)),
      blacklist_creators_(pow2_at_least(cfg.screen.blacklist_creators.size() * 2 + 16)),
      max_positions_(std::min<int>(cfg.risk.max_concurrent_positions, int(MAX_POSITIONS))),
      view_(new UpdateView),
      ixs_(new IxList),
      tx_(new SignedTx),
      wall_ms_(&pd::wall_ms),
      now_ns_(&pd::now_ns) {
  derive_user_keys(wallet.pubkey(), user_);
  user_b58_.set(user_.user);
  launch_fifo_cap_ = (size_t(1) << 19) * 7 / 10;
  launch_fifo_.reset(new Pubkey[launch_fifo_cap_]);
  for (auto& m : cfg.screen.blacklist_mints) *blacklist_mints_.upsert(parse_pubkey(m)) = 1;
  for (auto& c : cfg.screen.blacklist_creators) *blacklist_creators_.upsert(parse_pubkey(c)) = 1;
  for (auto& t : cfg.sender.tip_accounts) sender_tips_.push_back(parse_pubkey(t));
  for (auto& t : cfg.jito.tip_accounts) jito_tips_.push_back(parse_pubkey(t));
}

// ---------------------------------------------------------------------------
// main loop

void Engine::run() {
  uint64_t seq = 0;
  uint32_t since_poll = 0;
  while (!sh_.stop.load(std::memory_order_relaxed)) {
    const uint8_t* data;
    uint32_t len;
    uint64_t recv_ns;
    if (ring_->peek(seq, data, len, recv_ns)) {
      on_frame(data, len, recv_ns);
      ring_->release(++seq);
      if (++since_poll >= 16) {
        since_poll = 0;
        poll(now_ns_());
      }
    } else {
      since_poll = 0;
      poll(now_ns_());
      cpu_relax();
    }
  }
}

void Engine::on_frame(const uint8_t* data, size_t len, uint64_t recv_ns) {
  UpdateView& u = *view_;
  if (!parse_update(data, len, u)) {
    sh_.counters.parse_errors.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  last_frame_ns_ = recv_ns;
  switch (u.kind) {
    case UpdateView::Account:
      if (u.account.pubkey.size() != 32) return;
      if (u.has_filter("watch") || watches_.find(key_from(pb::u8(u.account.pubkey))))
        on_watched(u.account);
      else if (u.has_filter_prefix("band"))
        on_curve(u.account);
      break;
    case UpdateView::Transaction:
      if (u.tx.signature.size() != 64) return;
      if (u.has_filter("wallet")) on_wallet_tx(u.tx);
      if (u.has_filter("migrate")) on_migrate(u.tx, recv_ns);
      if (u.has_filter("creates")) on_create(u.tx);
      break;
    case UpdateView::Slot:
      on_slot(u.slot);
      break;
    default:
      break;
  }
}

void Engine::on_slot(const SlotView& s) {
  // Inter-slot statuses only time the slot; last_slot_ stays the processed tip.
  if (s.status == SLOT_FIRST_SHRED_RECEIVED || s.status == SLOT_COMPLETED) {
    SlotTimes& t = slot_times_[s.slot & 63];
    if (t.slot != s.slot) t = SlotTimes{s.slot, 0, 0};
    uint64_t& at = s.status == SLOT_FIRST_SHRED_RECEIVED ? t.first_shred_ns : t.completed_ns;
    if (!at) at = last_frame_ns_;
    return;
  }
  if (s.status != SLOT_PROCESSED) return;
  if (s.slot > last_slot_) {
    if (last_slot_ && s.slot - last_slot_ > uint64_t(cfg_.stream.gap_slot_jump)) {
      Event* e = ev(Ev::Gap);
      e->slot = s.slot;
      e->i[0] = int64_t(last_slot_);
      std::snprintf(e->text, sizeof e->text, "slot jump %llu -> %llu", (unsigned long long)last_slot_,
                    (unsigned long long)s.slot);
      emit();
    }
    last_slot_ = s.slot;
    sh_.counters.last_slot.store(s.slot, std::memory_order_relaxed);
  }
}

// ---------------------------------------------------------------------------
// curves

void Engine::on_curve(const AccountView& a) {
  Curve c;
  if (!decode_curve(pb::u8(a.data), a.data.size(), c)) return;
  Pubkey key = key_from(pb::u8(a.pubkey));
  double cost = completion_cost_sol(c, cfg_.racer.curve_fee_bps);
  CurveSlot* s = curves_.find(key);
  if (!s) {
    if (cost < 0 || cost > cfg_.racer.admit_cost_sol) return;
    s = admit(key, c, a);
    if (!s) return;
  }
  s->cost_sol = cost;
  s->last_update_ns = now_ns_();
  if (a.txn_signature.size() == 64) std::memcpy(s->last_sig.data(), a.txn_signature.data(), 64);
  if (c.has_creator && is_zero(s->creator)) {
    s->creator = c.creator;
    if (blacklist_creators_.find(c.creator)) s->blacklisted = true;
  }
  s->mayhem = c.mayhem;
  s->cashback = c.cashback;
  if (!s->fired && cost >= 0 && cost <= cfg_.racer.max_completion_cost_sol) fire(*s, a.slot);
  if (c.complete && c.real_sol > 0 && !s->complete) {
    s->complete = true;
    s->complete_ns = now_ns_();
  }
}

CurveSlot* Engine::admit(const Pubkey& key, const Curve& c, const AccountView& a) {
  if (curves_.size() >= size_t(cfg_.racer.max_curves)) return nullptr;
  bool created = false;
  CurveSlot* s = curves_.upsert(key, &created);
  if (!s) return nullptr;
  s->curve = key;
  if (c.has_creator) s->creator = c.creator;
  s->mayhem = c.mayhem;
  s->cashback = c.cashback;
  s->admitted_ns = now_ns_();
  if (c.has_creator && blacklist_creators_.find(c.creator)) s->blacklisted = true;
  sh_.counters.curves.fetch_add(1, std::memory_order_relaxed);

  if (const Launch* L = launches_.find(key)) {
    TransferFee fee = (L->token_program == prog::TOKEN || L->no_fee_proven) ? TransferFee::None : TransferFee::Unknown;
    set_mint(*s, L->mint, L->token_program, fee, 0);
    if (fee == TransferFee::Unknown) {
      AuxReq r;
      r.op = AuxOp::ReadMint;
      r.curve = key;
      r.mint = L->mint;
      sh_.aux_reqs.push(r);
    }
  } else {
    // Created before boot: resolve curve -> mint off-thread from the tx that wrote it.
    s->resolving = true;
    ++unresolved_curves_;
    AuxReq r;
    r.op = AuxOp::ResolveCurve;
    r.curve = key;
    if (a.txn_signature.size() == 64) std::memcpy(r.sig.data(), a.txn_signature.data(), 64);
    if (!sh_.aux_reqs.push(r)) warn("aux queue full: curve resolve dropped");
  }
  Event* e = ev(Ev::CurveAdmit);
  e->slot = a.slot;
  e->a = key;
  e->b = s->mint;
  e->d[0] = completion_cost_sol(c, cfg_.racer.curve_fee_bps);
  e->i[0] = s->mint_known;
  emit();
  return s;
}

void Engine::set_mint(CurveSlot& s, const Pubkey& mint, const Pubkey& token_program, TransferFee fee, uint64_t supply) {
  if (!s.mint_known) {
    s.mint = mint;
    s.mint_known = true;
    s.mint_b58.set(mint);
    s.pump_suffix = ends_with(s.mint_b58.sv(), "pump");
    if (blacklist_mints_.find(mint)) s.blacklisted = true;
    if (Pubkey* c = mint_index_.upsert(mint)) *c = s.curve;
  }
  if (!is_zero(token_program)) s.token_program = token_program;
  if (fee != TransferFee::Unknown) s.fee = fee;
  if (supply) s.mint_supply = supply;
  if (s.resolving) {
    s.resolving = false;
    if (unresolved_curves_) --unresolved_curves_;
  }
  if (s.fired && !s.keys_ready && !is_zero(s.token_program)) prepare_keys(s);
}

void Engine::fire(CurveSlot& s, uint64_t slot) {
  s.fired = true;
  s.fired_ns = now_ns_();
  s.fire_slot = slot;
  sh_.counters.fires.fetch_add(1, std::memory_order_relaxed);
  if (s.mint_known && !is_zero(s.token_program)) prepare_keys(s);
  Event* e = ev(Ev::CurveFire);
  e->slot = slot;
  e->a = s.curve;
  e->b = s.mint;
  e->d[0] = s.cost_sol;
  e->i[0] = s.keys_ready;
  emit();
}

void Engine::prepare_keys(CurveSlot& s) {
  derive_swap_keys(user_, s.mint, s.token_program, s.creator, s.cashback, s.mayhem, s.keys);
  s.keys_ready = true;
  s.pool_b58.set(s.keys.pool);
  watch(s.keys.pool, s.curve, ROLE_POOL);
  watch(s.keys.pool_base_vault, s.curve, ROLE_BASE_VAULT);
  watch(s.keys.pool_quote_vault, s.curve, ROLE_QUOTE_VAULT);
}

void Engine::drop_curve(const Pubkey& key) {
  CurveSlot* s = curves_.find(key);
  if (!s) return;
  if (s->keys_ready) {
    unwatch(s->keys.pool);
    unwatch(s->keys.pool_base_vault);
    unwatch(s->keys.pool_quote_vault);
  }
  if (s->mint_known) mint_index_.erase(s->mint);
  if (s->resolving && unresolved_curves_) --unresolved_curves_;
  curves_.erase(key);
}

void Engine::expire(uint64_t now) {
  Pubkey drop[256];
  size_t n = 0;
  const uint64_t window = uint64_t(cfg_.racer.track_window_minutes) * 60'000'000'000ULL;
  const uint64_t no_pool = uint64_t(cfg_.racer.complete_no_pool_sec) * 1'000'000'000ULL;
  curves_.for_each([&](const Pubkey& k, CurveSlot& s) {
    if (n >= 256) return;
    for (const Position& p : positions_)
      if (p.state != PosState::Empty && p.curve == k) return;  // a position still reads this curve's pool
    bool done = (s.traded || s.migrated) && s.migrated_ns && now - s.migrated_ns > 60'000'000'000ULL;
    bool stale_complete = s.complete && !s.migrated && now - s.complete_ns > no_pool;
    bool too_old = now - s.admitted_ns > window;
    if (done || stale_complete || too_old) drop[n++] = k;
  });
  for (size_t i = 0; i < n; ++i) drop_curve(drop[i]);
}

// ---------------------------------------------------------------------------
// watches

void Engine::watch(const Pubkey& k, const Pubkey& curve, uint8_t role) {
  bool created = false;
  WatchRef* w = watches_.upsert(k, &created);
  if (!w) {
    warn("watch table full");
    return;
  }
  if (created) {
    w->curve = curve;
    w->role = role;
    w->users = 1;
    WatchCmd c;
    c.add = true;
    c.key = k;
    if (!sh_.watch_cmds.push(c)) warn("watch queue full");
  } else {
    ++w->users;
    if (is_zero(w->curve)) w->curve = curve;
  }
}

void Engine::unwatch(const Pubkey& k) {
  WatchRef* w = watches_.find(k);
  if (!w) return;
  if (w->users > 1) {
    --w->users;
    return;
  }
  watches_.erase(k);
  WatchCmd c;
  c.add = false;
  c.key = k;
  if (!sh_.watch_cmds.push(c)) warn("watch queue full");
}

void Engine::on_watched(const AccountView& a) {
  Pubkey key = key_from(pb::u8(a.pubkey));
  WatchRef* w = watches_.find(key);
  if (!w) return;
  uint64_t now = now_ns_();
  CurveSlot* s = is_zero(w->curve) ? nullptr : curves_.find(w->curve);

  if (w->role == ROLE_POOL) {
    Pool pool;
    if (!decode_pool(pb::u8(a.data), a.data.size(), pool)) return;
    if (s && s->keys_ready && pool.base_mint == s->mint) {
      if (s->traded && !s->pool_seen && pool.virtual_quote != cfg_.racer.default_virtual_quote_lamports) {
        // The trade priced off the default virtual quote; record how far off it was.
        warn("virtual quote %llu != default %llu", (unsigned long long)pool.virtual_quote,
             (unsigned long long)cfg_.racer.default_virtual_quote_lamports);
      }
      s->pool = pool;
      s->pool_seen = true;
      if (pool.coin_creator != s->keys.coin_creator) set_coin_creator(s->keys, pool.coin_creator);
      s->keys.cashback = pool.cashback;
      s->keys.mayhem = pool.mayhem;
    }
    for (size_t i = 0; i < positions_.size(); ++i) {
      Position& p = positions_[i];
      if (p.state == PosState::Empty || p.keys.pool != key || pool.base_mint != p.mint) continue;
      p.virtual_quote = pool.virtual_quote;
      if (pool.coin_creator != p.keys.coin_creator) set_coin_creator(p.keys, pool.coin_creator);
      p.keys.cashback = pool.cashback;
      p.keys.mayhem = pool.mayhem;
    }
  } else {
    uint64_t amount;
    if (!decode_token_amount(pb::u8(a.data), a.data.size(), amount)) return;
    if (s) {
      if (w->role == ROLE_BASE_VAULT) { s->base_amount = amount; s->base_seen = true; }
      else { s->quote_amount = amount; s->quote_seen = true; }
    }
    for (size_t i = 0; i < positions_.size(); ++i) {
      Position& p = positions_[i];
      if (p.state == PosState::Empty) continue;
      bool hit = false;
      if (p.keys.pool_base_vault == key) { p.base_res = amount; hit = true; }
      if (p.keys.pool_quote_vault == key) {
        p.quote_res = amount;
        hit = true;
        // Count other txs' writes to the pool between our buy send and our own write.
        if (p.state == PosState::BuySent && p.writes_ahead < 0 && a.txn_signature.size() == 64) {
          if (std::memcmp(a.txn_signature.data(), p.buy_sig.data(), 64) == 0) p.writes_ahead = p.writes_counting;
          else ++p.writes_counting;
        }
      }
      if (hit) {
        mark(p, now);
        maybe_exit(p, int(i), now);
      }
    }
  }

  if (s && s->fired && !s->traded) try_trigger(*s, s->awaiting_pool ? s->await_recv_ns : last_frame_ns_, TriggerSrc::PoolPush);
}

void Engine::try_trigger(CurveSlot& s, uint64_t recv_ns, TriggerSrc src) {
  if (!s.keys_ready || !s.pool_seen || !s.base_seen || !s.quote_seen || s.base_amount == 0 || s.quote_amount == 0) return;
  s.awaiting_pool = false;
  s.migrated = true;
  if (!s.migrated_ns) s.migrated_ns = now_ns_();
  trigger(s, s.base_amount, s.quote_amount, s.pool.virtual_quote, recv_ns, src);
}

// ---------------------------------------------------------------------------
// creates and migrations

void Engine::on_create(const TxView& tx) {
  LaunchInfo L = parse_launch(tx);
  if (!L.ok) return;
  Pubkey curve, mint, tp{};
  if (!b58_decode32(L.curve, curve) || !b58_decode32(L.mint, mint)) return;
  if (!L.token_program.empty()) b58_decode32(L.token_program, tp);
  bool created = false;
  Launch* l = launches_.upsert(curve, &created);
  if (!l) {
    // Full: evict the oldest launches until there is room.
    for (int i = 0; i < 64 && launch_fifo_size_; ++i) {
      launches_.erase(launch_fifo_[launch_fifo_head_]);
      launch_fifo_head_ = (launch_fifo_head_ + 1) % launch_fifo_cap_;
      --launch_fifo_size_;
    }
    l = launches_.upsert(curve, &created);
    if (!l) return;
  }
  l->mint = mint;
  l->token_program = tp;
  l->no_fee_proven = L.no_transfer_fee;
  if (created) {
    if (launch_fifo_size_ == launch_fifo_cap_) {
      launches_.erase(launch_fifo_[launch_fifo_head_]);
      launch_fifo_head_ = (launch_fifo_head_ + 1) % launch_fifo_cap_;
      --launch_fifo_size_;
    }
    launch_fifo_[(launch_fifo_head_ + launch_fifo_size_) % launch_fifo_cap_] = curve;
    ++launch_fifo_size_;
  }
  // A curve admitted before its create arrived (rare; same-slot creates) resolves now.
  if (CurveSlot* s = curves_.find(curve); s && !s->mint_known) {
    TransferFee fee = (tp == prog::TOKEN || L.no_transfer_fee) ? TransferFee::None : TransferFee::Unknown;
    set_mint(*s, mint, tp, fee, 0);
  }
}

void Engine::on_migrate(const TxView& tx, uint64_t recv_ns) {
  MigrateInfo m = parse_migrate(tx);
  if (!m.is_migrate) return;
  sh_.counters.migrates.fetch_add(1, std::memory_order_relaxed);

  CurveSlot* s = nullptr;
  std::string_view mint_sv;
  for (uint8_t i = 0; i < m.n_mints && !s; ++i) {
    Pubkey mint;
    if (!b58_decode32(m.mints[i], mint)) continue;
    if (const Pubkey* curve = mint_index_.find(mint)) s = curves_.find(*curve);
    if (!s && unresolved_curves_ > 0) {
      // Admitted with the mint still unresolved: derive its curve and resolve inline.
      Pubkey curve;
      if (find_program_address({std::string_view("bonding-curve"), mint}, prog::PUMP_FUN, curve)) {
        if (CurveSlot* c = curves_.find(curve)) {
          Pubkey tp{};
          std::string_view tps = token_program_of(tx, m.mints[i]);
          if (!tps.empty()) b58_decode32(tps, tp);
          set_mint(*c, mint, tp, tp == prog::TOKEN ? TransferFee::None : TransferFee::Unknown, 0);
          s = c;
        }
      }
    }
    if (s) mint_sv = m.mints[i];
  }

  Event* e = ev(Ev::Migrate);
  e->slot = tx.slot;
  std::memcpy(e->sig.data(), tx.signature.data(), 64);
  if (s) e->a = s->mint;
  if (m.caller.size() == 32) e->b = key_from(pb::u8(m.caller));
  e->i[0] = m.landed;
  e->i[1] = s != nullptr;
  e->i[2] = s && s->fired;
  e->i[3] = m.curve_buy_before;
  e->i[4] = m.pool_buy_after;
  emit();

  if (!s || !m.landed) return;
  s->migrated = true;
  if (!s->migrated_ns) s->migrated_ns = now_ns_();
  if (s->traded) return;
  if (!s->fired) {
    s->traded = true;
    Event* d = ev(Ev::Decision);
    d->slot = tx.slot;
    d->a = s->mint;
    std::snprintf(d->text, sizeof d->text, "not_fired");
    emit();
    return;
  }
  if (!s->keys_ready) {
    if (is_zero(s->token_program)) {
      std::string_view tps = token_program_of(tx, mint_sv);
      if (!tps.empty()) b58_decode32(tps, s->token_program);
    }
    if (is_zero(s->token_program)) {
      s->traded = true;
      warn("migrate for fired curve without a token program");
      return;
    }
    prepare_keys(*s);
  }
  // The pool the migrate created: the largest holder of the mint after the tx.
  std::string_view holder = largest_holder(tx, mint_sv);
  if (holder != s->pool_b58.sv()) {
    s->traded = true;
    Event* d = ev(Ev::Decision);
    d->slot = tx.slot;
    d->a = s->mint;
    std::snprintf(d->text, sizeof d->text, "non_canonical_pool");
    emit();
    sh_.counters.vetoes.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  uint64_t base = sum_tokens(tx.post_tokens, tx.n_post_tokens, mint_sv, holder);
  uint64_t quote = sum_tokens(tx.post_tokens, tx.n_post_tokens, known_b58().wsol_sv(), holder);
  if (base == 0 || quote == 0) {
    s->traded = true;
    Event* d = ev(Ev::Decision);
    d->slot = tx.slot;
    d->a = s->mint;
    std::snprintf(d->text, sizeof d->text, "non_wsol_quote");
    emit();
    return;
  }
  // The tx's post balances are the vaults as of this slot; later vault pushes overwrite them.
  if (!s->base_seen) { s->base_amount = base; s->base_seen = true; }
  if (!s->quote_seen) { s->quote_amount = quote; s->quote_seen = true; }
  if (s->pool_seen) {
    try_trigger(*s, recv_ns, TriggerSrc::MigrateTx);
  } else {
    s->awaiting_pool = true;
    s->await_since_ns = now_ns_();
    s->await_recv_ns = recv_ns;
    ++awaiting_pools_;
  }
}

// ---------------------------------------------------------------------------
// trigger: screen, size, build, sign, send

int Engine::free_position() const {
  for (size_t i = 0; i < positions_.size(); ++i)
    if (positions_[i].state == PosState::Empty) return int(i);
  return -1;
}

int Engine::active_positions() const {
  int n = 0;
  for (const Position& p : positions_) n += p.state != PosState::Empty;
  return n;
}

TxParams Engine::tx_params(bool exit) {
  TxParams p;
  p.cu_limit = exit ? cfg_.exits.cu_limit : cfg_.entry.cu_limit;
  uint64_t price = sh_.cu_price.load(std::memory_order_relaxed);
  if (!price) price = cfg_.fees.cu_price_default;
  price = uint64_t(double(price) * (exit ? cfg_.exits.exit_priority_multiplier : cfg_.entry.priority_multiplier));
  p.cu_price = price;
  if (!sender_tips_.empty()) {
    p.tips[p.n_tips++] = Tip{sender_tips_[tip_rr_ % sender_tips_.size()], exit ? cfg_.sender.exit_tip_lamports : cfg_.sender.tip_lamports};
  }
  if (cfg_.jito.enabled && !jito_tips_.empty()) p.tips[p.n_tips++] = Tip{jito_tips_[tip_rr_ % jito_tips_.size()], cfg_.jito.tip_lamports};
  ++tip_rr_;
  // Rotate fee recipients (write-lock spread), skipping unset entries.
  for (int i = 0; i < 8; ++i) {
    uint8_t idx = uint8_t((fee_rr_ + uint32_t(i)) & 7);
    if (!is_zero(amm_.protocol_recipients[idx])) { p.fee_recipient_index = idx; break; }
  }
  for (int i = 0; i < 8; ++i) {
    uint8_t idx = uint8_t((fee_rr_ + uint32_t(i)) & 7);
    if (!is_zero(amm_.buyback_recipients[idx])) { p.buyback_index = idx; break; }
  }
  ++fee_rr_;
  return p;
}

bool Engine::send_packet(Side side, uint8_t pos, uint8_t attempt, const IxList& ixs, uint64_t recv_ns, Signature& sig_out) {
  BlockhashSnap bh;
  if (!sh_.blockhash.load(bh) || bh.fetched_ns == 0) return false;
  if (!build_signed_tx(wallet_, ixs, bh.hash, *tx_)) return false;
  Packet* pk = sh_.packets.claim();
  if (!pk) {
    sh_.counters.packets_dropped.fetch_add(1, std::memory_order_relaxed);
    return false;
  }
  pk->side = side;
  pk->pos = pos;
  pk->attempt = attempt;
  pk->sig = tx_->sig;
  pk->t_recv_ns = recv_ns;
  pk->b64_len = static_cast<uint16_t>(b64_encode(tx_->bytes, tx_->len, pk->b64));
  pk->t_signed_ns = now_ns_();
  sh_.packets.publish();
  sig_out = tx_->sig;
  return true;
}

void Engine::trigger(CurveSlot& s, uint64_t base_res, uint64_t quote_res, uint64_t virt, uint64_t recv_ns, TriggerSrc src) {
  s.traded = true;
  sh_.counters.triggers.fetch_add(1, std::memory_order_relaxed);
  uint64_t t_decide = now_ns_();
  const char* veto = nullptr;
  double pool_sol = double(quote_res) / 1e9;
  double effective_sol = double(quote_res + virt) / 1e9;
  uint64_t size = 0, need = 0;
  BuyQuote q{};
  double impact = 0;

  refresh_amm();
  BlockhashSnap bh;
  bool have_bh = sh_.blockhash.load(bh) && bh.fetched_ns && t_decide - bh.fetched_ns < BLOCKHASH_MAX_AGE_NS;
  if (kill_latched_ || sh_.kill_file.load(std::memory_order_relaxed)) veto = "kill";
  else if (!amm_.valid) veto = "amm_config_missing";
  else if (amm_.disable_flags & (1u << 3)) veto = "buys_disabled";
  else if (!have_bh) veto = "blockhash_stale";
  else if (s.blacklisted) veto = "blacklist";
  else if (cfg_.screen.require_pump_suffix && !s.pump_suffix) veto = "no_pump_suffix";
  else if (s.token_program == prog::TOKEN_2022 && s.fee == TransferFee::Present) veto = "t22_transfer_fee";
  else if (s.token_program == prog::TOKEN_2022 && s.fee != TransferFee::None) veto = "t22_fee_unverified";
  else if (s.pool_seen && s.pool.data_len < POOL_ACCOUNT_NEW_SIZE) veto = "pool_needs_extend";
  else if (pool_sol < cfg_.screen.min_pool_sol) veto = "pool_below_min";
  else if (pool_sol > cfg_.screen.max_pool_sol) veto = "pool_above_max";
  else if (active_positions() >= max_positions_ || free_position() < 0) veto = "max_concurrent";

  if (!veto) {
    uint64_t avail = wallet_lamports_ > reserved_ ? wallet_lamports_ - reserved_ : 0;
    size = uint64_t(double(avail) * cfg_.entry.size_wallet_pct / 100.0);
    if (cfg_.entry.max_size_sol > 0) size = std::min<uint64_t>(size, uint64_t(cfg_.entry.max_size_sol * 1e9));
    FeeBps fees = compute_fees_bps(amm_, true, s.mint_supply ? s.mint_supply : DEFAULT_MINT_SUPPLY, base_res,
                                   quote_res + virt, s.keys.mayhem, s.pool_seen ? s.pool.creator_fee_bps : 0);
    q = buy_quote_input(size, cfg_.entry.slippage_pct, base_res, quote_res, virt, fees, is_zero(s.keys.coin_creator));
    TxParams tp = tx_params(false);
    uint64_t tips = 0;
    for (uint8_t i = 0; i < tp.n_tips; ++i) tips += tp.tips[i].lamports;
    uint64_t priority = uint64_t(tp.cu_limit) * tp.cu_price / 1'000'000;
    uint64_t base_rent = s.token_program == prog::TOKEN_2022 ? RENT_ATA_2022 : RENT_ATA_SPL;
    need = q.max_quote + base_rent + RENT_ATA_SPL + tips + priority + SIGNATURE_FEE;
    impact = effective_sol > 0 ? double(size) / 1e9 / effective_sol * 100.0 : 100.0;
    uint64_t headroom = uint64_t(cfg_.risk.gas_headroom_sol * 1e9) + uint64_t(cfg_.risk.wallet_floor_sol * 1e9);
    if (double(size) < cfg_.entry.min_size_sol * 1e9) veto = "size_below_min";
    else if (!q.ok) veto = "quote_failed";
    else if (impact > cfg_.screen.max_buy_impact_pct) veto = "buy_impact";
    else if (wallet_lamports_ < reserved_ + need + headroom) veto = "insufficient_balance";
    else {
      int idx = free_position();
      buy_ixs(*ixs_, user_, s.keys, amm_, tp, q.base_out, q.max_quote);
      Signature sig;
      if (!send_packet(Side::Buy, uint8_t(idx), 0, *ixs_, recv_ns, sig)) {
        veto = "build_or_queue_failed";
      } else {
        Position& p = positions_[size_t(idx)];
        p = Position{};
        p.state = PosState::BuySent;
        p.curve = s.curve;
        p.mint = s.mint;
        p.mint_b58 = s.mint_b58;
        p.keys = s.keys;
        p.buy_sig = sig;
        p.buy_sent_ns = now_ns_();
        p.buy_sent_ms = wall_ms_();
        p.order_slot = last_slot_;
        p.size_lamports = size;
        p.max_quote = q.max_quote;
        p.expected_base = q.base_out;
        p.reserved = need;
        p.overhead = tips + priority + SIGNATURE_FEE;
        p.base_res = base_res;
        p.quote_res = quote_res;
        p.virtual_quote = virt;
        reserved_ += need;
        watch(s.keys.pool, s.curve, ROLE_POOL);
        watch(s.keys.pool_base_vault, s.curve, ROLE_BASE_VAULT);
        watch(s.keys.pool_quote_vault, s.curve, ROLE_QUOTE_VAULT);
        sh_.counters.buys.fetch_add(1, std::memory_order_relaxed);

        Event* o = ev(Ev::Order);
        o->slot = last_slot_;
        o->sig = sig;
        o->a = s.mint;
        o->b = s.keys.pool;
        o->i[0] = int64_t(Side::Buy);
        o->i[1] = int64_t(size);
        o->i[2] = int64_t(q.max_quote);
        o->i[3] = int64_t(q.base_out);
        o->i[4] = int64_t(tp.cu_price);
        o->i[5] = int64_t(tp.tips[0].lamports);
        o->i[6] = 0;
        o->i[7] = int64_t(p.buy_sent_ns - recv_ns);
        // Enough to restore the position if the process stops before the fill is seen.
        o->c = s.keys.coin_creator;
        o->d[3] = s.keys.base_token_program == prog::TOKEN_2022;
        o->d[4] = s.keys.cashback;
        o->d[5] = s.keys.mayhem;
        std::snprintf(o->text, sizeof o->text, "%s",
                      src == TriggerSrc::MigrateTx ? "migrate_tx" : src == TriggerSrc::PoolPush ? "pool_push" : "pool_wait_timeout");
        emit();
      }
    }
  }

  Event* d = ev(Ev::Decision);
  d->slot = last_slot_;
  d->a = s.mint;
  d->b = s.keys.pool;
  d->d[0] = double(size) / 1e9;
  d->d[1] = pool_sol;
  d->d[2] = impact;
  d->d[3] = s.cost_sol;
  d->d[4] = double(virt) / 1e9;
  d->i[0] = int64_t(t_decide - recv_ns);  // frame arrival -> decision
  d->i[1] = int64_t(now_ns_() - recv_ns); // frame arrival -> packet queued (or veto)
  d->i[2] = int64_t(src);
  d->i[3] = s.pool_seen;
  d->i[4] = int64_t(base_res);
  d->i[5] = int64_t(quote_res);
  std::snprintf(d->text, sizeof d->text, "%s", veto ? veto : "buy");
  emit();
  if (veto) sh_.counters.vetoes.fetch_add(1, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------
// positions

void Engine::mark(Position& p, uint64_t) {
  if (p.base_res == 0) return;
  p.last_price = pool_price_sol(p.base_res, p.quote_res, p.virtual_quote);
  p.marked = p.last_price > 0;
}

void Engine::maybe_exit(Position& p, int idx, uint64_t) {
  if (p.state != PosState::Open || !p.marked) return;
  exit_observe(p.exit, p.last_price, cfg_.exits);
  if (p.sell_attempts >= cfg_.exits.max_exit_attempts) return;  // exits exhausted: kill latched, hands off
  ExitDecision d = evaluate_exit(p.exit, p.last_price, wall_ms_(), cfg_.exits);
  if (d.trigger == ExitTrigger::None) return;
  send_sell(p, idx, d.trigger, d.sell_fraction, last_frame_ns_);
}

void Engine::send_sell(Position& p, int idx, ExitTrigger t, double fraction, uint64_t recv_ns) {
  const auto& tiers = is_protective(t) ? cfg_.exits.protective_slippage_tiers : cfg_.exits.take_profit_slippage_tiers;
  double slip = tiers[std::min<size_t>(size_t(p.sell_attempts), tiers.size() - 1)];
  bool full = fraction >= 0.999;
  uint64_t base_in = full ? p.base_amount : std::min<uint64_t>(p.base_amount, uint64_t(std::llround(double(p.base_bought) * fraction)));
  if (base_in >= p.base_amount) { base_in = p.base_amount; full = true; }
  if (base_in == 0) {
    close_position(idx, "nothing_to_sell");
    return;
  }
  refresh_amm();
  FeeBps fees = compute_fees_bps(amm_, true, DEFAULT_MINT_SUPPLY, p.base_res, p.quote_res + p.virtual_quote,
                                 p.keys.mayhem, 0);
  SellQuote q = sell_base_input(base_in, slip, p.base_res, p.quote_res, p.virtual_quote, fees, is_zero(p.keys.coin_creator));
  // A real vault that can't pay the quote means liquidity is gone: salvage what's there.
  uint64_t min_quote = q.ok ? q.min_quote : 0;
  TxParams tp = tx_params(true);
  sell_ixs(*ixs_, user_, p.keys, amm_, tp, base_in, min_quote, full);
  Signature sig;
  if (!send_packet(Side::Sell, uint8_t(idx), uint8_t(p.sell_attempts), *ixs_, recv_ns, sig)) {
    warn("sell build/queue failed (pos %d)", idx);
    p.sell_sent_ns = now_ns_();  // retry on the exit timer
    p.state = PosState::SellSent;
    p.sell_trigger = t;
    p.sell_base = base_in;
    p.sell_full = full;
    return;
  }
  p.sell_sigs[p.n_sell_sigs % 8] = sig;
  ++p.n_sell_sigs;
  p.state = PosState::SellSent;
  p.sell_sent_ns = now_ns_();
  p.order_slot = last_slot_;
  p.sell_trigger = t;
  p.sell_base = base_in;
  p.sell_full = full;
  sh_.counters.sells.fetch_add(1, std::memory_order_relaxed);

  Event* o = ev(Ev::Order);
  o->slot = last_slot_;
  o->sig = sig;
  o->a = p.mint;
  o->b = p.keys.pool;
  o->i[0] = int64_t(Side::Sell);
  o->i[1] = int64_t(base_in);
  o->i[2] = int64_t(min_quote);
  o->i[3] = int64_t(q.final_quote);
  o->i[4] = int64_t(tp.cu_price);
  o->i[5] = int64_t(tp.tips[0].lamports);
  o->i[6] = p.sell_attempts;
  o->i[7] = int64_t(now_ns_() - recv_ns);
  o->d[0] = p.last_price;
  o->d[1] = gain_pct(p.exit.entry_price, p.last_price);
  o->d[2] = slip;
  std::snprintf(o->text, sizeof o->text, "%s", exit_trigger_name(t));
  emit();
}

void Engine::fill_landing(Event* f, uint64_t order_slot, uint64_t slot, int64_t tx_index, uint64_t seen_ns) {
  f->i[5] = tx_index;
  f->i[6] = seen_ns && order_slot && slot >= order_slot ? int64_t(slot - order_slot) : -1;
  f->i[7] = 0;
  f->d[2] = 0;
  f->d[5] = 1;  // landing fields present (older fill rows have zeros here)
  const SlotTimes& t = slot_times_[slot & 63];
  if (seen_ns && t.slot == slot) {
    if (t.first_shred_ns) f->i[7] = int64_t(seen_ns) - int64_t(t.first_shred_ns);
    if (t.completed_ns) f->d[2] = double(int64_t(seen_ns) - int64_t(t.completed_ns)) / 1e6;
  }
}

void Engine::on_buy_result(int idx, bool ok, int64_t sol_delta, int64_t base_delta, uint64_t slot, const char* how,
                           int64_t tx_index, uint64_t seen_ns) {
  Position& p = positions_[size_t(idx)];
  if (p.state != PosState::BuySent) return;
  reserved_ = reserved_ > p.reserved ? reserved_ - p.reserved : 0;
  p.reserved = 0;

  Event* f = ev(Ev::Fill);
  f->slot = slot;
  f->sig = p.buy_sig;
  f->a = p.mint;
  f->i[0] = int64_t(Side::Buy);
  f->i[1] = ok && base_delta > 0;
  f->i[2] = base_delta;
  f->i[3] = sol_delta;
  f->i[4] = int64_t(now_ns_() - p.buy_sent_ns);
  fill_landing(f, p.order_slot, slot, tx_index, seen_ns);
  f->d[1] = double(p.writes_ahead);
  std::snprintf(f->text, sizeof f->text, "%s", how);

  if (!ok || base_delta <= 0) {
    emit();
    p.sol_spent = -sol_delta;
    close_position(idx, ok ? "buy_no_tokens" : "buy_failed");
    return;
  }
  p.base_amount = p.base_bought = uint64_t(base_delta);
  p.sol_spent = -sol_delta;
  // Entry = what the swap itself cost per token: strip the base ATA rent (returned at close),
  // the network fee and tips, which are costs but not price.
  uint64_t base_rent = p.keys.base_token_program == prog::TOKEN_2022 ? RENT_ATA_2022 : RENT_ATA_SPL;
  int64_t swap = p.sol_spent - int64_t(base_rent) - int64_t(p.overhead);
  if (swap <= 0) swap = p.sol_spent;
  p.entry_price = (double(swap) / 1e9) / (double(p.base_amount) / 1e6);
  p.opened_ms = wall_ms_();
  exit_init(p.exit, p.entry_price, p.opened_ms, cfg_.exits);
  p.state = PosState::Open;
  f->d[0] = p.entry_price;
  emit();

  Event* o = ev(Ev::PositionOpen);
  o->slot = slot;
  o->sig = p.buy_sig;
  o->a = p.mint;
  o->b = p.curve;
  o->c = p.keys.coin_creator;
  o->i[0] = int64_t(p.base_amount);
  o->i[1] = p.sol_spent;
  o->i[2] = p.opened_ms;
  o->i[3] = p.keys.cashback;
  o->i[4] = p.keys.mayhem;
  o->i[5] = p.keys.base_token_program == prog::TOKEN_2022;
  o->d[0] = p.entry_price;
  emit();

  mark(p, now_ns_());
  maybe_exit(p, idx, now_ns_());
}

void Engine::on_sell_result(int idx, const Signature& sig, bool ok, int64_t sol_delta, int64_t base_delta, uint64_t slot,
                            const char* how, int64_t tx_index, uint64_t seen_ns) {
  Position& p = positions_[size_t(idx)];
  const bool send_rejected = std::strcmp(how, "send_rejected") == 0;
  if (p.state != PosState::SellSent && p.state != PosState::Open) return;
  Event* f = ev(Ev::Fill);
  f->slot = slot;
  f->sig = sig;
  f->a = p.mint;
  f->i[0] = int64_t(Side::Sell);
  f->i[1] = ok;
  f->i[2] = base_delta;
  f->i[3] = sol_delta;
  f->i[4] = int64_t(now_ns_() - p.sell_sent_ns);
  fill_landing(f, p.order_slot, slot, tx_index, seen_ns);
  f->d[0] = p.last_price;
  f->d[1] = -1;
  std::snprintf(f->text, sizeof f->text, "%s %s", how, exit_trigger_name(p.sell_trigger));
  emit();
  if (!ok) {
    if (p.state != PosState::SellSent) return;  // a stale attempt; the live one is still pending
    if (send_rejected) {
      // Never reached the chain (no connection, rejected request): an instant resend
      // meets the same wall. The exit timer resends after exit_retry_ms.
      p.sell_sent_ns = now_ns_();
      return;
    }
    // Failed on-chain: retry at once on the next slippage tier.
    if (++p.sell_attempts >= cfg_.exits.max_exit_attempts) {
      latch_kill("exit attempts exhausted");
      p.state = PosState::Open;
      return;
    }
    send_sell(p, idx, p.sell_trigger, p.sell_full ? 1.0 : double(p.sell_base) / double(std::max<uint64_t>(p.base_bought, 1)), last_frame_ns_);
    return;
  }
  uint64_t sold = base_delta < 0 ? uint64_t(-base_delta) : 0;
  p.base_amount = p.base_amount > sold ? p.base_amount - sold : 0;
  p.sol_received += sol_delta;
  exit_on_fill(p.exit, p.sell_trigger, cfg_.exits);
  if (p.base_amount == 0) {
    close_position(idx, exit_trigger_name(p.sell_trigger));
    return;
  }
  p.state = PosState::Open;
  p.sell_attempts = 0;
  Event* u = ev(Ev::PositionUpdate);
  u->slot = slot;
  u->a = p.mint;
  u->i[0] = int64_t(p.base_amount);
  u->i[1] = p.sol_received;
  u->i[2] = p.exit.tp0_done;
  u->i[3] = p.exit.tp1_done;
  emit();
}

void Engine::close_position(int idx, const char* reason) {
  Position& p = positions_[size_t(idx)];
  if (p.state == PosState::Empty) return;
  reserved_ = reserved_ > p.reserved ? reserved_ - p.reserved : 0;
  Event* c = ev(Ev::PositionClose);
  c->slot = last_slot_;
  c->a = p.mint;
  c->sig = p.buy_sig;
  int64_t pnl = p.sol_received - p.sol_spent;
  c->i[0] = pnl;
  c->i[1] = p.opened_ms ? wall_ms_() - p.opened_ms : 0;
  c->i[2] = p.sol_spent;
  c->i[3] = p.sol_received;
  c->i[4] = int64_t(p.base_amount);
  c->d[0] = double(pnl) / 1e9;
  c->d[1] = p.sol_spent > 0 ? double(pnl) / double(p.sol_spent) * 100.0 : 0;
  c->d[2] = p.entry_price;
  c->d[3] = p.last_price;
  std::snprintf(c->text, sizeof c->text, "%s", reason);
  emit();
  release_watches(p);
  p = Position{};
}

void Engine::release_watches(const Position& p) {
  if (is_zero(p.keys.pool)) return;
  unwatch(p.keys.pool);
  unwatch(p.keys.pool_base_vault);
  unwatch(p.keys.pool_quote_vault);
}

void Engine::latch_kill(const char* why) {
  if (kill_latched_) return;
  kill_latched_ = true;
  Event* e = ev(Ev::Kill);
  e->slot = last_slot_;
  std::snprintf(e->text, sizeof e->text, "%s", why);
  emit();
}

void Engine::on_wallet_tx(const TxView& tx) {
  int wi = tx.key_index(user_.user);
  int64_t sol_delta = 0;
  if (wi >= 0 && wi < tx.n_post_balances && wi < tx.n_pre_balances) {
    sol_delta = int64_t(tx.post_balances[wi]) - int64_t(tx.pre_balances[wi]);
    set_wallet_lamports(tx.post_balances[wi], tx.slot);
  }
  const uint8_t* sig = pb::u8(tx.signature);
  for (size_t i = 0; i < positions_.size(); ++i) {
    Position& p = positions_[i];
    if (p.state == PosState::Empty) continue;
    auto base_delta = [&]() {
      return int64_t(sum_tokens(tx.post_tokens, tx.n_post_tokens, p.mint_b58.sv(), user_b58_.sv())) -
             int64_t(sum_tokens(tx.pre_tokens, tx.n_pre_tokens, p.mint_b58.sv(), user_b58_.sv()));
    };
    if (p.state == PosState::BuySent && std::memcmp(sig, p.buy_sig.data(), 64) == 0) {
      on_buy_result(int(i), !tx.failed, sol_delta, base_delta(), tx.slot, "stream", tx.index, last_frame_ns_);
      return;
    }
    for (uint8_t k = 0; k < std::min<uint8_t>(p.n_sell_sigs, 8); ++k) {
      if (std::memcmp(sig, p.sell_sigs[k].data(), 64) == 0) {
        on_sell_result(int(i), p.sell_sigs[k], !tx.failed, sol_delta, base_delta(), tx.slot, "stream", tx.index,
                       last_frame_ns_);
        return;
      }
    }
  }
}

void Engine::set_wallet_lamports(uint64_t lamports, uint64_t slot) {
  if (slot < wallet_slot_) return;
  wallet_slot_ = slot;
  wallet_lamports_ = lamports;
}

bool Engine::restore_position(const RestoredPosition& r) {
  int idx = free_position();
  if (idx < 0) return false;
  Position& p = positions_[size_t(idx)];
  p = Position{};
  p.curve = r.curve;
  p.mint = r.mint;
  p.mint_b58.set(r.mint);
  derive_swap_keys(user_, r.mint, r.token_program, r.creator, r.cashback, r.mayhem, p.keys);
  p.base_amount = r.base_amount;
  p.base_bought = r.base_bought ? r.base_bought : r.base_amount;
  p.entry_price = r.entry_price;
  p.sol_spent = r.sol_spent;
  p.sol_received = r.sol_received;
  p.opened_ms = r.opened_ms;
  exit_init(p.exit, r.entry_price, r.opened_ms, cfg_.exits);
  if (r.tp0_done) exit_on_fill(p.exit, ExitTrigger::TakeProfit0, cfg_.exits);
  if (r.tp1_done) exit_on_fill(p.exit, ExitTrigger::TakeProfit1, cfg_.exits);
  p.state = PosState::Open;
  p.base_res = r.base_res;
  p.quote_res = r.quote_res;
  p.virtual_quote = r.virtual_quote;
  mark(p, now_ns_());
  watch(p.keys.pool, Pubkey{}, ROLE_POOL);
  watch(p.keys.pool_base_vault, Pubkey{}, ROLE_BASE_VAULT);
  watch(p.keys.pool_quote_vault, Pubkey{}, ROLE_QUOTE_VAULT);
  return true;
}

// ---------------------------------------------------------------------------
// timers and queues

void Engine::refresh_amm() {
  uint64_t v = sh_.amm.version();
  if (v != amm_version_) amm_version_ = sh_.amm.load(amm_);
}

void Engine::drain_queues(uint64_t now) {
  SendResult r;
  while (sh_.send_results.try_pop(r)) {
    Event* e = ev(Ev::SendAck);
    e->sig = r.sig;
    e->i[0] = int64_t(r.route);
    e->i[1] = r.http_status;
    e->i[2] = r.ok;
    e->i[3] = int64_t(r.t_written_ns - r.t_signed_ns);
    e->i[4] = int64_t(r.t_ack_ns - r.t_written_ns);
    e->i[5] = int64_t(r.side);
    e->i[6] = r.definitive;
    e->i[7] = r.rate_limited;
    std::snprintf(e->text, sizeof e->text, "%s", r.error);
    emit();
    // Only a definitive Sender rejection fails an order: a dropped connection may still
    // have delivered it (the confirm timeout resolves those), and Jito is a second path.
    if (r.ok || !r.definitive || r.route != Route::Sender) continue;
    Position& p = positions_[r.pos];
    if (r.side == Side::Buy && p.state == PosState::BuySent && p.buy_sig == r.sig) {
      on_buy_result(r.pos, false, 0, 0, last_slot_, "send_rejected");
    } else if (r.side == Side::Sell && p.state == PosState::SellSent && p.n_sell_sigs &&
               p.sell_sigs[(p.n_sell_sigs - 1) % 8] == r.sig) {
      on_sell_result(r.pos, r.sig, false, 0, 0, last_slot_, "send_rejected");
    }
  }

  AuxResp a;
  while (sh_.aux_resps.try_pop(a)) {
    if (a.op == AuxOp::ResolveCurve || a.op == AuxOp::ReadMint) {
      CurveSlot* s = curves_.find(a.curve);
      if (!s) continue;
      if (!a.ok) {
        if (s->resolving) { s->resolving = false; if (unresolved_curves_) --unresolved_curves_; }
        continue;
      }
      set_mint(*s, a.mint, a.token_program, a.mint_info.fee, a.mint_info.supply);
    } else if (a.op == AuxOp::ResolveSig) {
      Position& p = positions_[a.pos];
      if (a.side == Side::Buy && p.state == PosState::BuySent && p.buy_sig == a.sig) {
        p.resolving = false;
        if (a.landed && a.has_fill) on_buy_result(a.pos, true, a.sol_delta, a.base_delta, a.slot, "rpc");
        else if (a.failed || a.expired) on_buy_result(a.pos, false, a.sol_delta, 0, a.slot, a.expired ? "expired" : "rpc_failed");
      }
    }
  }

  BalanceMsg b;
  while (sh_.balances.try_pop(b)) set_wallet_lamports(b.lamports, b.slot);
  (void)now;
}

void Engine::poll(uint64_t now) {
  refresh_amm();
  drain_queues(now);

  for (size_t i = 0; i < positions_.size(); ++i) {
    Position& p = positions_[i];
    switch (p.state) {
      case PosState::BuySent:
        if (!p.resolving && elapsed(now, p.buy_sent_ns) > uint64_t(cfg_.risk.buy_confirm_timeout_ms) * 1'000'000ULL) {
          AuxReq r;
          r.op = AuxOp::ResolveSig;
          r.sig = p.buy_sig;
          r.mint = p.mint;
          r.side = Side::Buy;
          r.pos = uint8_t(i);
          r.deadline_ms = uint64_t(p.buy_sent_ms + cfg_.risk.sent_resolve_ms);
          p.resolving = sh_.aux_reqs.push(r);
        }
        break;
      case PosState::SellSent:
        // elapsed(): a sell re-sent inside drain_queues() above is newer than `now`.
        if (elapsed(now, p.sell_sent_ns) > uint64_t(cfg_.exits.exit_retry_ms) * 1'000'000ULL) {
          if (++p.sell_attempts >= cfg_.exits.max_exit_attempts) {
            latch_kill("exit unconfirmed after max attempts");
            p.state = PosState::Open;  // keep marking; no automatic resend while killed
            p.sell_attempts = cfg_.exits.max_exit_attempts;
          } else {
            send_sell(p, int(i), p.sell_trigger, p.sell_full ? 1.0 : double(p.sell_base) / double(std::max<uint64_t>(p.base_bought, 1)), now);
          }
        }
        break;
      case PosState::Open:
        // Time-based exits need no tick; re-evaluate on the current mark.
        if (p.marked && p.sell_attempts < cfg_.exits.max_exit_attempts) maybe_exit(p, int(i), now);
        break;
      default:
        break;
    }
  }

  if (awaiting_pools_) {
    const uint64_t wait = uint64_t(cfg_.racer.pool_push_wait_ms) * 1'000'000ULL;
    awaiting_pools_ = 0;
    curves_.for_each([&](const Pubkey&, CurveSlot& s) {
      if (!s.awaiting_pool || s.traded) return;
      if (elapsed(now, s.await_since_ns) < wait) { ++awaiting_pools_; return; }
      // No pool push in time: price off the default virtual reserve (0 on mayhem curves).
      s.awaiting_pool = false;
      uint64_t virt = s.mayhem ? 0 : cfg_.racer.default_virtual_quote_lamports;
      trigger(s, s.base_amount, s.quote_amount, virt, s.await_recv_ns, TriggerSrc::PoolWaitTimeout);
    });
  }
  if (now >= next_expire_ns_) {
    next_expire_ns_ = now + 1'000'000'000ULL;
    expire(now);
  }
  if (now >= next_stats_ns_) {
    next_stats_ns_ = now + 10'000'000'000ULL;
    Event* e = ev(Ev::Stats);
    e->slot = last_slot_;
    e->i[0] = int64_t(curves_.size());
    e->i[1] = int64_t(watches_.size());
    e->i[2] = int64_t(launches_.size());
    e->i[3] = active_positions();
    e->i[4] = int64_t(wallet_lamports_);
    e->i[5] = int64_t(reserved_);
    e->i[6] = int64_t(unresolved_curves_);
    e->i[7] = kill_latched_ || sh_.kill_file.load(std::memory_order_relaxed);
    // In-memory counters have no event rows. pump-desk-status reads these slots
    // off the latest stats row (d0 frames, d1 parse_errors, d2 reconnects,
    // d3 events_dropped, d4 stream_up). One relaxed load each, every 10s.
    e->d[0] = double(sh_.counters.frames.load(std::memory_order_relaxed));
    e->d[1] = double(sh_.counters.parse_errors.load(std::memory_order_relaxed));
    e->d[2] = double(sh_.counters.reconnects.load(std::memory_order_relaxed));
    e->d[3] = double(sh_.counters.events_dropped.load(std::memory_order_relaxed));
    e->d[4] = sh_.counters.stream_up.load(std::memory_order_relaxed) ? 1.0 : 0.0;
    emit();
  }
}

// ---------------------------------------------------------------------------
// events

namespace {
Event g_scratch;
}

Event* Engine::ev(Ev type) {
  Event* e = sh_.events.claim();
  if (!e) {
    sh_.counters.events_dropped.fetch_add(1, std::memory_order_relaxed);
    e = &g_scratch;
    pending_ev_ = nullptr;
  } else {
    pending_ev_ = e;
  }
  *e = Event{};
  e->type = type;
  e->wall_ms = wall_ms_();
  return e;
}

void Engine::emit() {
  if (pending_ev_) {
    sh_.events.publish();
    pending_ev_ = nullptr;
  }
}

void Engine::warn(const char* fmt, ...) {
  Event* e = ev(Ev::Warn);
  e->slot = last_slot_;
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(e->text, sizeof e->text, fmt, ap);
  va_end(ap);
  emit();
}

}  // namespace pd
