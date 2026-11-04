#pragma once
// Runtime configuration (spec §6.9, §10, §12). dry_run defaults to true: going
// live is an explicit `dry_run = false` in the file.
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace arb {

struct Config {
  struct Mode {
    bool dry_run = true;
    std::string funding_mode = "flash";  // flash | own
    std::string kill_file = "KILL";      // relative to the run directory
    bool simulate_before_send = true;    // pre-send simulateTransaction (spec §9)
  } mode;

  struct Helius {
    std::string laserstream_endpoint = "VERIFY";  // from the Helius dashboard (Frankfurt)
    std::string rpc_url = "https://mainnet.helius-rpc.com";
    std::string api_key_env = "HELIUS_API_KEY";
    int max_grpc_connections = 4;  // pool shards; plus one wallet/reserve connection
    int shard_size = 300;
    int resync_lag_slots = 150;
    int reconnect_min_ms = 1000;
    int reconnect_max_ms = 30000;
    int ping_interval_ms = 10000;
    bool replay_from_slot = false;  // VERIFY: LaserStream from_slot on the Business plan
    int rpc_timeout_ms = 5000;
  } helius;

  struct Jito {
    std::vector<std::string> regions{"frankfurt", "amsterdam"};
    std::string block_engine_fmt = "https://%s.mainnet.block-engine.jito.wtf";
    std::string tip_floor_url = "https://bundles.jito.wtf/api/v1/bundles/tip_floor";
    uint64_t tip_min_lamports = 1000;
    uint64_t tip_max_lamports = 50'000'000;
    int tip_floor_refresh_ms = 10000;
    int tip_accounts_refresh_s = 3600;
    int backoff_429_ms = 1000;
    int status_poll_ms = 500;
    int status_timeout_ms = 30000;
    bool sender_enabled = false;
    std::string sender_url = "https://sender.helius-rpc.com/fast";
  } jito;

  struct Engine {
    std::vector<std::string> base_mints{"So11111111111111111111111111111111111111112",
                                        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"};
    int max_hops = 3;
    double min_spot_edge = 0.0005;
    uint64_t min_ev_lamports = 20000;
    uint64_t min_net_lamports = 10000;
    uint64_t max_trade_lamports = 500'000'000'000ULL;  // 500 SOL equivalent
    uint64_t min_trade_lamports = 1'000'000;
    double max_input_fraction = 0.20;
    double target_land_rate = 0.30;
    int max_bundles_per_slot = 3;
    int max_cycles_per_pool = 64;
    int max_send_lag_slots = 1;
    int max_state_age_slots = 2;
    int cooldown_slots = 2;
    int sizer_max_evals = 24;
    double alpha_init = 0.5;
    double alpha_min = 0.2, alpha_max = 0.95, alpha_step = 0.05;
    int alpha_window = 50;
    double p_land_init = 0.1;
    uint32_t cu_overhead = 60000;     // Kamino borrow+repay, tip, executor bookkeeping
    uint32_t max_route_cu = 1'200'000;
    double cu_headroom = 0.15;
    uint64_t base_fee_lamports = 5000;
    uint64_t cu_price_micro_lamports = 0;
  } engine;

  struct Dex {
    bool raydium_v4_enabled = true;
    bool whirlpool_enabled = true;
    uint32_t raydium_v4_cu = 40000;  // measured on a local validator (VM step); placeholder until then
    uint32_t whirlpool_cu = 80000;
  } dex;

  struct Discovery {
    double min_pool_tvl_sol = 50;
    int max_tracked_pools = 1500;
    int interval_s = 600;
  } discovery;

  struct Flash {
    std::string provider = "kamino";
    std::string lending_market = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF";  // Kamino Main Market
    std::vector<std::string> reserves;  // reserve pubkeys, one per base mint
  } flash;

  struct Risk {
    double max_daily_fee_spend_sol = 0.2;
    double min_sol_float = 0.3;
    double max_sol_float = 2.0;
    int max_consecutive_rejects = 200;
    int reject_pause_s = 300;
    int max_feed_lag_ms = 800;
    double quote_mismatch_rate = 0.02;
    int quote_mismatch_min_samples = 50;
    int transfer_fee_max_bps = 100;
    std::vector<std::string> freeze_allowlist{"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"};
    std::string denylist_path = "config/denylist.json";
    int denylist_ttl_s = 86400;
  } risk;

  struct Keys {
    std::string hot_wallet_path = "/etc/jito-arb/hot_wallet.json";
    std::string cold_wallet_pubkey = "VERIFY";
    std::string executor_program_id = "VERIFY";  // set after the executor is deployed
  } keys;

  struct Obs {
    std::string metrics_addr = "127.0.0.1";
    int metrics_port = 9100;
    std::string log_level = "info";
    std::string log_path;
    std::string decision_log_dir = "data/decisions";
    int decision_log_rotate_s = 3600;
  } obs;

  struct Recorder {
    std::string dir = "data/frames";
    int rotate_s = 3600;
  } recorder;

  std::string pools_path = "data/pools.json";  // discovery output the feed subscribes to
};

/** Load a TOML-subset file over the defaults. Throws on a malformed file or an unknown key. */
Config load_config(const std::string& path);

/** The TOML subset: [section] / key = value / "string" / number / bool / [single-line, array]. */
std::map<std::string, std::string> parse_toml(const std::string& text);

/** The RPC URL with the API key appended when the env var is set; public mainnet otherwise. */
std::string resolve_rpc_url(const Config& c);

}  // namespace arb
