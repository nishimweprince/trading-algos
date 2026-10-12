#pragma once
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <string>
#include <string_view>

#include "core/types.hpp"

namespace pd {

/** Call once before any crypto (sodium_init). Throws on failure. */
void crypto_init();

/**
 * Solana's "on curve" test: the 32 bytes decompress to an ed25519 point
 * (curve25519-dalek CompressedEdwardsY::decompress). Not a subgroup check —
 * libsodium's is_valid_point rejects 7/8 of on-curve points and would make
 * PDA derivation wrong, so this is the TweetNaCl decompression.
 */
bool is_on_curve(const uint8_t p[32]);

struct Seed {
  const uint8_t* ptr;
  size_t len;
  Seed(const Pubkey& k) : ptr(k.data()), len(32) {}  // NOLINT(implicit)
  Seed(std::string_view s) : ptr(reinterpret_cast<const uint8_t*>(s.data())), len(s.size()) {}  // NOLINT
  Seed(const uint8_t* p, size_t n) : ptr(p), len(n) {}
};

/** findProgramAddressSync. Returns false only if no bump in 255..0 is off-curve. */
bool find_program_address(std::initializer_list<Seed> seeds, const Pubkey& program, Pubkey& out, uint8_t* bump = nullptr);

/** Throwing convenience for boot-time constants. */
Pubkey pda(std::initializer_list<Seed> seeds, const Pubkey& program);

/** Associated token account (allowOwnerOffCurve semantics: no owner check). */
Pubkey ata(const Pubkey& owner, const Pubkey& mint, const Pubkey& token_program);

class Keypair {
 public:
  /** Base58 64-byte secret (Phantom/solana-keygen export) or a JSON byte array. */
  static Keypair parse(std::string_view secret);
  static Keypair from_seed(const uint8_t seed[32]);
  const Pubkey& pubkey() const { return pub_; }
  void sign(const uint8_t* msg, size_t len, uint8_t sig_out[64]) const;

 private:
  uint8_t sk_[64]{};
  Pubkey pub_{};
};

}  // namespace pd
