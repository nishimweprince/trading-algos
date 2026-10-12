#pragma once
// The ingest thread: one bidi geyser.Geyser/Subscribe stream to Helius
// LaserStream (Yellowstone-compatible) over grpc++'s generic API. Each
// SubscribeUpdate arrives as raw bytes and is copied straight into the frame
// ring without parsing. Also owns the request side: the subscription (rewritten
// when logic adds/removes watches) and keepalive pings.
#include <memory>
#include <string>
#include <vector>

#include "core/config.hpp"
#include "core/ring.hpp"
#include "logic/shared.hpp"

namespace pd {

class Logger;

class Ingest {
 public:
  Ingest(const Config& cfg, Shared& sh, FrameRing& ring, const Pubkey& wallet, Logger& log);
  ~Ingest();
  /** Pinned loop: subscribe, read, reconnect with backoff; returns when sh.stop is set. */
  void run();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace pd
