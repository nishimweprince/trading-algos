#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#include "pkm/config.hpp"
#include "pkm/net.hpp"
#include "pkm/platform.hpp"
#include "pkm/runtime.hpp"
#include "pkm/venue.hpp"

namespace {

using namespace pkm;

int usage() {
  std::cerr << "usage: pkm [--env FILE] <command>\n"
               "  run         the live maker (needs PKM_LIVE_CONFIRM=yes)\n"
               "  check       auth, protocol version, cash, then one post-only bid far below the\n"
               "              best bid (min size, <= $3), cancelled at once; nothing may fill\n"
               "  cancel-all  cancel every open order on the account\n";
  return 2;
}

struct Session {
  Config config;
  venue::Wallet wallet;
  clob::Credentials creds;
};

bool open_session(const std::string& env_file, const std::string& command, Session& s) {
  auto vars = process_environment();
  std::string error;
  if (!env_file.empty() && !read_env_file(env_file, vars, error)) {
    platform::log(error);
    return false;
  }
  if (!load_config(vars, s.config, error)) {
    platform::log(error);
    return false;
  }
  if (command == "run" && !s.config.live_confirm) {
    platform::log("pkm run trades real money: set PKM_LIVE_CONFIRM=yes");
    return false;
  }
  if (!venue::load_wallet(s.config, s.wallet, error) ||
      !venue::Clob::derive_credentials(s.config, s.wallet, s.creds, error)) {
    platform::log(error);
    return false;
  }
  platform::log("signer " + s.wallet.eoa_checksum + ", deposit wallet " + s.wallet.funder_checksum + ", api key " +
                s.creds.api_key.substr(0, 8) + "…");
  return true;
}

// The venue checks every command runs first: protocol V2, and our L2 headers accepted.
bool venue_checks(Session& s, venue::Clob& clob) {
  std::string error;
  int version = 0;
  if (!clob.version(version, error)) {
    platform::log(error);
    return false;
  }
  if (version != 2) {
    platform::log("CLOB protocol version is " + std::to_string(version) + ", not 2: this build signs V2 orders only");
    return false;
  }
  if (!clob.check_headers(error)) {
    platform::log(error);
    return false;
  }
  double cash = 0;
  if (!clob.cash(cash, error)) {
    platform::log(error);
    return false;
  }
  char buf[96];
  std::snprintf(buf, sizeof(buf), "CLOB V2, headers accepted, cash $%.2f", cash);
  platform::log(buf);
  (void)s;
  return true;
}

int cmd_cancel_all(Session& s) {
  venue::Clob clob(s.config, s.wallet, s.creds);
  int n = 0;
  std::string error;
  if (!clob.cancel_all(n, error)) {
    platform::log(error);
    return 1;
  }
  platform::log("cancel-all: " + std::to_string(n) + " cancelled");
  return 0;
}

int cmd_check(Session& s) {
  venue::Clob clob(s.config, s.wallet, s.creds);
  if (!venue_checks(s, clob)) return 1;
  std::string error;
  std::vector<venue::Clob::OpenOrder> open;
  if (!clob.open_orders(open, error)) {
    platform::log(error);
    return 1;
  }
  if (!open.empty()) {
    platform::log(std::to_string(open.size()) + " orders already rest on the account: refusing to probe");
    return 1;
  }
  venue::Discovery discovery(s.config);
  const std::string asset = s.config.assets.front();
  std::optional<engine::Window> w;
  if (!discovery.discover(asset, venue::window_open(net::now_ns() / 1'000'000'000LL), w, error) || !w) {
    platform::log("discover " + asset + ": " + (error.empty() ? "not listed yet, try again in a minute" : error));
    return 1;
  }
  venue::Clob::BookMeta book;
  if (!clob.book(w->up_token, book, error) || !book.best_bid) {
    platform::log("book: " + (error.empty() ? "no bid" : error));
    return 1;
  }
  const std::int64_t bid_cents = std::lround(*book.best_bid * 100.0);
  const std::int64_t cents = bid_cents - 5;
  const auto shares = static_cast<std::int64_t>(std::ceil(std::max(book.min_size, s.config.strategy.min_shares)));
  // Guards: strictly below the bid (and so the ask), at most $3, one order.
  if (cents < 1 || cents >= bid_cents || static_cast<double>(cents * shares) / 100.0 > 3.0) {
    platform::log("probe refused: bid " + std::to_string(bid_cents) + "c, " + std::to_string(shares) + " shares");
    return 1;
  }
  const std::int64_t t0 = net::mono_ns();
  const auto posted = clob.post(w->up_token, cents, shares, book.neg_risk || w->neg_risk);
  const std::int64_t t1 = net::mono_ns();
  if (!posted.ok) {
    platform::log("probe post refused: " + posted.error);
    return 1;
  }
  const bool cancelled = clob.cancel(posted.order_id, error);
  const std::int64_t t2 = net::mono_ns();
  std::optional<venue::Clob::OrderState> st;
  clob.order_state(posted.order_id, st, error);
  char buf[200];
  std::snprintf(buf, sizeof(buf), "probe %s %lldc x%lld: post %.1f ms, cancel %.1f ms (%s), matched %.2f", asset.c_str(),
                static_cast<long long>(cents), static_cast<long long>(shares), static_cast<double>(t1 - t0) / 1e6,
                static_cast<double>(t2 - t1) / 1e6, cancelled ? "ok" : error.c_str(), st ? st->matched : 0.0);
  platform::log(buf);
  if (!cancelled || (st && st->matched > 0)) {
    platform::log("CHECK FAILED: the probe was not cleanly cancelled");
    return 1;
  }
  platform::log("check passed");
  return 0;
}

int cmd_run(Session& s) {
  if (s.config.kalshi_key_id.empty() || !std::filesystem::exists(s.config.kalshi_key_path)) {
    platform::log("KALSHI_API_KEY_ID and a readable KALSHI_PRIVATE_KEY_PATH are needed");
    return 1;
  }
  venue::Clob clob(s.config, s.wallet, s.creds);
  if (!venue_checks(s, clob)) return 1;
  // Nothing of ours may rest from before.
  int n = 0;
  std::string error;
  if (!clob.cancel_all(n, error)) {
    platform::log(error);
    return 1;
  }
  platform::log("LIVE: cancel-all at start (" + std::to_string(n) + " cancelled); " + describe(s.config));
  std::atomic<bool> stop{false};
  platform::install_stop_handler(stop);
  runtime::Startup startup;
  startup.config = &s.config;
  startup.wallet = &s.wallet;
  startup.credentials = s.creds;
  return runtime::run(startup, stop);
}

}  // namespace

int main(int argc, char** argv) {
  std::string env_file;
  std::string command;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--env" && i + 1 < argc) env_file = argv[++i];
    else if (command.empty()) command = a;
    else return usage();
  }
  if (command != "run" && command != "check" && command != "cancel-all") return usage();
  Session s;
  if (!open_session(env_file, command, s)) return 1;
  if (command == "check") return cmd_check(s);
  if (command == "cancel-all") return cmd_cancel_all(s);
  return cmd_run(s);
}
