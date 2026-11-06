#pragma once
// Program IDs and well-known accounts. Each DEX/lender ID was confirmed on
// mainnet by getAccountInfo (executable, owned by a BPF loader); see
// config/dex_programs.toml for the pinned source commits.
#include "core/base58.hpp"
#include "core/types.hpp"

namespace arb::prog {

inline constexpr Pubkey SYSTEM = PK("11111111111111111111111111111111");
inline constexpr Pubkey TOKEN = PK("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
inline constexpr Pubkey TOKEN_2022 = PK("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
inline constexpr Pubkey ATA = PK("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
inline constexpr Pubkey COMPUTE_BUDGET = PK("ComputeBudget111111111111111111111111111111");
inline constexpr Pubkey ALT = PK("AddressLookupTab1e1111111111111111111111111");
inline constexpr Pubkey SYSVAR_INSTRUCTIONS = PK("Sysvar1nstructions1111111111111111111111111");

inline constexpr Pubkey RAYDIUM_V4 = PK("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8");
inline constexpr Pubkey WHIRLPOOL = PK("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc");
inline constexpr Pubkey RAYDIUM_CPMM = PK("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C");
inline constexpr Pubkey PUMPSWAP = PK("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
inline constexpr Pubkey PUMP = PK("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");      // bonding curve (pool-authority PDAs)
inline constexpr Pubkey PUMP_FEES = PK("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ");
inline constexpr Pubkey KLEND = PK("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD");

}  // namespace arb::prog

namespace arb::mint {
inline constexpr Pubkey WSOL = PK("So11111111111111111111111111111111111111112");
inline constexpr Pubkey USDC = PK("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
inline constexpr Pubkey USDT = PK("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB");
}  // namespace arb::mint
