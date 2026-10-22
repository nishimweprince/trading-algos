#include "hla/drills.hpp"
#include "hla/hl_feed.hpp"
#include "hla/hl_instruments.hpp"
#include "hla/hl_parser.hpp"
#include "hla/journal.hpp"
#include "hla/json.hpp"
#include "hla/mt5_bridge.hpp"
#include "hla/net.hpp"
#include "hla/platform.hpp"
#include "hla/runtime.hpp"

#include <curl/curl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {
using namespace hla;

constexpr std::string_view kDefaultCoins = "xyz:JPY,xyz:EUR,xyz:SILVER,xyz:SP500,xyz:XYZ100,xyz:CL,BTC,ETH";

std::atomic<bool> stop_requested{false};

std::string option(int argc, char** argv, std::string_view name, std::string fallback) {
  for (int i = 2; i + 1 < argc; ++i)
    if (argv[i] == name) return argv[i + 1];
  return fallback;
}

std::vector<std::string> coins_option(int argc, char** argv) {
  const auto* env = std::getenv("HLA_COINS");
  return parse_coins(option(argc, argv, "--coins", env != nullptr ? env : std::string(kDefaultCoins)));
}

std::uint64_t percentile(std::vector<std::uint64_t> values, double p) {
  if (values.empty()) return 0;
  const auto k = static_cast<std::ptrdiff_t>(p * static_cast<double>(values.size() - 1));
  std::nth_element(values.begin(), values.begin() + k, values.end());
  return values[static_cast<std::size_t>(k)];
}

std::string distribution_ms(const std::vector<std::uint64_t>& ns) {
  std::ostringstream out;
  out << "{\"n\":" << ns.size() << ",\"p10\":" << static_cast<double>(percentile(ns, 0.10)) / 1e6
      << ",\"p50\":" << static_cast<double>(percentile(ns, 0.50)) / 1e6
      << ",\"p90\":" << static_cast<double>(percentile(ns, 0.90)) / 1e6
      << ",\"p99\":" << static_cast<double>(percentile(ns, 0.99)) / 1e6 << "}";
  return out.str();
}

int markets(int argc, char** argv) {
  HttpClient http;
  std::vector<HlMarket> list;
  std::string error;
  if (!load_markets(http, coins_option(argc, argv), list, error)) {
    std::cerr << error << '\n';
    return 1;
  }
  for (const auto& m : list) {
    std::cout << "{\"coin\":\"" << m.coin << "\",\"dex\":\"" << m.dex << "\",\"asset\":" << m.asset_id
              << ",\"sz_decimals\":" << m.size_decimals << ",\"max_leverage\":" << m.max_leverage
              << ",\"growth_mode\":" << (m.growth_mode ? "true" : "false")
              << ",\"only_isolated\":" << (m.only_isolated ? "true" : "false") << ",\"mid\":" << m.mid.as_double()
              << ",\"tick\":" << m.tick.as_double() << ",\"step\":" << m.step.as_double() << "}\n";
  }
  return 0;
}

// Order-path proxies, one sample per second: an `info` request over keep-alive HTTP and
// the same request as a WS `post` (the transport the order gateway will use).
struct RttSamples final {
  std::vector<std::uint64_t> http_ns;
  std::vector<std::uint64_t> ws_post_ns;
  std::vector<std::int64_t> clock_offset_ms;  // HL time minus local wall time
  std::string error;
};

void sample_rtt(std::atomic<bool>& stop, RttSamples& out) {
  HttpClient http;
  WebSocket socket;
  if (!socket.connect(std::string(kHyperliquidWs), out.error)) return;
  std::string frame;
  for (std::uint64_t id = 1; !stop.load(); ++id) {
    const auto before = wall_ns();
    const auto result = hyperliquid_info(http, "{\"type\":\"exchangeStatus\"}");
    const auto after = wall_ns();
    std::uint64_t server_ms = 0;
    if (result.ok && json::u64(result.body, "\"time\"", server_ms)) {
      out.http_ns.push_back(result.elapsed_ns);
      out.clock_offset_ms.push_back(static_cast<std::int64_t>(server_ms) -
                                    static_cast<std::int64_t>((before + after) / 2 / 1'000'000));
    }
    const auto request = "{\"method\":\"post\",\"id\":" + std::to_string(id) +
                         ",\"request\":{\"type\":\"info\",\"payload\":{\"type\":\"exchangeStatus\"}}}";
    const auto sent = steady_ns();
    if (!socket.send_text(request, out.error)) return;
    const auto marker = "\"id\":" + std::to_string(id) + ",";
    while (!stop.load()) {
      const auto status = socket.receive(frame, 5'000, out.error);
      if (status == WebSocket::Status::timeout) break;
      if (status != WebSocket::Status::frame) return;
      if (frame.find("\"channel\":\"post\"") != std::string::npos && frame.find(marker) != std::string::npos) {
        out.ws_post_ns.push_back(steady_ns() - sent);
        break;
      }
    }
    for (int i = 0; i < 10 && !stop.load(); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
}

int probe(int argc, char** argv) {
  if (!websocket_supported()) {
    std::cerr << "linked libcurl has no wss support\n";
    return 1;
  }
  const auto coins = coins_option(argc, argv);
  const auto duration = std::chrono::seconds(std::stoll(option(argc, argv, "--duration-seconds", "60")));

  // Per coin: bbo inter-arrival (receive clock) and age (local receive minus HL block time).
  std::vector<std::vector<std::uint64_t>> gaps(coins.size());
  std::vector<std::vector<std::int64_t>> ages_ms(coins.size());
  std::vector<std::uint64_t> last_receive(coins.size(), 0);
  std::vector<std::uint64_t> ctx_count(coins.size(), 0);
  std::uint64_t errors = 0;
  HlFrame parsed;
  ResyncFlags resync{};
  HyperliquidFeed feed(coins, [&](std::uint64_t receive_ns, std::string_view frame) {
    if (!parse_hl_frame(frame, receive_ns, coins, parsed)) {
      ++errors;
      std::cerr << "frame error: " << frame.substr(0, 200) << '\n';
      return true;
    }
    if (parsed.kind == HlFrameKind::ctx) ++ctx_count[parsed.ctx.symbol];
    if (parsed.kind != HlFrameKind::bbo) return true;
    const auto s = parsed.bbo.symbol;
    if (last_receive[s] != 0) gaps[s].push_back(receive_ns - last_receive[s]);
    last_receive[s] = receive_ns;
    ages_ms[s].push_back(static_cast<std::int64_t>(receive_ns / 1'000'000) -
                         static_cast<std::int64_t>(parsed.bbo.event_ms));
    return true;
  });

  std::atomic<bool> stop{false};
  std::string feed_error;
  bool feed_ok = true;
  std::thread feed_thread([&] { feed_ok = feed.run(stop, resync, feed_error); });
  RttSamples rtt;
  std::thread rtt_thread([&] { sample_rtt(stop, rtt); });
  const auto deadline = std::chrono::steady_clock::now() + duration;
  while (std::chrono::steady_clock::now() < deadline && !stop_requested.load())
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  stop.store(true);
  feed_thread.join();
  rtt_thread.join();

  std::vector<std::int64_t> offsets = rtt.clock_offset_ms;
  std::sort(offsets.begin(), offsets.end());
  const auto offset = offsets.empty() ? 0 : offsets[offsets.size() / 2];
  std::cout << "{\"duration_s\":" << duration.count() << ",\"frame_errors\":" << errors
            << ",\"clock_offset_ms\":" << offset << ",\"info_http_rtt_ms\":" << distribution_ms(rtt.http_ns)
            << ",\"info_ws_post_rtt_ms\":" << distribution_ms(rtt.ws_post_ns) << ",\"coins\":[";
  for (std::size_t i = 0; i < coins.size(); ++i) {
    // Age is corrected by the measured clock offset; negative residuals clamp to 0.
    std::vector<std::uint64_t> ages;
    for (const auto age : ages_ms[i]) ages.push_back(static_cast<std::uint64_t>(std::max<std::int64_t>(age + offset, 0)) * 1'000'000);
    std::cout << (i == 0 ? "" : ",") << "{\"coin\":\"" << coins[i] << "\",\"ctx_frames\":" << ctx_count[i]
              << ",\"bbo_interarrival_ms\":" << distribution_ms(gaps[i])
              << ",\"bbo_age_ms\":" << distribution_ms(ages) << "}";
  }
  std::cout << "]}\n";
  if (!feed_ok) std::cerr << "feed: " << feed_error << '\n';
  if (!rtt.error.empty()) std::cerr << "rtt: " << rtt.error << '\n';
  return feed_ok && errors == 0 ? 0 : 1;
}

// Phase 0 broker latency: a buy then a sell of `--lots` through the bridge and HlaExecutor,
// timing submit -> result per leg. Real orders: use a demo account, or accept the spread.
int mt5_roundtrip(int argc, char** argv) {
  const auto symbol = option(argc, argv, "--symbol", "");
  const auto lots = std::stod(option(argc, argv, "--lots", "0.01"));
  const auto rounds = std::stoi(option(argc, argv, "--rounds", "5"));
  const auto* logins = std::getenv("HLA_MT5_LOGINS");
  const auto* name = std::getenv("HLA_BRIDGE_NAME");
  if (symbol.empty() || logins == nullptr || *logins == '\0') {
    std::cerr << "mt5-roundtrip needs --symbol S and HLA_MT5_LOGINS\n";
    return 2;
  }
  Mt5Bridge bridge;
  std::string error;
  const std::vector<std::string> symbols{symbol};
  if (!bridge.create(name != nullptr ? name : "Local\\hla_bridge_v1", symbols, error)) {
    std::cerr << error << '\n';
    return 1;
  }
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
  while (!(bridge.spec(0).valid && bridge.account().valid && wall_ns() - bridge.executor_ns() < 2'000'000'000ULL)) {
    bridge.heartbeat(wall_ns());
    if (std::chrono::steady_clock::now() > deadline || stop_requested.load()) {
      std::cerr << "HlaPublisher/HlaExecutor not running\n";
      return 1;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  const auto login = std::to_string(bridge.account().login);
  if (std::string(",") + logins + "," == "," || (std::string(",") + logins + ",").find("," + login + ",") == std::string::npos) {
    std::cerr << "MT5 login " << login << " is not in HLA_MT5_LOGINS\n";
    return 2;
  }
  std::vector<std::uint64_t> legs;
  for (int round = 0; round < rounds && !stop_requested.load(); ++round) {
    for (const auto side : {bridge::buy, bridge::sell}) {
      const auto now = wall_ns();
      const auto id = static_cast<std::uint64_t>(round * 2 + static_cast<int>(side) + 1);
      bridge.push_order({id, 0, side, lots, 30, 0, now, now + 5'000'000'000ULL});
      bridge::Txn txn{};
      const auto wait = std::chrono::steady_clock::now() + std::chrono::seconds(10);
      while (!bridge.pop_txn(txn)) {
        bridge.heartbeat(wall_ns());
        if (std::chrono::steady_clock::now() > wait) {
          std::cerr << "no result for order " << id << " (check the open position by hand)\n";
          return 1;
        }
      }
      const auto status = txn.status & 0xFFU;  // high bits: where the executor got the price
      std::cout << "{\"leg\":" << id << ",\"status\":" << status << ",\"retcode\":" << txn.retcode
                << ",\"lots\":" << txn.volume << ",\"price\":" << txn.price << ",\"px_src\":" << (txn.status >> 8)
                << ",\"ms\":" << static_cast<double>(txn.publish_ns - now) / 1e6 << "}\n";
      if (status != bridge::filled) return 1;
      legs.push_back(txn.publish_ns - now);
    }
  }
  std::cout << "{\"legs\":" << legs.size() << ",\"order_ms\":" << distribution_ms(legs) << "}\n";
  return 0;
}

bool flag(int argc, char** argv, std::string_view name) {
  for (int i = 2; i < argc; ++i)
    if (argv[i] == name) return true;
  return false;
}

int trade(int argc, char** argv, Mode mode) {
  RunOptions options;
  options.mode = mode;
  options.duration_s = std::stoull(option(argc, argv, "--duration-seconds", "0"));
  options.sim_mt5_from_hl = flag(argc, argv, "--sim-mt5");
  if (options.sim_mt5_from_hl && mode != Mode::dry_run) {
    std::cerr << "--sim-mt5 is only valid with dry-run\n";
    return 2;
  }
  return run_trader(options, stop_requested);
}

void usage() {
  std::cerr << "usage: hlarb <command> [options]   (configuration: HLA_* environment, see config.example.env)\n"
               "  check                                     every live preflight check, read-only\n"
               "  dry-run [--duration-seconds N] [--sim-mt5] full pipeline, nothing sent; --sim-mt5 needs no terminal\n"
               "  live [--duration-seconds N]                quote HL, hedge on MT5\n"
               "  report <journal>...                        fills, fees, hedge latency, markouts, halts\n"
               "  drill                                      failure drills through the production engine\n"
               "  mt5-roundtrip --symbol S [--lots L] [--rounds N]  broker order latency (real orders)\n"
               "  markets [--coins C]                       HL instrument data (HIP-3 aware)\n"
               "  probe [--coins C] [--duration-seconds N]   bbo cadence/age, info HTTP vs WS-post RTT\n"
               "coins default to $HLA_COINS, else " << kDefaultCoins << '\n';
}

}  // namespace

int main(int argc, char** argv) {
  platform::install_stop_handler(stop_requested);
  platform::raise_timer_resolution();
  curl_global_init(CURL_GLOBAL_DEFAULT);
  if (argc < 2) { usage(); return 2; }
  const std::string command = argv[1];
  try {
    if (command == "check") return trade(argc, argv, Mode::check);
    if (command == "dry-run") return trade(argc, argv, Mode::dry_run);
    if (command == "live") return trade(argc, argv, Mode::live);
    if (command == "drill") return run_drills(std::cout) == 0 ? 0 : 1;
    if (command == "report" && argc >= 3) return journal_report(std::vector<std::string>(argv + 2, argv + argc), std::cout);
    if (command == "mt5-roundtrip") return mt5_roundtrip(argc, argv);
    if (command == "markets") return markets(argc, argv);
    if (command == "probe") return probe(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "configuration: " << error.what() << '\n';
    return 2;
  }
  usage();
  return 2;
}
