#pragma once

#include "hla/types.hpp"

#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// Hyperliquid L1 action signing, byte-compatible with the official Python SDK
// (hyperliquid-python-sdk 0.24.0, utils/signing.py):
//   hash   = keccak256(msgpack(action) || nonce_be64 || 0x00 [|| 0x00 || expires_be64])
//   digest = EIP-712(Exchange/1/chain 1337, Agent{source: "a", connectionId: hash})
//   sig    = secp256k1 recoverable over digest, v = 27 + recid
// tests/fixtures/hl_sign_vectors.txt holds SDK-generated vectors for every action used.
namespace hla {

using Bytes32 = std::array<std::uint8_t, 32>;
using Cloid = std::array<std::uint8_t, 16>;

// An ordered value tree: one source for both the msgpack that is hashed and the JSON
// that is sent, so the two can never disagree.
struct Node final {
  enum class Kind : std::uint8_t { null, boolean, uint, string, array, map };
  Kind kind{Kind::null};
  bool flag{};
  std::uint64_t number{};
  std::string text;
  std::vector<Node> items;
  std::vector<std::pair<std::string, Node>> fields;

  static Node boolean(bool value) { Node n; n.kind = Kind::boolean; n.flag = value; return n; }
  static Node uint(std::uint64_t value) { Node n; n.kind = Kind::uint; n.number = value; return n; }
  static Node string(std::string value) { Node n; n.kind = Kind::string; n.text = std::move(value); return n; }
  static Node array(std::vector<Node> values) { Node n; n.kind = Kind::array; n.items = std::move(values); return n; }
  static Node map(std::vector<std::pair<std::string, Node>> values) {
    Node n; n.kind = Kind::map; n.fields = std::move(values); return n;
  }
};

void append_msgpack(const Node& node, std::string& out);
void append_json(const Node& node, std::string& out);

Bytes32 keccak256(std::string_view data);
std::string hex(std::span<const std::uint8_t> bytes, bool prefix = true);
bool parse_hex(std::string_view text, std::span<std::uint8_t> out) noexcept;

// SDK float_to_wire: shortest decimal, no exponent, "0" for zero.
std::string to_wire(Decimal value);
std::string cloid_hex(const Cloid& cloid);

enum class Tif : std::uint8_t { alo, ioc, gtc };

struct OrderSpec final {
  std::uint32_t asset{};
  bool is_buy{};
  Price price{};
  Quantity size{};
  bool reduce_only{};
  Tif tif{Tif::alo};
  std::optional<Cloid> cloid;
};

struct ModifySpec final {
  std::uint64_t oid{};
  OrderSpec order;
};

struct CancelSpec final {
  std::uint32_t asset{};
  std::uint64_t oid{};
};

struct CancelCloidSpec final {
  std::uint32_t asset{};
  Cloid cloid{};
};

Node order_action(std::span<const OrderSpec> orders);
Node modify_action(std::span<const ModifySpec> modifies);
Node cancel_action(std::span<const CancelSpec> cancels);
Node cancel_by_cloid_action(std::span<const CancelCloidSpec> cancels);
// No time clears a scheduled cancel. HL needs time >= now + 5 s; at most 10 triggers per day.
Node schedule_cancel_action(std::optional<std::uint64_t> time_ms);
Node update_leverage_action(std::uint32_t asset, bool is_cross, std::uint32_t leverage);

// expires_after_ms == 0 omits expiresAfter.
Bytes32 action_hash(const Node& action, std::uint64_t nonce, std::uint64_t expires_after_ms);
Bytes32 l1_digest(const Bytes32& action_hash, bool mainnet = true);

struct Signature final {
  std::string r;  // 0x-hex without leading zeros, as eth_utils.to_hex
  std::string s;
  int v{};
};

class Signer final {
 public:
  Signer();
  ~Signer();
  Signer(const Signer&) = delete;
  Signer& operator=(const Signer&) = delete;

  // 0x-prefixed or bare 64-hex secp256k1 key (the HL API/agent wallet key).
  bool load(std::string_view private_key_hex, std::string& error);
  [[nodiscard]] bool loaded() const noexcept { return loaded_; }
  // Lowercase 0x address derived from the key.
  [[nodiscard]] const std::string& address() const noexcept { return address_; }
  Signature sign(const Bytes32& digest) const;
  Signature sign_action(const Node& action, std::uint64_t nonce, std::uint64_t expires_after_ms,
                        bool mainnet = true) const;

 private:
  void* context_{};
  std::array<std::uint8_t, 32> key_{};
  std::string address_;
  bool loaded_{};
};

// The /exchange body and the WS `post` payload: {"action":..,"nonce":..,"signature":..,
// "vaultAddress":null[,"expiresAfter":N]}.
std::string exchange_payload(const Node& action, std::uint64_t nonce, const Signature& signature,
                             std::uint64_t expires_after_ms);

}  // namespace hla
