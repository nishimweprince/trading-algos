#pragma once
// Helius Preconfirmations (19 Nov 2026): transactions streamed the moment a leader's scheduler commits to them,
// before execution. Measured that day: Astralane's equivalent (postpacks) arrived 12.7 ms ahead of our LaserStream
// processed feed at the median; Helius reported preconf coverage of 55.3% of stake (BAM + Helius validators).
// Per docs.helius.dev (Pre-confirmations, preconfSubscribe, checked 2026-11-19):
//   wss://beta.helius-rpc.com/?api-key=<key>, JSON-RPC "preconfSubscribe" with a filter object (accountInclude
//   resolves v0 lookup tables server-side; up to 10,000 keys); the ack is a text frame with the subscription id;
//   notifications are BINARY frames:  u8 version (1) | u64 LE slot | u64 LE tx_index | u8 status (0 failed,
//   1 success, 2 unknown/BAM) | transaction (wire format: legacy, v0 or v1).
//   Billing: 10 credits per delivered message, and a slot with at least one message costs at least 300 credits,
//   per subscription. The same signature can arrive from both sources (Helius and BAM).
#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <vector>

#include "core/types.hpp"

namespace arb::feed {

struct PreconfTx {
  uint64_t slot = 0, tx_index = 0;
  uint8_t status = 2;
  uint64_t recv_ns = 0;
  std::string tx;  // wire-format transaction
};
/** One binary notification. False when too short or not version 1. */
bool parse_preconf(std::string_view payload, PreconfTx& out);

/** The keys a transaction references: static keys, and lookup-table references to resolve (v0 only). */
struct TxKeys {
  Signature sig{};
  uint8_t version = 0;  // 0xff legacy, 0 v0, 1 v1
  std::vector<Pubkey> statics;
  struct Lookup {
    Pubkey table{};
    std::vector<uint8_t> writable, readonly;
  };
  std::vector<Lookup> lookups;
};
/** Parse a wire-format transaction (legacy, v0, v1 per SIMD-0385). False when malformed. */
bool parse_tx_keys(const uint8_t* d, size_t n, TxKeys& out);

std::string preconf_subscribe_body(const std::vector<std::string>& accounts, bool include_failed, const std::vector<std::string>& regions);

/**
 * Credit guard. Billing per the docs: each slot with >= 1 delivered message costs max(300, 10 x messages).
 * Slots are tallied as messages arrive (a slot's charge is final once it is 8 slots old); RPC calls made because
 * of the feed (simulations, lookup-table reads) are added by the caller. Over the UTC-day cap, blocked() is true
 * until the next UTC day.
 */
class CreditGuard {
 public:
  explicit CreditGuard(uint64_t daily_cap) : cap_(daily_cap) {}
  void on_message(uint64_t slot, int64_t wall_ms);
  void add(uint64_t credits, int64_t wall_ms);
  uint64_t today(int64_t wall_ms);
  bool blocked(int64_t wall_ms);
  uint64_t cap() const { return cap_; }

 private:
  void roll(int64_t wall_ms);
  std::mutex mu_;
  uint64_t cap_;
  int64_t day_ = -1;
  uint64_t finalized_ = 0;
  std::map<uint64_t, uint64_t> open_;  // slot -> messages, recent slots only
};

struct PreconfConfig {
  std::string host = "beta.helius-rpc.com";
  std::string api_key;
  std::vector<std::string> accounts;  // accountInclude (base58)
  std::vector<std::string> regions;   // regionInclude; empty = all
  bool include_failed = false;
  int reconnect_min_ms = 1000, reconnect_max_ms = 30000;
  int ping_s = 20;
};

/**
 * One WebSocket subscription on its own thread: TLS, upgrade, subscribe, read binary frames, ping when idle,
 * reconnect with backoff. Each notification is billed (guard), de-duplicated by signature (both sources can send
 * it), and handed to `on_tx` unless it is a failed transaction. Disconnects when the guard blocks.
 */
class PreconfFeed {
 public:
  using OnTx = std::function<void(PreconfTx&&)>;
  PreconfFeed(PreconfConfig c, CreditGuard* guard, OnTx on_tx);
  ~PreconfFeed();
  PreconfFeed(const PreconfFeed&) = delete;
  PreconfFeed& operator=(const PreconfFeed&) = delete;
  void start();
  void stop();
  bool subscribed() const { return subscribed_.load(); }
  uint64_t messages() const { return msgs_.load(); }
  uint64_t duplicates() const { return dups_.load(); }
  uint64_t failed_skipped() const { return failed_.load(); }
  uint64_t bad() const { return bad_.load(); }
  uint64_t reconnects() const { return reconnects_.load(); }
  bool capped() const { return capped_.load(); }

 private:
  void run();
  bool seen(const Signature& sig);
  PreconfConfig c_;
  CreditGuard* guard_;
  OnTx on_tx_;
  std::thread th_;
  std::atomic<bool> stop_{false}, subscribed_{false}, capped_{false};
  std::atomic<uint64_t> msgs_{0}, dups_{0}, failed_{0}, bad_{0}, reconnects_{0};
  std::unordered_set<uint64_t> seen_;
  std::vector<uint64_t> seen_ring_;
  size_t seen_pos_ = 0;
};

}  // namespace arb::feed
