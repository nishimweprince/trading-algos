#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

// Hashing, encodings and keys: keccak-256 (our own, OpenSSL 3.0 has none),
// secp256k1 signing (libsecp256k1), HMAC-SHA256 and base64 (OpenSSL), and the
// Kalshi request signer (RSA-PSS or Ed25519, OpenSSL).
namespace pkm {

using Bytes32 = std::array<std::uint8_t, 32>;
using Address = std::array<std::uint8_t, 20>;

Bytes32 keccak256(const void* data, std::size_t size) noexcept;
inline Bytes32 keccak256(std::string_view text) noexcept { return keccak256(text.data(), text.size()); }

// Lower-case hex, "0x" first when `prefix`.
std::string to_hex(const std::uint8_t* data, std::size_t size, bool prefix = true);
template <std::size_t N>
std::string to_hex(const std::array<std::uint8_t, N>& bytes, bool prefix = true) {
  return to_hex(bytes.data(), N, prefix);
}
void append_hex(std::string& out, const std::uint8_t* data, std::size_t size);

// Exactly `size` bytes of hex, with or without "0x".
bool parse_hex(std::string_view text, std::uint8_t* out, std::size_t size) noexcept;
bool parse_address(std::string_view text, Address& out) noexcept;
// EIP-55 mixed-case checksum, with "0x".
std::string checksum_address(const Address& address);

// A non-negative decimal integer below 2^256, big-endian.
bool decimal_to_u256(std::string_view text, Bytes32& out) noexcept;
Bytes32 u256(std::uint64_t value) noexcept;
std::string u256_to_decimal(const Bytes32& value);

std::string base64_encode(const std::uint8_t* data, std::size_t size, bool url_safe);
bool base64_decode(std::string_view text, std::vector<std::uint8_t>& out, bool url_safe);

using Hmac32 = std::array<std::uint8_t, 32>;
Hmac32 hmac_sha256(const std::vector<std::uint8_t>& key, std::string_view message) noexcept;
// An HMAC fed in parts, so headers are signed without joining the message.
class HmacSha256 final {
 public:
  explicit HmacSha256(const std::vector<std::uint8_t>& key);
  ~HmacSha256();
  HmacSha256(const HmacSha256&) = delete;
  HmacSha256& operator=(const HmacSha256&) = delete;
  void begin() noexcept;
  void update(std::string_view part) noexcept;
  Hmac32 finish() noexcept;

 private:
  struct State;
  std::unique_ptr<State> state_;
};

// 65-byte signature r ‖ s ‖ v, v = 27 + recovery id (as Ethereum signs).
using Signature65 = std::array<std::uint8_t, 65>;

class EcSigner final {
 public:
  EcSigner();
  ~EcSigner();
  EcSigner(const EcSigner&) = delete;
  EcSigner& operator=(const EcSigner&) = delete;

  // A 32-byte hex private key, with or without "0x".
  bool load(std::string_view hex_key, std::string& error);
  [[nodiscard]] const Address& address() const noexcept { return address_; }
  // RFC 6979 deterministic, low-s; thread-safe after load.
  [[nodiscard]] Signature65 sign(const Bytes32& digest) const noexcept;

 private:
  struct Context;
  std::unique_ptr<Context> context_;
  std::array<std::uint8_t, 32> key_{};
  Address address_{};
};

// Recovers the signer of `digest` (tests and the startup self-check).
bool recover_address(const Bytes32& digest, const Signature65& signature, Address& out) noexcept;

// Kalshi signs `timestamp_ms + METHOD + path` (no query): RSA-PSS (SHA-256, salt =
// digest length) for an RSA key, Ed25519 for an Ed25519 key; base64.
class KalshiSigner final {
 public:
  KalshiSigner();
  ~KalshiSigner();
  KalshiSigner(const KalshiSigner&) = delete;
  KalshiSigner& operator=(const KalshiSigner&) = delete;

  bool load_pem(std::string_view key_id, std::string_view pem, std::string& error);
  [[nodiscard]] const std::string& key_id() const noexcept { return key_id_; }
  [[nodiscard]] bool loaded() const noexcept { return key_ != nullptr; }
  [[nodiscard]] bool is_ed25519() const noexcept { return ed25519_; }
  // Empty on failure.
  [[nodiscard]] std::string sign(std::string_view message) const;
  // KALSHI-ACCESS-* header lines (no trailing CRLF on the last).
  [[nodiscard]] std::string headers(std::int64_t timestamp_ms, std::string_view method, std::string_view path) const;
  // Verifies with the key's public half (tests).
  [[nodiscard]] bool verify(std::string_view message, std::string_view signature_b64) const;

 private:
  struct KeyDeleter {
    void operator()(void* key) const noexcept;
  };
  std::unique_ptr<void, KeyDeleter> key_;
  std::string key_id_;
  bool ed25519_ = false;
};

}  // namespace pkm
