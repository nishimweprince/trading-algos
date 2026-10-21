#pragma once

// Deterministic synthetic market used by `crossex drill`, `crossex benchmark`, and the
// unit tests. One symbol, two venues, books driven through the production paths.

#include "cxa/engine.hpp"

#include <memory>
#include <string_view>

namespace cxa {

Instrument test_instrument();
RuntimeConfig scenario_config();

class Scenario final {
 public:
  explicit Scenario(RuntimeConfig config = scenario_config());

  // Replaces the top of book on `venue` with one bid and one ask level of `size`.
  // Binance moves through bridged diffs, the peer through snapshots.
  void quote(Venue venue, std::string_view bid, std::string_view ask, std::string_view size = "100");
  void funding(Venue venue, std::string_view rate, std::uint64_t next_ms);
  // Moves time forward in 1 ms steps, refreshing both venues so legs can arrive.
  void advance_ms(std::uint64_t ms);
  // Emits a Binance diff whose pu does not chain (a sequence gap).
  void binance_gap();

  [[nodiscard]] Engine& engine() noexcept { return *engine_; }
  [[nodiscard]] std::uint64_t now_ns() const noexcept { return now_ns_; }
  [[nodiscard]] std::size_t count(OutputKind kind) const noexcept;
  [[nodiscard]] const Output* last(OutputKind kind) const noexcept;
  void clear() noexcept;

 private:
  void collect();
  std::unique_ptr<Engine> engine_;
  std::unique_ptr<DepthEvent> event_;
  std::vector<Output> outputs_;
  std::uint64_t now_ns_{1'700'000'000'000'000'000ULL};
  std::uint64_t binance_id_{1000};
  std::uint64_t peer_id_{0};
  bool binance_started_{};
  std::array<Level, kVenues> bid_{};
  std::array<Level, kVenues> ask_{};
};

}  // namespace cxa
