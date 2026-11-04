#pragma once
// Live and recorder modes: LaserStream shards -> frame rings -> one feed thread. Recorder writes every
// frame to rotated files and does not trade (spec §11). Live runs the Runtime and the BundleBuilder; in
// this build it is dry run only (dry_run = false is refused until the owner has reviewed the phase-2 gate
// and the phase-3 validator tests). Built with the real gRPC client on the VM (ARB_WITH_GRPC) and with a
// stub that yields no frames elsewhere.
#include <atomic>

#include "core/config.hpp"

namespace arb::app {

/** Blocks until `stop` (or `run_seconds` > 0 elapse). Returns a process exit code. */
int run_live(const Config& cfg, bool recorder, std::atomic<bool>& stop, int run_seconds);

}  // namespace arb::app
