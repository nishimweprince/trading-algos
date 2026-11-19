#pragma once
// Gravity Block Exchange (GBX) route, 19 Nov 2026. Astralane's regional GBX broadcasters accept bundles over gRPC
// and fan them out to the GBX builders (Bifrost, Titan) for validators running the GBX sidecar. Per the docs
// (astralane.gitbook.io, Gravity Block Exchange: submit bundles; github.com/Astralane/gbx-protos, checked
// 2026-11-19):
//   http://fra-gbx.astralane.io:5064 (plaintext gRPC), wire-compatible with the Jito searcher API:
//     auth.AuthService/GenerateAuthChallenge {role=SEARCHER(1), pubkey}           -> {challenge}
//     auth.AuthService/GenerateAuthTokens    {challenge, client_pubkey, signed}  -> {access_token, refresh_token}
//       signed = ed25519("<pubkey base58>-<challenge>"); calls carry "authorization: Bearer <access token>"
//     auth.AuthService/RefreshAccessToken    {refresh_token}                      -> {access_token}
//     searcher.SearcherService/SendBundle    {bundle{packets[{data}], dropping_indices, slot?, target_accounts}}
//                                                                                   -> {uuid}
//     searcher.SearcherService/GetConnectedLeaders {}  -> {map<validator, SlotList{repeated slots}>}
//   A bundle lands all or nothing, in order; a transaction in dropping_indices is dropped if it errors or its
//   signature already landed earlier. target_accounts (<= 16): the bundle is re-simulated after a write to them
//   (best effort when contended). No tip accounts: priority fee >= 5,000 lamports for unregistered keys
//   (5 requests/s per region). No result stream: landing is checked on chain by signature.
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "core/crypto.hpp"
#include "core/rpc.hpp"
#include "core/types.hpp"

namespace arb::submit {

// ---- wire format (pure; tested on the laptop) ----
std::string gbx_challenge_request(const Pubkey& pubkey);
bool gbx_parse_challenge(std::string_view resp, std::string& challenge);
/**
 * The bytes the challenge signature covers. Jito clients sign "<pubkey base58>-<challenge>" (variant 0), which the
 * GBX broadcaster refused on 19 Nov ("invalid or expired signed authentication challenge"); its proto only says
 * "the challenge token prepended with their pubkey". Variants: 0 "<b58>-<challenge>", 1 <32 raw bytes><challenge>,
 * 2 "<b58><challenge>", 3 "<challenge>". The client tries them in order and keeps the first that is accepted.
 */
std::string gbx_challenge_message(const Pubkey& pubkey, const std::string& challenge, int variant = 0);
constexpr int GBX_SIGN_VARIANTS = 4;
std::string gbx_tokens_request(const std::string& challenge, const Pubkey& pubkey, const Signature& signed_challenge);
struct GbxToken {
  std::string value;
  int64_t expires_s = 0;  // unix seconds (0 when absent)
};
bool gbx_parse_tokens(std::string_view resp, GbxToken& access, GbxToken& refresh);
std::string gbx_refresh_request(const std::string& refresh_token);
bool gbx_parse_refresh(std::string_view resp, GbxToken& access);
/** SendBundleRequest: transactions (wire bytes) in order, dropping indices, optional slot, target accounts. */
std::string gbx_bundle_request(const std::vector<std::string>& txs, const std::vector<uint32_t>& dropping,
                               const std::vector<Pubkey>& targets, uint64_t slot = 0);
bool gbx_parse_bundle_uuid(std::string_view resp, std::string& uuid);
/** ConnectedLeadersResponse: validator (base58) -> its upcoming leader slots. */
bool gbx_parse_connected_leaders(std::string_view resp, std::map<std::string, std::vector<uint64_t>>& out);

/** One unary gRPC call. Returns the gRPC status code (0 = OK), the response bytes in `resp`, the message in `err`. */
class GbxTransport {
 public:
  virtual ~GbxTransport() = default;
  virtual int call(const std::string& method, const std::string& req, std::string& resp, const std::string& bearer, std::string& err) = 0;
};
/** The production transport (grpc++ on the VM; a stand-in that always fails UNAVAILABLE elsewhere). */
std::unique_ptr<GbxTransport> make_gbx_transport(const std::string& url, int timeout_ms);

struct GbxJob {
  std::vector<std::string> txs;  // wire bytes, ours last
  std::vector<uint32_t> dropping;
  std::vector<Pubkey> targets;
  std::string sig;               // ours, base58
  uint64_t built_slot = 0;
  uint64_t fee_lamports = 0;     // ours: base + priority (paid only if the bundle lands)
  uint16_t route_class = 0;
  int64_t wall_ms = 0;
  bool with_trigger = false;
  uint64_t enq_ns = 0;
};
enum class GbxOutcome : uint8_t { landed = 1, failed = 2, dropped = 3 };
const char* gbx_outcome_name(GbxOutcome o);

struct GbxConfig {
  std::string url = "http://fra-gbx.astralane.io:5064";
  int timeout_ms = 2000;
  int leaders_refresh_s = 30;
  int status_poll_ms = 1000;
  int status_timeout_s = 90;
  uint64_t max_slot_lag = 1;
  /** Required: the transport factory (live: make_gbx_transport). Tests inject mocks for both. */
  std::function<std::unique_ptr<GbxTransport>()> transport;
  std::function<std::vector<RpcSigStatus>(const std::vector<std::string>&)> statuses;
};

class GbxClient {
 public:
  using Sent = std::function<void(const GbxJob&, int code, const std::string& uuid, const std::string& err, int64_t rtt_us)>;
  using Resolved = std::function<void(const GbxJob&, GbxOutcome, uint64_t slot)>;
  GbxClient(GbxConfig c, Keypair identity, const std::string& rpc_url, const std::atomic<uint64_t>* slot, Sent sent, Resolved resolved);
  ~GbxClient();
  GbxClient(const GbxClient&) = delete;
  GbxClient& operator=(const GbxClient&) = delete;
  bool send(GbxJob j);
  /** Is the leader of `slot` connected to GBX (per the latest GetConnectedLeaders)? */
  bool gbx_slot(uint64_t slot) const;
  /** Connected validators, and the share of the next `window` slots they lead (ppm), from the latest refresh. */
  size_t connected_validators() const;
  uint32_t coverage_ppm(uint64_t from_slot, uint64_t window) const;
  bool authed() const { return authed_.load(); }
  uint64_t sent_ok() const { return ok_.load(); }
  uint64_t rejected() const { return rejected_.load(); }
  uint64_t stale() const { return stale_.load(); }
  uint64_t landed() const { return landed_.load(); }
  uint64_t dropped() const { return dropped_.load(); }
  uint64_t failed() const { return failed_.load(); }
  uint64_t auth_failures() const { return auth_fail_.load(); }
  std::string last_error() const {
    std::lock_guard<std::mutex> g(info_mu_);
    return last_err_;
  }

 private:
  void run();
  void run_status();
  bool ensure_auth(GbxTransport& t);
  void refresh_leaders(GbxTransport& t);
  GbxConfig c_;
  Keypair id_;
  std::string rpc_url_;
  const std::atomic<uint64_t>* slot_;
  Sent sent_;
  Resolved resolved_;
  std::string access_, refresh_;
  int sign_variant_ = -1;  // the accepted challenge encoding, once known
  int64_t access_exp_ = 0, refresh_exp_ = 0;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<GbxJob> q_;
  mutable std::mutex info_mu_;
  std::map<uint64_t, std::string> slot_leader_;  // upcoming GBX slots
  size_t validators_ = 0;
  std::string last_err_;
  std::mutex pmu_;
  struct Pending {
    GbxJob job;
    int64_t deadline_ms = 0;
  };
  std::vector<Pending> pending_;
  std::atomic<bool> stop_{false}, authed_{false};
  std::atomic<uint64_t> ok_{0}, rejected_{0}, stale_{0}, landed_{0}, dropped_{0}, failed_{0}, auth_fail_{0};
  std::thread th_, status_th_;
};

}  // namespace arb::submit
