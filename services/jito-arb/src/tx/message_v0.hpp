#pragma once
// Fixed-capacity instructions and a v0 message compiler that reproduces web3.js
// 1.99 MessageV0.compile byte for byte, including address lookup tables
// (CompiledKeys.compile -> extractTableLookup per table -> getMessageComponents ->
// MessageAccountKeys.compileInstructions). Adapted from pump-desk's ALT-less compiler.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <span>
#include <vector>

#include "core/crypto.hpp"
#include "core/error.hpp"
#include "core/types.hpp"

namespace arb::tx {

constexpr size_t MAX_IX_ACCOUNTS = 64;
constexpr size_t MAX_IX_DATA = 1024;  // ALT extends carry up to ~30 addresses
constexpr size_t MAX_IXS = 16;
constexpr size_t MAX_TX_KEYS = 256;        // static + looked-up (u8 indexes)
constexpr size_t PACKET_DATA_SIZE = 1232;  // Solana's wire limit for a signed transaction

struct AccountMeta {
  Pubkey key;
  bool signer;
  bool writable;
};

struct Ix {
  Pubkey program{};
  AccountMeta accounts[MAX_IX_ACCOUNTS];
  uint8_t n_accounts = 0;
  uint8_t data[MAX_IX_DATA];
  uint16_t data_len = 0;

  Ix& acc(const Pubkey& k, bool signer, bool writable) {
    accounts[n_accounts++] = AccountMeta{k, signer, writable};
    return *this;
  }
  Ix& ro(const Pubkey& k) { return acc(k, false, false); }
  Ix& rw(const Pubkey& k) { return acc(k, false, true); }
  Ix& bytes(const void* p, size_t n) {
    std::memcpy(data + data_len, p, n);
    data_len = static_cast<uint16_t>(data_len + n);
    return *this;
  }
  Ix& u8(uint8_t v) { return bytes(&v, 1); }
  Ix& u16(uint16_t v) { return bytes(&v, 2); }
  Ix& u32(uint32_t v) { return bytes(&v, 4); }
  Ix& u64(uint64_t v) { return bytes(&v, 8); }
  Ix& u128v(unsigned __int128 v) { return bytes(&v, 16); }
};

struct IxList {
  Ix ixs[MAX_IXS];
  uint8_t n = 0;
  Ix& add(const Pubkey& program) {
    Ix& ix = ixs[n++];
    ix.program = program;
    ix.n_accounts = 0;
    ix.data_len = 0;
    return ix;
  }
  void clear() { n = 0; }
};

/** An address lookup table as the compiler sees it (AddressLookupTableAccount.state.addresses). */
struct LookupTable {
  Pubkey key{};
  std::vector<Pubkey> addresses;
};

struct CompileStats {
  size_t static_keys = 0, lookup_writable = 0, lookup_readonly = 0, tables_used = 0;
};

/**
 * Compile `ixs` into a v0 message at `out`. Tables are tried in order; signers, invoked programs and
 * keys in `force_static` (the Jito tip account) never move into a lookup. Errors: too_many_keys,
 * tx_too_large (message alone exceeds `cap`).
 */
Result<size_t> compile_message_v0(const Pubkey& payer, const IxList& ixs, const Hash& blockhash,
                                  std::span<const LookupTable> tables, uint8_t* out, size_t cap,
                                  std::span<const Pubkey> force_static = {}, CompileStats* stats = nullptr);

/** A signed v0 transaction in wire form. */
struct SignedTx {
  uint8_t bytes[PACKET_DATA_SIZE + 64];
  uint16_t len = 0;
  Signature sig{};
};

/**
 * Compile, check the 1,232-byte limit (tx_too_large) and sign with the payer (the only signer).
 * With `payer_kp == nullptr` the signature is left zero (unsigned, for simulateTransaction sigVerify:false).
 */
Result<size_t> build_tx(const Pubkey& payer, const Keypair* payer_kp, const IxList& ixs, const Hash& blockhash,
                        std::span<const LookupTable> tables, SignedTx& out, std::span<const Pubkey> force_static = {});

}  // namespace arb::tx
