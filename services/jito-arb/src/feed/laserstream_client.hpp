#pragma once
// One LaserStream (Yellowstone geyser.Geyser/Subscribe) bidi stream per shard over
// grpc++'s generic API, adapted from pump-desk's ingest. Frames are copied raw into
// a FrameRing; the single feed thread parses and applies them. Built only with
// ARB_WITH_GRPC (grpc++ is on the VM, not the laptop).
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

#include "core/config.hpp"
#include "core/ring.hpp"
#include "feed/subscription.hpp"

namespace arb::feed {

struct StreamCounters {
  std::atomic<uint64_t> frames{0}, bytes{0}, oversize{0}, reconnects{0};
  std::atomic<bool> up{false};
  std::atomic<uint64_t> last_slot{0};  // newest slot seen, for from_slot on reconnect
};

class LaserstreamClient {
 public:
  LaserstreamClient(const Config& cfg, std::string name, FrameRing& ring, std::atomic<bool>& stop);
  ~LaserstreamClient();
  /** Replace the shard's subscription (applied on the live stream, or at the next connect). */
  void set_spec(const ShardSpec& spec);
  /** Blocking: subscribe, read, reconnect with backoff; returns when `stop` is set. */
  void run();
  StreamCounters& counters();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace arb::feed
