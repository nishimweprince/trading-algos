#pragma once
// Blocking Solana JSON-RPC over a keep-alive HTTPS connection. Aux threads and
// boot only; never on the trading path.
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "core/types.hpp"
#include "net/conn.hpp"

namespace pd {

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
  /** sendTransaction (skipPreflight, maxRetries 3): the signature. Throws on a JSON-RPC error. */
  std::string send_transaction(const std::string& tx_b64);
  /** Empty string = success; otherwise the error plus logs. */
  std::string simulate(const std::string& tx_b64);

 private:
  Url url_;
  int timeout_ms_;
  Conn conn_;
  HttpResponseParser parser_;
  std::string req_, body_;
  uint64_t id_ = 1;
};

}  // namespace pd
