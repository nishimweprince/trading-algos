#pragma once
// Everything the pinned threads exchange: fixed-size messages over SPSC
// queues, seqlocked snapshots, and atomics. No locks, no heap on any path
// the logic or exec thread runs per frame.
#include <atomic>
#include <cstdint>
#include <vector>

#include "core/ring.hpp"
#include "core/types.hpp"
#include "sol/amm.hpp"
#include "sol/layouts.hpp"
#include "sol/swap.hpp"

namespace pd {

// --- logic -> exec ------------------------------------------------------------
enum class Side : uint8_t { Buy = 0, Sell = 1 };

constexpr size_t TX_B64_MAX = 1700;  // b64_len(1232) = 1644

struct Packet {
  Side side = Side::Buy;
  uint8_t pos = 0;      // position slot
  uint8_t attempt = 0;
  bool jito = false;    // also send to the Jito links (the tx carries a Jito tip)
  Signature sig{};
  uint64_t t_recv_ns = 0;    // frame arrival (ingest)
  uint64_t t_signed_ns = 0;  // packet ready (logic)
  uint16_t b64_len = 0;
  char b64[TX_B64_MAX];
};

// --- exec -> logic ------------------------------------------------------------
enum class Route : uint8_t { Sender = 0, Jito = 1 };

struct SendResult {
  Side side = Side::Buy;
  uint8_t pos = 0;
  uint8_t attempt = 0;
  Route route = Route::Sender;
  bool ok = false;
  bool definitive = false;  // a rejection that proves the tx was not accepted
  bool rate_limited = false;  // HTTP 429; exec resends the same bytes itself
  int http_status = 0;
  Signature sig{};
  uint64_t t_signed_ns = 0, t_written_ns = 0, t_ack_ns = 0;
  char error[120] = {0};
};

// --- logic -> ingest -----------------------------------------------------------
enum class WatchKind : uint8_t {
  Account = 0,  // account pushes ("watch")
  PoolTxs = 1,  // every tx that touches the key ("pools"): rival profiling
};

struct WatchCmd {
  bool add = true;
  WatchKind kind = WatchKind::Account;
  Pubkey key{};
};

// --- leaders and routing (aux -> logic) ------------------------------------------
/** How the leader orders transactions: by priority, by BAM arrival, or by fee per requested CU. */
enum class LeaderClass : uint8_t { Agave = 0, Bam = 1, Firedancer = 2 };
constexpr size_t N_LEADER_CLASSES = 3;

inline const char* leader_class_name(LeaderClass c) {
  switch (c) {
    case LeaderClass::Bam: return "bam";
    case LeaderClass::Firedancer: return "firedancer";
    default: return "agave";
  }
}

/** One epoch's leader schedule, classified. Immutable once published; aux keeps it alive. */
struct LeaderTable {
  uint64_t first_slot = 0;
  std::vector<uint8_t> cls;        // LeaderClass per slot offset
  std::vector<uint16_t> leader;    // index into identities per slot offset
  std::vector<Pubkey> identities;

  bool covers(uint64_t slot) const { return slot >= first_slot && slot - first_slot < cls.size(); }
  LeaderClass class_at(uint64_t slot) const { return covers(slot) ? LeaderClass(cls[slot - first_slot]) : LeaderClass::Agave; }
  const Pubkey* leader_at(uint64_t slot) const { return covers(slot) ? &identities[leader[slot - first_slot]] : nullptr; }
};

/** Fee, CU and route for one leader class. */
struct RouteProfile {
  uint64_t cu_price = 0;       // micro-lamports per CU, buys
  uint64_t exit_cu_price = 0;  // micro-lamports per CU, sells
  uint32_t cu_limit = 0;    // 0 = measured by logic from our fills (entry.cu_limit until enough samples)
  uint64_t sender_tip = 0;
  bool jito = false;        // also send through Jito (needs jito.enabled)
};

struct RoutePolicy {
  RouteProfile by_class[N_LEADER_CLASSES];
};

/** Wallets the rival guard checks (data/rivals.json, ranked by pump-desk-report --rivals). */
struct RivalSet {
  Pubkey wallets[MAX_RIVALS]{};
  uint8_t n = 0;
};

// --- logic <-> aux --------------------------------------------------------------
enum class AuxOp : uint8_t { ResolveCurve, ReadMint, ResolveSig };

struct AuxReq {
  AuxOp op = AuxOp::ResolveCurve;
  Pubkey curve{};
  Pubkey mint{};
  Signature sig{};  // ResolveCurve: the tx that wrote the curve; ResolveSig: the order
  Side side = Side::Buy;
  uint8_t pos = 0;
  uint64_t deadline_ms = 0;
};

struct AuxResp {
  AuxOp op = AuxOp::ResolveCurve;
  bool ok = false;
  Pubkey curve{}, mint{}, token_program{};
  MintInfo mint_info{};
  // ResolveSig: final status of an order the stream never confirmed.
  Signature sig{};
  Side side = Side::Buy;
  uint8_t pos = 0;
  bool landed = false, failed = false, expired = false;
  bool has_fill = false;
  int64_t sol_delta = 0;     // wallet lamports post - pre
  int64_t base_delta = 0;    // wallet base tokens post - pre
  uint64_t wallet_post = 0;  // wallet lamports after
  uint64_t slot = 0;
};

/** aux -> logic: periodic authoritative balance. */
struct BalanceMsg {
  uint64_t lamports = 0;
  uint64_t slot = 0;
};

struct BlockhashSnap {
  Hash hash{};
  uint64_t last_valid_height = 0;
  uint64_t fetched_ns = 0;
};

// --- events (logic -> writer) ---------------------------------------------------
enum class Ev : uint8_t {
  Boot, CurveAdmit, CurveFire, Migrate, Decision, Order, SendAck, Fill, PositionOpen, PositionUpdate,
  PositionClose, Gap, Kill, Stats, Warn, Route, RivalTx,
};

/** One generic fixed-size record; the meaning of a/b/c/i[]/d[] per type is in writer.cpp. */
struct Event {
  Ev type = Ev::Warn;
  int64_t wall_ms = 0;
  uint64_t slot = 0;
  Pubkey a{}, b{}, c{};
  Signature sig{};
  int64_t i[8] = {0};
  double d[6] = {0};
  char text[96] = {0};
};

/** Counters for the status line, written by their owning thread only. */
struct Counters {
  std::atomic<uint64_t> frames{0}, frame_bytes{0}, parse_errors{0}, reconnects{0}, ring_full_spins{0};
  std::atomic<uint64_t> curves{0}, fires{0}, migrates{0}, triggers{0}, vetoes{0}, buys{0}, sells{0};
  std::atomic<uint64_t> events_dropped{0}, packets_dropped{0}, oversize_frames{0};
  std::atomic<uint64_t> last_slot{0};
  std::atomic<bool> stream_up{false};
};

/** Process-wide shared state. */
struct Shared {
  Seqlock<BlockhashSnap> blockhash;
  Seqlock<AmmConfig> amm;
  std::atomic<uint64_t> cu_price{0};
  Seqlock<RoutePolicy> routes;               // aux-fast; version 0 = routing off or not computed yet
  Seqlock<RivalSet> rivals;                  // aux-fast, from routing rivals file
  std::atomic<const LeaderTable*> leaders{nullptr};  // aux-leaders; null until the first schedule
  std::atomic<bool> kill_file{false};
  std::atomic<bool> stop{false};
  Counters counters;

  Spsc<Packet, 64> packets;           // logic -> exec
  Spsc<SendResult, 256> send_results; // exec -> logic
  Spsc<WatchCmd, 4096> watch_cmds;    // logic -> ingest
  Spsc<AuxReq, 4096> aux_reqs;        // logic -> aux-req
  Spsc<AuxResp, 4096> aux_resps;      // aux-req -> logic
  Spsc<BalanceMsg, 64> balances;      // aux-fast -> logic
  Spsc<Event, 16384> events;          // logic -> writer
};

}  // namespace pd
