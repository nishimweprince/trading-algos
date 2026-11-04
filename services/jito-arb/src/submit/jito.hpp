#pragma once
// Jito block-engine client (spec §9), per docs.jito.wtf/lowlatencytxnsend (checked 2026-11-04):
//   POST /api/v1/bundles                    sendBundle [[base64 tx..], {"encoding":"base64"}] -> bundle id
//   POST /api/v1/getInflightBundleStatuses  [[ids <= 5]] -> Invalid | Pending | Failed | Landed (5 min look-back)
//   POST /api/v1/getBundleStatuses          [[ids <= 5]] -> slot, confirmation_status, err
//   POST /api/v1/getTipAccounts             [] -> tip accounts
//   GET  https://bundles.jito.wtf/api/v1/bundles/tip_floor -> landed_tips_{25,50,75,95,99}th_percentile (SOL)
// Default limit: 1 request per second per IP per region; HTTP 429 when exceeded -> back off that region.
//
// Submitter is the dry-run gate: constructed with dry_run = true it holds no client at all, so
// sendBundle is unreachable (spec §1 rule 6).
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "core/error.hpp"
#include "core/types.hpp"
#include "tx/message_v0.hpp"

namespace arb::submit {

constexpr size_t MAX_STATUS_IDS = 5;

std::string send_bundle_body(std::span<const std::string> txs_b64, uint64_t id = 1);
std::string statuses_body(const char* method, std::span<const std::string> bundle_ids, uint64_t id = 1);
/** The bundle id, or the block engine's error message. */
Result<std::string> parse_send_bundle(std::string_view body, std::string* error_message = nullptr);

enum class Inflight : uint8_t { Unknown = 0, Invalid, Pending, Failed, Landed };
struct InflightStatus {
  std::string bundle_id;
  Inflight status = Inflight::Unknown;
  uint64_t landed_slot = 0;
};
std::vector<InflightStatus> parse_inflight(std::string_view body);
std::vector<Pubkey> parse_tip_accounts(std::string_view body);
/** landed_tips_25th_percentile of the newest entry, SOL -> lamports (rounded up). 0 when absent. */
uint64_t parse_tip_floor_p25(std::string_view body);

/** One HTTP POST on a kept-alive connection; returns the status code, body in `out`. Throws on transport failure. */
class HttpPost {
 public:
  virtual ~HttpPost() = default;
  virtual int post(std::string_view path, std::string_view body, std::string& out) = 0;
};
using HttpFactory = std::function<std::unique_ptr<HttpPost>(const std::string& base_url)>;
/** The production transport: core/Rpc's kept-alive TLS connection. */
std::unique_ptr<HttpPost> make_rpc_http(const std::string& base_url, int timeout_ms);

struct RegionResult {
  std::string region;
  int http_status = 0;
  bool backed_off = false;  // skipped: the region answered 429 within the last backoff window
  std::string bundle_id, error;
};

class JitoClient {
 public:
  /** `url_fmt` has one %s for the region (config jito.block_engine_fmt). */
  JitoClient(std::vector<std::string> regions, const std::string& url_fmt, HttpFactory factory, int backoff_429_ms = 1000);
  /** Fan the bundle out to every region not in backoff. */
  std::vector<RegionResult> send_bundle(std::span<const std::string> txs_b64, int64_t now_ms);
  /** Status calls go to the first region. */
  std::vector<InflightStatus> inflight(std::span<const std::string> ids);
  std::vector<Pubkey> tip_accounts();
  uint64_t sends() const { return sends_; }

 private:
  struct Region {
    std::string name;
    std::unique_ptr<HttpPost> http;
    int64_t backoff_until_ms = 0;
    uint64_t status_429 = 0;
  };
  std::vector<Region> regions_;
  int backoff_ms_;
  uint64_t sends_ = 0;
};

enum class SubmitOutcome : uint8_t { DryRun = 0, Sent, Stale, NoRegion };

class Submitter {
 public:
  /** dry_run = true discards `client`: nothing this object does can reach sendBundle. */
  Submitter(bool dry_run, std::unique_ptr<JitoClient> client);
  bool dry_run() const { return !client_; }
  /**
   * Send one signed transaction as a one-transaction bundle, unless the slot it was built for has passed
   * (a stale arb is a new decision, never a retry).
   */
  SubmitOutcome submit(const tx::SignedTx& t, uint64_t built_slot, uint64_t current_slot, int64_t now_ms,
                       std::vector<RegionResult>* results = nullptr);

 private:
  std::unique_ptr<JitoClient> client_;
};

/**
 * Bundle tracker (spec §9 landing confirmation): polls getInflightBundleStatuses every 500 ms for up to
 * 30 s, and also accepts a landing seen first on the wallet's LaserStream subscription. Each attempt ends
 * as landed / not_landed / rejected; the callback feeds the tip controller and p_land.
 */
class BundleTracker {
 public:
  enum class Result : uint8_t { Landed = 1, NotLanded = 2, Rejected = 3 };
  using Done = std::function<void(const std::string& bundle_id, uint16_t route_class, Result r, uint64_t landed_slot)>;
  BundleTracker(Done done, int poll_ms = 500, int timeout_ms = 30000) : done_(std::move(done)), poll_ms_(poll_ms), timeout_ms_(timeout_ms) {}
  void track(const std::string& bundle_id, const Signature& sig, uint16_t route_class, int64_t now_ms);
  /** Up to 5 ids whose poll is due (and marks them polled). */
  std::vector<std::string> due(int64_t now_ms);
  void on_statuses(const std::vector<InflightStatus>& st, int64_t now_ms);
  /** The wallet's account changed in a transaction with this signature: landed, without waiting for a poll. */
  void on_signature_seen(const Signature& sig, uint64_t slot);
  /** Expire attempts past the timeout as not landed. */
  void expire(int64_t now_ms);
  size_t pending() const { return open_.size(); }

 private:
  struct Open {
    Signature sig{};
    uint16_t route_class = 0;
    int64_t sent_ms = 0, last_poll_ms = 0;
  };
  void finish(const std::string& id, Result r, uint64_t slot);
  Done done_;
  int poll_ms_, timeout_ms_;
  std::map<std::string, Open> open_;
};

}  // namespace arb::submit
