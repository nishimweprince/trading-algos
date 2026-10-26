#pragma once

#include <atomic>

#include "pkm/clob.hpp"
#include "pkm/config.hpp"
#include "pkm/venue.hpp"

// The live process (`pkm run`).
//
//   hot thread (pinned, busy-polls)   every trading socket: Kalshi, one
//       Polymarket market socket per coin, the user channel, the order
//       connections; the engine runs on each frame and its posts and cancels
//       go out from the same frame.
//   signer thread (pinned)            orders signed ahead (presign).
//   connector thread                  TLS/WebSocket handshakes, handed over.
//   control thread                    discovery, fill backstop, wallet
//       reconciliation, settlement, kill/ACK files, the dead man, state file.
//   redeemer thread                   relayer redemptions (each up to 120 s).
namespace pkm::runtime {

struct Startup {
  const Config* config = nullptr;
  const venue::Wallet* wallet = nullptr;
  clob::Credentials credentials;
};

// Runs until SIGINT/SIGTERM. Returns the process exit code.
int run(const Startup& startup, std::atomic<bool>& stop);

}  // namespace pkm::runtime
