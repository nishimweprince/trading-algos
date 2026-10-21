#include "cxa/allocation.hpp"
#include "cxa/config.hpp"
#include "cxa/engine.hpp"
#include "cxa/feeds.hpp"
#include "cxa/journal.hpp"
#include "cxa/rest.hpp"
#include "cxa/scenario.hpp"
#include "cxa/spsc.hpp"
#include "cxa/status.hpp"

#include <unistd.h>

#include <algorithm>
#include <array>
#include <ctime>
#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {
using namespace cxa;

std::atomic<bool> stop_requested{false};
void stop_handler(int) { stop_requested.store(true, std::memory_order_relaxed); }

std::string option(int argc, char** argv, std::string_view key, std::string fallback = {}) {
  for (int i = 2; i + 1 < argc; ++i)
    if (argv[i] == key) return argv[i + 1];
  return fallback;
}

bool flag(int argc, char** argv, std::string_view key) {
  for (int i = 2; i < argc; ++i)
    if (argv[i] == key) return true;
  return false;
}

std::vector<std::string> positional(int argc, char** argv) {
  std::vector<std::string> values;
  for (int i = 2; i < argc; ++i) {
    const std::string_view arg(argv[i]);
    if (arg.starts_with("--")) { ++i; continue; }
    values.emplace_back(arg);
  }
  return values;
}

std::string environment(const char* name, std::string fallback) {
  const auto* value = std::getenv(name);
  return value == nullptr ? std::move(fallback) : std::string(value);
}

// "%" fields expand as strftime in UTC so each run (and each daily restart) gets its
// own journal file, e.g. data/crossex-%Y%m%dT%H%M%SZ.jsonl.
std::string expand_time(const std::string& pattern) {
  if (pattern.find('%') == std::string::npos) return pattern;
  const auto now = std::time(nullptr);
  std::tm utc{};
  gmtime_r(&now, &utc);
  std::array<char, 512> buffer{};
  const auto size = std::strftime(buffer.data(), buffer.size(), pattern.c_str(), &utc);
  return size == 0 ? pattern : std::string(buffer.data(), size);
}

std::vector<std::string> venue_symbols(const std::vector<Instrument>& basket, Venue venue) {
  std::vector<std::string> symbols;
  for (const auto& instrument : basket) symbols.push_back(instrument.symbol[index(venue)]);
  return symbols;
}

bool parse_frame(Venue venue, std::string_view frame, std::uint64_t receive_ns,
                 const std::vector<std::string>& symbols, ParsedFrame& parsed) {
  return venue == Venue::binance ? parse_binance_frame(frame, receive_ns, symbols, parsed)
                                 : parse_bybit_frame(frame, receive_ns, symbols, parsed);
}

using FeedRunner = std::function<bool(std::atomic<bool>&, ResyncFlags&, std::string&)>;

// The slot-1 feed for the configured peer.
FeedRunner make_peer_feed(Venue, std::vector<std::string> symbols, FrameHandler frames) {
  auto feed = std::make_shared<BybitFeed>(std::move(symbols), std::move(frames));
  return [feed](std::atomic<bool>& stop, ResyncFlags& resync, std::string& error) {
    return feed->run(stop, resync, error);
  };
}

std::uint64_t percentile(std::vector<std::uint64_t> values, double p) {
  if (values.empty()) return 0;
  const auto k = static_cast<std::size_t>(p * static_cast<double>(values.size() - 1));
  std::nth_element(values.begin(), values.begin() + static_cast<std::ptrdiff_t>(k), values.end());
  return values[k];
}

std::string phase_name(PairPhase phase) {
  switch (phase) {
    case PairPhase::flat: return "flat";
    case PairPhase::opening: return "opening";
    case PairPhase::open: return "open";
    case PairPhase::closing: return "closing";
  }
  return "unknown";
}

std::string book_name(BookState state) {
  switch (state) {
    case BookState::empty: return "empty";
    case BookState::syncing: return "syncing";
    case BookState::verified: return "ok";
    case BookState::gap: return "gap";
  }
  return "unknown";
}

std::string summary_json(const Engine& engine, std::string_view latency_mode, std::string_view exec_mode,
                         bool kill_file) {
  const auto& m = engine.metrics();
  std::ostringstream out;
  bool ready = true;
  out << "{\"service_live\":true,\"mode\":\"shadow\",\"peer\":\"" << venue_name(engine.peer())
      << "\",\"latency_mode\":\"" << latency_mode
      << "\",\"exec_mode\":\"" << exec_mode << "\",\"halted\":" << (engine.halted() ? "true" : "false")
      << ",\"loss_halted\":" << (engine.loss_halted() ? "true" : "false")
      << ",\"kill_file\":" << (kill_file ? "true" : "false") << ",\"net_raw\":" << m.net_raw
      << ",\"fees_raw\":" << m.fees_raw << ",\"funding_raw\":" << m.funding_raw << ",\"cycles\":" << m.cycles
      << ",\"missed\":" << m.missed << ",\"repairs\":" << m.repairs << ",\"legs\":" << m.legs
      << ",\"fills\":" << m.fills << ",\"open_pairs\":" << m.open_pairs << ",\"census_episodes\":" << m.census_episodes
      << ",\"depth_events\":[" << m.depth_events[0] << ',' << m.depth_events[1] << "],\"gaps\":[" << m.gaps[0] << ','
      << m.gaps[1] << "],\"funding_settlements\":" << m.funding_settlements << ",\"latency\":[";
  for (std::size_t v = 0; v < kVenues; ++v) {
    const auto& model = engine.latency(slot_venue(v, engine.peer()));
    out << (v ? "," : "") << "{\"samples\":" << model.samples() << ",\"p50_ns\":" << model.percentile(0.5)
        << ",\"p95_ns\":" << model.percentile(0.95) << '}';
  }
  out << "],\"risk\":{";
  for (std::size_t i = 1; i < m.risk.size(); ++i)
    out << (i > 1 ? "," : "") << '"' << risk_code_name(static_cast<RiskCode>(i)) << "\":" << m.risk[i];
  out << "},\"symbols\":[";
  for (std::size_t s = 0; s < engine.instruments().size(); ++s) {
    const auto& b = engine.book(Venue::binance, s);
    const auto& y = engine.book(engine.peer(), s);
    ready = ready && b.verified() && y.verified();
    const auto mid_b = b.mid().raw;
    const auto mid_y = y.mid().raw;
    const auto basis = mid_b > 0 && mid_y > 0 ? ratio_ppm(mid_y - mid_b, (mid_y + mid_b) / 2) : 0;
    const auto& pair = engine.pair(s);
    out << (s ? "," : "") << "{\"name\":\"" << engine.instruments()[s].name << "\",\"books\":\""
        << book_name(b.state()) << '/' << book_name(y.state()) << "\",\"basis_ppm\":" << basis
        << ",\"mean_ppm\":" << engine.basis_mean_ppm(s) << ",\"warm\":" << (engine.basis_warm(s) ? "true" : "false")
        << ",\"disabled\":" << (engine.symbol_disabled(s) ? "true" : "false") << ",\"fee_ppm\":["
        << engine.taker_ppm(Venue::binance, s) << ',' << engine.taker_ppm(engine.peer(), s)
        << "],\"maker_ppm\":[" << engine.maker_ppm(Venue::binance, s) << ',' << engine.maker_ppm(engine.peer(), s)
        << "],\"funding_ppm\":["
        << engine.funding(Venue::binance, s).rate.raw / 100 << ',' << engine.funding(engine.peer(), s).rate.raw / 100
        << "],\"phase\":\"" << phase_name(pair.phase) << "\",\"dir\":" << sign(pair.dir) << ",\"qty_raw\":["
        << pair.qty_raw[0] << ',' << pair.qty_raw[1] << "]}";
  }
  out << "],\"ready\":" << (ready ? "true" : "false") << "}\n";
  return out.str();
}

// ---------------------------------------------------------------- shadow

enum class FeedKind : std::uint8_t { depth, funding, snapshot, gap };
struct FeedMessage final {
  FeedKind kind{FeedKind::depth};
  std::uint64_t receive_ns{};
  DepthEvent depth{};
  FundingEvent funding{};
  std::unique_ptr<BookSnapshot> snapshot;
};

enum class AuxKind : std::uint8_t { fee, latency };
struct AuxMessage final {
  AuxKind kind{AuxKind::latency};
  Venue venue{Venue::binance};
  std::uint16_t symbol{};
  std::int64_t taker_ppm{};
  std::int64_t maker_ppm{};
  std::uint64_t receive_ns{};
  std::uint64_t rtt_ns{};
};

enum class RecordKind : std::uint8_t { depth, funding, snapshot, aux, output, halt };
struct Record final {
  RecordKind kind{RecordKind::depth};
  DepthEvent depth{};
  FundingEvent funding{};
  std::unique_ptr<BookSnapshot> snapshot;
  AuxMessage aux{};
  Output output{};
  std::uint64_t now_ns{};
  bool halted{};
};

struct RawRecord final {
  std::uint64_t receive_ns{};
  std::string frame;
};

// Slots carry a 16 KB DepthEvent; sizes keep resident memory near 100 MB.
using FeedQueue = SpscRing<FeedMessage, 1024>;
using AuxQueue = SpscRing<AuxMessage, 1024>;
using RecordQueue = SpscRing<Record, 2048>;
using RawQueue = SpscRing<RawRecord, 8192>;

int shadow(int argc, char** argv) {
  // Refuse before touching config, keys, or the network.
  if (option(argc, argv, "--mode", "shadow") != "shadow") {
    std::cerr << "live order modes are not implemented in Phase 1; only --mode shadow is accepted\n";
    return 2;
  }
  const auto journal_path =
      expand_time(option(argc, argv, "--journal", environment("CXA_JOURNAL", "data/crossex-%Y%m%dT%H%M%SZ.jsonl")));
  const auto kill_path = option(argc, argv, "--kill-file", environment("CXA_KILL_FILE", "data/KILL"));
  const auto duration = std::stoull(option(argc, argv, "--duration-seconds", "0"));
  const auto port = static_cast<std::uint16_t>(std::stoul(option(argc, argv, "--port", environment("CXA_STATUS_PORT", "8070"))));
  const auto public_only = flag(argc, argv, "--public-only");
  const auto record_raw = flag(argc, argv, "--record-raw");
  if (!websocket_supported()) {
    std::cerr << "linked libcurl lacks wss:// support; build against a curl with WebSockets enabled\n";
    return 1;
  }
  const auto config = load_runtime_config();
  const auto peer = config.shadow.peer;
  auto basket = parse_basket(config.basket, config.shadow.peer);

  HttpClient http;
  std::string error;
  if (!load_instruments(http, basket, error, config.shadow.peer)) { std::cerr << "instruments: " << error << '\n'; return 1; }

  std::array<Credentials, kVenues> credentials{load_credentials(Venue::binance), load_credentials(peer)};
  std::array<std::vector<std::int64_t>, kVenues> taker_fees;
  std::array<std::vector<std::int64_t>, kVenues> maker_fees;
  if (public_only) {
    for (std::size_t v = 0; v < kVenues; ++v) {
      taker_fees[v].assign(basket.size(), config.shadow.fallback_taker_ppm[v]);
      maker_fees[v].assign(basket.size(), config.shadow.fallback_maker_ppm[v]);
    }
  } else {
    for (std::size_t v = 0; v < kVenues; ++v) {
      const auto venue = slot_venue(v, peer);
      std::string detail;
      if (!verify_read_only(http, venue, credentials[v], detail)) {
        if (environment("CXA_ALLOW_TRADING_KEYS", "0") != "1") {
          std::cerr << "key check failed: " << detail << " (use read-only keys, or --public-only)\n";
          return 2;
        }
        std::cerr << "WARNING: " << detail << "; proceeding with a trading-capable key "
                  << "(CXA_ALLOW_TRADING_KEYS=1)\n";
      } else {
        std::cerr << detail << '\n';
      }
      if (!fetch_fees(http, venue, credentials[v], basket, taker_fees[v], maker_fees[v], error)) {
        std::cerr << "fees: " << error << '\n';
        return 1;
      }
    }
  }

  const auto parent = std::filesystem::path(journal_path).parent_path();
  if (!parent.empty()) std::filesystem::create_directories(parent);
  Journal journal(journal_path);
  if (!journal.good() || !journal.header(config, public_only ? "shadow-public" : "shadow", wall_ns())) {
    std::cerr << "cannot open journal\n";
    return 1;
  }
  auto engine = std::make_unique<Engine>(basket, config.strategy, config.risk, config.shadow);
  for (std::size_t s = 0; s < basket.size(); ++s) {
    journal.instrument(s, basket[s]);
    for (std::size_t v = 0; v < kVenues; ++v) {
      engine->set_fees(slot_venue(v, peer), static_cast<std::uint16_t>(s), taker_fees[v][s], maker_fees[v][s]);
      journal.fee(slot_venue(v, peer), s, taker_fees[v][s], maker_fees[v][s]);
    }
  }
  journal.flush();

  const auto binance_symbols = venue_symbols(basket, Venue::binance);
  const auto peer_symbols = venue_symbols(basket, peer);
  std::array<ResyncFlags, kVenues> resync{};
  auto binance_queue = std::make_unique<FeedQueue>();
  auto peer_queue = std::make_unique<FeedQueue>();
  auto aux_queue = std::make_unique<AuxQueue>();
  auto record_queue = std::make_unique<RecordQueue>();
  std::array<std::unique_ptr<RawQueue>, kVenues> raw_queues{std::make_unique<RawQueue>(), std::make_unique<RawQueue>()};

  std::atomic<bool> kill_file_active{std::filesystem::exists(kill_path)};
  std::atomic<bool> feeds_done{false};
  std::atomic<bool> decision_done{false};
  std::atomic<bool> pipeline_failed{false};
  std::mutex error_mutex;
  std::string pipeline_error;
  const auto fail = [&](std::string message) {
    bool expected = false;
    if (pipeline_failed.compare_exchange_strong(expected, true)) {
      std::lock_guard lock(error_mutex);
      pipeline_error = std::move(message);
    }
    stop_requested.store(true, std::memory_order_relaxed);
  };

  std::mutex summary_mutex;
  std::string summary = "{\"service_live\":true,\"ready\":false}\n";
  StatusServer status(port, [&] {
    std::lock_guard lock(summary_mutex);
    return summary;
  }, journal_path);
  if (!status.start(error)) { std::cerr << "status server: " << error << '\n'; return 1; }

  std::thread recorder([&] {
    Record record;
    RawRecord raw;
    auto last_flush = std::chrono::steady_clock::now();
    while (!decision_done.load(std::memory_order_acquire) || !record_queue->empty() || !raw_queues[0]->empty() ||
           !raw_queues[1]->empty()) {
      bool worked = false;
      for (std::size_t v = 0; v < kVenues; ++v) {
        while (raw_queues[v]->try_pop(raw)) {
          worked = true;
          if (!journal.raw(slot_venue(v, peer), raw.receive_ns, raw.frame)) fail("raw journal write failed");
        }
      }
      while (record_queue->try_pop(record)) {
        worked = true;
        bool written = false;
        switch (record.kind) {
          case RecordKind::depth: written = journal.depth(record.depth); break;
          case RecordKind::funding: written = journal.funding(record.funding); break;
          case RecordKind::snapshot: written = journal.snapshot(*record.snapshot); break;
          case RecordKind::aux:
            written = record.aux.kind == AuxKind::fee
                          ? journal.fee(record.aux.venue, record.aux.symbol, record.aux.taker_ppm,
                                        record.aux.maker_ppm)
                          : journal.latency(record.aux.venue, record.aux.receive_ns, record.aux.rtt_ns);
            break;
          case RecordKind::output: written = journal.output(record.output); break;
          case RecordKind::halt: written = journal.halt(record.now_ns, record.halted); break;
        }
        record.snapshot.reset();
        if (!written) fail("journal write failed");
      }
      const auto now = std::chrono::steady_clock::now();
      if (now - last_flush >= std::chrono::seconds(1)) {
        journal.flush();
        if (!journal.good()) fail("journal flush failed");
        last_flush = now;
      }
      if (!worked) std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
    journal.flush();
  });

  std::thread kill_watcher([&] {
    while (!decision_done.load(std::memory_order_acquire)) {
      kill_file_active.store(std::filesystem::exists(kill_path), std::memory_order_release);
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });

  std::thread aux;
  std::atomic<bool> aux_stop{false};
  if (!public_only) {
    aux = std::thread([&] {
      HttpClient probe_http;
      auto next_fees = std::chrono::steady_clock::now() + std::chrono::hours(1);
      while (!aux_stop.load(std::memory_order_relaxed)) {
        for (std::size_t v = 0; v < kVenues; ++v) {
          const auto venue = slot_venue(v, peer);
          const auto result = signed_probe(probe_http, venue, credentials[v]);
          if (!result.ok) continue;
          if (!aux_queue->try_push({AuxKind::latency, venue, 0, 0, 0, wall_ns(), result.elapsed_ns}))
            fail("aux queue overflow");
        }
        if (std::chrono::steady_clock::now() >= next_fees) {
          next_fees += std::chrono::hours(1);
          for (std::size_t v = 0; v < kVenues; ++v) {
            std::vector<std::int64_t> updated_taker;
            std::vector<std::int64_t> updated_maker;
            std::string fee_error;
            if (!fetch_fees(probe_http, slot_venue(v, peer), credentials[v], basket, updated_taker, updated_maker,
                            fee_error)) {
              std::cerr << "fee refresh: " << fee_error << '\n';
              continue;
            }
            for (std::size_t s = 0; s < updated_taker.size(); ++s)
              aux_queue->try_push({AuxKind::fee, slot_venue(v, peer), static_cast<std::uint16_t>(s),
                                   updated_taker[s], updated_maker[s], wall_ns(), 0});
          }
        }
        for (int i = 0; i < 20 && !aux_stop.load(std::memory_order_relaxed); ++i)
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    });
  }

  std::thread decision([&] {
    bool kill_applied = false;
    FeedMessage message;
    AuxMessage aux_message;
    auto last_summary = std::chrono::steady_clock::now() - std::chrono::seconds(10);
    const auto push = [&](Record&& record) {
      if (!record_queue->try_push(std::move(record))) fail("recorder queue overflow");
    };
    const auto drain_outputs = [&] {
      if (engine->metrics().output_overflow) fail("engine output overflow");
      for (const auto& output : engine->outputs()) {
        if (output.kind == OutputKind::resync && output.symbol < kMaxSymbols)
          resync[index(output.venue)][output.symbol].store(true, std::memory_order_release);
        Record record;
        record.kind = RecordKind::output;
        record.output = output;
        push(std::move(record));
      }
      engine->clear_outputs();
    };
    const auto publish = [&] {
      auto text = summary_json(*engine, latency_mode_name(config.shadow.latency_mode),
                               exec_mode_name(config.strategy.exec_mode),
                               kill_file_active.load(std::memory_order_acquire));
      std::lock_guard lock(summary_mutex);
      summary.swap(text);
    };
    while (!feeds_done.load(std::memory_order_acquire) || !binance_queue->empty() || !peer_queue->empty() ||
           !aux_queue->empty()) {
      const auto kill_now = kill_file_active.load(std::memory_order_acquire);
      if (kill_now != kill_applied) {
        if (kill_now) engine->kill(wall_ns());
        else engine->acknowledge_halt();
        kill_applied = kill_now;
        Record record;
        record.kind = RecordKind::halt;
        record.now_ns = wall_ns();
        record.halted = kill_now;
        push(std::move(record));
        drain_outputs();
      }
      // Merge the three inputs in receive-time order.
      const auto* b = binance_queue->front();
      const auto* y = peer_queue->front();
      const auto* a = aux_queue->front();
      const auto bt = b ? b->receive_ns : UINT64_MAX;
      const auto yt = y ? y->receive_ns : UINT64_MAX;
      const auto at = a ? a->receive_ns : UINT64_MAX;
      if (!b && !y && !a) {
        std::this_thread::sleep_for(std::chrono::microseconds(20));
      } else if (at <= bt && at <= yt) {
        aux_queue->try_pop(aux_message);
        if (aux_message.kind == AuxKind::fee)
          engine->set_fees(aux_message.venue, aux_message.symbol, aux_message.taker_ppm, aux_message.maker_ppm);
        else engine->add_latency_sample(aux_message.venue, aux_message.rtt_ns);
        Record record;
        record.kind = RecordKind::aux;
        record.aux = aux_message;
        push(std::move(record));
      } else {
        auto& queue = bt <= yt ? *binance_queue : *peer_queue;
        queue.try_pop(message);
        Record record;
        switch (message.kind) {
          case FeedKind::depth:
            record.kind = RecordKind::depth;
            record.depth = message.depth;
            push(std::move(record));
            engine->on_depth(message.depth);
            break;
          case FeedKind::funding:
            record.kind = RecordKind::funding;
            record.funding = message.funding;
            push(std::move(record));
            engine->on_funding(message.funding);
            break;
          case FeedKind::snapshot:
            engine->on_snapshot(*message.snapshot);
            record.kind = RecordKind::snapshot;
            record.snapshot = std::move(message.snapshot);
            push(std::move(record));
            break;
          case FeedKind::gap:
            engine->on_feed_gap(message.depth.venue, message.depth.symbol, message.receive_ns);
            break;
        }
      }
      drain_outputs();
      const auto now = std::chrono::steady_clock::now();
      if (now - last_summary >= std::chrono::seconds(1)) {
        publish();
        last_summary = now;
      }
    }
    engine->liquidate(wall_ns(), ExitReason::shutdown);
    drain_outputs();
    publish();
    decision_done.store(true, std::memory_order_release);
  });

  const auto make_handler = [&](Venue venue, FeedQueue& queue, RawQueue& raw, const std::vector<std::string>& symbols) {
    return [&, venue, symbols_ptr = &symbols](std::uint64_t receive_ns, std::string_view frame) {
      if (record_raw && !raw.try_push(RawRecord{receive_ns, std::string(frame)})) {
        fail("raw recorder queue overflow");
        return false;
      }
      auto parsed = std::make_unique<ParsedFrame>();
      const auto ok = parse_frame(venue, frame, receive_ns, *symbols_ptr, *parsed);
      if (!ok) {
        fail(std::string(venue_name(venue)) + ": malformed frame: " + std::string(frame.substr(0, 200)));
        return false;
      }
      FeedMessage message;
      message.receive_ns = receive_ns;
      switch (parsed->kind) {
        case FrameKind::depth:
          message.kind = FeedKind::depth;
          message.depth = parsed->depth;
          break;
        case FrameKind::funding:
          message.kind = FeedKind::funding;
          message.funding = parsed->funding;
          break;
        case FrameKind::overflow:
          message.kind = FeedKind::gap;
          message.depth.venue = venue;
          message.depth.symbol = parsed->depth.symbol;
          break;
        default:
          return true;
      }
      if (!queue.try_push(std::move(message))) {
        fail(std::string(venue_name(venue)) + ": decision queue overflow");
        return false;
      }
      return true;
    };
  };

  BinanceFeed binance(binance_symbols, make_handler(Venue::binance, *binance_queue, *raw_queues[0], binance_symbols),
                      [&](BookSnapshot&& snapshot) {
                        FeedMessage message;
                        message.kind = FeedKind::snapshot;
                        message.receive_ns = snapshot.receive_ns;
                        message.snapshot = std::make_unique<BookSnapshot>(std::move(snapshot));
                        if (!binance_queue->try_push(std::move(message))) {
                          fail("binance: decision queue overflow on snapshot");
                          return false;
                        }
                        return true;
                      });
  auto peer_feed = make_peer_feed(peer, peer_symbols, make_handler(peer, *peer_queue, *raw_queues[1], peer_symbols));

  std::thread binance_thread([&] {
    std::string feed_error;
    if (!binance.run(stop_requested, resync[0], feed_error) && !stop_requested.load()) fail("binance: " + feed_error);
    stop_requested.store(true);
  });
  std::thread peer_thread([&] {
    std::string feed_error;
    if (!peer_feed(stop_requested, resync[1], feed_error) && !stop_requested.load())
      fail(std::string(venue_name(peer)) + ": " + feed_error);
    stop_requested.store(true);
  });

  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(duration);
  while (!stop_requested.load()) {
    if (duration > 0 && std::chrono::steady_clock::now() >= deadline) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  stop_requested.store(true);
  binance_thread.join();
  peer_thread.join();
  aux_stop.store(true);
  if (aux.joinable()) aux.join();
  feeds_done.store(true, std::memory_order_release);
  decision.join();
  kill_watcher.join();
  recorder.join();
  status.stop();
  std::cout << summary_json(*engine, latency_mode_name(config.shadow.latency_mode),
                            exec_mode_name(config.strategy.exec_mode), kill_file_active.load());
  if (pipeline_failed.load()) {
    std::lock_guard lock(error_mutex);
    std::cerr << pipeline_error << '\n';
    return 1;
  }
  return 0;
}

// ---------------------------------------------------------------- probe

int probe(int argc, char** argv) {
  if (!websocket_supported()) {
    std::cerr << "linked libcurl lacks wss:// support; build against a curl with WebSockets enabled\n";
    return 1;
  }
  const auto duration = std::stoull(option(argc, argv, "--duration-seconds", "600"));
  const auto config = load_runtime_config();
  const auto peer = config.shadow.peer;
  auto basket = parse_basket(config.basket, config.shadow.peer);
  std::array<Credentials, kVenues> credentials{load_credentials(Venue::binance), load_credentials(peer)};
  struct Samples final {
    std::mutex mutex;
    std::vector<std::uint64_t> feed_age_ns;
    std::vector<std::uint64_t> public_rtt_ns;
    std::vector<std::uint64_t> signed_rtt_ns;
    std::vector<std::int64_t> offset_ns;
    std::atomic<std::int64_t> offset{0};
    std::atomic<bool> offset_known{false};
    std::uint64_t frames{};
  };
  std::array<Samples, kVenues> samples;
  std::atomic<bool> stop{false};
  std::string failure;
  std::mutex failure_mutex;
  const auto record_age = [&](Venue venue, std::uint64_t receive_ns, std::uint64_t event_ms) {
    auto& s = samples[index(venue)];
    if (!s.offset_known.load(std::memory_order_acquire) || event_ms == 0) return;
    const auto venue_now = static_cast<std::int64_t>(receive_ns) + s.offset.load(std::memory_order_relaxed);
    const auto age = venue_now - static_cast<std::int64_t>(event_ms * 1'000'000);
    std::lock_guard lock(s.mutex);
    ++s.frames;
    if (s.feed_age_ns.size() < 2'000'000) s.feed_age_ns.push_back(static_cast<std::uint64_t>(std::max<std::int64_t>(age, 0)));
  };
  const auto binance_symbols = venue_symbols(basket, Venue::binance);
  const auto peer_symbols = venue_symbols(basket, peer);
  auto parsed = std::make_unique<std::array<ParsedFrame, kVenues>>();
  BinanceFeed binance(binance_symbols, [&](std::uint64_t receive_ns, std::string_view frame) {
    auto& p = (*parsed)[0];
    if (parse_binance_frame(frame, receive_ns, binance_symbols, p) && p.kind == FrameKind::depth)
      record_age(Venue::binance, receive_ns, p.depth.event_ms);
    return true;
  }, [](BookSnapshot&&) { return true; });
  auto peer_feed = make_peer_feed(peer, peer_symbols, [&](std::uint64_t receive_ns, std::string_view frame) {
    auto& p = (*parsed)[1];
    if (parse_frame(peer, frame, receive_ns, peer_symbols, p) && p.kind == FrameKind::depth)
      record_age(peer, receive_ns, p.depth.event_ms);
    return true;
  });
  std::array<ResyncFlags, kVenues> resync{};
  const auto feed = [&](auto&& run, std::size_t v) {
    std::string error;
    if (!run(stop, resync[v], error) && !stop.load()) {
      std::lock_guard lock(failure_mutex);
      failure = std::string(venue_name(slot_venue(v, peer))) + ": " + error;
      stop.store(true);
    }
  };
  std::thread rest([&] {
    std::array<HttpClient, kVenues> clients;
    std::uint64_t tick = 0;
    while (!stop.load()) {
      for (std::size_t v = 0; v < kVenues; ++v) {
        const auto venue = slot_venue(v, peer);
        auto& s = samples[v];
        std::uint64_t server_ms = 0;
        const auto sent = wall_ns();
        const auto result = time_probe(clients[v], venue, server_ms);
        if (result.ok && server_ms > 0) {
          const auto midpoint = static_cast<std::int64_t>(sent + result.elapsed_ns / 2);
          std::lock_guard lock(s.mutex);
          s.public_rtt_ns.push_back(result.elapsed_ns);
          s.offset_ns.push_back(static_cast<std::int64_t>(server_ms * 1'000'000) - midpoint);
          auto recent = std::vector<std::int64_t>(s.offset_ns.end() - std::min<std::ptrdiff_t>(31, static_cast<std::ptrdiff_t>(s.offset_ns.size())),
                                                  s.offset_ns.end());
          std::nth_element(recent.begin(), recent.begin() + static_cast<std::ptrdiff_t>(recent.size() / 2), recent.end());
          s.offset.store(recent[recent.size() / 2], std::memory_order_relaxed);
          s.offset_known.store(true, std::memory_order_release);
        }
        if (credentials[v].present() && tick % 2 == 0) {
          const auto signed_result = signed_probe(clients[v], venue, credentials[v]);
          if (signed_result.ok) {
            std::lock_guard lock(s.mutex);
            s.signed_rtt_ns.push_back(signed_result.elapsed_ns);
          }
        }
      }
      ++tick;
      for (int i = 0; i < 10 && !stop.load(); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });
  std::thread binance_thread([&] {
    feed([&](std::atomic<bool>& s, ResyncFlags& r, std::string& e) { return binance.run(s, r, e); }, 0);
  });
  std::thread peer_thread([&] { feed(peer_feed, 1); });
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(duration);
  while (!stop.load() && !stop_requested.load() && std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  stop.store(true);
  binance_thread.join();
  peer_thread.join();
  rest.join();
  char host[256] = {};
  gethostname(host, sizeof(host) - 1);
  const auto ms = [](std::uint64_t ns) {
    std::ostringstream out;
    out.setf(std::ios::fixed);
    out.precision(2);
    out << static_cast<double>(ns) / 1e6;
    return out.str();
  };
  std::cout << "{\"host\":\"" << host << "\",\"duration_s\":" << duration << ",\"venues\":{";
  for (std::size_t v = 0; v < kVenues; ++v) {
    auto& s = samples[v];
    std::lock_guard lock(s.mutex);
    const auto triple = [&](const std::vector<std::uint64_t>& values) {
      return "{\"n\":" + std::to_string(values.size()) + ",\"p50_ms\":" + ms(percentile(values, 0.5)) +
             ",\"p95_ms\":" + ms(percentile(values, 0.95)) + ",\"p99_ms\":" + ms(percentile(values, 0.99)) + "}";
    };
    std::cout << (v ? "," : "") << '"' << venue_name(slot_venue(v, peer)) << "\":{\"feed_age\":"
              << triple(s.feed_age_ns) << ",\"public_rtt\":" << triple(s.public_rtt_ns)
              << ",\"signed_rtt\":" << triple(s.signed_rtt_ns) << ",\"clock_offset_ms\":"
              << static_cast<double>(s.offset.load()) / 1e6 << '}';
  }
  std::cout << "}}\n";
  if (!failure.empty()) {
    std::cerr << failure << '\n';
    return 1;
  }
  return 0;
}

// ---------------------------------------------------------------- offline commands

int check_keys() {
  const auto config = load_runtime_config();
  const auto peer = config.shadow.peer;
  auto basket = parse_basket(config.basket, config.shadow.peer);
  HttpClient http;
  std::string error;
  if (!load_instruments(http, basket, error, config.shadow.peer)) { std::cerr << "instruments: " << error << '\n'; return 1; }
  bool ok = true;
  std::cout << "{";
  for (std::size_t v = 0; v < kVenues; ++v) {
    const auto venue = slot_venue(v, peer);
    const auto credentials = load_credentials(venue);
    std::string detail;
    const auto read_only = verify_read_only(http, venue, credentials, detail);
    std::vector<std::int64_t> taker;
    std::vector<std::int64_t> maker;
    std::string fee_error;
    const auto fees_ok = read_only && fetch_fees(http, venue, credentials, basket, taker, maker, fee_error);
    ok = ok && read_only && fees_ok;
    std::cout << (v ? "," : "") << '"' << venue_name(venue) << "\":{\"read_only\":" << (read_only ? "true" : "false")
              << ",\"detail\":\"" << detail << "\",\"taker_ppm\":{";
    for (std::size_t s = 0; s < taker.size(); ++s)
      std::cout << (s ? "," : "") << '"' << basket[s].name << "\":" << taker[s];
    std::cout << "},\"maker_ppm\":{";
    for (std::size_t s = 0; s < maker.size(); ++s)
      std::cout << (s ? "," : "") << '"' << basket[s].name << "\":" << maker[s];
    std::cout << "}" << (fee_error.empty() ? "" : ",\"fee_error\":\"" + fee_error + "\"") << '}';
  }
  std::cout << "}\n";
  return ok ? 0 : 1;
}

int replay(const std::string& path) {
  const auto config = load_runtime_config();
  ReplayResult first, second;
  std::string error;
  if (!replay_journal(path, config, first, error) || !replay_journal(path, config, second, error)) {
    std::cerr << error << '\n';
    return 1;
  }
  const auto& m = first.metrics;
  std::cout << "{\"hash\":" << first.hash << ",\"deterministic\":" << (first.hash == second.hash ? "true" : "false")
            << ",\"events\":" << first.events << ",\"cycles\":" << m.cycles << ",\"missed\":" << m.missed
            << ",\"census_episodes\":" << m.census_episodes << ",\"net_raw\":" << m.net_raw << "}\n";
  return first.hash == second.hash ? 0 : 1;
}

int run_gapcheck(const std::string& path) {
  GapReport report;
  std::string error;
  if (!gapcheck(path, report, error)) { std::cerr << error << '\n'; return 1; }
  const bool ok = report.recoveries[0] >= report.gaps[0] && report.recoveries[1] >= report.gaps[1];
  std::cout << "{\"ok\":" << (ok ? "true" : "false") << ",\"depth_records\":" << report.depth_records
            << ",\"gaps\":[" << report.gaps[0] << ',' << report.gaps[1] << "],\"recoveries\":["
            << report.recoveries[0] << ',' << report.recoveries[1] << "]}\n";
  return ok ? 0 : 1;
}

int report(bool census, const std::vector<std::string>& paths) {
  if (paths.empty()) { std::cerr << "no journal files given\n"; return 2; }
  std::string json, error;
  const auto ok = census ? census_report(paths, json, error) : daily_report(paths, json, error);
  if (!ok) { std::cerr << error << '\n'; return 1; }
  std::cout << json;
  return 0;
}

// Failure drills on the synthetic market: gap -> resync, stale feed blocks entry,
// one-legged entry is repaired flat, kill flattens and blocks.
int drill() {
  const auto far = [](const Scenario& s) { return s.now_ns() / 1'000'000 + 8 * 3'600'000; };
  const auto start = [&](Scenario& s) {
    s.quote(Venue::binance, "100.00", "100.02");
    s.quote(Venue::bybit, "100.00", "100.02");
    s.funding(Venue::binance, "0.0001", far(s));
    s.funding(Venue::bybit, "0.0001", far(s));
    s.advance_ms(2);
  };

  Scenario gap;
  start(gap);
  gap.binance_gap();
  const bool gap_resync = gap.count(OutputKind::resync) == 1 && !gap.engine().book(Venue::binance, 0).verified();

  // Only Bybit updates for 1.2 s: Binance feed age exceeds the 1 s limit.
  Scenario stale;
  start(stale);
  const auto stale_code = [&] {
    auto& e = stale.engine();
    auto event = std::make_unique<DepthEvent>();
    event->venue = Venue::bybit;
    event->snapshot = true;
    event->receive_ns = e.now_ns() + 1'200'000'000;
    event->final_id = 99'999;
    event->bids[0] = {Decimal::parse("100.00"), Decimal::parse("100")};
    event->asks[0] = {Decimal::parse("100.02"), Decimal::parse("100")};
    event->bid_count = event->ask_count = 1;
    e.on_depth(*event);
    return e.entry_risk(0, 1'000 * kScale);
  }();
  const bool stale_blocked = stale_code == RiskCode::feed_stale;

  Scenario legged;
  start(legged);
  // Rich Bybit triggers long Binance / short Bybit; Bybit's bid vanishes before arrival.
  legged.quote(Venue::bybit, "100.60", "100.62");
  const bool opened = legged.count(OutputKind::decision) == 1;
  legged.advance_ms(5);  // Binance leg fills
  legged.quote(Venue::bybit, "99.00", "99.02");  // bid below the IOC limit
  legged.advance_ms(200);
  const auto* cycle = legged.last(OutputKind::cycle);
  const bool repaired = opened && cycle != nullptr && cycle->cycle.reason == ExitReason::legged_out &&
                        legged.engine().pair(0).phase == PairPhase::flat && legged.engine().metrics().repairs >= 1;

  Scenario killed;
  start(killed);
  killed.quote(Venue::bybit, "100.60", "100.62");
  killed.advance_ms(100);
  const bool was_open = killed.engine().pair(0).phase == PairPhase::open;
  killed.engine().kill(killed.now_ns());
  killed.advance_ms(100);
  const auto* kill_cycle = killed.last(OutputKind::cycle);
  killed.quote(Venue::bybit, "100.60", "100.62");
  const bool kill_ok = was_open && kill_cycle != nullptr && kill_cycle->cycle.reason == ExitReason::kill &&
                       killed.engine().pair(0).phase == PairPhase::flat &&
                       killed.engine().entry_risk(0, 1'000 * kScale) == RiskCode::halted;

  const auto b = [](bool value) { return value ? "true" : "false"; };
  std::cout << "{\"gap_resync\":" << b(gap_resync) << ",\"stale_feed_blocked\":" << b(stale_blocked)
            << ",\"one_leg_repaired\":" << b(repaired) << ",\"kill_flattened\":" << b(kill_ok) << "}\n";
  return gap_resync && stale_blocked && repaired && kill_ok ? 0 : 1;
}

// Decision path: book update -> census -> both edges -> risk, with no fills pending.
int benchmark() {
  Scenario scenario;
  scenario.quote(Venue::binance, "100.00", "100.02");
  scenario.quote(Venue::bybit, "100.00", "100.02");
  auto& engine = scenario.engine();
  auto event = std::make_unique<DepthEvent>();
  auto& e = *event;
  e.venue = Venue::bybit;
  e.snapshot = true;
  e.bid_count = e.ask_count = 20;
  for (std::uint16_t i = 0; i < 20; ++i) {
    e.bids[i] = {Price{(100 * kScale) - i * kScale / 100}, Decimal::parse("5")};
    e.asks[i] = {Price{(100 * kScale) + (i + 2) * kScale / 100}, Decimal::parse("5")};
  }
  constexpr std::size_t iterations = 200'000;
  std::vector<std::uint64_t> samples(iterations);
  e.receive_ns = scenario.now_ns();
  allocation::begin();
  for (std::size_t i = 0; i < iterations; ++i) {
    e.receive_ns += 1'000;
    e.final_id = i + 10;
    const auto shift = static_cast<std::int64_t>(i & 1U) * kScale / 100;
    e.bids[0].price.raw = 100 * kScale - shift;
    const auto begin = std::chrono::steady_clock::now();
    engine.on_depth(e);
    engine.clear_outputs();
    samples[i] = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - begin).count());
  }
  const auto allocations = allocation::end();
  std::cout << "{\"iterations\":" << iterations << ",\"p50_ns\":" << percentile(samples, 0.5)
            << ",\"p99_ns\":" << percentile(samples, 0.99) << ",\"p999_ns\":" << percentile(samples, 0.999)
            << ",\"hot_allocations\":" << allocations << ",\"target_p99_ns\":20000}\n";
  return allocations == 0 && percentile(samples, 0.99) <= 20'000 ? 0 : 1;
}

void usage() {
  std::cerr << "usage: crossex <command> [options]\n"
               "  shadow [--mode shadow] [--public-only] [--record-raw] [--journal P] [--kill-file P]\n"
               "         [--port N] [--duration-seconds N]\n"
               "  probe [--duration-seconds N]      feed age + REST RTT to both venues\n"
               "  check-keys                         verify read-only keys, print taker fees\n"
               "  replay <journal>                   deterministic state hash\n"
               "  gapcheck <journal>\n"
               "  daily-report <journal>...          cycle P&L and PREREG gate\n"
               "  census-report <journal>...         executable-gap episodes\n"
               "  drill | benchmark\n";
}

}  // namespace

int main(int argc, char** argv) {
  std::signal(SIGINT, stop_handler);
  std::signal(SIGTERM, stop_handler);
  std::signal(SIGPIPE, SIG_IGN);
  if (argc < 2) { usage(); return 2; }
  const std::string command = argv[1];
  try {
    if (command == "shadow") return shadow(argc, argv);
    if (command == "probe") return probe(argc, argv);
    if (command == "check-keys") return check_keys();
    if (command == "replay" && argc >= 3) return replay(argv[2]);
    if (command == "gapcheck" && argc >= 3) return run_gapcheck(argv[2]);
    if (command == "daily-report") return report(false, positional(argc, argv));
    if (command == "census-report") return report(true, positional(argc, argv));
    if (command == "drill") return drill();
    if (command == "benchmark") return benchmark();
  } catch (const std::exception& error) {
    std::cerr << "configuration: " << error.what() << '\n';
    return 2;
  }
  usage();
  return 2;
}
