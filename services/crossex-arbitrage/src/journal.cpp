#include "cxa/journal.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <map>
#include <memory>
#include <sstream>

namespace cxa {
namespace {

// PREREG.md gate. Changing these after shadow data exists invalidates the gate.
constexpr double kGateDays = 14.0;
constexpr std::size_t kGateCycles = 150;
constexpr double kGateMaxLeggingRate = 0.10;
constexpr std::size_t kBootstrapResamples = 10'000;

std::uint64_t timestamp_field(const std::string& line, std::size_t field) {
  std::size_t pos = 0;
  for (std::size_t i = 0; i < field; ++i) {
    pos = line.find(' ', pos);
    if (pos == std::string::npos) return 0;
    ++pos;
  }
  return std::strtoull(line.c_str() + pos, nullptr, 10);
}

template <typename Levels>
void write_levels(std::ofstream& out, const Levels& levels, std::size_t count) {
  out << ' ' << count;
  for (std::size_t i = 0; i < count; ++i) out << ' ' << levels[i].price.raw << ' ' << levels[i].quantity.raw;
}

bool read_levels(std::istringstream& row, std::vector<Level>& levels) {
  std::size_t count = 0;
  if (!(row >> count)) return false;
  levels.resize(count);
  for (auto& level : levels)
    if (!(row >> level.price.raw >> level.quantity.raw)) return false;
  return true;
}

template <std::size_t N>
bool read_levels(std::istringstream& row, std::array<Level, N>& levels, std::uint16_t& count) {
  std::size_t n = 0;
  if (!(row >> n) || n > N) return false;
  count = static_cast<std::uint16_t>(n);
  for (std::size_t i = 0; i < n; ++i)
    if (!(row >> levels[i].price.raw >> levels[i].quantity.raw)) return false;
  return true;
}

// Reads plain journals directly and *.zst journals through `zstd -dc`.
class LineReader final {
 public:
  explicit LineReader(const std::string& path) {
    if (path.ends_with(".zst")) {
      if (path.find('\'') != std::string::npos) return;
      file_ = ::popen(("zstd -dc -- '" + path + "'").c_str(), "r");
      pipe_ = true;
    } else {
      file_ = std::fopen(path.c_str(), "r");
    }
  }
  ~LineReader() {
    std::free(buffer_);
    if (file_ == nullptr) return;
    if (pipe_) ::pclose(file_);
    else std::fclose(file_);
  }
  LineReader(const LineReader&) = delete;
  LineReader& operator=(const LineReader&) = delete;
  explicit operator bool() const noexcept { return file_ != nullptr; }
  bool next(std::string& line) {
    const auto length = ::getline(&buffer_, &capacity_, file_);
    if (length < 0) return false;
    auto size = static_cast<std::size_t>(length);
    if (size > 0 && buffer_[size - 1] == '\n') --size;
    line.assign(buffer_, size);
    return true;
  }

 private:
  std::FILE* file_{};
  bool pipe_{};
  char* buffer_{};
  std::size_t capacity_{};
};

Venue venue_from(int value, Venue peer) { return value == 0 ? Venue::binance : peer; }

// Z header: start mode latency exec notional min_net available basket [peer]. Journals
// written before the peer field are Binance-Bybit.
Venue header_peer(const std::string& line) {
  std::istringstream row(line.substr(2));
  std::string field;
  for (int i = 0; i < 8 && row >> field; ++i) {}
  return row >> field ? parse_peer(field) : Venue::bybit;
}

struct FileSpan final {
  std::uint64_t start_ns{};
  std::uint64_t last_ns{};
  std::string latency_mode;
};

// Coverage from the Z header to the latest timestamped record.
void track_span(const std::string& line, FileSpan& span) {
  std::uint64_t ts = 0;
  switch (line[0]) {
    case 'Z': {
      std::istringstream row(line.substr(2));
      row >> span.start_ns >> span.latency_mode >> span.latency_mode;  // start mode latency_mode
      return;
    }
    case 'D': ts = timestamp_field(line, 4); break;
    case 'M': ts = timestamp_field(line, 3); break;
    case 'S': ts = timestamp_field(line, 3); break;
    default: return;
  }
  span.last_ns = std::max(span.last_ns, ts);
}

std::string fixed(double value, int digits = 2) {
  if (!std::isfinite(value)) return "null";
  std::ostringstream out;
  out.setf(std::ios::fixed);
  out.precision(digits);
  out << value;
  return out.str();
}

}  // namespace

Journal::Journal(const std::string& path) : buffer_(1 << 20) {
  stream_.rdbuf()->pubsetbuf(buffer_.data(), static_cast<std::streamsize>(buffer_.size()));
  stream_.open(path, std::ios::app);
}

bool Journal::header(const RuntimeConfig& config, std::string_view mode, std::uint64_t start_ns) {
  stream_ << "Z " << start_ns << ' ' << mode << ' ' << latency_mode_name(config.shadow.latency_mode) << ' '
          << exec_mode_name(config.strategy.exec_mode) << ' ' << config.strategy.trade_notional_raw << ' '
          << config.strategy.min_net_ppm << ' ' << config.shadow.available_ppm << ' ' << config.basket << ' ' << venue_name(config.shadow.peer) << '\n';
  return stream_.good();
}

bool Journal::instrument(std::size_t symbol, const Instrument& i) {
  stream_ << "Y " << symbol << ' ' << i.name << ' ' << i.symbol[0] << ' ' << i.symbol[1];
  for (std::size_t v = 0; v < kVenues; ++v)
    stream_ << ' ' << i.tick[v].raw << ' ' << i.step[v].raw << ' ' << i.min_qty[v].raw << ' '
            << i.min_notional[v].raw;
  stream_ << ' ' << i.common_step.raw << ' ' << i.common_min_qty.raw << '\n';
  return stream_.good();
}

bool Journal::fee(Venue venue, std::size_t symbol, std::int64_t taker_ppm, std::int64_t maker_ppm) {
  stream_ << "C " << index(venue) << ' ' << symbol << ' ' << taker_ppm << ' ' << maker_ppm << '\n';
  return stream_.good();
}

bool Journal::latency(Venue venue, std::uint64_t receive_ns, std::uint64_t rtt_ns) {
  stream_ << "L " << index(venue) << ' ' << receive_ns << ' ' << rtt_ns << '\n';
  return stream_.good();
}

bool Journal::snapshot(const BookSnapshot& s) {
  stream_ << "S " << index(s.venue) << ' ' << s.symbol << ' ' << s.receive_ns << ' ' << s.last_update_id;
  write_levels(stream_, s.bids, s.bids.size());
  write_levels(stream_, s.asks, s.asks.size());
  stream_ << '\n';
  return stream_.good();
}

bool Journal::depth(const DepthEvent& e) {
  stream_ << "D " << index(e.venue) << ' ' << e.symbol << ' ' << e.snapshot << ' ' << e.receive_ns << ' '
          << e.event_ms << ' ' << e.first_id << ' ' << e.final_id << ' ' << e.previous_final_id;
  write_levels(stream_, e.bids, e.bid_count);
  write_levels(stream_, e.asks, e.ask_count);
  stream_ << '\n';
  return stream_.good();
}

bool Journal::funding(const FundingEvent& e) {
  stream_ << "M " << index(e.venue) << ' ' << e.symbol << ' ' << e.receive_ns << ' ' << e.event_ms << ' '
          << e.next_funding_ms << ' ' << e.mark.raw << ' ' << e.funding_rate.raw << ' ' << e.has_rate << '\n';
  return stream_.good();
}

bool Journal::raw(Venue venue, std::uint64_t receive_ns, std::string_view frame) {
  stream_ << "R " << index(venue) << ' ' << receive_ns << ' ' << frame << '\n';
  return stream_.good();
}

bool Journal::halt(std::uint64_t now_ns, bool halted) {
  stream_ << "H " << now_ns << ' ' << halted << '\n';
  return stream_.good();
}

bool Journal::output(const Output& o) {
  switch (o.kind) {
    case OutputKind::leg_submit:
    case OutputKind::leg_fill: {
      const auto& l = o.leg;
      stream_ << (o.kind == OutputKind::leg_submit ? "I " : "F ") << l.leg_id << ' ' << l.cycle_id << ' '
              << l.symbol << ' ' << index(l.venue) << ' ' << static_cast<int>(l.side) << ' '
              << static_cast<int>(l.purpose) << ' ' << l.quantity.raw << ' ' << l.limit.raw << ' '
              << l.decision_ns << ' ' << l.arrival_ns << ' ' << l.filled.raw << ' ' << l.vwap.raw << ' '
              << l.fee_raw << '\n';
      break;
    }
    case OutputKind::decision: {
      const auto& d = o.decision;
      stream_ << "X " << d.cycle_id << ' ' << d.symbol << ' ' << sign(d.dir) << ' ' << d.decision_ns << ' '
              << d.quantity.raw << ' ' << d.gap_ppm << ' ' << d.exit_gap_ppm << ' ' << d.basis_ppm << ' '
              << d.mean_ppm << ' ' << d.capture_ppm << ' ' << d.fees_ppm << ' ' << d.funding_ppm << ' '
              << d.net_ppm << '\n';
      break;
    }
    case OutputKind::cycle: {
      const auto& c = o.cycle;
      stream_ << "P " << c.cycle_id << ' ' << c.symbol << ' ' << sign(c.dir) << ' ' << exit_reason_name(c.reason)
              << ' ' << c.entry_ns << ' ' << c.exit_ns << ' ' << c.quantity.raw << ' ' << c.entry_gap_ppm << ' '
              << c.exit_gap_ppm << ' ' << c.expected_capture_ppm << ' ' << c.expected_net_ppm << ' '
              << c.gross_raw << ' ' << c.fees_raw << ' ' << c.funding_raw << ' ' << c.net_raw << ' '
              << c.notional_raw << ' ' << c.repairs << '\n';
      break;
    }
    case OutputKind::census: {
      const auto& c = o.census;
      stream_ << "O " << c.symbol << ' ' << static_cast<int>(c.size_index) << ' ' << sign(c.dir) << ' '
              << c.threshold_ppm << ' ' << c.start_ns << ' ' << c.duration_ns << ' ' << c.peak_ppm << '\n';
      break;
    }
    case OutputKind::resync:
      stream_ << "G " << index(o.venue) << ' ' << o.symbol << '\n';
      break;
    case OutputKind::disabled:
      stream_ << "G 9 " << o.symbol << '\n';
      break;
  }
  return stream_.good();
}

// ---------------------------------------------------------------- replay

bool replay_journal(const std::string& path, const RuntimeConfig& config, ReplayResult& result,
                    std::string& error) {
  LineReader input(path);
  if (!input) { error = "cannot open journal " + path; return false; }
  std::vector<Instrument> instruments;
  std::unique_ptr<Engine> engine;
  const auto peer = config.shadow.peer;
  struct PendingFee final {
    int venue{};
    std::uint16_t symbol{};
    std::int64_t taker_ppm{};
    std::int64_t maker_ppm{};
  };
  struct PendingLatency final {
    int venue{};
    std::uint64_t rtt{};
  };
  // Fees and latency samples precede market data in any order (the writer emits
  // each instrument with its fees); they apply once the engine exists.
  std::vector<PendingFee> pending_fees;
  std::vector<PendingLatency> pending_latency;
  auto depth = std::make_unique<DepthEvent>();
  std::string line;
  std::uint64_t line_number = 0;
  const auto ensure_engine = [&]() -> bool {
    if (engine) return true;
    if (instruments.empty()) { error = "journal has no instruments before market data"; return false; }
    engine = std::make_unique<Engine>(instruments, config.strategy, config.risk, config.shadow);
    for (const auto& fee : pending_fees)
      engine->set_fees(venue_from(fee.venue, peer), fee.symbol, fee.taker_ppm, fee.maker_ppm);
    for (const auto& sample : pending_latency) engine->add_latency_sample(venue_from(sample.venue, peer), sample.rtt);
    pending_fees.clear();
    pending_latency.clear();
    return true;
  };
  while (input.next(line)) {
    ++line_number;
    if (line.size() < 2) continue;
    std::istringstream row(line.substr(2));
    const auto bad = [&] {
      error = "malformed record at line " + std::to_string(line_number);
      return false;
    };
    switch (line[0]) {
      case 'Z':
        // Peer-dependent defaults (latency penalty, fallback fees) come from the
        // environment, so the environment must name the same peer as the journal.
        if (header_peer(line) != peer) {
          error = "journal peer is " + std::string(venue_name(header_peer(line))) + "; set CXA_PEER to match";
          return false;
        }
        break;
      case 'Y': {
        Instrument i;
        std::size_t symbol = 0;
        row >> symbol >> i.name >> i.symbol[0] >> i.symbol[1];
        for (std::size_t v = 0; v < kVenues; ++v)
          row >> i.tick[v].raw >> i.step[v].raw >> i.min_qty[v].raw >> i.min_notional[v].raw;
        row >> i.common_step.raw >> i.common_min_qty.raw;
        if (!row || engine || symbol != instruments.size()) return bad();
        instruments.push_back(std::move(i));
        break;
      }
      case 'C': {
        int venue = 0;
        std::uint16_t symbol = 0;
        std::int64_t taker_ppm = 0;
        std::int64_t maker_ppm = 0;
        if (!(row >> venue >> symbol >> taker_ppm)) return bad();
        if (!(row >> maker_ppm)) maker_ppm = taker_ppm;  // archived 3-field C lines
        if (engine) engine->set_fees(venue_from(venue, peer), symbol, taker_ppm, maker_ppm);
        else pending_fees.push_back({venue, symbol, taker_ppm, maker_ppm});
        break;
      }
      case 'L': {
        int venue = 0;
        std::uint64_t receive_ns = 0;
        std::uint64_t rtt = 0;
        if (!(row >> venue >> receive_ns >> rtt)) return bad();
        if (engine) engine->add_latency_sample(venue_from(venue, peer), rtt);
        else pending_latency.push_back({venue, rtt});
        break;
      }
      case 'S': {
        BookSnapshot snapshot;
        int venue = 0;
        if (!(row >> venue >> snapshot.symbol >> snapshot.receive_ns >> snapshot.last_update_id) ||
            !read_levels(row, snapshot.bids) || !read_levels(row, snapshot.asks) || !ensure_engine())
          return bad();
        snapshot.venue = venue_from(venue, peer);
        engine->on_snapshot(snapshot);
        ++result.events;
        break;
      }
      case 'D': {
        int venue = 0;
        auto& e = *depth;
        if (!(row >> venue >> e.symbol >> e.snapshot >> e.receive_ns >> e.event_ms >> e.first_id >> e.final_id >>
              e.previous_final_id) ||
            !read_levels(row, e.bids, e.bid_count) || !read_levels(row, e.asks, e.ask_count) || !ensure_engine())
          return bad();
        e.venue = venue_from(venue, peer);
        engine->on_depth(e);
        ++result.events;
        break;
      }
      case 'M': {
        FundingEvent e;
        int venue = 0;
        if (!(row >> venue >> e.symbol >> e.receive_ns >> e.event_ms >> e.next_funding_ms >> e.mark.raw >>
              e.funding_rate.raw >> e.has_rate) ||
            !ensure_engine())
          return bad();
        e.venue = venue_from(venue, peer);
        engine->on_funding(e);
        ++result.events;
        break;
      }
      default:
        break;  // outputs and raw frames are not inputs
    }
    if (engine) engine->clear_outputs();
  }
  if (!engine) { error = "journal has no market data"; return false; }
  result.hash = engine->state_hash();
  result.metrics = engine->metrics();
  return true;
}

bool gapcheck(const std::string& path, GapReport& report, std::string& error) {
  LineReader input(path);
  if (!input) { error = "cannot open journal " + path; return false; }
  auto books = std::make_unique<std::array<std::array<OrderBook, kMaxSymbols>, kVenues>>();
  std::array<std::array<bool, kMaxSymbols>, kVenues> pending{};  // gapped, not yet recovered
  auto e = std::make_unique<DepthEvent>();
  std::string line;
  while (input.next(line)) {
    if (line.size() < 2 || (line[0] != 'D' && line[0] != 'S')) continue;
    std::istringstream row(line.substr(2));
    int venue = 0;
    if (line[0] == 'S') {
      BookSnapshot s;
      if (!(row >> venue >> s.symbol >> s.receive_ns >> s.last_update_id) || !read_levels(row, s.bids) ||
          !read_levels(row, s.asks) || s.symbol >= kMaxSymbols) {
        error = "malformed snapshot record";
        return false;
      }
      (*books)[static_cast<std::size_t>(venue)][s.symbol].apply_snapshot(s.last_update_id, s.bids, s.asks,
                                                                     s.receive_ns, false);
      continue;
    }
    if (!(row >> venue >> e->symbol >> e->snapshot >> e->receive_ns >> e->event_ms >> e->first_id >> e->final_id >>
          e->previous_final_id) ||
        !read_levels(row, e->bids, e->bid_count) || !read_levels(row, e->asks, e->ask_count) ||
        e->symbol >= kMaxSymbols) {
      error = "malformed depth record";
      return false;
    }
    ++report.depth_records;
    const auto v = static_cast<std::size_t>(venue);
    auto& book = (*books)[v][e->symbol];
    const auto before = book.state();
    const auto ok = venue == 0 ? book.apply_binance(*e) : book.apply_bybit(*e);
    auto& gapped = pending[v][e->symbol];
    if (!ok && book.state() == BookState::gap && before != BookState::gap) {
      ++report.gaps[v];
      gapped = true;
    }
    if (book.verified() && gapped) {
      ++report.recoveries[v];
      gapped = false;
    }
  }
  return true;
}

// ---------------------------------------------------------------- reports

bool daily_report(const std::vector<std::string>& paths, std::string& json, std::string& error) {
  struct Cycle final {
    std::string reason;
    double net_ppm{};
    double net_usd{};
    double gross_usd{};
    double fees_usd{};
    double funding_usd{};
    double hold_s{};
    double expected_net_ppm{};
    std::uint32_t repairs{};
  };
  std::vector<Cycle> cycles;
  std::size_t missed = 0;
  double coverage_ns = 0;
  bool all_pessimistic = !paths.empty();
  std::map<std::string, std::size_t> reasons;
  for (const auto& path : paths) {
    LineReader input(path);
    if (!input) { error = "cannot open journal " + path; return false; }
    FileSpan span;
    std::string line;
    while (input.next(line)) {
      if (line.size() < 2) continue;
      track_span(line, span);
      if (line[0] != 'P') continue;
      std::istringstream row(line.substr(2));
      std::uint64_t cycle_id = 0, entry_ns = 0, exit_ns = 0;
      std::int64_t symbol = 0, dir = 0, qty = 0, entry_gap = 0, exit_gap = 0, exp_capture = 0, exp_net = 0;
      std::int64_t gross = 0, fees = 0, funding = 0, net = 0, notional = 0;
      Cycle c;
      row >> cycle_id >> symbol >> dir >> c.reason >> entry_ns >> exit_ns >> qty >> entry_gap >> exit_gap >>
          exp_capture >> exp_net >> gross >> fees >> funding >> net >> notional >> c.repairs;
      if (!row) { error = "malformed cycle record in " + path; return false; }
      ++reasons[c.reason];
      if (c.reason == "missed") { ++missed; continue; }
      const auto scale = static_cast<double>(kScale);
      c.net_usd = static_cast<double>(net) / scale;
      c.gross_usd = static_cast<double>(gross) / scale;
      c.fees_usd = static_cast<double>(fees) / scale;
      c.funding_usd = static_cast<double>(funding) / scale;
      c.net_ppm = notional == 0 ? 0.0 : static_cast<double>(net) * 1e6 / static_cast<double>(notional);
      c.hold_s = static_cast<double>(exit_ns - entry_ns) / 1e9;
      c.expected_net_ppm = static_cast<double>(exp_net);
      cycles.push_back(std::move(c));
    }
    if (span.start_ns > 0 && span.last_ns > span.start_ns) coverage_ns += static_cast<double>(span.last_ns - span.start_ns);
    if (span.latency_mode != "pessimistic") all_pessimistic = false;
  }
  const auto n = cycles.size();
  double net_usd = 0, gross_usd = 0, fees_usd = 0, funding_usd = 0, sum_ppm = 0, worst_usd = 0, hold = 0, expected = 0;
  std::size_t legged = 0;
  for (const auto& c : cycles) {
    net_usd += c.net_usd;
    gross_usd += c.gross_usd;
    fees_usd += c.fees_usd;
    funding_usd += c.funding_usd;
    sum_ppm += c.net_ppm;
    worst_usd = std::min(worst_usd, c.net_usd);
    hold += c.hold_s;
    expected += c.expected_net_ppm;
    if (c.repairs > 0 || c.reason == "legged_out") ++legged;
  }
  const auto count = static_cast<double>(n);
  const auto mean_ppm = n == 0 ? 0.0 : sum_ppm / count;
  const auto mean_usd = n == 0 ? 0.0 : net_usd / count;
  // Percentile bootstrap of the mean net ppm, fixed seed for reproducible reports.
  double ci_low = 0.0;
  if (n > 1) {
    std::vector<double> means(kBootstrapResamples);
    std::uint64_t state = 0x2545F4914F6CDD1DULL;
    for (auto& mean : means) {
      double total = 0;
      for (std::size_t i = 0; i < n; ++i) {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        total += cycles[state % n].net_ppm;
      }
      mean = total / count;
    }
    std::sort(means.begin(), means.end());
    ci_low = means[static_cast<std::size_t>(0.025 * static_cast<double>(means.size() - 1))];
  }
  const auto days = coverage_ns / 86'400e9;
  const auto legging_rate = n == 0 ? 0.0 : static_cast<double>(legged) / count;
  const bool pass_days = days >= kGateDays;
  const bool pass_cycles = n >= kGateCycles;
  const bool pass_legging = legging_rate <= kGateMaxLeggingRate;
  // Mean net, CI, and worst cycle are reported for edge evaluation; they do not gate.
  const bool pass = all_pessimistic && pass_days && pass_cycles && pass_legging;
  const auto flag = [](bool value) { return value ? "true" : "false"; };
  std::ostringstream out;
  out << "{\"files\":" << paths.size() << ",\"coverage_days\":" << fixed(days, 3) << ",\"cycles\":" << n
      << ",\"missed\":" << missed << ",\"net_usd\":" << fixed(net_usd, 4) << ",\"gross_usd\":" << fixed(gross_usd, 4)
      << ",\"fees_usd\":" << fixed(fees_usd, 4) << ",\"funding_usd\":" << fixed(funding_usd, 4)
      << ",\"mean_net_ppm\":" << fixed(mean_ppm) << ",\"mean_expected_net_ppm\":"
      << fixed(n == 0 ? 0.0 : expected / count) << ",\"bootstrap_ci_low_ppm\":" << fixed(ci_low)
      << ",\"mean_net_usd\":" << fixed(mean_usd, 4) << ",\"worst_cycle_usd\":" << fixed(worst_usd, 4)
      << ",\"legging_rate\":" << fixed(legging_rate, 4) << ",\"mean_hold_s\":" << fixed(n == 0 ? 0.0 : hold / count)
      << ",\"reasons\":{";
  bool first = true;
  for (const auto& [reason, total] : reasons) {
    out << (first ? "" : ",") << '"' << reason << "\":" << total;
    first = false;
  }
  out << "},\"gate\":{\"pessimistic_latency\":" << flag(all_pessimistic) << ",\"days\":" << flag(pass_days)
      << ",\"cycles\":" << flag(pass_cycles) << ",\"legging\":" << flag(pass_legging) << ",\"pass\":" << flag(pass)
      << "}}\n";
  json = out.str();
  return true;
}

bool census_report(const std::vector<std::string>& paths, std::string& json, std::string& error) {
  struct Bucket final {
    std::uint64_t episodes{};
    std::uint64_t ge_5ms{};
    std::uint64_t ge_50ms{};
    std::uint64_t ge_500ms{};
    std::uint64_t ge_5s{};
    std::int64_t peak_ppm{};
  };
  std::map<std::pair<int, std::int64_t>, Bucket> by_size;                  // (size index, threshold)
  std::map<std::pair<std::string, std::int64_t>, Bucket> by_symbol;        // size index 1 only
  double coverage_ns = 0;
  for (const auto& path : paths) {
    LineReader input(path);
    if (!input) { error = "cannot open journal " + path; return false; }
    FileSpan span;
    std::vector<std::string> names;
    std::string line;
    while (input.next(line)) {
      if (line.size() < 2) continue;
      track_span(line, span);
      if (line[0] == 'Y') {
        std::istringstream row(line.substr(2));
        std::size_t symbol = 0;
        std::string name;
        row >> symbol >> name;
        names.push_back(name);
        continue;
      }
      if (line[0] != 'O') continue;
      std::istringstream row(line.substr(2));
      std::size_t symbol = 0;
      int size = 0, dir = 0;
      std::int64_t threshold = 0, peak = 0;
      std::uint64_t start = 0, duration = 0;
      if (!(row >> symbol >> size >> dir >> threshold >> start >> duration >> peak)) {
        error = "malformed census record in " + path;
        return false;
      }
      const auto add = [&](Bucket& b) {
        ++b.episodes;
        if (duration >= 5'000'000) ++b.ge_5ms;
        if (duration >= 50'000'000) ++b.ge_50ms;
        if (duration >= 500'000'000) ++b.ge_500ms;
        if (duration >= 5'000'000'000ULL) ++b.ge_5s;
        b.peak_ppm = std::max(b.peak_ppm, peak);
      };
      add(by_size[{size, threshold}]);
      if (size == 1) add(by_symbol[{symbol < names.size() ? names[symbol] : std::to_string(symbol), threshold}]);
    }
    if (span.start_ns > 0 && span.last_ns > span.start_ns) coverage_ns += static_cast<double>(span.last_ns - span.start_ns);
  }
  const auto days = coverage_ns / 86'400e9;
  const auto per_day = [days](std::uint64_t value) { return days <= 0 ? 0.0 : static_cast<double>(value) / days; };
  const auto write = [&](std::ostringstream& out, const Bucket& b) {
    out << "\"episodes\":" << b.episodes << ",\"per_day\":" << fixed(per_day(b.episodes)) << ",\"ge_5ms\":" << b.ge_5ms
        << ",\"ge_50ms\":" << b.ge_50ms << ",\"ge_500ms\":" << b.ge_500ms << ",\"ge_5s\":" << b.ge_5s
        << ",\"ge_500ms_per_day\":" << fixed(per_day(b.ge_500ms)) << ",\"peak_ppm\":" << b.peak_ppm;
  };
  std::ostringstream out;
  out << "{\"coverage_days\":" << fixed(days, 3) << ",\"by_size\":[";
  bool first = true;
  for (const auto& [key, bucket] : by_size) {
    out << (first ? "" : ",") << "{\"size_index\":" << key.first << ",\"threshold_ppm\":" << key.second << ',';
    write(out, bucket);
    out << '}';
    first = false;
  }
  out << "],\"by_symbol_1k\":[";
  first = true;
  for (const auto& [key, bucket] : by_symbol) {
    out << (first ? "" : ",") << "{\"symbol\":\"" << key.first << "\",\"threshold_ppm\":" << key.second << ',';
    write(out, bucket);
    out << '}';
    first = false;
  }
  out << "]}\n";
  json = out.str();
  return true;
}

}  // namespace cxa
