#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace bpmm {

// Journal-backed dashboard data. Everything here runs on the control
// (status-server) thread: it reads the journal file and serves JSON/HTML.
// It never touches decision-thread state; live counters arrive through the
// atomic snapshot owned by shadow mode.

struct DashboardFill final {
  std::uint64_t fill_ns{};
  std::uint64_t order_id{};
  int side{};  // 0 buy, 1 sell, matching the journal encoding of Side
  std::int64_t price_raw{};
  std::int64_t quantity_raw{};
  std::int64_t fee_raw{};
  std::int64_t realized_pnl_raw{};  // cumulative, after this fill
  std::int64_t position_raw{};      // net base units, after this fill
};

struct DashboardCandle final {
  std::uint64_t open_ms{};
  std::uint64_t close_ms{};
  std::int64_t open_raw{};
  std::int64_t high_raw{};
  std::int64_t low_raw{};
  std::int64_t close_raw{};
  bool closed{};
};

struct DashboardActivity final {
  std::vector<DashboardFill> fills;
  std::vector<DashboardCandle> candles;
};

// Average-entry position accounting. This mirrors Engine::apply_fills
// (src/engine.cpp), including fees deducted on the closing leg only; the two
// are kept in lockstep by test_dashboard.
class PnlTracker final {
 public:
  void apply(int side, std::int64_t price_raw, std::int64_t quantity_raw,
             std::int64_t fee_raw) noexcept;
  [[nodiscard]] std::int64_t realized_pnl_raw() const noexcept { return realized_; }
  [[nodiscard]] std::int64_t position_raw() const noexcept { return quantity_; }

 private:
  std::int64_t quantity_{};
  std::int64_t average_entry_raw_{};
  std::int64_t realized_{};
};

struct DashboardLimits final {
  std::size_t max_bytes{512 * 1024};
  std::size_t max_fills{500};
  std::size_t max_candles{500};
};

// Reads the tail of a journal file, keeping the newest complete 'F' and 'K'
// records in chronological order. A missing file yields empty activity (a
// fresh run has no journal yet); a trailing partial line is ignored because
// the recorder may be mid-write. Malformed lines are skipped.
DashboardActivity read_journal_tail(const std::string& path, DashboardLimits limits = {});

// Serializes activity for GET /api/activity. Money fields stay raw scaled
// integers; "scale" states the divisor (kScale).
std::string activity_json(const DashboardActivity& activity);

// Embedded dashboard page served at GET /.
const char* dashboard_page();

}  // namespace bpmm
