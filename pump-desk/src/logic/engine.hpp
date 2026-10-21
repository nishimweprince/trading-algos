#pragma once
// The logic thread. Sole consumer that parses the ingest ring:
//   curve bands  -> admit / fire (precompute the canonical pool's keys, watch it)
//   migrate tx   -> trigger (screen, size, build, sign, hand to exec)
//   pool pushes  -> trigger (whichever completes the input set first, once)
//   vault pushes -> marks -> exit FSM -> sells
//   wallet txs   -> fills and the in-memory balance
//   pool txs     -> rival profiling (who lands ahead of us, how they pay)
// Nothing here allocates after construction.
#include <array>
#include <cstdint>
#include <string_view>

#include "core/config.hpp"
#include "core/key_map.hpp"
#include "core/ring.hpp"
#include "logic/exit_fsm.hpp"
#include "logic/shared.hpp"
#include "sol/amm.hpp"
#include "sol/crypto.hpp"
#include "sol/layouts.hpp"
#include "sol/swap.hpp"
#include "stream/update.hpp"

namespace pd {

constexpr size_t MAX_POSITIONS = 8;
constexpr size_t B58_KEY_MAX = 48;

struct B58Key {
  char s[B58_KEY_MAX] = {0};
  uint8_t len = 0;
  void set(const Pubkey& k);
  std::string_view sv() const { return {s, len}; }
};

struct CurveSlot {
  Pubkey curve{}, mint{}, creator{}, token_program{};
  B58Key mint_b58, pool_b58;
  bool mint_known = false, resolving = false, fired = false, complete = false, migrated = false, traded = false;
  bool mayhem = false, cashback = false, pump_suffix = false, blacklisted = false;
  bool seen_incomplete = false;  // observed before completion; pre-graduated curves never set this
  TransferFee fee = TransferFee::Unknown;
  uint64_t mint_supply = 0;
  double cost_sol = 0;
  uint64_t admitted_ns = 0, fired_ns = 0, complete_ns = 0, migrated_ns = 0, last_update_ns = 0;
  uint64_t fire_slot = 0;
  Signature last_sig{};  // the tx that last wrote the curve (mint resolution fallback)
  bool keys_ready = false;
  SwapKeys keys;
  // Rival guard: wallets from the rivals file and their ATAs for this mint, derived at fire.
  Pubkey rivals[MAX_RIVALS]{}, rival_atas[MAX_RIVALS]{};
  uint8_t n_rivals = 0;
  bool pool_txs_watched = false;
  uint64_t migrate_slot = 0;
  // Pool-side inputs, from pushes (watched from fire).
  bool pool_seen = false, base_seen = false, quote_seen = false;
  Pool pool;
  uint64_t base_amount = 0, quote_amount = 0;
  // Migrate tx seen before the pool push: vaults known, waiting for the true virtual reserve.
  bool awaiting_pool = false;
  uint64_t await_since_ns = 0, await_recv_ns = 0;
};

struct Launch {
  Pubkey mint{}, token_program{};
  bool no_fee_proven = false;
};

struct WatchRef {
  Pubkey curve{};  // zero when only a position needs it
  uint8_t role = 0;
  uint8_t users = 0;  // curve + positions referencing the key
};

enum class PosState : uint8_t { Empty, BuySent, Open, SellSent };

struct Position {
  PosState state = PosState::Empty;
  Pubkey curve{}, mint{};
  B58Key mint_b58;
  SwapKeys keys;
  // buy
  Signature buy_sig{};
  uint64_t buy_sent_ns = 0;
  int64_t buy_sent_ms = 0;
  uint64_t size_lamports = 0, max_quote = 0, expected_base = 0, reserved = 0;
  uint64_t overhead = 0;  // tips + priority + signature fee of the buy
  bool resolving = false;
  // Pool quote-vault writes by other txs after our buy was sent and before ours; -1 until ours is seen.
  int64_t writes_ahead = -1;
  int64_t writes_counting = 0;
  // Slot of the frame that triggered the latest buy or sell (fill slot minus this = slots to land).
  uint64_t order_slot = 0;
  // Lighthouse guards on the buy: instruction index of the first, and the rival behind each rival guard.
  BuyGuards guards;
  uint8_t guard_ix0 = 0;
  Pubkey rivals[MAX_RIVALS]{};
  uint32_t buy_cu_limit = 0;
  // open
  uint64_t base_amount = 0;
  uint64_t base_bought = 0;
  double entry_price = 0;
  int64_t opened_ms = 0;
  int64_t sol_spent = 0;     // lamports out on the buy (fees, tip, rent, swap)
  int64_t sol_received = 0;  // lamports back from sells
  ExitState exit;
  // mark
  uint64_t base_res = 0, quote_res = 0, virtual_quote = 0;
  double last_price = 0;
  bool marked = false;
  // sell
  Signature sell_sigs[8]{};
  uint8_t n_sell_sigs = 0;
  uint64_t sell_sent_ns = 0;
  int sell_attempts = 0;
  ExitTrigger sell_trigger = ExitTrigger::None;
  uint64_t sell_base = 0;
  bool sell_full = false;
  // Jupiter, after the PumpSwap exits are exhausted: pending from the request until the
  // position closes or Jupiter gives up; due = when the next attempt may start (0 = in flight).
  bool jupiter_pending = false;
  uint8_t jupiter_attempts = 0;
  uint64_t jupiter_due_ns = 0;
  Signature jupiter_sig{};  // the latest Jupiter tx sent
};

/** An open position loaded from the database at boot. */
struct RestoredPosition {
  Pubkey curve{}, mint{}, creator{}, token_program{};
  bool cashback = false, mayhem = false;
  uint64_t base_amount = 0, base_bought = 0;
  double entry_price = 0;
  int64_t sol_spent = 0, sol_received = 0, opened_ms = 0;
  bool tp0_done = false, tp1_done = false;
  // Current pool state read at boot, so exits can price before the next swap.
  uint64_t base_res = 0, quote_res = 0, virtual_quote = 0;
};

class Engine {
 public:
  Engine(const Config& cfg, Shared& sh, const Keypair& wallet, FrameRing* ring);

  /** Logic-thread main loop (pinned by the caller). Returns when sh.stop is set. */
  void run();

  /** One raw SubscribeUpdate frame. Public for replay and tests. */
  void on_frame(const uint8_t* data, size_t len, uint64_t recv_ns);
  /** Timers, queues from exec/aux, expiry. Called whenever the ring is idle. */
  void poll(uint64_t now_ns);

  void set_wallet_lamports(uint64_t lamports, uint64_t slot);
  bool restore_position(const RestoredPosition& r);
  /** Tests: deterministic clocks. */
  void set_clocks(int64_t (*wall)(), uint64_t (*mono)()) { wall_ms_ = wall; now_ns_ = mono; }

  // Read-only views for tests and the status line.
  const Position& position(size_t i) const { return positions_[i]; }
  const CurveSlot* curve(const Pubkey& k) const { return curves_.find(k); }
  uint64_t wallet_lamports() const { return wallet_lamports_; }
  uint64_t reserved_lamports() const { return reserved_; }
  bool killed() const { return kill_latched_; }
  uint64_t last_slot() const { return last_slot_; }
  /** The buy CU limit measured from landed buys (0 until the first sample). */
  uint32_t measured_cu_limit() const { return measured_cu_; }

 private:
  // frame handlers
  void on_curve(const AccountView& a);
  void on_watched(const AccountView& a);
  void on_migrate(const TxView& tx, uint64_t recv_ns);
  void on_create(const TxView& tx);
  void on_wallet_tx(const TxView& tx);
  void on_external_tx(const TxView& tx, int64_t sol_delta);
  void on_slot(const SlotView& s);

  // curve lifecycle
  CurveSlot* admit(const Pubkey& key, const Curve& c, const AccountView& a);
  void set_mint(CurveSlot& s, const Pubkey& mint, const Pubkey& token_program, TransferFee fee, uint64_t supply);
  void fire(CurveSlot& s, uint64_t slot);
  void prepare_keys(CurveSlot& s);
  void drop_curve(const Pubkey& key);
  void expire(uint64_t now_ns);

  // watches
  void watch(const Pubkey& k, const Pubkey& curve, uint8_t role);
  void unwatch(const Pubkey& k);

  // trading
  enum class TriggerSrc : uint8_t { MigrateTx, PoolPush, PoolWaitTimeout };
  /** Which leader a packet is priced for, and how. */
  struct RouteChoice {
    LeaderClass cls = LeaderClass::Agave;
    Pubkey leader{};
    uint64_t slot = 0;
    bool routed = false;  // a per-class profile was applied (else the single legacy policy)
    bool jito = false;
  };
  void trigger(CurveSlot& s, uint64_t base_res, uint64_t quote_res, uint64_t virt, uint64_t recv_ns, TriggerSrc src);
  /** Fire when the pool account and both vault balances are known. */
  void try_trigger(CurveSlot& s, uint64_t recv_ns, TriggerSrc src);
  int free_position() const;
  int active_positions() const;
  bool send_packet(Side side, uint8_t pos, uint8_t attempt, const IxList& ixs, uint64_t recv_ns, bool jito, Signature& sig_out);
  void mark(Position& p, uint64_t now_ns);
  void maybe_exit(Position& p, int idx, uint64_t now_ns);
  void send_sell(Position& p, int idx, ExitTrigger t, double fraction, uint64_t recv_ns);
  /** tx_index and seen_ns come from a stream fill; -1 / 0 otherwise. beaten_by: the rival a guard reverted on. */
  void on_buy_result(int idx, bool ok, int64_t sol_delta, int64_t base_delta, uint64_t slot, const char* how,
                     int64_t tx_index = -1, uint64_t seen_ns = 0, const Pubkey* beaten_by = nullptr);
  void on_sell_result(int idx, const Signature& sig, bool ok, int64_t sol_delta, int64_t base_delta, uint64_t slot,
                      const char* how, int64_t tx_index = -1, uint64_t seen_ns = 0);
  /** Landing fields shared by buy and sell Fill events (i[5..7], d[2]). */
  void fill_landing(Event* f, uint64_t order_slot, uint64_t slot, int64_t tx_index, uint64_t seen_ns);
  void close_position(int idx, const char* reason);
  void release_watches(const Position& p);
  void latch_kill(const char* why);
  /** PumpSwap exits are used up: entries stop; the position goes to Jupiter, else to the operator. */
  void exits_exhausted(Position& p, int idx, const char* why);
  /** Ask aux-req for one Jupiter sell of the whole position. False when none is left to try. */
  bool try_jupiter_exit(Position& p, int idx);
  TxParams tx_params(bool exit, RouteChoice& rc);
  void emit_route(const Signature& sig, Side side, const TxParams& tp, const RouteChoice& rc, uint8_t guards);
  void refresh_rivals();
  void build_guards(const CurveSlot& s, uint64_t quote_res, BuyGuards& g, Pubkey* rivals) const;
  /** How many of the curve's rival guards still fit one packet on its widest buy. */
  uint8_t fit_rival_guards(const CurveSlot& s);
  void on_pool_tx(const TxView& tx);
  void add_cu_sample(uint64_t cu);

  // shared snapshots
  void refresh_amm();
  void refresh_routes();
  void drain_queues(uint64_t now_ns);

  // events
  Event* ev(Ev type);
  void emit();
  void warn(const char* fmt, ...);

  const Config& cfg_;
  Shared& sh_;
  const Keypair& wallet_;
  FrameRing* ring_;
  UserKeys user_;
  B58Key user_b58_;

  KeyMap<CurveSlot> curves_;
  KeyMap<Pubkey> mint_index_;  // mint -> curve
  KeyMap<Launch> launches_;    // curve -> mint (from create txs)
  std::unique_ptr<Pubkey[]> launch_fifo_;
  size_t launch_fifo_cap_ = 0, launch_fifo_head_ = 0, launch_fifo_size_ = 0;
  KeyMap<WatchRef> watches_;
  KeyMap<uint8_t> blacklist_mints_, blacklist_creators_;
  std::array<Position, MAX_POSITIONS> positions_{};
  int max_positions_;

  UpdateView* view_;  // large; heap-allocated once
  IxList* ixs_;
  SignedTx* tx_;
  AmmConfig amm_;
  uint64_t amm_version_ = 0;
  RoutePolicy routes_{};
  uint64_t routes_version_ = 0;  // 0 = no per-class policy yet: legacy fees
  RivalSet rivals_{};
  uint64_t rivals_version_ = 0;
  // Compute units our landed buys consumed; the max of the window (+ margin) is the measured CU limit.
  std::array<uint32_t, 32> cu_samples_{};
  uint32_t n_cu_samples_ = 0;
  uint32_t measured_cu_ = 0;
  bool build_too_large_ = false;  // the last send_packet failed because the tx exceeded one packet

  uint64_t wallet_lamports_ = 0, wallet_slot_ = 0, reserved_ = 0;
  bool kill_latched_ = false;
  uint64_t last_slot_ = 0, last_frame_ns_ = 0;
  uint64_t unresolved_curves_ = 0;
  uint64_t awaiting_pools_ = 0;
  // Arrival of the inter-slot statuses per slot, for the fill timing split.
  struct SlotTimes {
    uint64_t slot = 0, first_shred_ns = 0, completed_ns = 0;
  };
  std::array<SlotTimes, 64> slot_times_{};
  uint32_t fee_rr_ = 0, tip_rr_ = 0;
  std::vector<Pubkey> sender_tips_, jito_tips_;
  uint64_t next_expire_ns_ = 0, next_stats_ns_ = 0;
  Event* pending_ev_ = nullptr;

  int64_t (*wall_ms_)();
  uint64_t (*now_ns_)();
};

}  // namespace pd
