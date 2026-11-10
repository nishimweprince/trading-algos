#pragma once
// Runtime configuration (spec §6.9, §10, §12). dry_run defaults to true: going
// live is an explicit `dry_run = false` in the file.
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace arb {

struct Config {
  struct Chain {
    // Slot duration the time-based limits are converted with. Solana cut slots from 400 ms to 200 ms in four
    // stages that completed in October 2026 (216.6 ms measured on the Frankfurt feed, 2026-11-05).
    int slot_ms = 200;
  } chain;

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
    int resync_lag_ms = 60000;  // resync a pool whose snapshot lags the slot clock by this long (was 150 x 400 ms slots)
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
    // A sized profit above this fraction of the input is treated as broken or stale state, not an arb (skipped
    // as implausible_profit). Live dry runs quoted up to 3.4 SOL gross on routes the chain then rejected.
    double max_profit_ratio = 0.02;
    double target_land_rate = 0.30;
    int max_bundles_per_slot = 3;
    int max_cycles_per_pool = 64;
    int max_send_lag_ms = 400;  // drop a candidate whose triggering update is this old at dispatch
    int max_state_age_slots = 2;
    int settle_slots = 2;       // pool account/vault spread accepted once the newest write is this old (0: never)
    int cooldown_ms = 800;      // a dispatched cycle is not re-sent for this long
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
    bool whirlpool_enabled = true;   // sim-parity 204/204 exact (2026-11-04, Helius-funded); adaptive-fee pools stay refused
    uint32_t raydium_v4_cu = 40000;  // measured on a local validator (VM step); placeholder until then
    uint32_t whirlpool_cu = 80000;
    bool cpmm_enabled = true;        // sim-parity 260/260 exact (tests/fixtures/parity_cpmm.json, 2026-11-06)
    uint32_t cpmm_cu = 50000;        // simulated swap_base_input: mean 36.8k, max 46.1k CU
    bool dlmm_enabled = false;  // explicit opt-in; 246 exact swaps, see docs/DLMM-ACCEPTANCE.md
    uint32_t dlmm_cu = 140000;
    bool pumpswap_enabled = true;    // sim-parity 530/530 exact, both directions (tests/fixtures/parity_pumpswap.json, 2026-11-06)
    uint32_t pumpswap_cu = 90000;    // simulated sell_v2 / buy_exact_quote_in_v2: mean 67.8k, max 79.3k CU
    bool clmm_enabled = false;       // explicit opt-in; 256 exact swaps (tests/fixtures/parity_clmm.json)
    uint32_t clmm_cu = 120000;
  } dex;

  struct Discovery {
    double min_pool_tvl_sol = 50;
    int max_tracked_pools = 1500;
    int interval_s = 600;
    // Track (subscribe, record) Whirlpool pools even while dex.whirlpool_enabled keeps them out of quoting:
    // they give Raydium tokens a second pool, and the recordings serve the replay once parity is proven.
    bool track_whirlpool = true;
    // Raydium v4 pools whose base-side vault clears the floor are fetched in full, deepest first, up to this
    // many (about 16k cleared 25 SOL in Nov 2026); the cycle prefilter and TVL filter then cut the set.
    int raydium_fetch_cap = 16000;
    // Track CPMM pools before dex.cpmm_enabled (recordings, a second pool for tokens); fetched by the same
    // vault-floor path as Raydium v4, deepest first, up to cpmm_fetch_cap.
    bool track_cpmm = false;
    int cpmm_fetch_cap = 4000;
    int dlmm_fetch_cap = 4000;  // recently updated eligible pairs before dependent-account reads
    // PumpSwap has no shared vault authority to read balances from, and over a million pools. Discovery instead
    // derives each candidate token's canonical (pump-migrated) wSOL pool and reads those that exist.
    bool track_pumpswap = false;
    // CLMM pools with active liquidity and a static fee, fetched like Whirlpools (full accounts filtered by mint).
    bool track_dlmm = false;
    bool track_clmm = false;
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
    uint32_t skip_log_max_per_s = 0;  // 0 keeps every skip; never limits signed candidates/outcomes
  } obs;

  // Read-only simulation of every dry-run bundle's swap legs (spec §9), paid by a public holder of the base token.
  struct Sim {
    bool enabled = true;
    double max_per_s = 5;              // above this, bundles are sampled (dropped), never queued without bound
    std::string holder_seed_path = "harness/holders_seed.json";
    int holder_refresh_s = 600;        // re-read the base tokens' largest holders
    uint64_t min_payer_lamports = 20'000'000;  // holder wallet SOL for fees and intermediate ATA rent
    // Quarantine: a pool direction on a route that failed or came out short (by short_bps) quarantine_strikes
    // times within quarantine_window_s is not dispatched for quarantine_s. A clean simulation clears its strikes.
    int quarantine_strikes = 3;
    int quarantine_window_s = 600;
    int quarantine_s = 1800;
    int short_bps = 50;
    // Freshness: a job queued longer than max_wait_ms is dropped, not simulated late; only outcomes within
    // strike_max_lag_slots of the candidate strike or clear quarantine (later ones are logged, never acted on).
    int max_wait_ms = 1000;
    int strike_max_lag_slots = 4;
  } sim;

  struct Recorder {
    std::string dir = "data/frames";
    int rotate_s = 3600;
  } recorder;

  std::string pools_path = "data/pools.json";  // discovery output the feed subscribes to
};

/** Slots covering `ms` at the configured slot duration (rounded up, at least 1). */
inline uint64_t ms_to_slots(const Config& c, int ms) {
  int s = c.chain.slot_ms > 0 ? c.chain.slot_ms : 400;
  return uint64_t(ms <= 0 ? 1 : (ms + s - 1) / s);
}

/** Load a TOML-subset file over the defaults. Throws on a malformed file or an unknown key. */
Config load_config(const std::string& path);

/** The TOML subset: [section] / key = value / "string" / number / bool / [single-line, array]. */
std::map<std::string, std::string> parse_toml(const std::string& text);

/** The RPC URL with the API key appended when the env var is set; public mainnet otherwise. */
std::string resolve_rpc_url(const Config& c);

}  // namespace arb
