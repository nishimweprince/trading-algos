#include "pkm/eip712.hpp"

#include <cstring>

namespace pkm::eip712 {
namespace {

constexpr std::string_view kSoladyType =
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,address verifyingContract,bytes32 salt)"
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,"
    "uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)";
constexpr std::string_view kClobAuthType = "ClobAuth(address address,string timestamp,uint256 nonce,string message)";
constexpr std::string_view kClobAuthMessage = "This message attests that I control the given wallet";
constexpr std::string_view kBatchType =
    "Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)Call(address target,uint256 value,bytes data)";
constexpr std::string_view kCallType = "Call(address target,uint256 value,bytes data)";

// An ABI head being built: 32-byte words appended in order.
class Words final {
 public:
  explicit Words(std::size_t count) { bytes_.reserve(count * 32); }
  void word(const Bytes32& w) { std::memcpy(grow(), w.data(), 32); }
  void address(const Address& a) {
    std::uint8_t* at = grow();
    std::memset(at, 0, 12);
    std::memcpy(at + 12, a.data(), 20);
  }
  void uint(std::uint64_t v) { word(u256(v)); }
  [[nodiscard]] Bytes32 hash() const noexcept { return keccak256(bytes_.data(), bytes_.size()); }
  [[nodiscard]] const std::vector<std::uint8_t>& bytes() const noexcept { return bytes_; }

 private:
  std::uint8_t* grow() {
    bytes_.resize(bytes_.size() + 32);
    return bytes_.data() + bytes_.size() - 32;
  }
  std::vector<std::uint8_t> bytes_;
};

Address must_address(std::string_view text) noexcept {
  Address a{};
  parse_address(text, a);
  return a;
}

}  // namespace

Bytes32 domain_separator(std::string_view name, std::string_view version, std::uint64_t chain_id,
                         const Address* verifying_contract) noexcept {
  Words w(5);
  w.word(keccak256(verifying_contract != nullptr
                       ? "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                       : "EIP712Domain(string name,string version,uint256 chainId)"));
  w.word(keccak256(name));
  w.word(keccak256(version));
  w.uint(chain_id);
  if (verifying_contract != nullptr) w.address(*verifying_contract);
  return w.hash();
}

Bytes32 typed_digest(const Bytes32& domain, const Bytes32& struct_hash) noexcept {
  std::uint8_t input[66];
  input[0] = 0x19;
  input[1] = 0x01;
  std::memcpy(input + 2, domain.data(), 32);
  std::memcpy(input + 34, struct_hash.data(), 32);
  return keccak256(input, sizeof(input));
}

Bytes32 order_struct_hash(const OrderV2& order) noexcept {
  static const Bytes32 type_hash = keccak256(kOrderType);
  Words w(12);
  w.word(type_hash);
  w.uint(order.salt);
  w.address(order.maker);
  w.address(order.signer);
  w.word(order.token_id);
  w.uint(order.maker_amount);
  w.uint(order.taker_amount);
  w.uint(order.side);
  w.uint(static_cast<std::uint8_t>(order.signature_type));
  w.uint(order.timestamp_ms);
  w.word(order.metadata);
  w.word(order.builder);
  return w.hash();
}

Bytes32 exchange_domain(bool neg_risk) noexcept {
  static const Address standard = must_address(kExchangeV2);
  static const Address negative = must_address(kNegRiskExchangeV2);
  static const Bytes32 standard_domain = domain_separator("Polymarket CTF Exchange", "2", kPolygon, &standard);
  static const Bytes32 neg_risk_domain = domain_separator("Polymarket CTF Exchange", "2", kPolygon, &negative);
  return neg_risk ? neg_risk_domain : standard_domain;
}

std::string sign_order(const EcSigner& signer, const OrderV2& order, bool neg_risk) {
  const Bytes32 domain = exchange_domain(neg_risk);
  const Bytes32 contents = order_struct_hash(order);
  if (order.signature_type != SignatureType::deposit_wallet) {
    const Signature65 sig = signer.sign(typed_digest(domain, contents));
    return to_hex(sig);
  }
  static const Bytes32 solady_hash = keccak256(kSoladyType);
  static const Bytes32 name_hash = keccak256("DepositWallet");
  static const Bytes32 version_hash = keccak256("1");
  Words w(7);
  w.word(solady_hash);
  w.word(contents);
  w.word(name_hash);
  w.word(version_hash);
  w.uint(kPolygon);
  w.address(order.signer);
  w.word(Bytes32{});
  const Signature65 sig = signer.sign(typed_digest(domain, w.hash()));
  std::string out;
  out.reserve(2 + 130 + 64 + 64 + kOrderType.size() * 2 + 4);
  out = "0x";
  append_hex(out, sig.data(), sig.size());
  append_hex(out, domain.data(), domain.size());
  append_hex(out, contents.data(), contents.size());
  append_hex(out, reinterpret_cast<const std::uint8_t*>(kOrderType.data()), kOrderType.size());
  const std::uint8_t len[2] = {static_cast<std::uint8_t>(kOrderType.size() >> 8), static_cast<std::uint8_t>(kOrderType.size())};
  append_hex(out, len, 2);
  return out;
}

Bytes32 clob_auth_digest(const Address& address, std::int64_t timestamp_s, std::uint64_t nonce) noexcept {
  static const Bytes32 domain = domain_separator("ClobAuthDomain", "1", kPolygon, nullptr);
  Words w(5);
  w.word(keccak256(kClobAuthType));
  w.address(address);
  w.word(keccak256(std::to_string(timestamp_s)));
  w.uint(nonce);
  w.word(keccak256(kClobAuthMessage));
  return typed_digest(domain, w.hash());
}

Bytes32 batch_digest(const Address& wallet, const Bytes32& nonce, std::uint64_t deadline, const std::vector<Call>& calls) noexcept {
  static const Bytes32 batch_hash = keccak256(kBatchType);
  static const Bytes32 call_hash = keccak256(kCallType);
  Words call_hashes(calls.size());
  for (const auto& call : calls) {
    Words c(4);
    c.word(call_hash);
    c.address(call.target);
    c.word(call.value);
    c.word(keccak256(call.data.data(), call.data.size()));
    call_hashes.word(c.hash());
  }
  Words w(5);
  w.word(batch_hash);
  w.address(wallet);
  w.word(nonce);
  w.uint(deadline);
  w.word(call_hashes.hash());
  const Bytes32 domain = domain_separator("DepositWallet", "1", kPolygon, &wallet);
  return typed_digest(domain, w.hash());
}

std::vector<std::uint8_t> redeem_positions_calldata(const Address& collateral, const Bytes32& condition_id) {
  Words w(7);
  w.address(collateral);
  w.word(Bytes32{});
  w.word(condition_id);
  w.uint(0x80);  // offset of indexSets
  w.uint(2);
  w.uint(1);
  w.uint(2);
  std::vector<std::uint8_t> out = {0x01, 0xb7, 0x03, 0x7c};
  out.insert(out.end(), w.bytes().begin(), w.bytes().end());
  return out;
}

}  // namespace pkm::eip712
