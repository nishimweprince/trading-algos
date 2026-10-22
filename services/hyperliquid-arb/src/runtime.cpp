#include "hla/runtime.hpp"

#include "hla/config.hpp"
#include "hla/engine.hpp"
#include "hla/hl_feed.hpp"
#include "hla/hl_gateway.hpp"
#include "hla/hl_info.hpp"
#include "hla/hl_instruments.hpp"
#include "hla/hl_parser.hpp"
#include "hla/hl_sign.hpp"
#include "hla/journal.hpp"
#include "hla/mt5_bridge.hpp"
#include "hla/net.hpp"
#include "hla/platform.hpp"
#include "hla/spsc.hpp"
#include "hla/status_http.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <thread>

namespace hla {
namespace {

constexpr std::uint64_t kMs = 1'000'000ULL;
constexpr std::uint64_t kSecond = 1'000'000'000ULL;
constexpr std::uint64_t kDefaultMakerPpm = 150;  // validator-perp base tier, when no account is set

using MarketRing = SpscRing<HlFrame, 8192>;
using AuxRing = SpscRing<AuxReport, 64>;

struct Shared final {
  HlCommands commands;
  HlEvents events;
  MarketRing market;
  AuxRing aux;
};

void log(std::string_view text) { std::cerr << "[hlarb] " << text << std::endl; }

class RuntimeIo final : public EngineIo {
 public:
  RuntimeIo(HlCommands& commands, Mt5Bridge* bridge, Recorder& recorder, std::string kill_path, bool simulate_mt5)
      : commands_(commands), bridge_(bridge), recorder_(recorder), kill_path_(std::move(kill_path)),
        simulate_mt5_(simulate_mt5) {}
  bool hl_command(const HlCommand& command) override { return commands_.try_push(command); }
  bool mt5_order(const bridge::OrderRequest& order) override {
    if (simulate_mt5_) {
      simulated_orders.push_back(order);
      return true;
    }
    return bridge_->push_order(order);
  }
  void journal(std::string_view line) override { recorder_.push(line); }
  void kill(std::string_view reason) override {
    std::ofstream out(kill_path_, std::ios::app);
    out << wall_ns() << ' ' << reason << '\n';
    log("KILL: " + std::string(reason));
  }
  std::vector<bridge::OrderRequest> simulated_orders;

 private:
  HlCommands& commands_;
  Mt5Bridge* bridge_;
  Recorder& recorder_;
  std::string kill_path_;
  bool simulate_mt5_;
};

std::int64_t maker_ppm(const Config& config, const HlMarket& market, std::int64_t account_maker_ppm) {
  if (config.hl_maker_ppm >= 0) return config.hl_maker_ppm;
  auto ppm = account_maker_ppm;
  if (!market.dex.empty()) ppm *= 2;      // HIP-3 markets charge twice the validator rate
  if (market.growth_mode) ppm /= 10;      // growth mode cuts HIP-3 fees by 90%
  return ppm;
}

std::set<std::string> dexes_of(const std::vector<HlMarket>& markets) {
  std::set<std::string> out;
  for (const auto& market : markets) out.insert(market.dex);
  return out;
}

bool hl_state(HttpClient& http, const std::string& account, const std::vector<HlMarket>& markets, AuxReport& report,
              std::string& error) {
  report = {};
  report.hl_min_withdrawable = 1e18;
  std::map<std::string, Decimal> positions;
  for (const auto& dex : dexes_of(markets)) {
    HlAccountState state;
    if (!fetch_clearinghouse(http, account, dex, state, error)) return false;
    report.hl_account_value += state.account_value.as_double();
    report.hl_min_withdrawable = std::min(report.hl_min_withdrawable, state.withdrawable.as_double());
    for (const auto& position : state.positions) positions[position.coin] = position.size;
  }
  // Unified / portfolio margin: collateral lives in spot; per-dex clearinghouse stays ~0.
  std::string abstraction;
  if (fetch_user_abstraction(http, account, abstraction, error) && abstraction_uses_spot_margin(abstraction)) {
    Decimal spot_usdc;
    if (!fetch_spot_usdc(http, account, spot_usdc, error)) return false;
    const auto usd = spot_usdc.as_double();
    report.hl_account_value = usd;
    report.hl_min_withdrawable = usd;
  } else if (!error.empty() && abstraction.empty()) {
    return false;
  } else {
    error.clear();
  }
  for (std::size_t i = 0; i < markets.size(); ++i) {
    const auto found = positions.find(markets[i].coin);
    report.hl_positions[i] = found == positions.end() ? Decimal{} : found->second;
  }
  report.ok = true;
  report.receive_ns = wall_ns();
  return true;
}

// Cancels every open order on the basket coins (startup and shutdown sweep).
bool cancel_open_orders(HttpClient& http, HlGateway& gateway, const std::string& account,
                        const std::vector<HlMarket>& markets, std::size_t& cancelled, std::string& error) {
  cancelled = 0;
  for (const auto& dex : dexes_of(markets)) {
    std::vector<HlOpenOrder> open;
    if (!fetch_open_orders(http, account, dex, open, error)) return false;
    std::vector<CancelSpec> cancels;
    for (const auto& order : open)
      for (const auto& market : markets)
        if (market.coin == order.coin) cancels.push_back({market.asset_id, order.oid});
    for (std::size_t start = 0; start < cancels.size(); start += kMaxBatch) {
      const auto n = std::min(kMaxBatch, cancels.size() - start);
      std::string reply;
      if (!gateway.post_rest(cancel_action(std::span(cancels.data() + start, n)), reply, error)) return false;
      cancelled += n;
    }
  }
  return true;
}

// Synthetic MT5 spec sized so one lot step is about $1000 of HL notional.
Mt5Spec synthetic_spec(double mid) {
  Mt5Spec spec;
  spec.valid = true;
  spec.contract_size = 1;
  spec.volume_step = 0.01;
  spec.volume_min = 0.01;
  spec.volume_max = 100;
  spec.tick_size = 1e-5;
  spec.tick_value = 1000.0 / (mid * spec.volume_step) * spec.tick_size;
  spec.digits = 5;
  spec.trade_mode = 4;
  spec.session_open = true;
  return spec;
}

}  // namespace

int run_trader(const RunOptions& options, std::atomic<bool>& stop) {
  const bool live = options.mode == Mode::live;
  const bool check = options.mode == Mode::check;
  const bool dry = options.mode == Mode::dry_run;
  const bool sim_mt5 = dry && options.sim_mt5_from_hl;
  Config config;
  try {
    config = load_config(live);
  } catch (const std::exception& e) {
    log(std::string("configuration: ") + e.what());
    return 2;
  }
  std::string error;

  // ---- preflight
  if (std::filesystem::exists(config.kill_file)) {
    log("kill file " + config.kill_file + " exists: review it, then delete it to start");
    return 2;
  }
  if (!websocket_supported()) {
    log("linked libcurl has no wss support");
    return 2;
  }
  const bool has_account = !config.hl_account.empty();
  if (has_account && !valid_address(config.hl_account)) {
    log("HLA_HL_ACCOUNT is not a 0x address");
    return 2;
  }
  config.hl_account = lowercase(config.hl_account);
  if ((live || check) && (!has_account || config.agent_key_file.empty() || config.mt5_logins.empty())) {
    log("live/check need HLA_HL_ACCOUNT, HLA_AGENT_KEY_FILE and HLA_MT5_LOGINS");
    return 2;
  }
  std::vector<std::string> coins;
  std::vector<std::string> mt5_symbols;
  for (const auto& pair : config.pairs) {
    coins.push_back(pair.coin);
    mt5_symbols.push_back(pair.mt5_symbol);
  }
  HttpClient http;
  std::vector<HlMarket> markets;
  if (!load_markets(http, coins, markets, error)) {
    log(error);
    return 1;
  }
  std::int64_t account_taker_ppm = 0;
  std::int64_t account_maker_ppm = static_cast<std::int64_t>(kDefaultMakerPpm);
  if (has_account && !fetch_user_fees(http, config.hl_account, account_taker_ppm, account_maker_ppm, error)) {
    log(error);
    return 1;
  }
  Signer signer;
  if (live || check) {
    std::ifstream key_file(config.agent_key_file);
    std::string key((std::istreambuf_iterator<char>(key_file)), std::istreambuf_iterator<char>());
    if (!key_file || !signer.load(key, error)) {
      log("agent key: " + (error.empty() ? "cannot read " + config.agent_key_file : error));
      return 2;
    }
    key.assign(key.size(), '\0');
    if (signer.address() == config.hl_account) {
      log("refusing the master wallet key: create an API (agent) wallet and use its key");
      return 2;
    }
    std::vector<std::string> agents;
    if (!fetch_agents(http, config.hl_account, agents, error)) {
      log(error);
      return 1;
    }
    if (std::find(agents.begin(), agents.end(), signer.address()) == agents.end()) {
      log("agent " + signer.address() + " is not an approved API wallet of " + config.hl_account);
      return 2;
    }
  }

  // ---- MT5 bridge
  Mt5Bridge bridge;
  std::vector<bridge::Tick> last_tick(markets.size());
  if (!sim_mt5) {
    if (!bridge.create(config.bridge_name, mt5_symbols, error)) {
      log(error);
      return 1;
    }
    log("waiting for the HlaPublisher" + std::string(live ? " and HlaExecutor" : "") + " services...");
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    for (;;) {
      bridge::Tick tick;
      while (bridge.pop_tick(tick))
        if (tick.symbol < last_tick.size()) last_tick[tick.symbol] = tick;
      const auto now = wall_ns();
      bridge.heartbeat(now);
      bool specs = true;
      for (std::size_t i = 0; i < markets.size(); ++i) specs = specs && bridge.spec(i).valid;
      const auto account = bridge.account();
      const bool publisher = now - bridge.publisher_ns() < 2 * kSecond;
      const bool executor = !live || (now - bridge.executor_ns() < 2 * kSecond && account.positions_ns != 0);
      if (specs && account.valid && publisher && executor) break;
      if (std::chrono::steady_clock::now() > deadline || stop.load()) {
        log("MT5 bridge not ready: specs=" + std::to_string(specs) + " account=" + std::to_string(account.valid) +
            " publisher=" + std::to_string(publisher) + " executor=" + std::to_string(executor));
        return 1;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    const auto account = bridge.account();
    if (!config.mt5_logins.empty() &&
        std::find(config.mt5_logins.begin(), config.mt5_logins.end(), account.login) == config.mt5_logins.end()) {
      log("MT5 login " + std::to_string(account.login) + " is not in HLA_MT5_LOGINS");
      return 2;
    }
    if (live && !account.trade_allowed) {
      log("MT5 algo trading is disabled in the terminal");
      return 2;
    }
  }

  // ---- pairs, price sanity
  std::vector<PairSetup> pairs;
  for (std::size_t i = 0; i < markets.size(); ++i) {
    pairs.push_back({markets[i], config.pairs[i].mt5_symbol, config.pairs[i].ratio,
                     maker_ppm(config, markets[i], account_maker_ppm)});
    if (sim_mt5 || last_tick[i].bid <= 0) {
      if (!sim_mt5) log("warning: no MT5 tick yet for " + config.pairs[i].mt5_symbol + " (market closed?)");
      continue;
    }
    const auto hl_mid = markets[i].mid.as_double();
    const auto mt5_mid = config.pairs[i].ratio * (last_tick[i].bid + last_tick[i].ask) / 2;
    const auto gap_ppm = std::abs(hl_mid - mt5_mid) / hl_mid * 1e6;
    if (gap_ppm > static_cast<double>(config.max_basis_ppm)) {
      log("pair " + markets[i].coin + "=" + config.pairs[i].mt5_symbol + " prices differ by " +
          std::to_string(gap_ppm / 1e4) + "%: wrong symbol or ratio");
      return 2;
    }
  }

  // ---- positions
  AuxReport start_state;
  if (has_account && !hl_state(http, config.hl_account, markets, start_state, error)) {
    log(error);
    return 1;
  }
  const auto account = sim_mt5 ? Mt5Account{} : bridge.account();
  bool residual_ok = true;
  std::ostringstream summary;
  summary << "{\"mode\":\"" << (live ? "live" : check ? "check" : "dry-run") << "\",\"agent\":\"" << signer.address()
          << "\",\"hl_account_value\":" << start_state.hl_account_value << ",\"hl_min_withdrawable\":"
          << (has_account ? start_state.hl_min_withdrawable : 0) << ",\"mt5_login\":" << account.login
          << ",\"mt5_equity\":" << account.equity << ",\"mt5_margin_mode\":" << account.margin_mode << ",\"pairs\":[";
  for (std::size_t i = 0; i < markets.size(); ++i) {
    const auto spec = sim_mt5 ? Mt5Spec{} : bridge.spec(i);
    const auto vpp = spec.value_per_point();
    const auto hl_units = start_state.hl_positions[i].as_double();
    const auto mt5_lots = account.positions[i];
    const auto unhedged = vpp > 0 ? mt5_lots + hl_units * config.pairs[i].ratio / vpp : 0;
    const auto step = spec.volume_step > 0 ? spec.volume_step : 0.01;
    if (std::abs(unhedged) >= step / 2) residual_ok = false;
    summary << (i == 0 ? "" : ",") << "{\"coin\":\"" << markets[i].coin << "\",\"asset\":" << markets[i].asset_id
            << ",\"mt5\":\"" << config.pairs[i].mt5_symbol << "\",\"growth_mode\":"
            << (markets[i].growth_mode ? "true" : "false") << ",\"maker_ppm\":" << pairs[i].hl_maker_ppm
            << ",\"mt5_step\":" << spec.volume_step << ",\"mt5_value_per_point\":" << vpp
            << ",\"hl_units_per_lot_step\":" << (vpp > 0 ? spec.volume_step * vpp / config.pairs[i].ratio : 0)
            << ",\"hl_pos\":" << hl_units << ",\"mt5_lots\":" << mt5_lots << ",\"unhedged_lots\":" << unhedged << "}";
  }
  summary << "],\"hedged\":" << (residual_ok ? "true" : "false") << "}";
  std::cout << summary.str() << std::endl;
  if (has_account && start_state.hl_min_withdrawable < config.hl_min_margin_usd)
    log("warning: HL withdrawable below HLA_HL_MIN_MARGIN_USD (unified: spot USDC; standard: fund each basket dex)");
  if (check) return residual_ok ? 0 : 3;
  if (!residual_ok && !config.adopt_positions) {
    log("positions are not hedged; flatten them or set HLA_ADOPT_POSITIONS=1 to let the hedger square them");
    return 3;
  }

  // ---- threads
  auto shared = std::make_unique<Shared>();
  std::atomic<std::uint64_t> engine_heartbeat{steady_ns()};
  GatewayConfig gateway_config;
  gateway_config.account = config.hl_account;
  gateway_config.coins = coins;
  gateway_config.live = live;
  gateway_config.expires_after_ms = config.expires_after_ms;
  gateway_config.deadman_s = config.deadman_s;
  HlGateway gateway(gateway_config, signer, shared->commands, shared->events, engine_heartbeat);
  if (live) {
    std::size_t cancelled = 0;
    if (!cancel_open_orders(http, gateway, config.hl_account, markets, cancelled, error)) {
      log(error);
      return 1;
    }
    if (cancelled > 0) log("cancelled " + std::to_string(cancelled) + " open orders left from a previous run");
    for (const auto& market : markets) {
      std::string reply;
      const auto lev = static_cast<std::uint32_t>(
          std::clamp(static_cast<int>(config.hl_leverage), 1, std::max(1, market.max_leverage)));
      if (!gateway.post_rest(update_leverage_action(market.asset_id, !market.only_isolated, lev), reply, error) ||
          reply.find("\"ok\"") == std::string::npos) {
        log("updateLeverage " + market.coin + " x" + std::to_string(lev) + ": " +
            (error.empty() ? reply : error));
        return 1;
      }
    }
  }
  // Recorder holds an ~6 MiB SPSC ring; keep it on the heap (Windows default stack is 1 MiB).
  auto recorder = std::make_unique<Recorder>();
  if (!recorder->open(config.journal, config.status_file, error)) {
    log(error);
    return 1;
  }
  log("journal " + recorder->path());
  StatusHttp status_http(config.status_port);
  if (config.status_port != 0) {
    if (!status_http.start(error)) {
      log(error);
      return 1;
    }
    log("status http://127.0.0.1:" + std::to_string(config.status_port) + "/");
  }
  RuntimeIo io(shared->commands, sim_mt5 ? nullptr : &bridge, *recorder, config.kill_file, dry);
  Engine engine(config, pairs, io, dry);
  for (std::size_t i = 0; i < markets.size(); ++i)
    engine.seed_positions(i, start_state.hl_positions[i], account.positions[i]);
  recorder->push("{\"k\":\"start\",\"t\":" + std::to_string(wall_ns()) + ",\"summary\":" + summary.str() + "}");

  std::atomic<bool> stop_threads{false};
  std::atomic<bool> stop_recorder{false};
  std::atomic<bool> fatal{false};
  std::string fatal_reason;
  std::thread recorder_thread([&] { recorder->run(stop_recorder); });
  std::thread feed_thread([&] {
    if (config.feed_core >= 0) platform::pin_current_thread(static_cast<unsigned>(config.feed_core));
    HlFrame frame;
    while (!stop_threads.load()) {
      ResyncFlags resync{};
      HyperliquidFeed feed(coins, [&](std::uint64_t receive_ns, std::string_view text) {
        if (!parse_hl_frame(text, receive_ns, coins, frame)) return true;  // error frames: staleness handles it
        if (frame.kind == HlFrameKind::ignored) return true;
        if (!shared->market.try_push(frame)) {
          fatal.store(true);
          return false;
        }
        return true;
      });
      std::string feed_error;
      if (!feed.run(stop_threads, resync, feed_error) && !stop_threads.load()) {
        log("hl feed: " + feed_error + "; reconnecting");
        std::this_thread::sleep_for(std::chrono::seconds(1));
      }
    }
  });
  std::string gateway_error;
  std::thread gateway_thread([&] {
    if (config.gateway_core >= 0) platform::pin_current_thread(static_cast<unsigned>(config.gateway_core));
    if (!gateway.run(stop_threads, gateway_error)) fatal.store(true);
  });
  std::thread aux_thread([&] {
    HttpClient aux_http;
    std::uint64_t iteration = 0;
    while (!stop_threads.load()) {
      AuxReport report;
      std::string aux_error;
      if (has_account) {
        if (!hl_state(aux_http, config.hl_account, markets, report, aux_error)) {
          report.ok = false;
          log("aux: " + aux_error);
        }
        HlRateLimit limit;
        if (report.ok && iteration % 6 == 0 && fetch_rate_limit(aux_http, config.hl_account, limit, aux_error)) {
          report.has_rate_limit = true;
          report.requests_remaining = limit.remaining();
        }
      } else {
        report.ok = true;  // dry-run without an account: no margin or positions to check
        report.hl_min_withdrawable = 1e18;
        report.receive_ns = wall_ns();
      }
      ++iteration;
      if (!shared->aux.try_push(report)) log("aux: queue full");
      for (int i = 0; i < 50 && !stop_threads.load(); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });

  // ---- decision loop (this thread)
  if (config.decision_core >= 0) platform::pin_current_thread(static_cast<unsigned>(config.decision_core));
  const auto started = steady_ns();
  std::uint64_t last_state = 0;
  std::uint64_t last_kill_check = 0;
  std::uint64_t last_timer = 0;
  std::uint64_t last_status = 0;
  std::vector<bool> sim_spec_ready(markets.size(), false);
  bool stopping = false;
  std::uint64_t stop_deadline = 0;
  for (;;) {
    const auto steady = steady_ns();
    const auto now = wall_ns();
    engine_heartbeat.store(steady, std::memory_order_relaxed);
    bool busy = false;

    HlFrame frame;
    while (shared->market.try_pop(frame)) {
      busy = true;
      engine.on_hl_frame(frame, now);
      if (sim_mt5 && frame.kind == HlFrameKind::bbo && frame.bbo.has_bid && frame.bbo.has_ask) {
        const auto s = frame.bbo.symbol;
        const auto bid = frame.bbo.bid.price.as_double();
        const auto ask = frame.bbo.ask.price.as_double();
        if (!sim_spec_ready[s]) {
          engine.on_mt5_state(s, synthetic_spec((bid + ask) / 2));
          sim_spec_ready[s] = true;
        }
        engine.on_mt5_tick({s, 0, bid, ask, static_cast<std::int64_t>(now / kMs), now}, now);
      }
    }
    HlEvent event;
    while (shared->events.try_pop(event)) {
      busy = true;
      engine.on_hl_event(event, now);
    }
    if (!sim_mt5) {
      bridge::Tick tick;
      while (bridge.pop_tick(tick)) {
        busy = true;
        engine.on_mt5_tick(tick, now);
      }
      bridge::Txn txn;
      while (bridge.pop_txn(txn)) {
        busy = true;
        engine.on_mt5_txn(txn, now);
      }
      bridge.heartbeat(now);
    }
    AuxReport report;
    while (shared->aux.try_pop(report)) engine.on_aux(report, now);

    if (now - last_state >= 5 * kMs) {
      last_state = now;
      if (sim_mt5) {
        Mt5Account fake;
        fake.valid = true;
        fake.connected = true;
        fake.trade_allowed = true;
        fake.equity = 1e6;
        fake.margin_free = 1e6;
        fake.updated_ns = now;
        engine.on_mt5_account(fake, now, now, now);
      } else {
        for (std::size_t i = 0; i < markets.size(); ++i) engine.on_mt5_state(i, bridge.spec(i));
        engine.on_mt5_account(bridge.account(), bridge.publisher_ns(), bridge.executor_ns(), now);
      }
    }
    // Dry-run: HL quotes fill when the book trades through them; MT5 hedges fill at the touch.
    if (dry) {
      for (std::size_t s = 0; s < markets.size(); ++s) {
        for (const auto side : {Side::buy, Side::sell}) {
          const auto& q = engine.quote(s, side);
          if (q.state != QuoteState::resting || q.size.raw <= 0) continue;
          const auto ask = engine.hl_ask(s).price;
          const auto bid = engine.hl_bid(s).price;
          const bool through = side == Side::buy ? (ask.raw > 0 && ask <= q.price) : (bid.raw > 0 && bid >= q.price);
          if (!through) continue;
          HlEvent fill;
          fill.kind = HlEventKind::fill;
          fill.receive_ns = now;
          fill.symbol = static_cast<std::uint16_t>(s);
          fill.oid = q.oid;
          fill.side = side;
          fill.price = q.price;
          fill.size = q.size;
          fill.fee = Decimal{apply_ppm(mul_scaled(q.price.raw, q.size.raw), pairs[s].hl_maker_ppm)};
          fill.set_text("dry-run");
          engine.on_hl_event(fill, now);
        }
      }
      auto orders = std::move(io.simulated_orders);
      io.simulated_orders.clear();
      for (const auto& order : orders) {
        const auto price = order.side == bridge::buy ? engine.mt5_ask(order.symbol) : engine.mt5_bid(order.symbol);
        engine.on_mt5_txn({order.request_id, order.symbol, order.side, bridge::filled, 10009, order.volume, price, 0,
                           0, 0, wall_ns()},
                          now);
      }
    }
    if (now - last_kill_check >= 500 * kMs) {
      last_kill_check = now;
      engine.on_kill_file(std::filesystem::exists(config.kill_file), now);
    }
    if (now - last_timer >= kMs) {
      last_timer = now;
      engine.on_timer(now);
    }
    if (now - last_status >= kSecond) {
      last_status = now;
      const auto status = engine.status_json(now);
      recorder->set_status(status);
      status_http.publish(status);
    }
    if (engine.failed() || recorder->failed() || fatal.load()) {
      if (!stopping) {
        fatal_reason = engine.failed() ? "engine pipeline" : recorder->failed() ? "journal" : "feed/gateway: " + gateway_error;
        log("fatal: " + fatal_reason);
      }
      if (!stopping) stop.store(true);
    }
    const bool timed_out = options.duration_s != 0 && steady - started > options.duration_s * kSecond;
    if (!stopping && (stop.load() || timed_out)) {
      stopping = true;
      stop_deadline = steady + 3 * kSecond;
      engine.pull_all(now, "shutdown");
      log("stopping: pulling quotes");
    }
    if (stopping) {
      bool quiet = true;
      for (std::size_t s = 0; s < markets.size(); ++s)
        for (const auto side : {Side::buy, Side::sell})
          quiet = quiet && engine.quote(s, side).state == QuoteState::idle;
      if (quiet || steady > stop_deadline) break;
    }
    if (!busy) std::this_thread::yield();
  }

  stop_threads.store(true);
  gateway_thread.join();
  feed_thread.join();
  aux_thread.join();
  if (live) {
    std::size_t cancelled = 0;
    std::string sweep_error;
    if (!cancel_open_orders(http, gateway, config.hl_account, markets, cancelled, sweep_error))
      log("shutdown sweep: " + sweep_error + " (scheduleCancel will clear orders within HLA_DEADMAN_S)");
    else if (cancelled > 0)
      log("shutdown sweep cancelled " + std::to_string(cancelled) + " orders");
    std::string reply;
    gateway.post_rest(schedule_cancel_action(std::nullopt), reply, sweep_error);
  }
  std::ostringstream positions;
  positions << "{\"k\":\"stop\",\"t\":" << wall_ns() << ",\"reason\":\"" << (fatal_reason.empty() ? "requested" : fatal_reason)
            << "\",\"positions\":[";
  for (std::size_t s = 0; s < markets.size(); ++s)
    positions << (s == 0 ? "" : ",") << "{\"coin\":\"" << markets[s].coin << "\",\"hl\":"
              << engine.hl_position(s).as_double() << ",\"mt5\":" << engine.mt5_position(s)
              << ",\"unhedged_lots\":" << engine.unhedged_lots(s) << "}";
  positions << "]}";
  recorder->push(positions.str());
  {
    const auto status = engine.status_json(wall_ns());
    recorder->set_status(status);
    status_http.publish(status);
  }
  stop_recorder.store(true);
  recorder_thread.join();
  log(positions.str());
  return fatal_reason.empty() ? 0 : 1;
}

}  // namespace hla
