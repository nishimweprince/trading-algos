#include <openssl/evp.h>
#include <openssl/pem.h>

#include "check.hpp"
#include "pkm/clob.hpp"
#include "pkm/crypto.hpp"
#include "pkm/eip712.hpp"

using namespace pkm;
using pkm::test::require;
using pkm::test::require_eq;

namespace {

Address addr(std::string_view text) {
  Address a{};
  require(parse_address(text, a), "address parses");
  return a;
}

eip712::OrderV2 vector_order(const std::map<std::string, std::string>& v, bool deposit_wallet) {
  eip712::OrderV2 o;
  if (deposit_wallet) {
    o.salt = 987'654'321;
    o.maker = o.signer = addr(v.at("funder"));
    const auto amounts = clob::buy_amounts(31, 7);
    o.maker_amount = amounts.maker;
    o.taker_amount = amounts.taker;
    o.signature_type = eip712::SignatureType::deposit_wallet;
  } else {
    o.salt = 123'456'789;
    o.maker = o.signer = addr(v.at("address"));
    const auto amounts = clob::buy_amounts(47, 5);
    o.maker_amount = amounts.maker;
    o.taker_amount = amounts.taker;
    o.signature_type = eip712::SignatureType::eoa;
  }
  require(decimal_to_u256(v.at("token"), o.token_id), "token id parses");
  o.timestamp_ms = 1'761'500'000'123ULL;
  return o;
}

std::string pem_of(EVP_PKEY* key) {
  BIO* bio = BIO_new(BIO_s_mem());
  PEM_write_bio_PrivateKey(bio, key, nullptr, nullptr, 0, nullptr, nullptr);
  char* data = nullptr;
  const long n = BIO_get_mem_data(bio, &data);
  std::string out(data, static_cast<std::size_t>(n));
  BIO_free(bio);
  return out;
}

}  // namespace

PKM_TEST(keccak_known_answers) {
  require_eq(to_hex(keccak256("")), std::string("0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"), "keccak empty");
  require_eq(to_hex(keccak256("abc")), std::string("0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"), "keccak abc");
  // Inputs past the 136-byte rate are covered by the struct-hash vectors (384 bytes).
  require(keccak256(std::string(136, 'x')) != keccak256(std::string(135, 'x')), "keccak block edge");
  require_eq(to_hex(keccak256("transfer(address,uint256)")).substr(0, 10), std::string("0xa9059cbb"), "erc20 selector");
}

PKM_TEST(hex_u256_and_checksums) {
  Bytes32 n{};
  require(decimal_to_u256("255", n) && n[31] == 255 && n[30] == 0, "decimal small");
  require(!decimal_to_u256("12a", n), "decimal rejects letters");
  const std::string big = "71321045679252212594626385532706912750332728571942532289631379312455583992563";
  require(decimal_to_u256(big, n), "decimal big");
  require_eq(u256_to_decimal(n), big, "decimal round trip");
  require_eq(u256_to_decimal(u256(0)), std::string("0"), "zero");
  require_eq(checksum_address(addr("0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266")),
             std::string("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"), "EIP-55");
  Address a{};
  require(!parse_address("0x1234", a), "short address refused");
}

PKM_TEST(signer_address_and_recovery) {
  const auto v = test::read_vectors("clob_vectors.txt");
  EcSigner signer;
  std::string error;
  require(signer.load(v.at("key"), error), "key loads");
  require_eq(checksum_address(signer.address()), v.at("address"), "address from key");
  const Bytes32 digest = keccak256("message");
  const auto sig = signer.sign(digest);
  Address recovered{};
  require(recover_address(digest, sig, recovered) && recovered == signer.address(), "recovers signer");
  require(!signer.load("0x00", error), "short key refused");
}

PKM_TEST(order_vectors_match_the_sdk) {
  const auto v = test::read_vectors("clob_vectors.txt");
  EcSigner signer;
  std::string error;
  require(signer.load(v.at("key"), error), "key loads");
  require_eq(to_hex(eip712::exchange_domain(false)), v.at("domain_v2"), "V2 exchange domain");

  const auto eoa = vector_order(v, false);
  require_eq(to_hex(eip712::order_struct_hash(eoa)), v.at("eoa_struct_hash"), "EOA struct hash");
  const auto eoa_sig = eip712::sign_order(signer, eoa, false);
  require_eq(eoa_sig, v.at("eoa_signature"), "EOA signature");
  std::string body;
  clob::write_order_body(body, eoa, v.at("token"), eoa_sig, v.at("api_key"), true);
  require_eq(body, v.at("eoa_body"), "EOA body");

  const auto dw = vector_order(v, true);
  require_eq(to_hex(eip712::order_struct_hash(dw)), v.at("dw_struct_hash"), "deposit wallet struct hash");
  const auto dw_sig = eip712::sign_order(signer, dw, false);
  require_eq(dw_sig, v.at("dw_signature"), "deposit wallet wrapped signature");
  clob::write_order_body(body, dw, v.at("token"), dw_sig, v.at("api_key"), true);
  require_eq(body, v.at("dw_body"), "deposit wallet body");
}

PKM_TEST(l2_headers_match_the_sdk) {
  const auto v = test::read_vectors("clob_vectors.txt");
  clob::Credentials creds;
  creds.api_key = v.at("api_key");
  require(base64_decode(v.at("secret"), creds.secret, true), "secret decodes");
  creds.passphrase = "pp";
  creds.address = v.at("address");
  clob::L2Signer l2(creds);
  require_eq(l2.signature(1'761'500'000, "POST", "/order", v.at("dw_body")), v.at("l2_post_order"), "POST /order HMAC");
  require_eq(l2.signature(1'761'500'001, "DELETE", "/order", R"({"orderID":"0xabc"})"), v.at("l2_cancel"), "DELETE HMAC");
  require_eq(l2.signature(1'761'500'002, "GET", "/data/orders", ""), v.at("l2_get"), "GET HMAC");
  // Again, to show the reused HMAC state resets.
  require_eq(l2.signature(1'761'500'002, "GET", "/data/orders", ""), v.at("l2_get"), "GET HMAC twice");

  // polymarket_client_sdk_v2's own auth test vector.
  clob::Credentials sdk;
  require(base64_decode("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", sdk.secret, true), "sdk secret");
  clob::L2Signer sdk_l2(sdk);
  require_eq(sdk_l2.signature(1'000'000, "test-sign", "/orders", R"({"hash":"0x123"})"),
             std::string("4gJVbox-R6XlDK4nlaicig0_ANVL1qdcahiL8CXfXLM="), "SDK HMAC vector");

  std::string headers;
  l2.write_headers(headers, 1'761'500'002, "GET", "/data/orders", "");
  require(headers.find("POLY_SIGNATURE: " + v.at("l2_get") + "\r\n") != std::string::npos, "signature header line");
  require(headers.find("POLY_TIMESTAMP: 1761500002\r\n") != std::string::npos, "timestamp header line");
}

PKM_TEST(clob_auth_and_relayer_batch_match) {
  const auto v = test::read_vectors("clob_vectors.txt");
  EcSigner signer;
  std::string error;
  require(signer.load(v.at("key"), error), "key loads");
  const Bytes32 auth = eip712::clob_auth_digest(signer.address(), 1'761'500'000, 0);
  require_eq(to_hex(auth), v.at("clob_auth_digest"), "ClobAuth digest");
  require_eq(to_hex(signer.sign(auth)), v.at("clob_auth_signature"), "ClobAuth signature");

  Bytes32 condition{};
  require(parse_hex(v.at("condition"), condition.data(), 32), "condition parses");
  const auto data = eip712::redeem_positions_calldata(addr(eip712::kCollateral), condition);
  require_eq(to_hex(data.data(), data.size()), v.at("redeem_calldata"), "redeemPositions calldata");
  eip712::Call call;
  call.target = addr(eip712::kCtfCollateralAdapter);
  call.data = data;
  const Bytes32 batch = eip712::batch_digest(addr(v.at("funder")), u256(2), 1'761'500'300, {call});
  require_eq(to_hex(batch), v.at("batch_digest"), "Batch digest");
  require_eq(to_hex(signer.sign(batch)), v.at("batch_signature"), "Batch signature");
}

PKM_TEST(base64_and_hmac) {
  std::vector<std::uint8_t> out;
  require(base64_decode("aGVsbG8", out, true) && std::string(out.begin(), out.end()) == "hello", "unpadded decode");
  const std::uint8_t bytes[] = {0xfb, 0xff, 0xfe};
  require_eq(base64_encode(bytes, 3, true), std::string("-__-"), "url-safe encode");
  require_eq(base64_encode(bytes, 3, false), std::string("+//+"), "standard encode");
  require(base64_decode("-__-", out, true) && out.size() == 3 && out[0] == 0xfb, "url-safe decode");
  // RFC 4231 case 2.
  const std::vector<std::uint8_t> key = {'J', 'e', 'f', 'e'};
  const auto mac = hmac_sha256(key, "what do ya want for nothing?");
  require_eq(to_hex(mac.data(), mac.size(), false),
             std::string("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"), "RFC 4231 case 2");
}

PKM_TEST(kalshi_rsa_pss_and_ed25519) {
  for (const bool ed : {false, true}) {
    EVP_PKEY* key = ed ? EVP_PKEY_Q_keygen(nullptr, nullptr, "ED25519") : EVP_PKEY_Q_keygen(nullptr, nullptr, "RSA", size_t{2048});
    require(key != nullptr, "keygen");
    const std::string pem = pem_of(key);
    EVP_PKEY_free(key);
    KalshiSigner signer;
    std::string error;
    require(signer.load_pem("key-id", pem, error), "pem loads");
    require(signer.is_ed25519() == ed, "key type");
    const std::string message = "1761500000000GET/trade-api/ws/v2";
    const std::string sig = signer.sign(message);
    require(!sig.empty() && signer.verify(message, sig), "signature verifies");
    require(!signer.verify(message + "x", sig), "tampered message fails");
    const std::string headers = signer.headers(1'761'500'000'000, "GET", "/trade-api/ws/v2?x=1");
    require(headers.find("KALSHI-ACCESS-KEY: key-id\r\n") == 0, "key header");
    require(headers.find("KALSHI-ACCESS-TIMESTAMP: 1761500000000") != std::string::npos, "timestamp header");
    const auto at = headers.find("KALSHI-ACCESS-SIGNATURE: ") + 25;
    const auto sig_header = headers.substr(at, headers.find("\r\n", at) - at);
    require(signer.verify(message, sig_header), "header signs the path without the query");
  }
  KalshiSigner bad;
  std::string error;
  require(!bad.load_pem("k", "not a pem", error), "garbage refused");
}

PKM_TEST(post_and_cancel_answers) {
  auto r = clob::parse_post_response(200, R"({"errorMsg":"","orderID":"0xabc","success":true,"status":"live"})");
  require(r.ok && r.order_id == "0xabc", "post ok");
  r = clob::parse_post_response(400, R"({"error":"invalid post-only order: order crosses book"})");
  require(!r.ok && r.error.find("crosses") != std::string::npos, "post-only reject");
  r = clob::parse_post_response(400, R"({"error":"not enough balance / allowance: balance: 385700, order amount: 2700000"})");
  double free = 0;
  require(!r.ok && clob::free_balance(r.error, free) && free > 0.3856 && free < 0.3858, "balance refusal");
  require(!clob::free_balance("other", free), "not a balance refusal");
  r = clob::parse_post_response(502, "<html>bad gateway</html>");
  require(!r.ok && r.error.find("502") != std::string::npos, "non-JSON error");
  auto c = clob::parse_cancel_response(200, R"({"canceled":["0xabc"],"not_canceled":{}})", "0xabc");
  require(c.ok, "cancel ok");
  c = clob::parse_cancel_response(200, R"({"canceled":[],"not_canceled":{"0xabc":"order already matched"}})", "0xabc");
  require(!c.ok && c.error == "order already matched", "cancel refused");
  std::string body;
  clob::write_cancel_body(body, "0xabc");
  require_eq(body, std::string(R"({"orderID":"0xabc"})"), "cancel body");
}
