#pragma once
// Helius usage accounting and quota state, process-wide (any thread). The RPC client reports every call here;
// a 429 whose body says the plan's usage is exhausted ("max usage reached") flips the quota flag until a call
// succeeds again, and callers that poll (blockhash, resync, simulation) back off while it is set.
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace arb::usage {

struct MethodStats {
  uint64_t calls = 0, errors = 0, rate_limited = 0, bytes_out = 0, bytes_in = 0;
};

/** One RPC call: method, HTTP status, request and response sizes. A quota 429 sets the flag; a 200 clears it. */
void record_rpc(std::string_view method, int status, size_t bytes_out, size_t bytes_in, std::string_view body, int64_t now_ms);
std::vector<std::pair<std::string, MethodStats>> rpc_snapshot();

/** The response is the plan's usage cap, not a per-second rate limit. */
bool is_quota_body(std::string_view body);
bool quota_exhausted();
int64_t quota_since_ms();
void set_quota(bool exhausted, int64_t now_ms);

/**
 * Whether a periodic caller may make its call now: always while the quota is fine, at most once per
 * `hold_ms` while it is exhausted. Updates `last_ms` when it allows a call.
 */
bool may_call(int64_t& last_ms, int64_t now_ms, int64_t hold_ms = 60'000);

}  // namespace arb::usage
