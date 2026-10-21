#pragma once
// Program IDs and fixed accounts. Sources: pump-fun/src/core/constants.ts and
// @pump-fun/pump-swap-sdk 1.20.0 (PDAs in dist/index.js).
#include "core/base58.hpp"
#include "core/types.hpp"

namespace pd::prog {

inline constexpr Pubkey PUMP_FUN = PK("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
inline constexpr Pubkey PUMP_SWAP = PK("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
inline constexpr Pubkey PUMP_FEE = PK("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ");
inline constexpr Pubkey TOKEN = PK("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
inline constexpr Pubkey TOKEN_2022 = PK("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
inline constexpr Pubkey SYSTEM = PK("11111111111111111111111111111111");
inline constexpr Pubkey ATA = PK("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
inline constexpr Pubkey COMPUTE_BUDGET = PK("ComputeBudget111111111111111111111111111111");
/** lighthouse-sdk-legacy 2.0.1 LIGHTHOUSE_PROGRAM_ID. */
inline constexpr Pubkey LIGHTHOUSE = PK("L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95");

inline constexpr Pubkey WSOL = PK("So11111111111111111111111111111111111111112");
/** Every Migrate/MigrateV2 references it: `accountRequired` narrows pump.fun txs to migrations. */
inline constexpr Pubkey MIGRATION_AUTHORITY = PK("39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg");
/** Every Create/CreateV2 references it. */
inline constexpr Pubkey MINT_AUTHORITY = PK("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM");

}  // namespace pd::prog

namespace pd {

/** PDAs that never change, derived once at boot (init_derived). */
struct Derived {
  Pubkey global_config;        // PumpSwap ["global_config"]
  Pubkey event_authority;      // PumpSwap ["__event_authority"]
  Pubkey global_volume_acc;    // PumpSwap ["global_volume_accumulator"]
  Pubkey fee_config;           // PumpFee ["fee_config", PUMP_SWAP]
};

const Derived& derived();
void init_derived();

/** Anchor discriminators (sha256("global:<ix>") / sha256("account:<Name>"))[0..8]. */
namespace disc {
inline constexpr uint8_t BUY[8] = {102, 6, 61, 18, 1, 218, 235, 234};
inline constexpr uint8_t SELL[8] = {51, 230, 133, 164, 1, 127, 131, 173};
inline constexpr uint8_t EXTEND_ACCOUNT[8] = {234, 102, 194, 203, 150, 72, 62, 229};
inline constexpr uint8_t POOL[8] = {241, 154, 109, 4, 17, 177, 109, 188};
inline constexpr uint8_t GLOBAL_CONFIG[8] = {149, 8, 156, 202, 160, 252, 176, 217};
inline constexpr uint8_t FEE_CONFIG[8] = {143, 52, 146, 187, 219, 123, 76, 155};
inline constexpr uint8_t BONDING_CURVE[8] = {23, 183, 248, 55, 96, 216, 172, 96};
}  // namespace disc

}  // namespace pd
