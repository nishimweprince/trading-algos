#pragma once
// Fixed-capacity instructions and a v0 message compiler that reproduces
// web3.js MessageV0.compile (CompiledKeys ordering) byte for byte, then signs.
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "core/types.hpp"
#include "sol/crypto.hpp"

namespace pd {

constexpr size_t MAX_IX_ACCOUNTS = 32;
constexpr size_t MAX_IX_DATA = 64;
constexpr size_t MAX_IXS = 16;
constexpr size_t MAX_TX_KEYS = 64;
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
  uint8_t data_len = 0;

  Ix& acc(const Pubkey& k, bool signer, bool writable) {
    accounts[n_accounts++] = AccountMeta{k, signer, writable};
    return *this;
  }
  Ix& ro(const Pubkey& k) { return acc(k, false, false); }
  Ix& rw(const Pubkey& k) { return acc(k, false, true); }
  Ix& bytes(const void* p, size_t n) {
    std::memcpy(data + data_len, p, n);
    data_len = static_cast<uint8_t>(data_len + n);
    return *this;
  }
  Ix& u8(uint8_t v) { return bytes(&v, 1); }
  Ix& u32(uint32_t v) { return bytes(&v, 4); }
  Ix& u64(uint64_t v) { return bytes(&v, 8); }
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

/** A signed v0 transaction in wire form. */
struct SignedTx {
  uint8_t bytes[PACKET_DATA_SIZE + 64];
  uint16_t len = 0;
  Signature sig{};
};

/**
 * Compile `ixs` into a v0 message (no lookup tables) at `out`. Returns the
 * message length, or 0 when the key set or size overflows.
 */
size_t compile_message_v0(const Pubkey& payer, const IxList& ixs, const Hash& blockhash, uint8_t* out, size_t cap);

/** Compile and sign (payer is the only signer). False when the tx exceeds PACKET_DATA_SIZE. */
bool build_signed_tx(const Keypair& payer, const IxList& ixs, const Hash& blockhash, SignedTx& out);

// --- instruction builders ----------------------------------------------------
namespace ix {
void cu_limit(IxList& l, uint32_t units);
void cu_price(IxList& l, uint64_t micro_lamports);
void transfer(IxList& l, const Pubkey& from, const Pubkey& to, uint64_t lamports);
void ata_idempotent(IxList& l, const Pubkey& payer, const Pubkey& ata, const Pubkey& owner, const Pubkey& mint,
                    const Pubkey& token_program);
void sync_native(IxList& l, const Pubkey& account);
void close_account(IxList& l, const Pubkey& account, const Pubkey& dest, const Pubkey& owner, const Pubkey& token_program);
}  // namespace ix

}  // namespace pd
