#include "pkm/clob.hpp"

#include <openssl/evp.h>

#include <charconv>

#include "pkm/json.hpp"

namespace pkm::clob {
namespace {

void append_u64(std::string& out, std::uint64_t v) {
  char buf[24];
  const auto r = std::to_chars(buf, buf + sizeof(buf), v);
  out.append(buf, r.ptr);
}

void append_i64(std::string& out, std::int64_t v) {
  char buf[24];
  const auto r = std::to_chars(buf, buf + sizeof(buf), v);
  out.append(buf, r.ptr);
}

void append_quoted_u64(std::string& out, std::uint64_t v) {
  out.push_back('"');
  append_u64(out, v);
  out.push_back('"');
}

void append_address(std::string& out, const Address& a) {
  out.append("\"0x");
  append_hex(out, a.data(), a.size());
  out.push_back('"');
}

void append_bytes32(std::string& out, const Bytes32& b) {
  out.append("\"0x");
  append_hex(out, b.data(), b.size());
  out.push_back('"');
}

}  // namespace

void write_order_body(std::string& out, const eip712::OrderV2& order, std::string_view token, std::string_view signature,
                      std::string_view owner, bool post_only) {
  out.clear();
  out.append("{\"order\":{\"salt\":");
  append_u64(out, order.salt);
  out.append(",\"maker\":");
  append_address(out, order.maker);
  out.append(",\"signer\":");
  append_address(out, order.signer);
  out.append(",\"tokenId\":\"");
  out.append(token);
  out.append("\",\"makerAmount\":");
  append_quoted_u64(out, order.maker_amount);
  out.append(",\"takerAmount\":");
  append_quoted_u64(out, order.taker_amount);
  out.append(order.side == 0 ? ",\"side\":\"BUY\"" : ",\"side\":\"SELL\"");
  out.append(",\"expiration\":\"0\",\"signatureType\":");
  append_u64(out, static_cast<std::uint8_t>(order.signature_type));
  out.append(",\"timestamp\":");
  append_quoted_u64(out, order.timestamp_ms);
  out.append(",\"metadata\":");
  append_bytes32(out, order.metadata);
  out.append(",\"builder\":");
  append_bytes32(out, order.builder);
  out.append(",\"signature\":\"");
  out.append(signature);
  out.append("\"},\"orderType\":\"GTC\",\"owner\":\"");
  out.append(owner);
  out.append(post_only ? "\",\"postOnly\":true}" : "\"}");
}

void write_cancel_body(std::string& out, std::string_view order_id) {
  out.clear();
  out.append("{\"orderID\":\"");
  out.append(order_id);
  out.append("\"}");
}

L2Signer::L2Signer(Credentials credentials) : credentials_(std::move(credentials)), hmac_(credentials_.secret) {
  scratch_.reserve(4096);
}

std::string L2Signer::signature(std::int64_t timestamp_s, std::string_view method, std::string_view path, std::string_view body) {
  char out[48];
  std::size_t len = 0;
  signature_into(out, len, timestamp_s, method, path, body);
  return std::string(out, len);
}

void L2Signer::signature_into(char (&out)[48], std::size_t& len, std::int64_t timestamp_s, std::string_view method,
                              std::string_view path, std::string_view body) {
  char ts[24];
  const auto r = std::to_chars(ts, ts + sizeof(ts), timestamp_s);
  hmac_.begin();
  hmac_.update(std::string_view(ts, static_cast<std::size_t>(r.ptr - ts)));
  hmac_.update(method);
  hmac_.update(path);
  if (body.find('\'') == std::string_view::npos) {
    hmac_.update(body);
  } else {
    scratch_.assign(body);
    for (auto& c : scratch_)
      if (c == '\'') c = '"';
    hmac_.update(scratch_);
  }
  const Hmac32 mac = hmac_.finish();
  len = static_cast<std::size_t>(EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out), mac.data(), static_cast<int>(mac.size())));
  for (std::size_t i = 0; i < len; ++i) out[i] = out[i] == '+' ? '-' : out[i] == '/' ? '_' : out[i];
}

void L2Signer::write_headers(std::string& out, std::int64_t timestamp_s, std::string_view method, std::string_view path,
                             std::string_view body) {
  out.append("POLY_ADDRESS: ").append(credentials_.address).append("\r\n");
  out.append("POLY_API_KEY: ").append(credentials_.api_key).append("\r\n");
  out.append("POLY_PASSPHRASE: ").append(credentials_.passphrase).append("\r\n");
  char sig[48];
  std::size_t len = 0;
  signature_into(sig, len, timestamp_s, method, path, body);
  out.append("POLY_SIGNATURE: ").append(sig, len).append("\r\n");
  out.append("POLY_TIMESTAMP: ");
  append_i64(out, timestamp_s);
  out.append("\r\n");
}

PostResult parse_post_response(int status, std::string_view body) {
  PostResult result;
  json::Document doc;
  if (!doc.parse(body)) {
    result.error = "http " + std::to_string(status) + ": " + std::string(body.substr(0, 200));
    return result;
  }
  const auto root = doc.root();
  const auto id = root["orderID"].string();
  const bool success = root["success"].boolean_or(status / 100 == 2);
  if (status / 100 == 2 && success && !id.empty()) {
    result.ok = true;
    result.order_id = id;
    return result;
  }
  std::string message = root["errorMsg"].string();
  if (message.empty()) message = root["error"].string();
  if (message.empty()) message = "http " + std::to_string(status) + ": " + std::string(body.substr(0, 200));
  result.error = message;
  return result;
}

CancelResult parse_cancel_response(int status, std::string_view body, std::string_view order_id) {
  CancelResult result;
  json::Document doc;
  if (status / 100 != 2 || !doc.parse(body)) {
    result.error = "http " + std::to_string(status) + ": " + std::string(body.substr(0, 200));
    return result;
  }
  const auto root = doc.root();
  for (const auto id : root["canceled"])
    if (id.raw() == order_id) {
      result.ok = true;
      return result;
    }
  const auto why = root["not_canceled"][order_id];
  result.error = why ? why.string() : "not in canceled";
  return result;
}

bool free_balance(std::string_view error, double& out) noexcept {
  if (error.find("not enough balance") == std::string_view::npos) return false;
  const auto at = error.find("balance: ");
  if (at == std::string_view::npos) return false;
  const auto digits = error.substr(at + 9);
  std::uint64_t v = 0;
  const auto r = std::from_chars(digits.data(), digits.data() + digits.size(), v);
  if (r.ptr == digits.data()) return false;
  out = static_cast<double>(v) / 1e6;
  return true;
}

}  // namespace pkm::clob
