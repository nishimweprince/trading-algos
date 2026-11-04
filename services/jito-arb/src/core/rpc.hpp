#pragma once
// Blocking Solana JSON-RPC over a keep-alive HTTPS connection. Aux threads and
// boot only; never on the trading path.
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "core/types.hpp"
#include "submit/conn.hpp"

namespace arb {

struct RpcAccount {
  Pubkey owner{};
  uint64_t lamports = 0;
  std::vector<uint8_t> data;
};

struct RpcTokenBalance {
  uint32_t account_index = 0;
  std::string mint, owner, program_id;
  uint64_t amount = 0;
};

struct RpcTransaction {
  bool found = false;
  uint64_t slot = 0;
  bool failed = false;
  uint64_t fee = 0;
  std::vector<std::string> keys;  // static + loaded writable + loaded readonly
  std::vector<uint64_t> pre_balances, post_balances;
  std::vector<RpcTokenBalance> pre_tokens, post_tokens;
};

struct RpcSigStatus {
  bool found = false;
  uint64_t slot = 0;
  bool failed = false;
  std::string confirmation;  // processed | confirmed | finalized
};

class Rpc {
 public:
  Rpc(const std::string& url, int timeout_ms);

  /** Raw call: returns the response body. Throws on transport failure or a JSON-RPC error. */
  std::string call(const std::string& method, const std::string& params_json);
  /** One HTTP exchange on the kept-alive connection (any API on this host). Throws on transport failure only. */
  std::string http(const char* verb, std::string_view path, std::string_view body, std::string_view headers, int& status,
                   const char* what);

  struct Blockhash {
    Hash hash{};
    uint64_t last_valid_height = 0;
    uint64_t slot = 0;
  };
  Blockhash latest_blockhash(const char* commitment = "confirmed");
  std::vector<std::optional<RpcAccount>> multiple_accounts(const std::vector<Pubkey>& keys, const char* commitment = "confirmed");
  std::optional<RpcAccount> account(const Pubkey& key, const char* commitment = "confirmed");
  /** getTokenLargestAccounts: (token account, raw amount), largest first; `slot` = context slot. */
  std::vector<std::pair<Pubkey, uint64_t>> token_largest_accounts(const Pubkey& mint, uint64_t& slot,
                                                                  const char* commitment = "confirmed");
  /** {lamports, context slot} */
  std::pair<uint64_t, uint64_t> balance(const Pubkey& key, const char* commitment = "confirmed");
  RpcTransaction transaction(const std::string& sig_b58, const char* commitment = "confirmed");
  RpcSigStatus signature_status(const std::string& sig_b58);
  std::string latest_signature_for(const Pubkey& address);
  struct EpochInfo {
    uint64_t epoch = 0, absolute_slot = 0, slot_index = 0, slots_in_epoch = 0;
    uint64_t first_slot() const { return absolute_slot - slot_index; }
  };
  EpochInfo epoch_info(const char* commitment = "processed");
  /** Helius getPriorityFeeEstimate (micro-lamports/CU); 0 when unavailable. */
  uint64_t priority_fee_estimate(const std::vector<Pubkey>& accounts, const char* level = "VeryHigh");
  /** Empty string = success; otherwise the error plus logs. */
  std::string simulate(const std::string& tx_b64);
  /**
   * simulateTransaction with sigVerify:false and replaceRecentBlockhash:true, returning the
   * post-simulation state of `accounts` (base64). Read-only: nothing is sent to the cluster.
   */
  struct SimResult {
    bool ok = false;
    std::string err;  // JSON of value.err when it failed
    uint64_t slot = 0;
    uint64_t units = 0;
    std::vector<std::string> logs;
    std::vector<std::optional<RpcAccount>> accounts;
  };
  SimResult simulate_full(const std::string& tx_b64, const std::vector<Pubkey>& accounts, uint64_t min_context_slot = 0);
  /** getMultipleAccounts that also reports the context slot. */
  std::vector<std::optional<RpcAccount>> multiple_accounts_at(const std::vector<Pubkey>& keys, uint64_t& slot,
                                                              const char* commitment = "confirmed");
  /** getProgramAccounts (base64) with dataSize and memcmp (offset, base58 bytes) filters: raw response body. */
  std::string program_accounts_raw(const Pubkey& program, uint64_t data_size,
                                   const std::vector<std::pair<uint64_t, std::string>>& memcmps);
  /** getAccountInfo executable flag + owner, for the boot-time program ID check. */
  struct ProgramInfo {
    bool found = false, executable = false;
    Pubkey owner{};
  };
  ProgramInfo program_info(const Pubkey& key);
  uint64_t slot(const char* commitment = "processed");

 private:
  Url url_;
  int timeout_ms_;
  Conn conn_;
  HttpResponseParser parser_;
  std::string req_, body_;
  uint64_t id_ = 1;
};

}  // namespace arb
