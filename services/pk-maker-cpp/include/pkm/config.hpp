#pragma once

#include <map>
#include <string>
#include <vector>

#include "pkm/engine.hpp"

// Settings from the environment (systemd EnvironmentFile) or `--env FILE`.
// Venue and wallet names are the Rust service's (KALSHI_*, POLY_*), the
// strategy's are pk-polymaker's PK_PM_* (same rules, same defaults), so one
// env file serves both during the cutover. pkm's own are PKM_*.
namespace pkm {

struct Config {
  engine::Config strategy;
  std::vector<std::string> assets;

  // Wallet: a deposit wallet (POLY_SIGNATURE_TYPE=3, POLY_FUNDER = its address).
  std::string private_key;
  std::string funder;
  int signature_type = 3;
  std::string clob_api_key, clob_api_secret, clob_api_passphrase;  // derived when empty
  std::string relayer_api_key;
  std::string relayer_api_key_address;
  std::string collateral;  // pUSD unless overridden

  std::string kalshi_key_id;
  std::string kalshi_key_path;

  std::string clob_url = "https://clob.polymarket.com";
  std::string poly_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
  std::string poly_user_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user";
  std::string gamma_url = "https://gamma-api.polymarket.com";
  std::string relayer_url = "https://relayer-v2.polymarket.com";
  std::string kalshi_ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2";
  std::string kalshi_rest_url = "https://api.elections.kalshi.com/trade-api/v2";

  std::string state_dir = "data/state";
  std::string kill_file = "data/KILL";
  bool live_confirm = false;
  int hot_core = -1;
  int signer_core = -1;
  int order_channels = 8;
  std::int64_t feed_stale_ms = 5'000;
  std::int64_t poly_silence_ms = 30'000;
  std::int64_t fills_poll_ms = 2'000;
  std::int64_t reconcile_s = 30;
  std::int64_t presign_ttl_ms = 5'000;

  [[nodiscard]] std::string ack_file() const { return state_dir + "/ACK"; }
  [[nodiscard]] std::string state_file() const { return state_dir + "/state.json"; }
};

// KEY=VALUE lines (comments, blank lines, optional quotes, `export `).
bool read_env_file(const std::string& path, std::map<std::string, std::string>& out, std::string& error);
// Settings from `vars` (the process environment first, then the file's).
bool load_config(const std::map<std::string, std::string>& vars, Config& out, std::string& error);
std::map<std::string, std::string> process_environment();
// Strategy values as one line for the startup log.
std::string describe(const Config& c);

}  // namespace pkm
