#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "pkm/crypto.hpp"

// The typed data Polymarket's CLOB V2 and its deposit wallet verify, written
// out by hand from `polymarket_client_sdk_v2` 0.8.0 (`clob/types/mod.rs`,
// `clob/client.rs`, `auth.rs`) and pk-exec's `relayer.rs`. The golden vectors in
// tests/fixtures/clob_vectors.txt come from those crates.
namespace pkm::eip712 {

inline constexpr std::uint64_t kPolygon = 137;
inline constexpr std::string_view kExchangeV2 = "0xE111180000d2663C0091e4f400237545B87B996B";
inline constexpr std::string_view kNegRiskExchangeV2 = "0xe2222d279d744050d28e00520010520000310F59";
inline constexpr std::string_view kCollateral = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB";
inline constexpr std::string_view kCtfCollateralAdapter = "0xAdA100Db00Ca00073811820692005400218FcE1f";
inline constexpr std::string_view kDepositWalletFactory = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07";
inline constexpr std::string_view kOrderType =
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,"
    "uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)";

enum class SignatureType : std::uint8_t { eoa = 0, proxy = 1, safe = 2, deposit_wallet = 3 };

struct OrderV2 {
  std::uint64_t salt = 0;
  Address maker{};
  Address signer{};
  Bytes32 token_id{};
  std::uint64_t maker_amount = 0;  // 1e6 units
  std::uint64_t taker_amount = 0;
  std::uint8_t side = 0;           // 0 buy, 1 sell
  SignatureType signature_type = SignatureType::eoa;
  std::uint64_t timestamp_ms = 0;
  Bytes32 metadata{};
  Bytes32 builder{};
};

// EIP712Domain(string name,string version,uint256 chainId[,address verifyingContract]).
Bytes32 domain_separator(std::string_view name, std::string_view version, std::uint64_t chain_id,
                         const Address* verifying_contract) noexcept;
Bytes32 typed_digest(const Bytes32& domain, const Bytes32& struct_hash) noexcept;

Bytes32 order_struct_hash(const OrderV2& order) noexcept;
Bytes32 exchange_domain(bool neg_risk) noexcept;

// The order's `signature` field: 65-byte hex for an EOA, or the deposit wallet's
// ERC-7739 wrapped signature (Solady `TypedDataSign`) for signature type 3.
std::string sign_order(const EcSigner& signer, const OrderV2& order, bool neg_risk);

// L1: ClobAuth{address,string timestamp,uint256 nonce,string message}, domain ClobAuthDomain v1.
Bytes32 clob_auth_digest(const Address& address, std::int64_t timestamp_s, std::uint64_t nonce) noexcept;

// The relayer batch a deposit wallet executes: Batch{wallet,nonce,deadline,Call[]}.
struct Call {
  Address target{};
  Bytes32 value{};
  std::vector<std::uint8_t> data;
};
Bytes32 batch_digest(const Address& wallet, const Bytes32& nonce, std::uint64_t deadline, const std::vector<Call>& calls) noexcept;

// redeemPositions(address,bytes32,bytes32,uint256[]) with index sets [1, 2].
std::vector<std::uint8_t> redeem_positions_calldata(const Address& collateral, const Bytes32& condition_id);

}  // namespace pkm::eip712
