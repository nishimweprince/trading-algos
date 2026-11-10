#pragma once
// Pieces the live send path needs beyond the bundle builder (docs/LIVE-READINESS.md, Stage 1).
//
//   ArmedSet        the pools the bot may trade live. Every pool is observed; a pool is armed once its lookup-table
//                   entries and the wallet's token accounts for its mints exist. Dry run counts a candidate that
//                   touches an unarmed pool and carries on; live skips it (Reason::not_armed).
//   LandingWatcher  polls Jito for each sent bundle on its own thread (the status call is a blocking HTTP round
//                   trip, so it never runs on the engine thread) and hands back landed / not landed / rejected.
//
// Nothing here sends a bundle. LandingWatcher is only constructed when dry_run = false, which this build refuses.
#include <atomic>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "core/types.hpp"
#include "submit/jito.hpp"

namespace arb::app {

class ArmedSet {
 public:
  /** {"pools":["<base58>", ...]}. False (and nothing loaded) on a malformed file or address. */
  bool load_json(const std::string& body, std::string* err = nullptr);
  bool contains(const Pubkey& pool) const { return pools_.count(pool) != 0; }
  size_t size() const { return pools_.size(); }
  void insert(const Pubkey& pool) { pools_.insert(pool); }

 private:
  std::set<Pubkey> pools_;
};
std::string armed_to_json(const std::vector<Pubkey>& pools);

class LandingWatcher {
 public:
  struct Done {
    std::string bundle_id;
    uint16_t route_class = 0;
    submit::BundleTracker::Result result = submit::BundleTracker::Result::NotLanded;
    uint64_t landed_slot = 0;
  };
  LandingWatcher(std::unique_ptr<submit::JitoClient> client, int poll_ms, int timeout_ms);
  ~LandingWatcher();
  LandingWatcher(const LandingWatcher&) = delete;
  LandingWatcher& operator=(const LandingWatcher&) = delete;
  void start();
  void shutdown();
  /** A bundle was sent (engine thread; never blocks on the network). */
  void track(const std::string& bundle_id, const Signature& sig, uint16_t route_class, int64_t now_ms);
  /** The wallet stream saw this signature land (engine thread). */
  void signature_seen(const Signature& sig, uint64_t slot);
  /** Finished attempts, oldest first (engine thread). */
  std::vector<Done> drain();
  /** One poll round: adopt new tracks, ask Jito about the due ones, expire the old. The worker's body; tests call it. */
  void poll_once(int64_t now_ms);
  size_t pending();

 private:
  struct Track {
    std::string bundle_id;
    Signature sig{};
    uint16_t route_class = 0;
    int64_t now_ms = 0;
  };
  std::unique_ptr<submit::JitoClient> client_;
  submit::BundleTracker tracker_;  // worker thread only (and poll_once callers)
  std::mutex mu_;
  std::vector<Track> incoming_;
  std::vector<std::pair<Signature, uint64_t>> seen_;
  std::vector<Done> done_;
  std::atomic<bool> quit_{false};
  std::atomic<size_t> pending_{0};
  std::thread thread_;
};

}  // namespace arb::app
