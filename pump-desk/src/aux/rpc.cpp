#include "aux/rpc.hpp"

#include <simdjson.h>

#include <algorithm>
#include <cstdlib>
#include <stdexcept>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/sys.hpp"
#include "stream/update.hpp"

namespace pd {

namespace {

using simdjson::dom::element;

std::string keys_json(const std::vector<Pubkey>& keys) {
  std::string s = "[";
  for (size_t i = 0; i < keys.size(); ++i) {
    if (i) s += ",";
    s += "\"" + b58(keys[i]) + "\"";
  }
  return s + "]";
}

uint64_t u64_of(element e) {
  uint64_t v = 0;
  if (e.get(v) == simdjson::SUCCESS) return v;
  int64_t i = 0;
  if (e.get(i) == simdjson::SUCCESS) return i < 0 ? 0 : uint64_t(i);
  double d = 0;
  if (e.get(d) == simdjson::SUCCESS) return d < 0 ? 0 : uint64_t(d);
  return 0;
}

std::optional<RpcAccount> account_of(element v) {
  if (v.is_null()) return std::nullopt;
  RpcAccount a;
  std::string_view owner;
  if (v["owner"].get(owner) == simdjson::SUCCESS) b58_decode32(owner, a.owner);
  a.lamports = u64_of(v["lamports"]);
  simdjson::dom::array data;
  if (v["data"].get(data) == simdjson::SUCCESS) {
    std::string_view b64s;
    if (data.at(0).get(b64s) == simdjson::SUCCESS) b64_decode(b64s, a.data);
  }
  return a;
}

void token_balances(element arr, std::vector<RpcTokenBalance>& out) {
  simdjson::dom::array a;
  if (arr.get(a) != simdjson::SUCCESS) return;
  for (element t : a) {
    RpcTokenBalance b;
    b.account_index = uint32_t(u64_of(t["accountIndex"]));
    std::string_view s;
    if (t["mint"].get(s) == simdjson::SUCCESS) b.mint = s;
    if (t["owner"].get(s) == simdjson::SUCCESS) b.owner = s;
    if (t["programId"].get(s) == simdjson::SUCCESS) b.program_id = s;
    if (t["uiTokenAmount"]["amount"].get(s) == simdjson::SUCCESS) parse_u64(s, b.amount);
    out.push_back(std::move(b));
  }
}

}  // namespace

Rpc::Rpc(const std::string& url, int timeout_ms) : url_(parse_url(url)), timeout_ms_(timeout_ms) {}

std::string Rpc::call(const std::string& method, const std::string& params_json) {
  body_ = "{\"jsonrpc\":\"2.0\",\"id\":" + std::to_string(id_++) + ",\"method\":\"" + method + "\",\"params\":" + params_json + "}";
  int status = 0;
  std::string out = http("POST", url_.path, body_, {}, status, method.c_str());
  if (status != 200) throw std::runtime_error("rpc http " + std::to_string(status) + ": " + out.substr(0, 200));
  if (out.find("\"error\"") != std::string::npos && out.find("\"result\"") == std::string::npos)
    throw std::runtime_error("rpc error: " + method + ": " + out.substr(0, 300));
  return out;
}

std::string Rpc::http(const char* verb, std::string_view path, std::string_view body, std::string_view headers, int& status,
                      const char* what) {
  format_request(req_, verb, url_, path, body, headers);
  for (int attempt = 0; attempt < 2; ++attempt) {
    std::string err;
    if (!conn_.open() && !conn_.connect(url_, timeout_ms_, &err)) throw std::runtime_error(std::string("connect ") + what + ": " + err);
    parser_.reset();
    if (!conn_.write_all(req_.data(), req_.size(), timeout_ms_)) {
      conn_.close();
      continue;  // a stale keep-alive socket: reconnect once
    }
    uint64_t deadline = now_ns() + uint64_t(timeout_ms_) * 1'000'000ULL;
    char buf[16384];
    bool done = false, dead = false;
    while (!done) {
      int left = int((deadline - std::min(deadline, now_ns())) / 1'000'000ULL);
      if (left <= 0) {
        conn_.close();
        throw std::runtime_error(std::string("timeout: ") + what);
      }
      if (!conn_.wait_readable(left)) continue;
      ssize_t n = conn_.read_some(buf, sizeof buf);
      if (n < 0) { dead = true; break; }
      if (n > 0) done = parser_.feed(buf, size_t(n));
    }
    if (dead) {
      conn_.close();
      continue;
    }
    status = parser_.status();
    return std::string(parser_.body());
  }
  throw std::runtime_error(std::string("connection lost: ") + what);
}

Rpc::Blockhash Rpc::latest_blockhash(const char* commitment) {
  std::string body = call("getLatestBlockhash", std::string("[{\"commitment\":\"") + commitment + "\"}]");
  simdjson::dom::parser p;
  element r = p.parse(body)["result"];
  Blockhash b;
  std::string_view h = r["value"]["blockhash"];
  if (!b58_decode32(h, b.hash)) throw std::runtime_error("bad blockhash");
  b.last_valid_height = u64_of(r["value"]["lastValidBlockHeight"]);
  b.slot = u64_of(r["context"]["slot"]);
  return b;
}

std::vector<std::optional<RpcAccount>> Rpc::multiple_accounts(const std::vector<Pubkey>& keys, const char* commitment) {
  std::string body = call("getMultipleAccounts", "[" + keys_json(keys) + ",{\"encoding\":\"base64\",\"commitment\":\"" + commitment + "\"}]");
  simdjson::dom::parser p;
  simdjson::dom::array arr = p.parse(body)["result"]["value"];
  std::vector<std::optional<RpcAccount>> out;
  for (element v : arr) out.push_back(account_of(v));
  return out;
}

std::optional<RpcAccount> Rpc::account(const Pubkey& key, const char* commitment) {
  auto v = multiple_accounts({key}, commitment);
  return v.empty() ? std::nullopt : v[0];
}

std::vector<std::pair<Pubkey, uint64_t>> Rpc::token_largest_accounts(const Pubkey& mint, uint64_t& slot,
                                                                     const char* commitment) {
  std::string body = call("getTokenLargestAccounts", "[\"" + b58(mint) + "\",{\"commitment\":\"" + commitment + "\"}]");
  simdjson::dom::parser p;
  element r = p.parse(body)["result"];
  slot = u64_of(r["context"]["slot"]);
  std::vector<std::pair<Pubkey, uint64_t>> out;
  simdjson::dom::array arr = r["value"];
  for (element v : arr) {
    std::string_view address, amount;
    Pubkey k;
    if (v["address"].get(address) != simdjson::SUCCESS || !b58_decode32(address, k)) continue;
    if (v["amount"].get(amount) != simdjson::SUCCESS) continue;  // raw amount, a decimal string
    out.emplace_back(k, std::strtoull(std::string(amount).c_str(), nullptr, 10));
  }
  std::sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return a.second > b.second; });
  return out;
}

std::pair<uint64_t, uint64_t> Rpc::balance(const Pubkey& key, const char* commitment) {
  std::string body = call("getBalance", "[\"" + b58(key) + "\",{\"commitment\":\"" + commitment + "\"}]");
  simdjson::dom::parser p;
  element r = p.parse(body)["result"];
  return {u64_of(r["value"]), u64_of(r["context"]["slot"])};
}

RpcTransaction Rpc::transaction(const std::string& sig, const char* commitment) {
  std::string body = call("getTransaction", "[\"" + sig + "\",{\"encoding\":\"json\",\"commitment\":\"" + commitment +
                                                "\",\"maxSupportedTransactionVersion\":1}]");
  simdjson::dom::parser p;
  element r = p.parse(body)["result"];
  RpcTransaction t;
  if (r.is_null()) return t;
  t.found = true;
  t.slot = u64_of(r["slot"]);
  element meta = r["meta"];
  element err;
  t.failed = meta["err"].get(err) == simdjson::SUCCESS && !err.is_null();
  t.fee = u64_of(meta["fee"]);
  simdjson::dom::array a;
  if (meta["preBalances"].get(a) == simdjson::SUCCESS) for (element v : a) t.pre_balances.push_back(u64_of(v));
  if (meta["postBalances"].get(a) == simdjson::SUCCESS) for (element v : a) t.post_balances.push_back(u64_of(v));
  token_balances(meta["preTokenBalances"], t.pre_tokens);
  token_balances(meta["postTokenBalances"], t.post_tokens);
  std::string_view s;
  if (r["transaction"]["message"]["accountKeys"].get(a) == simdjson::SUCCESS)
    for (element v : a) if (v.get(s) == simdjson::SUCCESS) t.keys.emplace_back(s);
  if (meta["loadedAddresses"]["writable"].get(a) == simdjson::SUCCESS)
    for (element v : a) if (v.get(s) == simdjson::SUCCESS) t.keys.emplace_back(s);
  if (meta["loadedAddresses"]["readonly"].get(a) == simdjson::SUCCESS)
    for (element v : a) if (v.get(s) == simdjson::SUCCESS) t.keys.emplace_back(s);
  return t;
}

RpcSigStatus Rpc::signature_status(const std::string& sig) {
  std::string body = call("getSignatureStatuses", "[[\"" + sig + "\"],{\"searchTransactionHistory\":false}]");
  simdjson::dom::parser p;
  element v = p.parse(body)["result"]["value"].at(0);
  RpcSigStatus st;
  if (v.is_null()) return st;
  st.found = true;
  st.slot = u64_of(v["slot"]);
  element err;
  st.failed = v["err"].get(err) == simdjson::SUCCESS && !err.is_null();
  std::string_view c;
  if (v["confirmationStatus"].get(c) == simdjson::SUCCESS) st.confirmation = c;
  return st;
}

std::string Rpc::latest_signature_for(const Pubkey& address) {
  std::string body = call("getSignaturesForAddress", "[\"" + b58(address) + "\",{\"limit\":1,\"commitment\":\"confirmed\"}]");
  simdjson::dom::parser p;
  simdjson::dom::array a = p.parse(body)["result"];
  for (element v : a) {
    std::string_view s;
    if (v["signature"].get(s) == simdjson::SUCCESS) return std::string(s);
  }
  return "";
}

Rpc::EpochInfo Rpc::epoch_info(const char* commitment) {
  std::string body = call("getEpochInfo", std::string("[{\"commitment\":\"") + commitment + "\"}]");
  simdjson::dom::parser p;
  element r = p.parse(body)["result"];
  EpochInfo e;
  e.epoch = u64_of(r["epoch"]);
  e.absolute_slot = u64_of(r["absoluteSlot"]);
  e.slot_index = u64_of(r["slotIndex"]);
  e.slots_in_epoch = u64_of(r["slotsInEpoch"]);
  if (!e.slots_in_epoch || e.slot_index > e.absolute_slot) throw std::runtime_error("bad getEpochInfo");
  return e;
}

std::string Rpc::send_transaction(const std::string& tx_b64) {
  std::string body = call("sendTransaction", "[\"" + tx_b64 +
                                                 "\",{\"encoding\":\"base64\",\"skipPreflight\":true,\"maxRetries\":3}]");
  simdjson::dom::parser p;
  std::string_view sig;
  if (p.parse(body)["result"].get(sig) != simdjson::SUCCESS) throw std::runtime_error("sendTransaction: no signature");
  return std::string(sig);
}

uint64_t Rpc::priority_fee_estimate(const std::vector<Pubkey>& accounts, const char* level) {
  try {
    std::string body = call("getPriorityFeeEstimate", "[{\"accountKeys\":" + keys_json(accounts) +
                                                          ",\"options\":{\"priorityLevel\":\"" + level + "\"}}]");
    simdjson::dom::parser p;
    return u64_of(p.parse(body)["result"]["priorityFeeEstimate"]);
  } catch (const std::exception&) {
    return 0;
  }
}

std::string Rpc::simulate(const std::string& tx_b64) {
  std::string body = call("simulateTransaction", "[\"" + tx_b64 +
                                                     "\",{\"encoding\":\"base64\",\"sigVerify\":false,\"replaceRecentBlockhash\":true,\"commitment\":\"processed\"}]");
  simdjson::dom::parser p;
  element v = p.parse(body)["result"]["value"];
  element err;
  if (v["err"].get(err) != simdjson::SUCCESS || err.is_null()) return "";
  std::string out = simdjson::to_string(err);
  simdjson::dom::array logs;
  if (v["logs"].get(logs) == simdjson::SUCCESS) {
    for (element l : logs) {
      std::string_view s;
      if (l.get(s) == simdjson::SUCCESS) out += "\n  " + std::string(s);
    }
  }
  return out;
}

}  // namespace pd
