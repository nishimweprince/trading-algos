#include "pkm/crypto.hpp"

#include <openssl/bio.h>
#include <openssl/core_names.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>
#include <secp256k1.h>
#include <secp256k1_recovery.h>

#include <cstring>

namespace pkm {
namespace {

constexpr char kHex[] = "0123456789abcdef";

constexpr std::uint64_t kRoundConstants[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808AULL, 0x8000000080008000ULL,
    0x000000000000808BULL, 0x0000000080000001ULL, 0x8000000080008081ULL, 0x8000000000008009ULL,
    0x000000000000008AULL, 0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000AULL,
    0x000000008000808BULL, 0x800000000000008BULL, 0x8000000000008089ULL, 0x8000000000008003ULL,
    0x8000000000008002ULL, 0x8000000000000080ULL, 0x000000000000800AULL, 0x800000008000000AULL,
    0x8000000080008081ULL, 0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL};
constexpr int kRotations[25] = {0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14};

inline std::uint64_t rotl(std::uint64_t x, int n) noexcept { return n == 0 ? x : (x << n) | (x >> (64 - n)); }

void keccak_f(std::uint64_t a[25]) noexcept {
  for (const auto rc : kRoundConstants) {
    std::uint64_t c[5];
    for (int x = 0; x < 5; ++x) c[x] = a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20];
    for (int x = 0; x < 5; ++x) {
      const std::uint64_t d = c[(x + 4) % 5] ^ rotl(c[(x + 1) % 5], 1);
      for (int y = 0; y < 25; y += 5) a[y + x] ^= d;
    }
    std::uint64_t b[25];
    for (int x = 0; x < 5; ++x)
      for (int y = 0; y < 5; ++y) b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(a[x + 5 * y], kRotations[x + 5 * y]);
    for (int y = 0; y < 25; y += 5)
      for (int x = 0; x < 5; ++x) a[y + x] = b[y + x] ^ (~b[y + (x + 1) % 5] & b[y + (x + 2) % 5]);
    a[0] ^= rc;
  }
}

int hex_value(char c) noexcept {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

secp256k1_context* secp_context() noexcept {
  static secp256k1_context* const context = secp256k1_context_create(SECP256K1_CONTEXT_NONE);
  return context;
}

}  // namespace

Bytes32 keccak256(const void* data, std::size_t size) noexcept {
  constexpr std::size_t kRate = 136;
  std::uint64_t state[25] = {};
  const auto* bytes = static_cast<const std::uint8_t*>(data);
  auto absorb = [&state](const std::uint8_t* block) {
    for (std::size_t i = 0; i < kRate / 8; ++i) {
      std::uint64_t lane = 0;
      for (int b = 7; b >= 0; --b) lane = (lane << 8) | block[i * 8 + static_cast<std::size_t>(b)];
      state[i] ^= lane;
    }
    keccak_f(state);
  };
  while (size >= kRate) {
    absorb(bytes);
    bytes += kRate;
    size -= kRate;
  }
  std::uint8_t last[kRate] = {};
  if (size > 0) std::memcpy(last, bytes, size);
  last[size] = 0x01;
  last[kRate - 1] |= 0x80;
  absorb(last);
  Bytes32 out{};
  for (std::size_t i = 0; i < 4; ++i)
    for (std::size_t b = 0; b < 8; ++b) out[i * 8 + b] = static_cast<std::uint8_t>(state[i] >> (8 * b));
  return out;
}

void append_hex(std::string& out, const std::uint8_t* data, std::size_t size) {
  for (std::size_t i = 0; i < size; ++i) {
    out.push_back(kHex[data[i] >> 4]);
    out.push_back(kHex[data[i] & 0x0F]);
  }
}

std::string to_hex(const std::uint8_t* data, std::size_t size, bool prefix) {
  std::string out;
  out.reserve(size * 2 + 2);
  if (prefix) out = "0x";
  append_hex(out, data, size);
  return out;
}

bool parse_hex(std::string_view text, std::uint8_t* out, std::size_t size) noexcept {
  if (text.size() >= 2 && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) text.remove_prefix(2);
  if (text.size() != size * 2) return false;
  for (std::size_t i = 0; i < size; ++i) {
    const int hi = hex_value(text[2 * i]);
    const int lo = hex_value(text[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = static_cast<std::uint8_t>((hi << 4) | lo);
  }
  return true;
}

bool parse_address(std::string_view text, Address& out) noexcept { return parse_hex(text, out.data(), out.size()); }

std::string checksum_address(const Address& address) {
  const std::string lower = to_hex(address, false);
  const Bytes32 hash = keccak256(lower);
  std::string out = "0x";
  for (std::size_t i = 0; i < lower.size(); ++i) {
    const char c = lower[i];
    const int nibble = (i % 2 == 0) ? (hash[i / 2] >> 4) : (hash[i / 2] & 0x0F);
    out.push_back((c >= 'a' && nibble >= 8) ? static_cast<char>(c - 'a' + 'A') : c);
  }
  return out;
}

bool decimal_to_u256(std::string_view text, Bytes32& out) noexcept {
  if (text.empty() || text.size() > 78) return false;
  out.fill(0);
  for (const char c : text) {
    if (c < '0' || c > '9') return false;
    unsigned carry = static_cast<unsigned>(c - '0');
    for (int i = 31; i >= 0; --i) {
      const unsigned v = out[static_cast<std::size_t>(i)] * 10U + carry;
      out[static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(v & 0xFF);
      carry = v >> 8;
    }
    if (carry != 0) return false;
  }
  return true;
}

Bytes32 u256(std::uint64_t value) noexcept {
  Bytes32 out{};
  for (int i = 0; i < 8; ++i) out[static_cast<std::size_t>(31 - i)] = static_cast<std::uint8_t>(value >> (8 * i));
  return out;
}

std::string u256_to_decimal(const Bytes32& value) {
  Bytes32 n = value;
  std::string digits;
  bool zero = false;
  while (!zero) {
    unsigned rem = 0;
    zero = true;
    for (auto& byte : n) {
      const unsigned cur = (rem << 8) | byte;
      byte = static_cast<std::uint8_t>(cur / 10);
      rem = cur % 10;
      if (byte != 0) zero = false;
    }
    digits.push_back(static_cast<char>('0' + rem));
  }
  return {digits.rbegin(), digits.rend()};
}

std::string base64_encode(const std::uint8_t* data, std::size_t size, bool url_safe) {
  std::string out(4 * ((size + 2) / 3), '\0');
  const int n = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()), data, static_cast<int>(size));
  out.resize(static_cast<std::size_t>(n));
  if (url_safe)
    for (auto& c : out) c = c == '+' ? '-' : c == '/' ? '_' : c;
  return out;
}

bool base64_decode(std::string_view text, std::vector<std::uint8_t>& out, bool url_safe) {
  std::string s(text);
  if (url_safe)
    for (auto& c : s) c = c == '-' ? '+' : c == '_' ? '/' : c;
  while (s.size() % 4 != 0) s.push_back('=');
  out.assign(3 * s.size() / 4, 0);
  const int n = EVP_DecodeBlock(out.data(), reinterpret_cast<const unsigned char*>(s.data()), static_cast<int>(s.size()));
  if (n < 0) return false;
  std::size_t pad = 0;
  if (!s.empty() && s.back() == '=') ++pad;
  if (s.size() > 1 && s[s.size() - 2] == '=') ++pad;
  out.resize(static_cast<std::size_t>(n) - pad);
  return true;
}

Hmac32 hmac_sha256(const std::vector<std::uint8_t>& key, std::string_view message) noexcept {
  Hmac32 out{};
  unsigned int len = 0;
  HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()), reinterpret_cast<const unsigned char*>(message.data()),
       message.size(), out.data(), &len);
  return out;
}

struct HmacSha256::State {
  EVP_MAC* mac = nullptr;
  EVP_MAC_CTX* base = nullptr;
  EVP_MAC_CTX* work = nullptr;
};

HmacSha256::HmacSha256(const std::vector<std::uint8_t>& key) : state_(std::make_unique<State>()) {
  state_->mac = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
  state_->base = EVP_MAC_CTX_new(state_->mac);
  char digest[] = "SHA256";
  OSSL_PARAM params[] = {OSSL_PARAM_construct_utf8_string(OSSL_MAC_PARAM_DIGEST, digest, 0), OSSL_PARAM_construct_end()};
  EVP_MAC_init(state_->base, key.data(), key.size(), params);
}

HmacSha256::~HmacSha256() {
  EVP_MAC_CTX_free(state_->work);
  EVP_MAC_CTX_free(state_->base);
  EVP_MAC_free(state_->mac);
}

void HmacSha256::begin() noexcept {
  EVP_MAC_CTX_free(state_->work);
  state_->work = EVP_MAC_CTX_dup(state_->base);
}

void HmacSha256::update(std::string_view part) noexcept {
  EVP_MAC_update(state_->work, reinterpret_cast<const unsigned char*>(part.data()), part.size());
}

Hmac32 HmacSha256::finish() noexcept {
  Hmac32 out{};
  std::size_t len = 0;
  EVP_MAC_final(state_->work, out.data(), &len, out.size());
  return out;
}

struct EcSigner::Context {
  secp256k1_context* ctx = nullptr;
};

EcSigner::EcSigner() : context_(std::make_unique<Context>()) {
  context_->ctx = secp256k1_context_create(SECP256K1_CONTEXT_SIGN | SECP256K1_CONTEXT_VERIFY);
}

EcSigner::~EcSigner() {
  key_.fill(0);
  secp256k1_context_destroy(context_->ctx);
}

bool EcSigner::load(std::string_view hex_key, std::string& error) {
  while (!hex_key.empty() && (hex_key.back() == '\n' || hex_key.back() == '\r' || hex_key.back() == ' ')) hex_key.remove_suffix(1);
  if (!parse_hex(hex_key, key_.data(), key_.size())) {
    error = "private key is not 32 bytes of hex";
    return false;
  }
  if (secp256k1_ec_seckey_verify(context_->ctx, key_.data()) != 1) {
    error = "private key is out of range";
    return false;
  }
  secp256k1_pubkey pubkey;
  if (secp256k1_ec_pubkey_create(context_->ctx, &pubkey, key_.data()) != 1) {
    error = "public key derivation failed";
    return false;
  }
  std::uint8_t serialized[65];
  std::size_t len = sizeof(serialized);
  secp256k1_ec_pubkey_serialize(context_->ctx, serialized, &len, &pubkey, SECP256K1_EC_UNCOMPRESSED);
  const Bytes32 hash = keccak256(serialized + 1, 64);
  std::memcpy(address_.data(), hash.data() + 12, 20);
  return true;
}

Signature65 EcSigner::sign(const Bytes32& digest) const noexcept {
  secp256k1_ecdsa_recoverable_signature signature;
  secp256k1_ecdsa_sign_recoverable(context_->ctx, &signature, digest.data(), key_.data(), nullptr, nullptr);
  Signature65 out{};
  int recid = 0;
  secp256k1_ecdsa_recoverable_signature_serialize_compact(context_->ctx, out.data(), &recid, &signature);
  out[64] = static_cast<std::uint8_t>(27 + recid);
  return out;
}

bool recover_address(const Bytes32& digest, const Signature65& signature, Address& out) noexcept {
  const int recid = signature[64] >= 27 ? signature[64] - 27 : signature[64];
  secp256k1_ecdsa_recoverable_signature parsed;
  if (secp256k1_ecdsa_recoverable_signature_parse_compact(secp_context(), &parsed, signature.data(), recid) != 1) return false;
  secp256k1_pubkey pubkey;
  if (secp256k1_ecdsa_recover(secp_context(), &pubkey, &parsed, digest.data()) != 1) return false;
  std::uint8_t serialized[65];
  std::size_t len = sizeof(serialized);
  secp256k1_ec_pubkey_serialize(secp_context(), serialized, &len, &pubkey, SECP256K1_EC_UNCOMPRESSED);
  const Bytes32 hash = keccak256(serialized + 1, 64);
  std::memcpy(out.data(), hash.data() + 12, 20);
  return true;
}

void KalshiSigner::KeyDeleter::operator()(void* key) const noexcept { EVP_PKEY_free(static_cast<EVP_PKEY*>(key)); }

KalshiSigner::KalshiSigner() = default;
KalshiSigner::~KalshiSigner() = default;

bool KalshiSigner::load_pem(std::string_view key_id, std::string_view pem, std::string& error) {
  BIO* bio = BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size()));
  EVP_PKEY* key = PEM_read_bio_PrivateKey(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  if (key == nullptr) {
    error = "KALSHI private key is not a PEM RSA or Ed25519 key";
    return false;
  }
  const int id = EVP_PKEY_get_base_id(key);
  if (id != EVP_PKEY_RSA && id != EVP_PKEY_ED25519) {
    EVP_PKEY_free(key);
    error = "KALSHI private key must be RSA or Ed25519";
    return false;
  }
  ed25519_ = id == EVP_PKEY_ED25519;
  key_.reset(key);
  key_id_ = std::string(key_id);
  return true;
}

std::string KalshiSigner::sign(std::string_view message) const {
  if (!key_) return {};
  auto* key = static_cast<EVP_PKEY*>(key_.get());
  EVP_MD_CTX* md = EVP_MD_CTX_new();
  EVP_PKEY_CTX* pctx = nullptr;
  std::string out;
  if (EVP_DigestSignInit(md, &pctx, ed25519_ ? nullptr : EVP_sha256(), nullptr, key) == 1) {
    bool ok = true;
    if (!ed25519_) {
      ok = EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) == 1 &&
           EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) == 1;
    }
    std::size_t len = 0;
    const auto* msg = reinterpret_cast<const unsigned char*>(message.data());
    if (ok && EVP_DigestSign(md, nullptr, &len, msg, message.size()) == 1) {
      std::vector<std::uint8_t> signature(len);
      if (EVP_DigestSign(md, signature.data(), &len, msg, message.size()) == 1)
        out = base64_encode(signature.data(), len, false);
    }
  }
  EVP_MD_CTX_free(md);
  return out;
}

bool KalshiSigner::verify(std::string_view message, std::string_view signature_b64) const {
  if (!key_) return false;
  std::vector<std::uint8_t> signature;
  if (!base64_decode(signature_b64, signature, false)) return false;
  auto* key = static_cast<EVP_PKEY*>(key_.get());
  EVP_MD_CTX* md = EVP_MD_CTX_new();
  EVP_PKEY_CTX* pctx = nullptr;
  bool ok = EVP_DigestVerifyInit(md, &pctx, ed25519_ ? nullptr : EVP_sha256(), nullptr, key) == 1;
  if (ok && !ed25519_)
    ok = EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) == 1 &&
         EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) == 1;
  ok = ok && EVP_DigestVerify(md, signature.data(), signature.size(), reinterpret_cast<const unsigned char*>(message.data()),
                              message.size()) == 1;
  EVP_MD_CTX_free(md);
  return ok;
}

std::string KalshiSigner::headers(std::int64_t timestamp_ms, std::string_view method, std::string_view path) const {
  const auto query = path.find('?');
  if (query != std::string_view::npos) path = path.substr(0, query);
  const std::string ts = std::to_string(timestamp_ms);
  std::string message = ts;
  message.append(method).append(path);
  std::string out = "KALSHI-ACCESS-KEY: " + key_id_ + "\r\nKALSHI-ACCESS-SIGNATURE: " + sign(message) +
                    "\r\nKALSHI-ACCESS-TIMESTAMP: " + ts;
  return out;
}

}  // namespace pkm
