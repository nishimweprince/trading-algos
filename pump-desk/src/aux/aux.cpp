#include "aux/aux.hpp"

#include <simdjson.h>
#include <sys/stat.h>

#include <chrono>
#include <fstream>
#include <sstream>
#include <thread>
#include <vector>

#include "aux/rpc.hpp"
#include "aux/writer.hpp"
#include "core/base58.hpp"
#include "core/sys.hpp"
#include "sol/constants.hpp"
#include "sol/layouts.hpp"

namespace pd {

namespace {

void sleep_ms(int ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }

bool file_exists(const std::string& path) {
  struct stat st;
  return !path.empty() && ::stat(path.c_str(), &st) == 0;
}

int64_t mtime_of(const std::string& path) {
  struct stat st;
  if (path.empty() || ::stat(path.c_str(), &st) != 0) return -1;
  return int64_t(st.st_mtime);
}

}  // namespace

RivalsFile parse_rivals(std::string_view json, const Pubkey& self, size_t max_rivals) {
  simdjson::dom::parser p;
  simdjson::dom::element root = p.parse(json.data(), json.size());
  RivalsFile out;
  simdjson::dom::array guard;
  if (root["guard"].get(guard) == simdjson::SUCCESS) {
    for (simdjson::dom::element v : guard) {
      std::string_view s;
      Pubkey k{};
      if (out.set.n >= std::min(max_rivals, MAX_RIVALS) || v.get(s) != simdjson::SUCCESS || !b58_decode32(s, k) || k == self) continue;
      out.set.wallets[out.set.n++] = k;
    }
  }
  for (size_t c = 0; c < N_LEADER_CLASSES; ++c) {
    uint64_t v = 0;
    if (root["beat_cu_price"][leader_class_name(LeaderClass(c))].get(v) == simdjson::SUCCESS) out.beat_cu_price[c] = v;
  }
  return out;
}

RoutePolicy make_route_policy(const Config& cfg, uint64_t estimate, const uint64_t beat_cu_price[N_LEADER_CLASSES]) {
  RoutePolicy rp;
  const Config::RouteClass* rcs[N_LEADER_CLASSES] = {&cfg.routing.agave, &cfg.routing.bam, &cfg.routing.firedancer};
  uint64_t base = estimate ? estimate : cfg.fees.cu_price_default;
  for (size_t c = 0; c < N_LEADER_CLASSES; ++c) {
    const Config::RouteClass& rc = *rcs[c];
    RouteProfile& p = rp.by_class[c];
    uint64_t est = uint64_t(double(base) * cfg.entry.priority_multiplier * rc.cu_price_multiplier);
    if (rc.cu_price_mode == "floor") p.cu_price = cfg.fees.cu_price_floor;
    else if (rc.cu_price_mode == "beat_rivals") p.cu_price = std::max(est, std::min(beat_cu_price[c], cfg.fees.cu_price_cap));
    else p.cu_price = est;
    p.cu_limit = rc.cu_limit;
    p.sender_tip = rc.sender_tip_lamports ? rc.sender_tip_lamports : cfg.sender.tip_lamports;
    p.jito = rc.jito && cfg.jito.enabled;
  }
  return rp;
}

void refresh_amm_config(Rpc& rpc, Shared& sh) {
  const Derived& d = derived();
  auto accts = rpc.multiple_accounts({d.global_config, d.fee_config});
  if (accts.size() != 2 || !accts[0]) throw std::runtime_error("PumpSwap global config not found");
  AmmConfig cfg;
  if (!decode_global_config(accts[0]->data.data(), accts[0]->data.size(), cfg))
    throw std::runtime_error("PumpSwap global config did not decode");
  if (accts[1] && !decode_fee_config(accts[1]->data.data(), accts[1]->data.size(), cfg))
    throw std::runtime_error("PumpSwap fee config did not decode");
  cfg.valid = true;
  sh.amm.store(cfg);
}

void run_aux_fast(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log) {
  name_thread("pd-aux-fast");
  pin_thread(cfg.threads.aux_cpu);
  Rpc rpc(cfg.rpc.http_url, cfg.rpc.timeout_ms);
  uint64_t next_bh = 0, next_amm = 0, next_bal = 0, next_fee = 0, next_kill = 0, next_rivals = 0;
  int bh_failures = 0;
  int64_t rivals_mtime = -2;
  RivalsFile rivals;
  bool routes_dirty = true;
  while (!sh.stop.load(std::memory_order_relaxed)) {
    uint64_t now = now_ns();
    try {
      if (now >= next_kill) {
        next_kill = now + 200'000'000ULL;
        bool k = file_exists(cfg.risk.kill_file);
        if (k != sh.kill_file.load()) log.info(k ? "KILL file present: entries stopped" : "KILL file removed: entries resume");
        sh.kill_file.store(k);
      }
      if (now >= next_bh) {
        next_bh = now + uint64_t(cfg.rpc.blockhash_refresh_ms) * 1'000'000ULL;
        try {
          Rpc::Blockhash b = rpc.latest_blockhash("confirmed");
          BlockhashSnap s;
          s.hash = b.hash;
          s.last_valid_height = b.last_valid_height;
          s.fetched_ns = now_ns();
          sh.blockhash.store(s);
          bh_failures = 0;
        } catch (const std::exception& e) {
          if (++bh_failures % 5 == 1) log.warn(std::string("blockhash refresh failed: ") + e.what());
        }
      }
      if (now >= next_amm) {
        next_amm = now + uint64_t(cfg.rpc.amm_config_refresh_ms) * 1'000'000ULL;
        refresh_amm_config(rpc, sh);
      }
      if (now >= next_bal) {
        next_bal = now + uint64_t(cfg.rpc.balance_refresh_ms) * 1'000'000ULL;
        auto [lamports, slot] = rpc.balance(wallet);
        sh.balances.push(BalanceMsg{lamports, slot});
      }
      if (now >= next_fee) {
        next_fee = now + uint64_t(cfg.fees.refresh_ms) * 1'000'000ULL;
        uint64_t est = rpc.priority_fee_estimate({prog::PUMP_SWAP, prog::WSOL}, cfg.fees.priority_level.c_str());
        if (est) {
          est = std::max(cfg.fees.cu_price_floor, std::min(cfg.fees.cu_price_cap, est));
          sh.cu_price.store(est);
        }
        routes_dirty = true;
      }
      if (now >= next_rivals) {
        next_rivals = now + 1'000'000'000ULL;
        int64_t m = mtime_of(cfg.routing.rivals_path);
        if (m != rivals_mtime) {
          rivals_mtime = m;
          if (m < 0) {
            rivals = RivalsFile{};
          } else {
            std::ifstream f(cfg.routing.rivals_path);
            std::stringstream ss;
            ss << f.rdbuf();
            rivals = parse_rivals(ss.str(), wallet, size_t(cfg.lighthouse.max_rivals));
          }
          sh.rivals.store(rivals.set);
          log.info("rivals: " + std::to_string(rivals.set.n) + " guarded, beat cu_price agave " +
                   std::to_string(rivals.beat_cu_price[0]) + " / bam " + std::to_string(rivals.beat_cu_price[1]) +
                   " / firedancer " + std::to_string(rivals.beat_cu_price[2]));
          routes_dirty = true;
        }
      }
      if (routes_dirty && cfg.routing.enabled) {
        routes_dirty = false;
        sh.routes.store(make_route_policy(cfg, sh.cu_price.load(), rivals.beat_cu_price));
      }
    } catch (const std::exception& e) {
      log.warn(std::string("aux-fast: ") + e.what());
      sleep_ms(200);
    }
    sleep_ms(20);
  }
}

namespace {

struct Deferred {
  AuxReq req;
  uint64_t due_ms = 0;
  int tries = 0;
};

/** curve -> mint from the tx that wrote the curve: the mint the curve owns a token balance of. */
bool mint_from_tx(const RpcTransaction& t, const Pubkey& curve, Pubkey& mint) {
  const std::string curve_b58 = b58(curve);
  for (auto* list : {&t.post_tokens, &t.pre_tokens})
    for (auto& b : *list)
      if (b.owner == curve_b58 && b58_decode32(b.mint, mint)) return true;
  // Creator-fee sweeps write the curve without moving tokens: find the key whose
  // bonding-curve PDA is this curve.
  for (auto& k : t.keys) {
    Pubkey cand, derived_curve;
    if (!b58_decode32(k, cand)) continue;
    if (find_program_address({std::string_view("bonding-curve"), cand}, prog::PUMP_FUN, derived_curve) && derived_curve == curve) {
      mint = cand;
      return true;
    }
  }
  return false;
}

bool read_mint(Rpc& rpc, const Pubkey& mint, AuxResp& out) {
  auto a = rpc.account(mint);
  if (!a) return false;
  bool t22 = a->owner == prog::TOKEN_2022;
  if (!t22 && a->owner != prog::TOKEN) return false;
  out.token_program = a->owner;
  return decode_mint(a->data.data(), a->data.size(), t22, out.mint_info);
}

}  // namespace

void run_aux_req(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log) {
  name_thread("pd-aux-req");
  pin_thread(cfg.threads.aux_cpu);
  Rpc rpc(cfg.rpc.http_url, cfg.rpc.timeout_ms);
  std::vector<Deferred> queue;
  const std::string wallet_b58 = b58(wallet);
  static constexpr int CURVE_RETRY_MS[] = {0, 300, 800, 1500, 3000, 6000};

  auto respond = [&](const AuxResp& r) {
    while (!sh.aux_resps.push(r) && !sh.stop.load()) sleep_ms(1);
  };

  while (!sh.stop.load(std::memory_order_relaxed)) {
    AuxReq in;
    while (sh.aux_reqs.try_pop(in)) queue.push_back(Deferred{in, 0, 0});
    int64_t now = wall_ms();
    bool worked = false;
    for (size_t i = 0; i < queue.size();) {
      Deferred& d = queue[i];
      if (d.due_ms > uint64_t(now)) { ++i; continue; }
      worked = true;
      bool done = true;
      try {
        AuxResp r;
        r.op = d.req.op;
        r.curve = d.req.curve;
        r.mint = d.req.mint;
        r.sig = d.req.sig;
        r.side = d.req.side;
        r.pos = d.req.pos;
        if (d.req.op == AuxOp::ResolveCurve) {
          std::string sig = is_zero(key_from(d.req.sig.data())) && is_zero(key_from(d.req.sig.data() + 32))
                                ? rpc.latest_signature_for(d.req.curve)
                                : b58(d.req.sig);
          RpcTransaction t = sig.empty() ? RpcTransaction{} : rpc.transaction(sig);
          Pubkey mint{};
          if (t.found && mint_from_tx(t, d.req.curve, mint)) {
            r.mint = mint;
            r.ok = read_mint(rpc, mint, r);
            respond(r);
          } else if (d.tries + 1 < int(sizeof CURVE_RETRY_MS / sizeof CURVE_RETRY_MS[0])) {
            done = false;  // processed tx not yet visible at confirmed: retry
          } else {
            r.ok = false;
            respond(r);
          }
        } else if (d.req.op == AuxOp::ReadMint) {
          r.ok = read_mint(rpc, d.req.mint, r);
          respond(r);
        } else {  // ResolveSig
          std::string sig = b58(d.req.sig);
          RpcSigStatus st = rpc.signature_status(sig);
          if (st.found && st.failed) {
            r.failed = true;
            r.slot = st.slot;
            respond(r);
          } else if (st.found && (st.confirmation == "confirmed" || st.confirmation == "finalized")) {
            RpcTransaction t = rpc.transaction(sig);
            if (!t.found) {
              done = false;
            } else {
              r.landed = !t.failed;
              r.failed = t.failed;
              r.slot = t.slot;
              int wi = -1;
              for (size_t k = 0; k < t.keys.size(); ++k)
                if (t.keys[k] == wallet_b58) { wi = int(k); break; }
              if (wi >= 0 && size_t(wi) < t.post_balances.size() && size_t(wi) < t.pre_balances.size()) {
                r.sol_delta = int64_t(t.post_balances[size_t(wi)]) - int64_t(t.pre_balances[size_t(wi)]);
                r.wallet_post = t.post_balances[size_t(wi)];
              }
              std::string mint_b58 = b58(d.req.mint);
              int64_t pre = 0, post = 0;
              for (auto& b : t.pre_tokens) if (b.owner == wallet_b58 && b.mint == mint_b58) pre += int64_t(b.amount);
              for (auto& b : t.post_tokens) if (b.owner == wallet_b58 && b.mint == mint_b58) post += int64_t(b.amount);
              r.base_delta = post - pre;
              r.has_fill = true;
              respond(r);
            }
          } else if (uint64_t(now) > d.req.deadline_ms) {
            r.expired = true;
            respond(r);
          } else {
            done = false;
          }
        }
      } catch (const std::exception& e) {
        log.warn(std::string("aux-req: ") + e.what());
        done = d.tries >= 8;
      }
      if (done) {
        queue[i] = queue.back();
        queue.pop_back();
      } else {
        ++d.tries;
        int delay = d.req.op == AuxOp::ResolveCurve
                        ? CURVE_RETRY_MS[std::min<size_t>(size_t(d.tries), sizeof CURVE_RETRY_MS / sizeof CURVE_RETRY_MS[0] - 1)]
                        : 1000;
        d.due_ms = uint64_t(now) + uint64_t(delay);
        ++i;
      }
    }
    if (!worked) sleep_ms(2);
  }
}

}  // namespace pd
