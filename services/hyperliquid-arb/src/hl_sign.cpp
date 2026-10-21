#include "hla/hl_sign.hpp"

#include <openssl/evp.h>
#include <secp256k1.h>
#include <secp256k1_recovery.h>

#include <cstring>
#include <stdexcept>

namespace hla {
namespace {

constexpr char kHex[] = "0123456789abcdef";

void put_be(std::string& out, std::uint64_t value, int bytes) {
  for (int i = bytes - 1; i >= 0; --i) out.push_back(static_cast<char>((value >> (8 * i)) & 0xFF));
}

void msgpack_uint(std::uint64_t value, std::string& out) {
  if (value < 0x80) {
    out.push_back(static_cast<char>(value));
  } else if (value <= 0xFF) {
    out.push_back(static_cast<char>(0xCC));
    put_be(out, value, 1);
  } else if (value <= 0xFFFF) {
    out.push_back(static_cast<char>(0xCD));
    put_be(out, value, 2);
  } else if (value <= 0xFFFFFFFFULL) {
    out.push_back(static_cast<char>(0xCE));
    put_be(out, value, 4);
  } else {
    out.push_back(static_cast<char>(0xCF));
    put_be(out, value, 8);
  }
}

void msgpack_string(std::string_view text, std::string& out) {
  const auto n = text.size();
  if (n < 32) {
    out.push_back(static_cast<char>(0xA0 | n));
  } else if (n <= 0xFF) {
    out.push_back(static_cast<char>(0xD9));
    put_be(out, n, 1);
  } else if (n <= 0xFFFF) {
    out.push_back(static_cast<char>(0xDA));
    put_be(out, n, 2);
  } else {
    out.push_back(static_cast<char>(0xDB));
    put_be(out, n, 4);
  }
  out.append(text);
}

void msgpack_header(std::size_t n, std::uint8_t fix, std::uint8_t b16, std::uint8_t b32, std::string& out) {
  if (n < 16) {
    out.push_back(static_cast<char>(fix | n));
  } else if (n <= 0xFFFF) {
    out.push_back(static_cast<char>(b16));
    put_be(out, n, 2);
  } else {
    out.push_back(static_cast<char>(b32));
    put_be(out, n, 4);
  }
}

void json_string(std::string_view text, std::string& out) {
  out.push_back('"');
  for (const char c : text) {
    if (c == '"' || c == '\\') out.push_back('\\');
    out.push_back(c);
  }
  out.push_back('"');
}

Node order_wire(const OrderSpec& order) {
  const char* tif = order.tif == Tif::alo ? "Alo" : order.tif == Tif::ioc ? "Ioc" : "Gtc";
  std::vector<std::pair<std::string, Node>> fields{
      {"a", Node::uint(order.asset)},
      {"b", Node::boolean(order.is_buy)},
      {"p", Node::string(to_wire(order.price))},
      {"s", Node::string(to_wire(order.size))},
      {"r", Node::boolean(order.reduce_only)},
      {"t", Node::map({{"limit", Node::map({{"tif", Node::string(tif)}})}})},
  };
  if (order.cloid) fields.emplace_back("c", Node::string(cloid_hex(*order.cloid)));
  return Node::map(std::move(fields));
}

std::string trimmed_hex(std::span<const std::uint8_t> bytes) {
  std::string digits = hex(bytes, false);
  const auto first = digits.find_first_not_of('0');
  return "0x" + (first == std::string::npos ? std::string("0") : digits.substr(first));
}

secp256k1_context* ctx(void* context) { return static_cast<secp256k1_context*>(context); }

}  // namespace

void append_msgpack(const Node& node, std::string& out) {
  switch (node.kind) {
    case Node::Kind::null: out.push_back(static_cast<char>(0xC0)); return;
    case Node::Kind::boolean: out.push_back(static_cast<char>(node.flag ? 0xC3 : 0xC2)); return;
    case Node::Kind::uint: msgpack_uint(node.number, out); return;
    case Node::Kind::string: msgpack_string(node.text, out); return;
    case Node::Kind::array:
      msgpack_header(node.items.size(), 0x90, 0xDC, 0xDD, out);
      for (const auto& item : node.items) append_msgpack(item, out);
      return;
    case Node::Kind::map:
      msgpack_header(node.fields.size(), 0x80, 0xDE, 0xDF, out);
      for (const auto& [key, value] : node.fields) {
        msgpack_string(key, out);
        append_msgpack(value, out);
      }
      return;
  }
}

void append_json(const Node& node, std::string& out) {
  switch (node.kind) {
    case Node::Kind::null: out += "null"; return;
    case Node::Kind::boolean: out += node.flag ? "true" : "false"; return;
    case Node::Kind::uint: out += std::to_string(node.number); return;
    case Node::Kind::string: json_string(node.text, out); return;
    case Node::Kind::array:
      out.push_back('[');
      for (std::size_t i = 0; i < node.items.size(); ++i) {
        if (i != 0) out.push_back(',');
        append_json(node.items[i], out);
      }
      out.push_back(']');
      return;
    case Node::Kind::map:
      out.push_back('{');
      for (std::size_t i = 0; i < node.fields.size(); ++i) {
        if (i != 0) out.push_back(',');
        json_string(node.fields[i].first, out);
        out.push_back(':');
        append_json(node.fields[i].second, out);
      }
      out.push_back('}');
      return;
  }
}

Bytes32 keccak256(std::string_view data) {
  // KECCAK-256 (pre-NIST padding) is in OpenSSL's default provider from 3.2.
  static EVP_MD* md = EVP_MD_fetch(nullptr, "KECCAK-256", nullptr);
  if (md == nullptr) throw std::runtime_error("OpenSSL >= 3.2 with KECCAK-256 is required");
  Bytes32 out{};
  unsigned int length = 0;
  if (EVP_Digest(data.data(), data.size(), out.data(), &length, md, nullptr) != 1 || length != out.size())
    throw std::runtime_error("keccak256 failed");
  return out;
}

std::string hex(std::span<const std::uint8_t> bytes, bool prefix) {
  std::string out = prefix ? "0x" : "";
  out.reserve(out.size() + bytes.size() * 2);
  for (const auto byte : bytes) {
    out.push_back(kHex[byte >> 4]);
    out.push_back(kHex[byte & 0x0F]);
  }
  return out;
}

bool parse_hex(std::string_view text, std::span<std::uint8_t> out) noexcept {
  if (text.starts_with("0x") || text.starts_with("0X")) text.remove_prefix(2);
  if (text.size() != out.size() * 2) return false;
  auto nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  for (std::size_t i = 0; i < out.size(); ++i) {
    const auto high = nibble(text[2 * i]);
    const auto low = nibble(text[2 * i + 1]);
    if (high < 0 || low < 0) return false;
    out[i] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

std::string to_wire(Decimal value) {
  auto raw = value.raw;
  std::string out;
  if (raw < 0) {
    out.push_back('-');
    raw = -raw;
  }
  out += std::to_string(raw / kScale);
  auto fraction = raw % kScale;
  if (fraction != 0) {
    std::string digits = std::to_string(fraction);
    digits.insert(0, 8 - digits.size(), '0');
    digits.erase(digits.find_last_not_of('0') + 1);
    out += "." + digits;
  }
  return out == "-0" ? "0" : out;
}

std::string cloid_hex(const Cloid& cloid) { return hex(cloid); }

Node order_action(std::span<const OrderSpec> orders) {
  std::vector<Node> wires;
  for (const auto& order : orders) wires.push_back(order_wire(order));
  return Node::map({{"type", Node::string("order")}, {"orders", Node::array(std::move(wires))},
                    {"grouping", Node::string("na")}});
}

Node modify_action(std::span<const ModifySpec> modifies) {
  std::vector<Node> wires;
  for (const auto& modify : modifies)
    wires.push_back(Node::map({{"oid", Node::uint(modify.oid)}, {"order", order_wire(modify.order)}}));
  return Node::map({{"type", Node::string("batchModify")}, {"modifies", Node::array(std::move(wires))}});
}

Node cancel_action(std::span<const CancelSpec> cancels) {
  std::vector<Node> wires;
  for (const auto& cancel : cancels)
    wires.push_back(Node::map({{"a", Node::uint(cancel.asset)}, {"o", Node::uint(cancel.oid)}}));
  return Node::map({{"type", Node::string("cancel")}, {"cancels", Node::array(std::move(wires))}});
}

Node cancel_by_cloid_action(std::span<const CancelCloidSpec> cancels) {
  std::vector<Node> wires;
  for (const auto& cancel : cancels)
    wires.push_back(
        Node::map({{"asset", Node::uint(cancel.asset)}, {"cloid", Node::string(cloid_hex(cancel.cloid))}}));
  return Node::map({{"type", Node::string("cancelByCloid")}, {"cancels", Node::array(std::move(wires))}});
}

Node schedule_cancel_action(std::optional<std::uint64_t> time_ms) {
  std::vector<std::pair<std::string, Node>> fields{{"type", Node::string("scheduleCancel")}};
  if (time_ms) fields.emplace_back("time", Node::uint(*time_ms));
  return Node::map(std::move(fields));
}

Node update_leverage_action(std::uint32_t asset, bool is_cross, std::uint32_t leverage) {
  return Node::map({{"type", Node::string("updateLeverage")}, {"asset", Node::uint(asset)},
                    {"isCross", Node::boolean(is_cross)}, {"leverage", Node::uint(leverage)}});
}

Bytes32 action_hash(const Node& action, std::uint64_t nonce, std::uint64_t expires_after_ms) {
  std::string data;
  append_msgpack(action, data);
  put_be(data, nonce, 8);
  data.push_back('\0');  // no vault address
  if (expires_after_ms != 0) {
    data.push_back('\0');
    put_be(data, expires_after_ms, 8);
  }
  return keccak256(data);
}

Bytes32 l1_digest(const Bytes32& hash, bool mainnet) {
  auto concat = [](std::initializer_list<std::span<const std::uint8_t>> parts) {
    std::string out;
    for (const auto part : parts) out.append(reinterpret_cast<const char*>(part.data()), part.size());
    return out;
  };
  static const auto domain_type = keccak256(
      "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
  static const auto agent_type = keccak256("Agent(string source,bytes32 connectionId)");
  static const auto name = keccak256("Exchange");
  static const auto version = keccak256("1");
  Bytes32 chain{};
  chain[30] = 0x05;  // 1337 = 0x0539
  chain[31] = 0x39;
  const Bytes32 contract{};
  static const auto domain = keccak256(concat({domain_type, name, version, chain, contract}));
  const auto source = keccak256(mainnet ? "a" : "b");
  const auto message = keccak256(concat({agent_type, source, hash}));
  const std::array<std::uint8_t, 2> prefix{0x19, 0x01};
  return keccak256(concat({prefix, domain, message}));
}

Signer::Signer() : context_(secp256k1_context_create(SECP256K1_CONTEXT_NONE)) {}

Signer::~Signer() {
  key_.fill(0);
  secp256k1_context_destroy(ctx(context_));
}

bool Signer::load(std::string_view private_key_hex, std::string& error) {
  while (!private_key_hex.empty() && (private_key_hex.back() == '\n' || private_key_hex.back() == '\r' ||
                                      private_key_hex.back() == ' '))
    private_key_hex.remove_suffix(1);
  if (!parse_hex(private_key_hex, key_) || secp256k1_ec_seckey_verify(ctx(context_), key_.data()) != 1) {
    error = "agent key is not a valid 32-byte secp256k1 key";
    key_.fill(0);
    return false;
  }
  secp256k1_pubkey pubkey;
  if (secp256k1_ec_pubkey_create(ctx(context_), &pubkey, key_.data()) != 1) {
    error = "agent key: public key derivation failed";
    return false;
  }
  std::array<std::uint8_t, 65> serialized{};
  std::size_t length = serialized.size();
  secp256k1_ec_pubkey_serialize(ctx(context_), serialized.data(), &length, &pubkey, SECP256K1_EC_UNCOMPRESSED);
  const auto digest =
      keccak256(std::string_view(reinterpret_cast<const char*>(serialized.data() + 1), serialized.size() - 1));
  address_ = hex(std::span<const std::uint8_t>(digest).subspan(12));
  loaded_ = true;
  return true;
}

Signature Signer::sign(const Bytes32& digest) const {
  if (!loaded_) throw std::logic_error("signer has no key");
  secp256k1_ecdsa_recoverable_signature signature;
  if (secp256k1_ecdsa_sign_recoverable(ctx(context_), &signature, digest.data(), key_.data(), nullptr, nullptr) != 1)
    throw std::runtime_error("secp256k1 signing failed");
  std::array<std::uint8_t, 64> compact{};
  int recid = 0;
  secp256k1_ecdsa_recoverable_signature_serialize_compact(ctx(context_), compact.data(), &recid, &signature);
  const std::span<const std::uint8_t> bytes(compact);
  return {trimmed_hex(bytes.first(32)), trimmed_hex(bytes.subspan(32)), 27 + recid};
}

Signature Signer::sign_action(const Node& action, std::uint64_t nonce, std::uint64_t expires_after_ms,
                              bool mainnet) const {
  return sign(l1_digest(action_hash(action, nonce, expires_after_ms), mainnet));
}

std::string exchange_payload(const Node& action, std::uint64_t nonce, const Signature& signature,
                             std::uint64_t expires_after_ms) {
  std::string out = "{\"action\":";
  append_json(action, out);
  out += ",\"nonce\":" + std::to_string(nonce) + ",\"signature\":{\"r\":\"" + signature.r + "\",\"s\":\"" +
         signature.s + "\",\"v\":" + std::to_string(signature.v) + "},\"vaultAddress\":null";
  if (expires_after_ms != 0) out += ",\"expiresAfter\":" + std::to_string(expires_after_ms);
  out += "}";
  return out;
}

}  // namespace hla
