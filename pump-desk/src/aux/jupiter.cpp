#include "aux/jupiter.hpp"

#include <simdjson.h>

#include <stdexcept>

#include "core/base58.hpp"
#include "sol/constants.hpp"

namespace pd {

namespace {

/** Solana shortvec (compact-u16). Returns bytes read, 0 on malformed input. */
size_t read_shortvec(const uint8_t* p, size_t n, size_t& v) {
  v = 0;
  for (size_t i = 0; i < 3 && i < n; ++i) {
    v |= size_t(p[i] & 0x7f) << (7 * i);
    if (!(p[i] & 0x80)) return i + 1;
  }
  return 0;
}

}  // namespace

Jupiter::Jupiter(const Config& cfg) : cfg_(cfg), http_(cfg.jupiter.url, cfg.rpc.timeout_ms) {
  base_path_ = parse_url(cfg.jupiter.url).path;
  while (!base_path_.empty() && base_path_.back() == '/') base_path_.pop_back();
  if (!cfg.jupiter.api_key.empty()) headers_ = "x-api-key: " + cfg.jupiter.api_key + "\r\n";
}

std::string Jupiter::get(const std::string& path) {
  int status = 0;
  std::string body = http_.http("GET", base_path_ + path, {}, headers_, status, "jupiter");
  if (status != 200) throw std::runtime_error("jupiter http " + std::to_string(status) + ": " + body.substr(0, 200));
  return body;
}

std::string Jupiter::post(const std::string& path, const std::string& body) {
  int status = 0;
  std::string out = http_.http("POST", base_path_ + path, body, headers_, status, "jupiter");
  if (status != 200) throw std::runtime_error("jupiter http " + std::to_string(status) + ": " + out.substr(0, 200));
  return out;
}

JupiterSwap Jupiter::sell_to_sol(const Pubkey& mint, uint64_t amount, const Pubkey& user) {
  const Config::Jupiter& j = cfg_.jupiter;
  std::string quote = get("/quote?inputMint=" + b58(mint) + "&outputMint=" + b58(prog::WSOL) + "&amount=" +
                          std::to_string(amount) + "&slippageBps=" + std::to_string(j.slippage_bps) +
                          "&restrictIntermediateTokens=true");
  JupiterSwap out;
  simdjson::dom::parser p;
  simdjson::dom::element q = p.parse(quote);
  std::string_view s;
  if (q["outAmount"].get(s) == simdjson::SUCCESS) out.out_lamports = std::stoull(std::string(s));
  if (q["otherAmountThreshold"].get(s) == simdjson::SUCCESS) out.min_out_lamports = std::stoull(std::string(s));
  simdjson::dom::array plan;
  if (q["routePlan"].get(plan) == simdjson::SUCCESS)
    for (simdjson::dom::element leg : plan)
      if (leg["swapInfo"]["label"].get(s) == simdjson::SUCCESS) out.route += (out.route.empty() ? "" : ">") + std::string(s);
  if (out.route.empty()) throw std::runtime_error("jupiter: no route for " + b58(mint) + ": " + quote.substr(0, 200));

  // wrapAndUnwrapSol: proceeds land as SOL, so the wallet stream's lamport delta is the fill.
  std::string body = "{\"quoteResponse\":" + quote + ",\"userPublicKey\":\"" + b58(user) +
                     "\",\"wrapAndUnwrapSol\":true,\"dynamicComputeUnitLimit\":true,\"prioritizationFeeLamports\":"
                     "{\"priorityLevelWithMaxLamports\":{\"maxLamports\":" +
                     std::to_string(j.max_priority_lamports) + ",\"priorityLevel\":\"veryHigh\"}}}";
  std::string swap = post("/swap", body);
  simdjson::dom::parser p2;
  simdjson::dom::element r = p2.parse(swap);
  if (r["swapTransaction"].get(s) != simdjson::SUCCESS) throw std::runtime_error("jupiter swap: " + swap.substr(0, 200));
  out.tx_b64 = std::string(s);
  simdjson::dom::element sim;
  if (r["simulationError"].get(sim) == simdjson::SUCCESS && !sim.is_null()) out.sim_error = simdjson::to_string(sim);
  return out;
}

bool sign_single_signer_tx(std::vector<uint8_t>& tx, const Keypair& kp, std::string* err) {
  auto fail = [&](const char* why) {
    if (err) *err = why;
    return false;
  };
  size_t n_sigs = 0;
  size_t o = read_shortvec(tx.data(), tx.size(), n_sigs);
  if (!o || n_sigs != 1) return fail("not a one-signer transaction");
  size_t msg = o + 64;
  if (tx.size() < msg + 4) return fail("truncated transaction");
  // v0: 0x80 prefix, then the 3-byte header; legacy: the header directly.
  size_t h = msg + ((tx[msg] & 0x80) ? 1 : 0);
  if (tx[h] != 1) return fail("more than one required signature");
  size_t n_keys = 0;
  size_t k = read_shortvec(tx.data() + h + 3, tx.size() - h - 3, n_keys);
  size_t first_key = h + 3 + k;
  if (!k || n_keys == 0 || tx.size() < first_key + 32) return fail("no account keys");
  if (!key_eq(tx.data() + first_key, kp.pubkey())) return fail("fee payer is not our wallet");
  kp.sign(tx.data() + msg, tx.size() - msg, tx.data() + o);
  return true;
}

}  // namespace pd
