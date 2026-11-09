// End-to-end BundleSim tests against loopback only. No production RPC or credentials.
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <functional>
#include <map>
#include <mutex>
#include <thread>

#include "app/bundle_sim.hpp"
#include "core/base64.hpp"
#include "core/programs.hpp"
#include "fixture.hpp"
#include "state/state_store.hpp"
#include "test.hpp"
#include "tx/builders.hpp"

using namespace arb;
namespace {

class TempDir {
 public:
  std::string path;
  TempDir() {
    std::string pattern = (std::filesystem::temp_directory_path() / "jito-sim-rpc-XXXXXX").string();
    std::vector<char> name(pattern.begin(), pattern.end());
    name.push_back(0);
    if (!mkdtemp(name.data())) throw std::runtime_error("mkdtemp");
    path = name.data();
  }
  ~TempDir() { std::error_code ec; std::filesystem::remove_all(path, ec); }
};

// Always restore the test-specific environment variable; never inspect real API keys.
class TestKey {
  std::string old_;
  bool existed_;
 public:
  static constexpr const char* NAME = "JITO_SIM_LOOPBACK_TEST_KEY";
  TestKey() : existed_(std::getenv(NAME) != nullptr) {
    if (existed_) old_ = std::getenv(NAME);
    setenv(NAME, "local-only", 1);
  }
  ~TestKey() { if (existed_) setenv(NAME, old_.c_str(), 1); else unsetenv(NAME); }
};

class FakeRpc {
  int fd_ = -1;
  std::atomic<bool> stop_{false};
  std::thread thread_;
  std::mutex mu_;
  std::vector<std::string> methods_, errors_;
  std::function<std::string(const std::string&, simdjson::dom::element)> handle_;

  void run() {
    while (!stop_) {
      int c = accept(fd_, nullptr, nullptr);
      if (c < 0) break;
      timeval timeout{2, 0};
      setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
      std::string req;
      size_t header = std::string::npos, length = 0;
      char buf[4096];
      while (!stop_) {
        ssize_t n = recv(c, buf, sizeof buf, 0);
        if (n <= 0) break;
        req.append(buf, size_t(n));
        header = req.find("\r\n\r\n");
        if (header == std::string::npos) continue;
        auto pos = req.find("Content-Length:");
        if (pos == std::string::npos) pos = req.find("content-length:");
        if (pos == std::string::npos) break;
        length = std::stoull(req.substr(pos + 15));
        if (req.size() >= header + 4 + length) break;
      }
      if (header != std::string::npos && req.size() >= header + 4 + length) {
        std::string result;
        try {
          simdjson::dom::parser parser;
          auto doc = parser.parse(req.substr(header + 4, length));
          std::string method{std::string_view(doc["method"])};
          { std::lock_guard<std::mutex> g(mu_); methods_.push_back(method); }
          result = handle_(method, doc["params"]);
        } catch (const std::exception& e) {
          { std::lock_guard<std::mutex> g(mu_); errors_.push_back(e.what()); }
          result = "{\"error\":{\"code\":-32000,\"message\":\"unexpected test request\"}}";
        }
        std::string wire = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                           std::to_string(result.size()) + "\r\n\r\n" + result;
        size_t sent = 0;
        while (sent < wire.size()) {
          ssize_t n = send(c, wire.data() + sent, wire.size() - sent, 0);
          if (n <= 0) break;
          sent += size_t(n);
        }
      }
      close(c);
    }
  }
 public:
  std::string url;
  explicit FakeRpc(std::function<std::string(const std::string&, simdjson::dom::element)> handle)
      : handle_(std::move(handle)) {
    fd_ = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof addr) || listen(fd_, 8))
      throw std::runtime_error("loopback listen");
    socklen_t len = sizeof addr;
    if (getsockname(fd_, reinterpret_cast<sockaddr*>(&addr), &len)) throw std::runtime_error("getsockname");
    url = "http://127.0.0.1:" + std::to_string(ntohs(addr.sin_port));
    thread_ = std::thread([this] { run(); });
  }
  ~FakeRpc() { stop_ = true; ::shutdown(fd_, SHUT_RDWR); close(fd_); thread_.join(); }
  size_t calls(const std::string& method) {
    std::lock_guard<std::mutex> g(mu_);
    return size_t(std::count(methods_.begin(), methods_.end(), method));
  }
  void check() { std::lock_guard<std::mutex> g(mu_); CHECK(errors_.empty()); }
};

std::string account_json(const Pubkey& owner, const std::vector<uint8_t>& data, uint64_t lamports = 100'000'000) {
  return "{\"owner\":\"" + b58(owner) + "\",\"lamports\":" + std::to_string(lamports) +
         ",\"data\":[\"" + b64(data.data(), data.size()) + "\",\"base64\"]}";
}
std::string token_json(const Pubkey& mint_key, const Pubkey& owner, uint64_t amount) {
  std::vector<uint8_t> data(165);
  std::memcpy(data.data(), mint_key.data(), 32);
  std::memcpy(data.data() + 32, owner.data(), 32);
  std::memcpy(data.data() + 64, &amount, 8);
  data[108] = 1;
  return account_json(prog::TOKEN, data);
}

app::SimJob fixture_job(const Keypair& signer) {
  StateStore store;
  std::optional<PoolDef> def;
  state::DiscoveryConfig dc;
  dc.base_mints = {mint::WSOL, mint::USDC};
  simdjson::dom::parser parser;
  simdjson::padded_string ps(arbtest::read_file(arbtest::fixture_path("pools.json")));
  for (auto e : simdjson::dom::array(parser.parse(ps)["raydium_v4"])) {
    const auto* fa = arbtest::fixture(std::string(std::string_view(e)));
    if (!fa || !fa->exists) continue;
    auto d = state::pool_def_from_account(fa->raw, dc);
    if (d && (d->mint_a == mint::WSOL || d->mint_b == mint::WSOL)) { def = *d; break; }
  }
  if (!def) throw std::runtime_error("no fixture Raydium wSOL pool");
  auto id = store.add_pool(*def);
  for (const auto& key : store.subscribed_keys()) {
    const auto* fa = arbtest::fixture(b58(key));
    if (!fa) continue;
    AccountUpdate u;
    u.key = key; u.slot = 100;
    if (!fa->exists) u.missing = true;
    else { u.owner = fa->raw.owner; u.data = fa->raw.data.data(); u.len = fa->raw.data.size(); }
    store.apply(u);
  }
  app::SimJob j;
  auto p = store.pool(id);
  j.legs[0] = j.legs[1] = p;
  j.c.n = 2;
  j.c.base = mint::WSOL;
  bool d = p->mint_a == mint::WSOL;
  Pubkey other = d ? p->mint_b : p->mint_a;
  j.c.legs[0] = {id, d, mint::WSOL, other};
  j.c.legs[1] = {id, !d, other, mint::WSOL};
  j.o.x = 1000; j.o.final_out = 1200; j.o.profit_base = 200;
  j.o.leg_out[0] = 2000; j.o.leg_out[1] = 1200;
  j.cand.kind = uint8_t(obs::RecordKind::Candidate);
  j.cand.action = uint8_t(obs::Action::DryRun);
  j.cand.slot = 100; j.cand.hops = 2; j.cand.x = 1000;
  j.cand.final_out = 1200; j.cand.profit_base = 200;
  j.cand.gross_lamports = 200; j.cand.tip_lamports = 10; j.cand.fee_lamports = 5;
  j.cand.leg_out[0] = 2000; j.cand.leg_out[1] = 1200;
  std::memcpy(j.cand.base, j.c.base.data(), 32);
  for (int i = 0; i < 2; ++i) {
    j.cand.dex[i] = uint8_t(p->kind);
    std::memcpy(j.cand.pools[i], p->address.data(), 32);
  }
  tx::IxList ixs;
  if (!app::build_sim_ixs(ixs, j.c, std::span(j.legs.data(), 2), j.o, signer.pubkey(), {false}))
    throw std::runtime_error("fixture sim build");
  tx::SignedTx signed_tx;
  if (!tx::build_tx(signer.pubkey(), &signer, ixs, Hash{}, {}, signed_tx)) throw std::runtime_error("fixture sign");
  std::memcpy(j.cand.signature, signed_tx.sig.data(), 64);
  return j;
}

enum class Scenario { funded, wrong_mint, pda, insufficient, fallback, sim_failure, setup_failure, rpc_failure, moved, inventory, loss };

struct Case {
  TempDir dir;
  TestKey env;
  uint8_t seed[32]{};
  Keypair signer = Keypair::from_seed(seed);
  app::SimJob job = fixture_job(signer);
  Pubkey owner = signer.pubkey();
  Pubkey base_ata = ata(owner, job.c.base, prog::TOKEN);
  Pubkey middle_mint = job.c.legs[0].mint_out;
  Pubkey middle_ata = ata(owner, middle_mint, prog::TOKEN);
  Pubkey hint = job.legs[0]->address;  // seed can be a noncanonical account owned by the holder
  Pubkey invalid_owner = ata(owner, middle_mint, prog::TOKEN_2022); // off-curve PDA
  Scenario scenario;
  std::atomic<unsigned> pre_reads{0};
  Config cfg;
  FakeRpc rpc;

  explicit Case(Scenario s) : scenario(s), rpc([this](const std::string& method, simdjson::dom::element params) {
    return handle(method, params);
  }) {
    cfg.sim.enabled = true;
    cfg.sim.max_per_s = 1000;
    cfg.sim.holder_seed_path = dir.path + "/seeds.json";
    cfg.obs.decision_log_dir = dir.path + "/decisions";
    cfg.helius.rpc_url = rpc.url;
    cfg.helius.api_key_env = TestKey::NAME;
    cfg.helius.rpc_timeout_ms = 1000;
    std::ofstream seeds(cfg.sim.holder_seed_path);
    seeds << "{\"holders\":{\"" << b58(job.c.base) << "\":[";
    if (s != Scenario::fallback) seeds << '"' << b58(hint) << '"';
    seeds << "]}}";
  }

  std::string handle(const std::string& method, simdjson::dom::element params) {
    if (method == "getTokenLargestAccounts") {
      return "{\"result\":{\"context\":{\"slot\":100},\"value\":[{\"address\":\"" + b58(base_ata) +
             "\",\"amount\":\"10000\"}]}}";
    }
    if (method == "getMultipleAccounts") {
      auto keys = simdjson::dom::array(params.at(0));
      if (scenario == Scenario::rpc_failure) return "{\"error\":{\"code\":-32000,\"message\":\"fixture failure\"}}";
      bool is_pre = keys.size() == 3;
      bool is_after = keys.size() == 2;
      if (is_pre) ++pre_reads;
      std::string out = "{\"result\":{\"context\":{\"slot\":" + std::to_string(is_after ? 102 : 100) + "},\"value\":[";
      size_t i = 0;
      for (auto key : keys) {
        if (i++) out += ',';
        std::string k{std::string_view(key)};
        if (k == b58(hint)) {
          out += token_json(scenario == Scenario::wrong_mint ? middle_mint : job.c.base,
                            scenario == Scenario::pda ? invalid_owner : owner, scenario == Scenario::insufficient ? 500 : 10000);
        } else if (k == b58(owner) || k == b58(invalid_owner)) out += account_json(prog::SYSTEM, {});
        else if (k == b58(base_ata))
          out += token_json(job.c.base, owner, scenario == Scenario::insufficient ? 500 :
                            scenario == Scenario::moved && is_after ? 9999 : 10000);
        else if (k == b58(middle_ata)) out += token_json(middle_mint, owner, 500);
        else out += "null";
      }
      return out + "]}}";
    }
    if (method == "simulateTransaction") {
      auto options = params.at(1);
      if (bool(options["sigVerify"]) || !bool(options["replaceRecentBlockhash"]) || uint64_t(options["minContextSlot"]) < 100)
        throw std::runtime_error("unsafe/wrong simulation options");
      auto watched = simdjson::dom::array(options["accounts"]["addresses"]);
      if (watched.size() != 2 || std::string_view(watched.at(0)) != b58(base_ata) || std::string_view(watched.at(1)) != b58(middle_ata))
        throw std::runtime_error("wrong watched account order");
      std::string err = scenario == Scenario::sim_failure ? "{\"InstructionError\":[2,{\"Custom\":30}]}" : scenario == Scenario::setup_failure ? "{\"InstructionError\":[0,{\"Custom\":30}]}" : "null";
      return "{\"result\":{\"context\":{\"slot\":101},\"value\":{\"err\":" + err +
             ",\"unitsConsumed\":60000,\"logs\":[],\"accounts\":[" +
             token_json(job.c.base, owner, scenario == Scenario::loss ? 9900 : 10200) + "," +
             token_json(middle_mint, owner, scenario == Scenario::inventory ? 499 : 500) + "]}}}";
    }
    throw std::runtime_error("forbidden/unexpected RPC method: " + method);
  }

  obs::DecisionRecord run() {
    std::atomic<bool> stop{false};
    app::BundleSim sim(cfg, stop);
    CHECK(std::any_of(std::begin(job.cand.signature), std::end(job.cand.signature), [](uint8_t v) { return v != 0; }));
    CHECK(sim.submit(job));
    sim.start();
    std::vector<obs::DecisionRecord> done;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (std::chrono::steady_clock::now() < deadline && sim.counters().terminal.load() == 0)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    sim.shutdown();
    done = sim.drain();
    CHECK_EQ(done.size(), size_t(1));
    CHECK_EQ(sim.counters().submitted.load(), uint64_t(1));
    CHECK_EQ(sim.counters().terminal.load(), uint64_t(1));
    CHECK_EQ(sim.counters().queued.load(), uint64_t(0));
    CHECK_EQ(sim.counters().in_flight.load(), uint64_t(0));
    CHECK_EQ(sim.counters().dropped.load(), uint64_t(0));
    CHECK(sim.drain().empty());
    if (scenario == Scenario::setup_failure) {
      CHECK_EQ(sim.counters().setup_error.load(),uint64_t(1));
      CHECK_EQ(sim.counters().failed.load(),uint64_t(0));
    }
    rpc.check();
    if (done.empty()) return {};
    CHECK(std::memcmp(done[0].signature, job.cand.signature, 64) == 0);
    CHECK(done[0].kind == uint8_t(obs::RecordKind::Outcome));
    CHECK(done[0].reason != uint8_t(obs::SimFail::shutdown));
    size_t sidecars = 0;
    if (std::filesystem::exists(cfg.obs.decision_log_dir))
      for (const auto& entry : std::filesystem::directory_iterator(cfg.obs.decision_log_dir)) {
        std::ifstream f(entry.path());
        std::string line;
        while (std::getline(f, line)) {
          simdjson::dom::parser parser;
          auto record = parser.parse(line);
          CHECK(std::string_view(record["signature"]) == b58(job.cand.signature, 64));
          ++sidecars;
        }
      }
    CHECK_EQ(sidecars, size_t(1));
    return done[0];
  }
};

}  // namespace

TEST(bundle_sim_rpc_seeded_noncanonical_hint_resolves_funded_canonical_without_largest) {
  Case c(Scenario::funded);
  auto r = c.run();
  CHECK(r.outcome == uint8_t(obs::Outcome::SimulatedOk));
  CHECK_EQ(r.final_out, uint64_t(1200));
  CHECK_EQ(r.profit_base, int64_t(200));
  CHECK_EQ(r.net_lamports, int64_t(185));
  CHECK_EQ(r.cu_limit, uint32_t(60000));
  CHECK_EQ(c.rpc.calls("getTokenLargestAccounts"), size_t(0));
  CHECK_EQ(c.rpc.calls("simulateTransaction"), size_t(1));
}

TEST(bundle_sim_rpc_invalid_seed_pda_or_wrong_mint_falls_back_to_valid_largest) {
  for (auto scenario : {Scenario::pda, Scenario::wrong_mint, Scenario::fallback}) {
    Case c(scenario);
    auto r = c.run();
    CHECK(r.outcome == uint8_t(obs::Outcome::SimulatedOk));
    CHECK_EQ(c.rpc.calls("getTokenLargestAccounts"), size_t(1));
    CHECK_EQ(c.rpc.calls("simulateTransaction"), size_t(1));
  }
}

TEST(bundle_sim_rpc_insufficient_canonical_balance_never_simulates) {
  Case c(Scenario::insufficient);
  auto r = c.run();
  CHECK(r.reason == uint8_t(obs::SimFail::no_holder));
  CHECK_EQ(c.rpc.calls("simulateTransaction"), size_t(0));
}

TEST(bundle_sim_rpc_process_errors_are_terminal_and_signature_correlated) {
  for (auto scenario : {Scenario::sim_failure, Scenario::rpc_failure, Scenario::moved, Scenario::inventory}) {
    Case c(scenario);
    auto r = c.run();
    CHECK(r.outcome == uint8_t(obs::Outcome::SimulationFailed));
    auto expected = scenario == Scenario::sim_failure ? obs::SimFail::leg2 : scenario == Scenario::rpc_failure ? obs::SimFail::rpc :
                    scenario == Scenario::moved ? obs::SimFail::funding_changed : obs::SimFail::intermediate_balance;
    CHECK(r.reason == uint8_t(expected));
    CHECK_EQ(r.final_out, uint64_t(0));
    CHECK_EQ(r.net_lamports, int64_t(0));
  }
}

TEST(bundle_sim_rpc_successful_execution_can_have_negative_profit) {
  Case c(Scenario::loss);
  auto r = c.run();
  CHECK(r.outcome == uint8_t(obs::Outcome::SimulatedOk));
  CHECK_EQ(r.final_out, uint64_t(900));
  CHECK_EQ(r.profit_base, int64_t(-100));
  CHECK_EQ(r.net_lamports, int64_t(-115));
}

TEST(bundle_sim_rpc_setup_failures_are_not_route_failures) {
  Case c(Scenario::setup_failure);auto r=c.run();
  CHECK(r.outcome==uint8_t(obs::Outcome::SimulationFailed));
  CHECK(r.reason==uint8_t(obs::SimFail::setup));
}
