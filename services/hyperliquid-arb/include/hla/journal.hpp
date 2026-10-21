#pragma once

#include "hla/spsc.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <ostream>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

struct JournalLine final {
  std::uint16_t length{};
  std::array<char, 766> text{};
};

// One JSON object per line, written by a recorder thread so the decision thread never
// blocks on disk. Overflow or a write error makes `failed()` true (the engine halts).
class Recorder final {
 public:
  using Ring = SpscRing<JournalLine, 8192>;

  // `path` may hold strftime fields (UTC).
  bool open(const std::string& path, const std::string& status_path, std::string& error);
  // Decision thread only.
  bool push(std::string_view line) noexcept;
  // Any thread; the status file is rewritten about once a second.
  void set_status(std::string status);
  void run(std::atomic<bool>& stop);
  [[nodiscard]] bool failed() const noexcept { return failed_.load(); }
  [[nodiscard]] const std::string& path() const noexcept { return path_; }

 private:
  void drain();
  void write_status();
  Ring ring_;
  std::FILE* file_{};
  std::string path_;
  std::string status_path_;
  std::mutex status_mutex_;
  std::string status_;
  bool status_dirty_{};
  std::atomic<bool> failed_{false};
};

std::string utc_path(const std::string& pattern);

// Summary of one or more journals: fills, fees, hedge latency, markouts, halts, equity.
int journal_report(const std::vector<std::string>& paths, std::ostream& out);

}  // namespace hla
