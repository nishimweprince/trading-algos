#include "bpmm/allocation.hpp"
#include "bpmm/binance.hpp"
#include "bpmm/config.hpp"
#include "bpmm/engine.hpp"
#include "bpmm/journal.hpp"
#include "bpmm/parser.hpp"
#include "bpmm/status.hpp"
#include "bpmm/spsc.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {
std::atomic<bool> stop_requested{false};

void stop_handler(int) { stop_requested.store(true, std::memory_order_relaxed); }

std::uint64_t wall_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

std::string option(int argc, char** argv, std::string_view key, std::string fallback = {}) {
  for (int i = 2; i + 1 < argc; ++i) if (argv[i] == key) return argv[i + 1];
  return fallback;
}

std::string environment(const char* name, std::string fallback) {
  const auto* value = std::getenv(name);
  return value == nullptr ? std::move(fallback) : std::string(value);
}

int benchmark() {
  bpmm::Engine engine;
  bpmm::Snapshot snapshot;
  snapshot.last_update_id = 100;
  snapshot.bids[0] = {bpmm::Decimal::parse("100.00"), bpmm::Decimal::parse("10")};
  snapshot.asks[0] = {bpmm::Decimal::parse("100.02"), bpmm::Decimal::parse("10")};
  snapshot.bid_count = snapshot.ask_count = 1;
  engine.on_snapshot(snapshot);
  engine.on_candle({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("101"),
                    bpmm::Decimal::parse("99"), bpmm::Decimal::parse("100.5"), true});
  constexpr std::size_t iterations = 200'000;
  std::vector<std::uint64_t> samples(iterations);
  bpmm::DepthEvent event;
  event.first_id = 101;
  event.final_id = 101;
  event.previous_final_id = 100;
  event.event_ms = 60'001;
  event.receive_ns = wall_ns();
  event.bid_count = event.ask_count = 2;
  event.bids[0] = snapshot.bids[0];
  event.asks[0] = snapshot.asks[0];
  event.bids[1] = snapshot.bids[0];
  event.asks[1] = snapshot.asks[0];
  engine.on_depth(event, event.receive_ns);
  bpmm::allocation::begin();
  for (std::size_t i = 0; i < iterations; ++i) {
    event.previous_final_id = event.final_id++;
    event.first_id = event.final_id;
    event.receive_ns += 1'000;
    event.event_ms += 1;
    const auto shifted = (i & 1U) != 0;
    event.bids[0] = {bpmm::Price{shifted ? 10'001'000'000LL : 10'000'000'000LL}, {}};
    event.asks[0] = {bpmm::Price{shifted ? 10'003'000'000LL : 10'002'000'000LL}, {}};
    event.bids[1] = {bpmm::Price{shifted ? 10'000'000'000LL : 10'001'000'000LL},
                     bpmm::Decimal::parse("10")};
    event.asks[1] = {bpmm::Price{shifted ? 10'002'000'000LL : 10'003'000'000LL},
                     bpmm::Decimal::parse("10")};
    const auto start = std::chrono::steady_clock::now();
    engine.on_depth(event, event.receive_ns);
    const auto end = std::chrono::steady_clock::now();
    samples[i] = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
  }
  const auto allocations = bpmm::allocation::end();
  std::sort(samples.begin(), samples.end());
  const auto percentile = [&samples](double p) {
    const auto index = static_cast<std::size_t>(p * static_cast<double>(samples.size() - 1));
    return samples[index];
  };
  std::cout << "{\"iterations\":" << iterations << ",\"p50_ns\":" << percentile(0.50)
            << ",\"p99_ns\":" << percentile(0.99) << ",\"p999_ns\":" << percentile(0.999)
            << ",\"max_ns\":" << samples.back() << ",\"hot_allocations\":" << allocations
            << ",\"target_p99_ns\":9000}\n";
  return allocations == 0 && percentile(0.99) <= 9'000 ? 0 : 1;
}

int replay(const std::string& path) {
  bpmm::Engine first;
  bpmm::Engine second;
  std::uint64_t first_hash = 0;
  std::uint64_t second_hash = 0;
  std::string error;
  if (!bpmm::replay_normalized(path, first, first_hash, error) ||
      !bpmm::replay_normalized(path, second, second_hash, error)) {
    std::cerr << error << '\n';
    return 1;
  }
  std::cout << "{\"hash\":" << first_hash << ",\"deterministic\":"
            << (first_hash == second_hash ? "true" : "false") << "}\n";
  return first_hash == second_hash ? 0 : 1;
}

int calibrate(const std::string& path, std::size_t train_size, std::size_t evaluation_size) {
  std::ifstream input(path);
  if (!input) { std::cerr << "cannot open calibration file\n"; return 1; }
  if (train_size < 2 || evaluation_size == 0) {
    std::cerr << "calibration windows must have at least two training rows and one evaluation row\n";
    return 2;
  }
  struct Observation final { double bp; double next_return; };
  std::vector<Observation> observations;
  double bp = 0;
  double next_return = 0;
  char comma = 0;
  while (input >> bp >> comma >> next_return) {
    if (comma != ',') { std::cerr << "expected bp,next_return CSV\n"; return 1; }
    observations.push_back({bp, next_return});
  }
  if (observations.size() < train_size + evaluation_size) {
    std::cerr << "calibration file does not contain one complete train/evaluation window\n";
    return 1;
  }
  std::size_t windows = 0;
  std::size_t evaluated = 0;
  std::size_t direction_correct = 0;
  double squared_error = 0;
  double slope_sum = 0;
  for (std::size_t start = 0; start + train_size + evaluation_size <= observations.size();
       start += evaluation_size) {
    bpmm::RollingRegression regression(train_size);
    for (std::size_t i = start; i < start + train_size; ++i)
      regression.push(observations[i].bp, observations[i].next_return);
    const auto fit = regression.fit();
    if (!fit.calibrated) {
      std::cerr << "singular calibration window at row " << start << '\n';
      return 1;
    }
    slope_sum += fit.slope;
    ++windows;
    for (std::size_t i = start + train_size;
         i < start + train_size + evaluation_size; ++i) {
      const auto forecast = fit.predict(observations[i].bp);
      const auto error = forecast - observations[i].next_return;
      squared_error += error * error;
      if ((forecast >= 0) == (observations[i].next_return >= 0)) ++direction_correct;
      ++evaluated;
    }
  }
  const auto window_count = static_cast<double>(windows);
  const auto evaluated_count = static_cast<double>(evaluated);
  std::cout << "{\"observations\":" << observations.size() << ",\"train_size\":" << train_size
            << ",\"evaluation_size\":" << evaluation_size << ",\"windows\":" << windows
            << ",\"evaluated\":" << evaluated << ",\"mean_slope\":" << slope_sum / window_count
            << ",\"mse\":" << squared_error / evaluated_count << ",\"direction_accuracy\":"
            << static_cast<double>(direction_correct) / evaluated_count << "}\n";
  return 0;
}

int daily_report(const std::string& path) {
  std::ifstream input(path);
  if (!input) { std::cerr << "cannot open journal\n"; return 1; }
  std::uint64_t snapshots = 0, depth = 0, trades = 0, candles = 0, intents = 0, fills = 0;
  std::string line;
  while (std::getline(input, line)) {
    if (line.starts_with("S ")) ++snapshots;
    else if (line.starts_with("D ")) ++depth;
    else if (line.starts_with("T ")) ++trades;
    else if (line.starts_with("K ")) ++candles;
    else if (line.starts_with("I ")) ++intents;
    else if (line.starts_with("F ")) ++fills;
  }
  bpmm::Engine engine;
  std::uint64_t hash = 0;
  std::string replay_error;
  const auto replayed = bpmm::replay_normalized(path, engine, hash, replay_error);
  std::cout << "{\"snapshots\":" << snapshots << ",\"depth_events\":" << depth
            << ",\"trades\":" << trades << ",\"candles\":" << candles
            << ",\"intents\":" << intents << ",\"fills\":" << fills
            << ",\"realized_pnl_raw\":" << engine.metrics().realized_pnl_raw
            << ",\"state_hash\":" << hash << ",\"replay_ok\":"
            << (replayed ? "true" : "false") << "}\n";
  if (!replayed) std::cerr << replay_error << '\n';
  return replayed ? 0 : 1;
}

int drill() {
  bpmm::Engine engine;
  bpmm::Snapshot snap;
  snap.last_update_id = 10;
  snap.bids[0] = {bpmm::Decimal::parse("100"), bpmm::Decimal::parse("5")};
  snap.asks[0] = {bpmm::Decimal::parse("100.02"), bpmm::Decimal::parse("5")};
  snap.bid_count = snap.ask_count = 1;
  engine.on_snapshot(snap);
  engine.on_candle({0, 59'999, bpmm::Decimal::parse("100"), bpmm::Decimal::parse("101"),
                    bpmm::Decimal::parse("99"), bpmm::Decimal::parse("100.5"), true});
  bpmm::DepthEvent event;
  event.receive_ns = 1'000'000'000;
  event.event_ms = 60'000;
  event.first_id = event.final_id = 11;
  event.previous_final_id = 10;
  event.bids[0] = snap.bids[0];
  event.asks[0] = snap.asks[0];
  event.bid_count = event.ask_count = 1;
  engine.on_depth(event, event.receive_ns);
  event.first_id = event.final_id = 12;
  event.previous_final_id = 9;
  engine.on_depth(event, event.receive_ns + 1);
  const auto gap_halted = engine.halted();
  snap.last_update_id = 20;
  engine.on_snapshot(snap);
  event.first_id = event.final_id = 21;
  event.previous_final_id = 20;
  engine.on_depth(event, event.receive_ns + 2);
  const auto recovered = !engine.halted();
  engine.kill();
  snap.last_update_id = 30;
  engine.on_snapshot(snap);
  event.first_id = event.final_id = 31;
  event.previous_final_id = 30;
  engine.on_depth(event, event.receive_ns + 3);
  const auto kill_persisted = engine.halted();
  std::cout << "{\"gap_halted\":" << (gap_halted ? "true" : "false")
            << ",\"resync_recovered\":" << (recovered ? "true" : "false")
            << ",\"kill_persisted\":" << (kill_persisted ? "true" : "false") << "}\n";
  return gap_halted && recovered && kill_persisted ? 0 : 1;
}

enum class FeedKind : std::uint8_t { snapshot, parsed };
struct FeedMessage final {
  FeedKind kind{FeedKind::parsed};
  std::uint64_t receive_ns{};
  bpmm::Snapshot snapshot{};
  bpmm::ParsedEvent parsed{};
};

struct RawRecord final {
  std::uint64_t receive_ns{};
  std::string frame;
};

enum class RecordKind : std::uint8_t { snapshot, depth, trade, candle, mark_price, intent, fill };
struct NormalRecord final {
  RecordKind kind{RecordKind::depth};
  std::uint64_t receive_ns{};
  bpmm::Snapshot snapshot{};
  bpmm::DepthEvent depth{};
  bpmm::TradeEvent trade{};
  bpmm::CandleEvent candle{};
  bpmm::MarkPriceEvent mark_price{};
  bpmm::QuoteIntent intent{};
  bpmm::ShadowFill fill{};
};

int shadow(int argc, char** argv) {
  if (option(argc, argv, "--mode", "shadow") != "shadow") {
    std::cerr << "live order modes are disabled; only --mode shadow is accepted\n";
    return 2;
  }
  const auto symbol = option(argc, argv, "--symbol", environment("BPMM_SYMBOL", "BTCUSDT"));
  const auto journal_path = option(argc, argv, "--journal", environment("BPMM_JOURNAL", "data/bar-mm.jsonl"));
  const auto kill_path = option(argc, argv, "--kill-file", environment("BPMM_KILL_FILE", "data/KILL"));
  const auto duration_text = option(argc, argv, "--duration-seconds", "0");
  const auto port_text = option(argc, argv, "--port", environment("BPMM_STATUS_PORT", "8060"));
  const auto duration = std::stoull(duration_text);
  const auto port = static_cast<std::uint16_t>(std::stoul(port_text));
  const auto parent = std::filesystem::path(journal_path).parent_path();
  if (!parent.empty()) std::filesystem::create_directories(parent);
  bpmm::Journal journal(journal_path);
  if (!journal.good()) { std::cerr << "cannot open journal\n"; return 1; }
  const auto runtime = bpmm::load_runtime_config();
  bpmm::Engine engine(runtime.strategy, runtime.risk, runtime.shadow_latency_ns,
                      runtime.maker_fee_ppm);
  std::atomic<bool> kill_file_active{std::filesystem::exists(kill_path)};
  std::atomic<bool> status_ready{false};
  std::atomic<bool> status_halted{kill_file_active.load()};
  std::atomic<std::uint64_t> status_depth{0};
  std::atomic<std::uint64_t> status_trades{0};
  std::atomic<std::uint64_t> status_fills{0};
  std::atomic<std::int64_t> status_realized{0};
  std::atomic<std::int64_t> status_position{0};
  std::atomic<std::int64_t> status_entry{0};
  std::atomic<std::int64_t> status_mid{0};
  std::atomic<std::uint64_t> status_intents{0};
  std::atomic<std::uint64_t> status_rejects{0};
  std::atomic<std::uint64_t> status_gaps{0};
  std::atomic<std::uint64_t> status_candles{0};
  std::atomic<bool> feed_done{false};
  std::atomic<bool> decision_done{false};
  std::atomic<bool> pipeline_failed{false};
  std::mutex pipeline_error_mutex;
  std::string pipeline_error;
  const auto fail_pipeline = [&](std::string message) {
    bool expected = false;
    if (pipeline_failed.compare_exchange_strong(expected, true)) {
      std::lock_guard lock(pipeline_error_mutex);
      pipeline_error = std::move(message);
    }
    stop_requested.store(true, std::memory_order_relaxed);
  };
  auto feed_queue = std::make_unique<bpmm::SpscRing<FeedMessage, 512>>();
  auto raw_queue = std::make_unique<bpmm::SpscRing<RawRecord, 2048>>();
  auto record_queue = std::make_unique<bpmm::SpscRing<NormalRecord, 1024>>();
  bpmm::StatusServer status(port, [&] {
    return std::string{"{\"service_live\":true,\"ready\":"} +
           (status_ready.load() ? "true" : "false") + ",\"mode\":\"shadow\",\"symbol\":\"" + symbol +
           "\",\"halted\":" + (status_halted.load() ? "true" : "false") +
           ",\"depth_events\":" + std::to_string(status_depth.load()) +
           ",\"trade_events\":" + std::to_string(status_trades.load()) +
           ",\"fills\":" + std::to_string(status_fills.load()) +
           ",\"realized_pnl_raw\":" + std::to_string(status_realized.load()) +
           ",\"position_raw\":" + std::to_string(status_position.load()) +
           ",\"entry_raw\":" + std::to_string(status_entry.load()) +
           ",\"mid_raw\":" + std::to_string(status_mid.load()) +
           ",\"intents\":" + std::to_string(status_intents.load()) +
           ",\"risk_rejects\":" + std::to_string(status_rejects.load()) +
           ",\"gaps\":" + std::to_string(status_gaps.load()) +
           ",\"closed_candles\":" + std::to_string(status_candles.load()) + "}\n";
  }, journal_path);
  std::string error;
  if (!status.start(error)) { std::cerr << "status server: " << error << '\n'; return 1; }

  std::thread recorder([&] {
    RawRecord raw;
    NormalRecord record;
    auto last_flush = std::chrono::steady_clock::now();
    while (!decision_done.load(std::memory_order_acquire) ||
           !feed_done.load(std::memory_order_acquire) || !raw_queue->empty() ||
           !record_queue->empty()) {
      bool worked = false;
      while (raw_queue->try_pop(raw)) {
        worked = true;
        if (!journal.raw(raw.receive_ns, raw.frame)) fail_pipeline("raw journal write failed");
      }
      while (record_queue->try_pop(record)) {
        worked = true;
        bool written = false;
        switch (record.kind) {
          case RecordKind::snapshot: written = journal.snapshot(record.snapshot, record.receive_ns); break;
          case RecordKind::depth: written = journal.depth(record.depth); break;
          case RecordKind::trade: written = journal.trade(record.trade); break;
          case RecordKind::candle: written = journal.candle(record.candle); break;
          case RecordKind::mark_price: written = journal.mark_price(record.mark_price); break;
          case RecordKind::intent: written = journal.intent(record.intent); break;
          case RecordKind::fill: written = journal.fill(record.fill); break;
        }
        if (!written) fail_pipeline("normalized journal write failed");
      }
      if (!worked) std::this_thread::sleep_for(std::chrono::microseconds(100));
      const auto now = std::chrono::steady_clock::now();
      if (now - last_flush >= std::chrono::seconds(1)) {
        journal.flush();
        if (!journal.good()) fail_pipeline("journal flush failed");
        last_flush = now;
      }
    }
    journal.flush();
  });

  std::thread kill_watcher([&] {
    while (!feed_done.load(std::memory_order_acquire) ||
           !decision_done.load(std::memory_order_acquire)) {
      kill_file_active.store(std::filesystem::exists(kill_path), std::memory_order_release);
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });

  std::thread decision([&] {
    bool kill_applied = false;
    FeedMessage message;
    const auto push_record = [&](NormalRecord record) {
      if (!record_queue->try_push(std::move(record))) fail_pipeline("normalized recorder queue overflow");
    };
    while (!feed_done.load(std::memory_order_acquire) || !feed_queue->empty()) {
      if (!feed_queue->try_pop(message)) {
        std::this_thread::sleep_for(std::chrono::microseconds(50));
        continue;
      }
      const auto kill_now = kill_file_active.load(std::memory_order_acquire);
      if (kill_now && !kill_applied) engine.kill();
      if (!kill_now && kill_applied) engine.acknowledge_halt();
      kill_applied = kill_now;
      if (message.kind == FeedKind::snapshot) {
        if (!engine.on_snapshot(message.snapshot)) {
          fail_pipeline("invalid Binance depth snapshot");
          continue;
        }
        NormalRecord record;
        record.kind = RecordKind::snapshot;
        record.receive_ns = message.receive_ns;
        record.snapshot = message.snapshot;
        push_record(std::move(record));
      } else if (message.parsed.kind == bpmm::ParsedKind::depth) {
        NormalRecord record;
        record.kind = RecordKind::depth;
        record.depth = message.parsed.depth;
        push_record(std::move(record));
        const auto intents = engine.on_depth(message.parsed.depth, message.receive_ns);
        for (std::size_t i = 0; i < intents.size; ++i) {
          NormalRecord intent_record;
          intent_record.kind = RecordKind::intent;
          intent_record.intent = intents.values[i];
          push_record(std::move(intent_record));
        }
        status_depth.store(engine.metrics().depth_events);
        status_intents.store(engine.metrics().intents);
        status_rejects.store(engine.metrics().risk_rejects);
        status_gaps.store(engine.metrics().gaps);
        const auto& book = engine.book();
        status_mid.store((book.best_bid().price.raw + book.best_ask().price.raw) / 2);
      } else if (message.parsed.kind == bpmm::ParsedKind::trade) {
        NormalRecord record;
        record.kind = RecordKind::trade;
        record.trade = message.parsed.trade;
        push_record(std::move(record));
        engine.on_trade(message.parsed.trade);
        for (const auto& fill : engine.fill_events()) {
          NormalRecord fill_record;
          fill_record.kind = RecordKind::fill;
          fill_record.fill = fill;
          push_record(std::move(fill_record));
        }
        engine.clear_fill_events();
        status_trades.store(engine.metrics().trade_events);
        status_fills.store(engine.metrics().fills);
        status_realized.store(engine.metrics().realized_pnl_raw);
        status_position.store(engine.position().quantity_raw);
        status_entry.store(engine.position().average_entry.raw);
      } else if (message.parsed.kind == bpmm::ParsedKind::candle) {
        NormalRecord record;
        record.kind = RecordKind::candle;
        record.candle = message.parsed.candle;
        push_record(std::move(record));
        engine.on_candle(message.parsed.candle);
        status_candles.store(engine.metrics().closed_candles);
      } else if (message.parsed.kind == bpmm::ParsedKind::mark_price) {
        NormalRecord record;
        record.kind = RecordKind::mark_price;
        record.mark_price = message.parsed.mark_price;
        push_record(std::move(record));
        engine.set_next_funding_ms(message.parsed.mark_price.next_funding_ms);
      }
      status_ready.store(engine.ready());
      status_halted.store(engine.halted());
    }
    decision_done.store(true, std::memory_order_release);
  });

  bpmm::BinancePublicClient client(
      symbol,
      [&](std::uint64_t receive_ns, const bpmm::Snapshot& snapshot) {
        FeedMessage message;
        message.kind = FeedKind::snapshot;
        message.receive_ns = receive_ns;
        message.snapshot = snapshot;
        if (!feed_queue->try_push(std::move(message))) {
          fail_pipeline("decision queue overflow on snapshot");
          return false;
        }
        return true;
      },
      [&](std::uint64_t receive_ns, std::string_view frame) {
        if (!raw_queue->try_push(RawRecord{receive_ns, std::string(frame)})) {
          fail_pipeline("raw recorder queue overflow");
          return false;
        }
        bpmm::ParsedEvent parsed;
        if (!bpmm::parse_stream_event(frame, receive_ns, parsed)) {
          if (frame.find("depthUpdate") != std::string_view::npos) {
            fail_pipeline("malformed depth frame");
            return false;
          }
          return true;
        }
        FeedMessage message;
        message.kind = FeedKind::parsed;
        message.receive_ns = receive_ns;
        message.parsed = parsed;
        if (!feed_queue->try_push(std::move(message))) {
          fail_pipeline("decision queue overflow");
          return false;
        }
        return true;
      });
  std::thread timer;
  std::atomic<bool> client_done{false};
  if (duration > 0) {
    timer = std::thread([duration, &client_done] {
      const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(duration);
      while (!client_done.load(std::memory_order_relaxed) &&
             std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      if (client_done.load(std::memory_order_relaxed)) return;
      stop_requested.store(true, std::memory_order_relaxed);
    });
  }
  const auto ok = client.run(stop_requested, error);
  client_done.store(true, std::memory_order_relaxed);
  feed_done.store(true, std::memory_order_release);
  if (timer.joinable()) timer.join();
  decision.join();
  kill_watcher.join();
  recorder.join();
  status.stop();
  const auto& metrics = engine.metrics();
  std::cout << "{\"ready\":" << (engine.ready() ? "true" : "false")
            << ",\"depth_events\":" << metrics.depth_events << ",\"trade_events\":"
            << metrics.trade_events << ",\"candles\":" << metrics.closed_candles
            << ",\"intents\":" << metrics.intents << ",\"fills\":" << metrics.fills << "}\n";
  if (!ok) std::cerr << error << '\n';
  if (pipeline_failed.load()) {
    std::lock_guard lock(pipeline_error_mutex);
    std::cerr << pipeline_error << '\n';
  }
  return ok && !pipeline_failed.load() ? 0 : 1;
}

void usage() {
  std::cerr << "usage: bar-mm <shadow|benchmark|replay|gapcheck|calibrate|daily-report|drill> [options]\n";
}
}  // namespace

int main(int argc, char** argv) {
  std::signal(SIGINT, stop_handler);
  std::signal(SIGTERM, stop_handler);
  if (argc < 2) { usage(); return 2; }
  const std::string command = argv[1];
  if (command == "benchmark") return benchmark();
  if (command == "shadow") {
    try { return shadow(argc, argv); }
    catch (const std::exception& error) { std::cerr << "configuration: " << error.what() << '\n'; return 2; }
  }
  if (command == "replay" && argc >= 3) return replay(argv[2]);
  if (command == "calibrate" && argc >= 3) {
    const auto train = static_cast<std::size_t>(std::stoull(option(argc, argv, "--train", "51840")));
    const auto evaluation = static_cast<std::size_t>(std::stoull(option(argc, argv, "--evaluate", "12959")));
    return calibrate(argv[2], train, evaluation);
  }
  if (command == "daily-report" && argc >= 3) return daily_report(argv[2]);
  if (command == "drill") return drill();
  if (command == "gapcheck" && argc >= 3) {
    std::string error;
    const auto ok = bpmm::gapcheck(argv[2], error);
    std::cout << "{\"ok\":" << (ok ? "true" : "false") << "}\n";
    if (!ok) std::cerr << error << '\n';
    return ok ? 0 : 1;
  }
  usage();
  return 2;
}
