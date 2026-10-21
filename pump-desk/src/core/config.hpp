#pragma once
// Runtime configuration. Defaults are the curve-in, migrate-out pilot settings (2026-10-21).
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace pd {

struct Config {
  struct Stream {
    std::string endpoint;  // HELIUS_GRPC_URL when empty
    std::string token;     // HELIUS_GRPC_TOKEN when empty
    int band_values = 12;
    int ping_interval_ms = 10000;
    int reconnect_min_ms = 1000;
    int reconnect_max_ms = 30000;
    int gap_silence_ms = 3000;
    int gap_slot_jump = 12;
    int silence_reconnect_ms = 30000;
  } stream;

  struct Rpc {
    std::string http_url;  // HELIUS_HTTP_URL when empty
    int blockhash_refresh_ms = 2000;
    int amm_config_refresh_ms = 30000;
    int balance_refresh_ms = 30000;
    int timeout_ms = 5000;
  } rpc;

  /**
   * Curve-in, migrate-out: buy the last part of a bonding curve, hold through
   * completion and the migrate, sell into the first PumpSwap slots.
   */
  struct Curves {
    double admit_cost_sol = 5.0;   // track curves this close to completing
    int fee_bps = 125;             // floor on the curve fee used for quotes (Global may say more)
    int track_window_minutes = 120;
    int complete_no_pool_sec = 120;
    /** PumpSwap virtual quote reserve when the pool push has not arrived (0 on mayhem pools). */
    uint64_t default_virtual_quote_lamports = 17'584'492'770ULL;
    int max_curves = 8192;
    double entry_max_remaining_sol = 4.0;  // buy once completion costs at most this
    double entry_min_remaining_sol = 0.0;  // ... and more than this
    bool allow_mayhem = false;             // mayhem pools open with almost no liquidity
    double max_hold_s = 30;                // not complete this long after the buy: sell back on the curve
    double abort_remaining_sol = 0;        // completion cost climbs past this: sell on the curve; 0 = off
    double migrate_wait_s = 30;            // complete but no migrate this long: hand to Jupiter
    bool partial_fill = true;              // pump buy fills the remaining tokens instead of failing 6021
  } curve;

  struct Screen {
    bool require_pump_suffix = true;
    std::vector<std::string> blacklist_mints;
    std::vector<std::string> blacklist_creators;
  } screen;

  struct Entry {
    double size_sol = 0.05;          // fixed SOL per curve buy
    double slippage_pct = 10;        // max_sol_cost = size * (1 + this)
    uint32_t cu_limit = 150000;      // until routing.cu_samples_min buys have landed
    double priority_multiplier = 1;  // on the CU price for buys, like exits.exit_priority_multiplier
  } entry;

  /** After the migrate: take profit at tp_pct over entry, else sell timeout_slots after the migrate slot. */
  struct Exits {
    double tp_pct = 0;  // 0 = off
    int timeout_slots = 3;
    std::vector<double> protective_slippage_tiers{20, 90};
    std::vector<double> take_profit_slippage_tiers{8, 20, 90};
    int exit_retry_ms = 1500;
    int max_exit_attempts = 6;
    double exit_priority_multiplier = 1;
    uint32_t cu_limit = 150000;
  } exits;

  struct Risk {
    int max_concurrent_positions = 1;
    double gas_headroom_sol = 0.003;
    double wallet_floor_sol = 0;
    std::string kill_file = "KILL";
    int buy_confirm_timeout_ms = 4000;
    int sent_resolve_ms = 90000;
    double max_loss_sol = 0.06;  // realized loss since boot that latches the kill; 0 = off
  } risk;

  struct Fees {
    uint64_t cu_price_default = 1'000'000;
    uint64_t cu_price_floor = 100'000;
    uint64_t cu_price_cap = 5'000'000;
    int refresh_ms = 3000;
    std::string priority_level = "VeryHigh";  // Helius getPriorityFeeEstimate level
  } fees;

  struct Sender {
    std::string url = "http://fra-sender.helius-rpc.com/fast";
    bool swqos_only = true;
    uint64_t tip_lamports = 5000;
    uint64_t exit_tip_lamports = 5000;
    int connections = 2;
    int ping_ms = 15000;
    int retry_429_ms = 1000;  // backoff before resending a 429'd tx, times the try number (Sender allows ~1 tx/s)
    int max_429_retries = 2;  // resends of the same bytes after a 429
    std::vector<std::string> tip_accounts;
  } sender;

  struct Jito {
    bool enabled = false;
    std::string url = "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/transactions";
    uint64_t tip_lamports = 10000;
    std::vector<std::string> tip_accounts;
  } jito;

  /** Per leader class: how this client orders transactions decides what we pay for. */
  struct RouteClass {
    std::string cu_price_mode = "estimate";  // estimate | floor | beat_rivals
    double cu_price_multiplier = 1;           // on top of entry.priority_multiplier
    std::string exit_cu_price_mode = "estimate";  // sells: same modes, times exits.exit_priority_multiplier
    uint32_t cu_limit = 0;                    // 0 = measured from our own fills
    uint64_t sender_tip_lamports = 0;         // 0 = sender.tip_lamports
    bool jito = false;                        // also send via Jito (needs jito.enabled)
  };

  struct Routing {
    bool enabled = true;
    int lookahead_slots = 1;  // price for the leader of (processed tip + this)
    std::string bam_validators_path = "data/bam_validators.json";
    int cluster_nodes_refresh_s = 600;
    std::string rivals_path = "data/rivals.json";  // pump-desk-report --rivals writes it
    int rival_window_slots = 4;     // record pool txs up to migrate slot + this
    double cu_limit_margin_pct = 15;
    int cu_samples_min = 8;         // measured CU needs this many buy fills, else entry.cu_limit
    int max_rivals = 3;             // wallets kept from the rivals file
    // Agave/Jito: priority decides. BAM: arrival decides across ~20 ms auctions.
    // Firedancer: fee per requested CU decides.
    RouteClass agave, bam, firedancer;
  } routing;

  /** Jupiter Swap API: sells a position the PumpSwap exits could not (exits.max_exit_attempts). */
  struct Jupiter {
    bool enabled = true;          // and JUPITER_API_KEY set; otherwise off
    std::string url = "https://api.jup.ag/swap/v1";
    std::string api_key;          // JUPITER_API_KEY only (never the config file)
    int slippage_bps = 1500;
    int max_attempts = 3;         // Jupiter sells per position; then hands off
    int confirm_ms = 20000;       // wait this long for the stream to show it before the next attempt
    uint64_t max_priority_lamports = 2'000'000;
  } jupiter;

  struct Threads {
    int ingest_cpu = 1, logic_cpu = 2, exec_cpu = 3, aux_cpu = -1;
    int fifo_priority = 0;
    bool lock_memory = true;
  } threads;

  struct Ring {
    int slots = 2048;
    int slot_bytes = 65536;
  } ring;

  struct Storage {
    std::string db_path = "data/pump-desk.db";
    std::string jsonl_path = "data/events.jsonl";
    std::string capture_path;  // raw frame capture for replay; empty = off
  } storage;

  std::string wallet_env = "WALLET_PRIVATE_KEY";
};

/** Load a TOML-subset file over the defaults; env vars fill endpoint/token/url when empty. Throws on error. */
Config load_config(const std::string& path);

/** The TOML subset: [section] / key = value / "string" / number / bool / [single-line, array]. */
std::map<std::string, std::string> parse_toml(const std::string& text);

}  // namespace pd
