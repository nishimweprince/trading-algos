#include "check.hpp"
#include "pkm/json.hpp"

using namespace pkm;
using pkm::test::require;
using pkm::test::require_eq;

PKM_TEST(json_parses_venue_shapes) {
  json::Document doc;
  require(doc.parse(R"({"event_type":"price_change","market":"0xab","price_changes":[{"asset_id":"123","price":"0.47","size":"10.5","side":"BUY","best_bid":"0.47","best_ask":"0.49"}],"timestamp":"1761500000123"})"),
          "price_change parses");
  const auto root = doc.root();
  require(root["event_type"].raw() == "price_change", "string member");
  const auto changes = root["price_changes"];
  require(changes.is_array() && changes.size() == 1, "array size");
  const auto first = changes.at(0);
  require(first["price"].number_or(0) == 0.47, "string number");
  require(root["timestamp"].integer_or(0) == 1'761'500'000'123LL, "string integer");
  require(!root["missing"], "missing member is invalid");
  require(!root["missing"]["deeper"], "chained missing is invalid");

  require(doc.parse(R"([{"a":1},{"a":-2.5e1,"b":true,"c":null,"d":"x\"yé"}])"), "array root parses");
  int n = 0;
  for (const auto item : doc.root()) {
    ++n;
    if (n == 2) {
      require(item["a"].number_or(0) == -25.0, "exponent");
      require(item["b"].boolean_or(false), "bool");
      require(item["c"].is_null(), "null");
      require_eq(item["d"].string(), std::string("x\"y\xc3\xa9"), "escapes unescaped");
    }
  }
  require(n == 2, "iterates both elements");
  for (const char* bad : {"{", "[1,]", "{\"a\" 1}", "tru", "\"open", "{} x", ""}) require(!doc.parse(bad), bad);
  require(!doc.root(), "failed parse has no root");
}

PKM_TEST(json_string_escaping) {
  std::string out;
  json::append_string(out, "a\"b\\c\nd\x01");
  require_eq(out, std::string("\"a\\\"b\\\\c\\nd\\u0001\""), "escaped");
}
