#pragma once
// Runtime configuration (spec §6.9, §10, §12). The bot is live only: dry_run = true refuses the start
// (kept so an old config fails loudly instead of trading unexpectedly).
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
    bool dry_run = false;                // removed: the bot is live only; true refuses the start (kept to catch old configs)
    std::string funding_mode = "flash";  // flash | own
    std::string kill_file = "KILL";      // relative to the run directory
    // {"pools":[..]} written by the live-plan tool: the pools whose lookup tables and token accounts exist.
    // Routes through any other pool are skipped (not_armed). Live refuses to start without it.
    std::string armed_path;
    // {"tables":[..]} written by arm-accounts: the bot's lookup tables, loaded from chain; live refuses to start
    // without any.
    std::string alts_path;
  } mode;

  struct Helius {
    std::string laserstream_endpoint = "VERIFY";  // from the Helius dashboard (Frankfurt)
    std::string rpc_url = "https://mainnet.helius-rpc.com";
    std::string api_key_env = "HELIUS_API_KEY";
    int max_grpc_connections = 4;  // pool shards; plus one wallet/reserve connection
    int shard_size = 300;
    int max_frames_per_pass = 64;  // frames read per shard before the engine prices the batch (latency vs dedupe)
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
    int tip_floor_percentile = 25;     // tip_min = max(tip_min_lamports, this landed-tip percentile); 0 = off
    int tip_accounts_refresh_s = 3600;
    int backoff_429_ms = 1000;
    int status_poll_ms = 500;
    int status_timeout_ms = 30000;
    bool sender_enabled = false;
    std::string sender_url = "https://sender.helius-rpc.com/fast";
    // Leader-aware routing (18 Nov 2026): each bundle goes to the regions nearest the leaders of its slot and the
    // next, plus `always_regions`, on per-region sender threads. Off: every bundle to `regions`, as before.
    bool route_by_leader = true;
    std::vector<std::string> route_regions{"frankfurt", "amsterdam", "london", "dublin", "ny", "slc", "tokyo", "singapore"};
    std::vector<std::string> always_regions{"frankfurt"};
    bool skip_not_jito_leaders = true;   // both leaders run no Jito: no bundle path reaches them
    bool skip_bam_leaders = false;       // both leaders run BAM and there is no Direct Bundle Ingest UUID
    std::string bam_dbi_fmt = "http://%s.mainnet.bam.jito.wtf:9090";
    std::string bam_uuid_env = "JITO_BAM_UUID";  // BAM Direct Bundle Ingest (Jito allowlist); unset = off
    int send_timeout_ms = 2000;
    int keepalive_s = 45;                // idle block-engine connections pinged after this long (0 = off)
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
    int max_sized_per_update = 4;  // cycles sized per pool update, largest spot edge first (0: all)
    double alpha_init = 0.5;
    double alpha_min = 0.2, alpha_max = 0.95, alpha_step = 0.05;
    int alpha_window = 50;
    std::string tip_state_path;       // learned alpha / p_land per route class, kept across restarts ("" = off)
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
    int feed_flap_reconnects_per_min = 3;  // reconnects (all shards) in 60 s that close the feed gate
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
    std::string sent_log_path;           // one JSON line per region send and audit verdict ("" = off)
    std::string decision_log_dir = "data/decisions";
    int decision_log_rotate_s = 3600;
    uint32_t skip_log_max_per_s = 0;  // 0 keeps every skip; never limits signed candidates/outcomes
    std::string alert_name = "jito-arb";  // prefix of every alert (Telegram token and chat id come from the environment)
    int alert_after_s = 60;               // unhealthy feed this long before "FEED DOWN"
    int alert_repeat_s = 1800;            // reminder while it stays down
  } obs;


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
