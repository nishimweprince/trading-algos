#pragma once

#include "cxa/config.hpp"
#include "cxa/engine.hpp"
#include "cxa/instruments.hpp"
#include "cxa/parser.hpp"

#include <fstream>
#include <string>
#include <string_view>
#include <vector>

namespace cxa {

// Append-only, space-separated records; one letter per kind. Inputs (Y C L S D M) are
// everything replay needs; the rest are decision/fill/report records.
//   Z header   Y instrument   C taker fee   L latency sample   S Binance snapshot
//   D depth    M funding      R raw frame   I leg submit       F leg result
//   X entry decision          P closed cycle                   O census episode
//   G resync request          H halt change
class Journal final {
 public:
  explicit Journal(const std::string& path);
  bool header(const RuntimeConfig& config, std::string_view mode, std::uint64_t start_ns);
  bool instrument(std::size_t symbol, const Instrument& instrument);
  bool fee(Venue venue, std::size_t symbol, std::int64_t taker_ppm);
  bool latency(Venue venue, std::uint64_t receive_ns, std::uint64_t rtt_ns);
  bool snapshot(const BookSnapshot& snapshot);
  bool depth(const DepthEvent& event);
  bool funding(const FundingEvent& event);
  bool raw(Venue venue, std::uint64_t receive_ns, std::string_view frame);
  bool output(const Output& output);
  bool halt(std::uint64_t now_ns, bool halted);
  void flush() { stream_.flush(); }
  [[nodiscard]] bool good() const { return stream_.good(); }

 private:
  std::ofstream stream_;
  std::vector<char> buffer_;
};

struct ReplayResult final {
  std::uint64_t hash{};
  std::uint64_t events{};
  EngineMetrics metrics{};
};

// Rebuilds instruments from Y records and feeds inputs through a fresh Engine built
// from `config`. Same journal + same config => same hash.
bool replay_journal(const std::string& path, const RuntimeConfig& config, ReplayResult& result,
                    std::string& error);

struct GapReport final {
  std::array<std::uint64_t, kVenues> gaps{};
  std::array<std::uint64_t, kVenues> recoveries{};
  std::uint64_t depth_records{};
};
bool gapcheck(const std::string& path, GapReport& report, std::string& error);

// JSON reports. `daily_report` evaluates the PREREG gate over all files.
bool daily_report(const std::vector<std::string>& paths, std::string& json, std::string& error);
bool census_report(const std::vector<std::string>& paths, std::string& json, std::string& error);

}  // namespace cxa
