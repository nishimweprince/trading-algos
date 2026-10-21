#include "hla/journal.hpp"

#include "hla/json.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <thread>

namespace hla {
namespace {

double number(std::string_view line, std::string_view key) {
  const auto text = json::scalar(line, key);
  if (text.empty()) return 0;
  try {
    return std::stod(std::string(text));
  } catch (...) {
    return 0;
  }
}

std::uint64_t u64(std::string_view line, std::string_view key) {
  std::uint64_t value = 0;
  json::u64(line, key, value);
  return value;
}

double percentile(std::vector<double> values, double p) {
  if (values.empty()) return 0;
  std::sort(values.begin(), values.end());
  return values[static_cast<std::size_t>(p * static_cast<double>(values.size() - 1))];
}

}  // namespace

std::string utc_path(const std::string& pattern) {
  const auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
  std::tm utc{};
#ifdef _WIN32
  gmtime_s(&utc, &now);
#else
  gmtime_r(&now, &utc);
#endif
  std::array<char, 512> buffer{};
  const auto n = std::strftime(buffer.data(), buffer.size(), pattern.c_str(), &utc);
  return n == 0 ? pattern : std::string(buffer.data(), n);
}

bool Recorder::open(const std::string& path, const std::string& status_path, std::string& error) {
  path_ = utc_path(path);
  status_path_ = status_path;
  file_ = std::fopen(path_.c_str(), "ab");
  if (file_ == nullptr) {
    error = "journal: cannot open " + path_;
    return false;
  }
  return true;
}

bool Recorder::push(std::string_view line) noexcept {
  JournalLine record;
  record.length = static_cast<std::uint16_t>(std::min(line.size(), record.text.size()));
  std::memcpy(record.text.data(), line.data(), record.length);
  if (ring_.try_push(record)) return true;
  failed_.store(true);
  return false;
}

void Recorder::set_status(std::string status) {
  std::lock_guard lock(status_mutex_);
  status_ = std::move(status);
  status_dirty_ = true;
}

void Recorder::drain() {
  JournalLine record;
  bool wrote = false;
  while (ring_.try_pop(record)) {
    if (std::fwrite(record.text.data(), 1, record.length, file_) != record.length || std::fputc('\n', file_) == EOF)
      failed_.store(true);
    wrote = true;
  }
  if (wrote && std::fflush(file_) != 0) failed_.store(true);
}

void Recorder::write_status() {
  std::string status;
  {
    std::lock_guard lock(status_mutex_);
    if (!status_dirty_ || status_path_.empty()) return;
    status = status_;
    status_dirty_ = false;
  }
  // Write then rename, so readers never see a partial file.
  const auto temporary = status_path_ + ".tmp";
  {
    std::ofstream out(temporary, std::ios::trunc);
    out << status << '\n';
  }
  std::remove(status_path_.c_str());
  std::rename(temporary.c_str(), status_path_.c_str());
}

void Recorder::run(std::atomic<bool>& stop) {
  auto last_status = std::chrono::steady_clock::now();
  while (!stop.load(std::memory_order_relaxed)) {
    drain();
    const auto now = std::chrono::steady_clock::now();
    if (now - last_status >= std::chrono::seconds(1)) {
      last_status = now;
      write_status();
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  drain();
  write_status();
  if (file_ != nullptr) std::fclose(file_);
  file_ = nullptr;
}

int journal_report(const std::vector<std::string>& paths, std::ostream& out) {
  struct Fill final {
    std::string coin;
    std::uint64_t t{};
    double px{};
    double sign{};
  };
  struct Coin final {
    std::size_t fills{};
    std::size_t taker_fills{};
    double notional{};
    double fees{};
    std::vector<std::pair<std::uint64_t, double>> fair;  // (t, fair) once a second
  };
  std::map<std::string, Coin> coins;
  std::vector<Fill> fills;
  std::vector<double> hedge_ms;
  std::vector<double> order_ms;
  std::vector<std::string> halts;
  std::size_t hedges = 0;
  std::size_t rejects = 0;
  double first_equity = 0;
  double last_equity = 0;
  for (const auto& path : paths) {
    std::ifstream in(path);
    if (!in) {
      out << "{\"error\":\"cannot open " << path << "\"}\n";
      return 1;
    }
    for (std::string line; std::getline(in, line);) {
      const auto kind = json::scalar(line, "\"k\"");
      if (kind == "fill") {
        Fill fill{std::string(json::scalar(line, "\"coin\"")), u64(line, "\"t\""), number(line, "\"px\""),
                  json::scalar(line, "\"side\"") == "B" ? 1.0 : -1.0};
        auto& coin = coins[fill.coin];
        ++coin.fills;
        if (json::value_after(line, "\"crossed\"").starts_with("true")) ++coin.taker_fills;
        coin.notional += fill.px * number(line, "\"sz\"");
        coin.fees += number(line, "\"fee\"");
        fills.push_back(std::move(fill));
      } else if (kind == "f") {
        const auto fair = number(line, "\"fair\"");
        if (fair > 0) coins[std::string(json::scalar(line, "\"coin\""))].fair.emplace_back(u64(line, "\"t\""), fair);
      } else if (kind == "hedge") {
        ++hedges;
      } else if (kind == "txn" && json::value_after(line, "\"ours\"").starts_with("true")) {
        const auto status = u64(line, "\"status\"");
        if (status == 1 || status == 2) {
          hedge_ms.push_back(number(line, "\"hedge_ms\""));
          order_ms.push_back(number(line, "\"order_ms\""));
        } else {
          ++rejects;
        }
      } else if (kind == "halt") {
        halts.emplace_back(json::scalar(line, "\"detail\""));
      } else if (kind == "equity") {
        const auto equity = number(line, "\"hl\"") + number(line, "\"mt5\"");
        if (first_equity == 0) first_equity = equity;
        last_equity = equity;
      }
    }
  }
  // Markout: fair at t+h against the fill price, signed so positive is a profit, in ppm.
  auto markout = [&](const Fill& fill, std::uint64_t horizon_ns, double& value) {
    const auto& series = coins[fill.coin].fair;
    const auto it = std::lower_bound(series.begin(), series.end(), std::pair<std::uint64_t, double>{fill.t + horizon_ns, 0});
    if (it == series.end() || fill.px <= 0) return false;
    value = fill.sign * (it->second - fill.px) / fill.px * 1e6;
    return true;
  };
  std::map<std::string, std::pair<std::vector<double>, std::vector<double>>> markouts;
  for (const auto& fill : fills) {
    double value = 0;
    if (markout(fill, 1'000'000'000ULL, value)) markouts[fill.coin].first.push_back(value);
    if (markout(fill, 10'000'000'000ULL, value)) markouts[fill.coin].second.push_back(value);
  }
  auto mean = [](const std::vector<double>& v) {
    double sum = 0;
    for (const auto x : v) sum += x;
    return v.empty() ? 0.0 : sum / static_cast<double>(v.size());
  };
  out << "{\"fills\":" << fills.size() << ",\"hedges\":" << hedges << ",\"hedge_rejects\":" << rejects
      << ",\"hedge_ms\":{\"p50\":" << percentile(hedge_ms, 0.5) << ",\"p99\":" << percentile(hedge_ms, 0.99)
      << "},\"mt5_order_ms\":{\"p50\":" << percentile(order_ms, 0.5) << ",\"p99\":" << percentile(order_ms, 0.99)
      << "},\"equity_change_usd\":" << (last_equity - first_equity) << ",\"coins\":[";
  bool first = true;
  for (const auto& [name, coin] : coins) {
    if (coin.fills == 0) continue;
    const auto& m = markouts[name];
    out << (first ? "" : ",") << "{\"coin\":\"" << name << "\",\"fills\":" << coin.fills
        << ",\"taker_fills\":" << coin.taker_fills << ",\"notional_usd\":" << coin.notional
        << ",\"hl_fees_usd\":" << coin.fees << ",\"markout_1s_ppm\":" << mean(m.first)
        << ",\"markout_10s_ppm\":" << mean(m.second) << "}";
    first = false;
  }
  out << "],\"halts\":[";
  for (std::size_t i = 0; i < halts.size(); ++i) out << (i == 0 ? "" : ",") << '"' << halts[i] << '"';
  out << "]}\n";
  return 0;
}

}  // namespace hla
