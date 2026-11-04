#pragma once
// Decision log (spec §11): one fixed-size binary record per sized candidate with enough data to
// replay it (slot, cycle, pool states' slot and seq, size, per-leg outputs, costs, tip, EV, action
// and reason), plus outcome records appended later by the tracker. Little-endian, as on every host
// the bot runs on.
//   file:   "ARBDEC01" then records; rotated hourly as <dir>/decisions-YYYYMMDD-HH.bin
#include <cstdint>
#include <cstdio>
#include <string>

namespace arb::obs {

constexpr char DECISION_MAGIC[8] = {'A', 'R', 'B', 'D', 'E', 'C', '0', '1'};

enum class RecordKind : uint8_t { Candidate = 1, Outcome = 2 };
enum class Action : uint8_t { Skipped = 0, Sent = 1, DryRun = 2 };  // DryRun: would have been sent
enum class Outcome : uint8_t { Pending = 0, Landed = 1, NotLanded = 2, Rejected = 3, SimulationFailed = 4 };

struct DecisionRecord {
  uint8_t kind = uint8_t(RecordKind::Candidate);
  uint8_t hops = 0;
  uint8_t action = 0;
  uint8_t reason = 0;  // engine::Reason
  uint16_t route_class = 0;
  uint8_t outcome = 0;
  uint8_t dex[3]{};  // DexKind per leg
  uint8_t a_to_b_mask = 0;
  uint8_t pad0[5]{};
  uint32_t cycle_id = 0;
  uint32_t pad1 = 0;
  int64_t wall_ms = 0;
  uint64_t slot = 0;  // the update slot that triggered the evaluation
  uint64_t x = 0, final_out = 0, flash_fee = 0;
  int64_t profit_base = 0;
  uint64_t gross_lamports = 0, tip_lamports = 0, fee_lamports = 0;
  int64_t net_lamports = 0, ev_lamports = 0;
  uint64_t min_out = 0;
  uint32_t cu_limit = 0, p_land_ppm = 0;
  uint64_t leg_out[3]{};
  uint64_t state_slot[3]{};
  uint64_t state_seq[3]{};
  uint8_t pools[3][32]{};
  uint8_t base[32]{};
  uint8_t signature[64]{};  // the signed transaction's signature (dry-run bundles too); outcome records key on it
};
static_assert(sizeof(DecisionRecord) == 392, "on-disk layout");

class DecisionLog {
 public:
  DecisionLog(std::string dir, int rotate_s) : dir_(std::move(dir)), rotate_s_(rotate_s) {}
  ~DecisionLog();
  bool write(const DecisionRecord& r);
  /** Write to exactly this path (tests); no rotation. */
  bool open_path(const std::string& path);
  void close();
  uint64_t records() const { return records_; }
  const std::string& current_path() const { return path_; }

 private:
  std::string dir_;
  int rotate_s_;
  FILE* f_ = nullptr;
  std::string path_;
  int64_t bucket_ = -1;
  bool fixed_ = false;
  uint64_t records_ = 0;
};

class DecisionReader {
 public:
  bool open(const std::string& path);
  bool next(DecisionRecord& r);
  ~DecisionReader();

 private:
  FILE* f_ = nullptr;
};

}  // namespace arb::obs
