#include "pkm/json.hpp"

#include <charconv>
#include <cstdlib>

namespace pkm::json {
namespace {

constexpr int kMaxDepth = 64;

void append_utf8(std::string& out, unsigned cp) {
  if (cp < 0x80) {
    out.push_back(static_cast<char>(cp));
  } else if (cp < 0x800) {
    out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else if (cp < 0x10000) {
    out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  } else {
    out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
    out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
  }
}

bool hex4(std::string_view s, std::size_t at, unsigned& out) {
  if (at + 4 > s.size()) return false;
  out = 0;
  for (std::size_t i = at; i < at + 4; ++i) {
    const char c = s[i];
    out <<= 4;
    if (c >= '0' && c <= '9') out |= static_cast<unsigned>(c - '0');
    else if (c >= 'a' && c <= 'f') out |= static_cast<unsigned>(c - 'a' + 10);
    else if (c >= 'A' && c <= 'F') out |= static_cast<unsigned>(c - 'A' + 10);
    else return false;
  }
  return true;
}

}  // namespace

std::string unescape(std::string_view raw) {
  std::string out;
  out.reserve(raw.size());
  for (std::size_t i = 0; i < raw.size(); ++i) {
    const char c = raw[i];
    if (c != '\\' || i + 1 >= raw.size()) {
      out.push_back(c);
      continue;
    }
    const char e = raw[++i];
    switch (e) {
      case 'n': out.push_back('\n'); break;
      case 't': out.push_back('\t'); break;
      case 'r': out.push_back('\r'); break;
      case 'b': out.push_back('\b'); break;
      case 'f': out.push_back('\f'); break;
      case 'u': {
        unsigned cp = 0;
        if (!hex4(raw, i + 1, cp)) break;
        i += 4;
        if (cp >= 0xD800 && cp < 0xDC00 && i + 6 < raw.size() && raw[i + 1] == '\\' && raw[i + 2] == 'u') {
          unsigned lo = 0;
          if (hex4(raw, i + 3, lo) && lo >= 0xDC00 && lo < 0xE000) {
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            i += 6;
          }
        }
        append_utf8(out, cp);
        break;
      }
      default: out.push_back(e);
    }
  }
  return out;
}

void append_string(std::string& out, std::string_view text) {
  static constexpr char kHex[] = "0123456789abcdef";
  out.push_back('"');
  for (const char c : text) {
    switch (c) {
      case '"': out.append("\\\""); break;
      case '\\': out.append("\\\\"); break;
      case '\n': out.append("\\n"); break;
      case '\r': out.append("\\r"); break;
      case '\t': out.append("\\t"); break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          out.append("\\u00");
          out.push_back(kHex[(c >> 4) & 0xF]);
          out.push_back(kHex[c & 0xF]);
        } else {
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

void Document::skip_ws() noexcept {
  while (p_ < end_ && (*p_ == ' ' || *p_ == '\n' || *p_ == '\r' || *p_ == '\t')) ++p_;
}

bool Document::parse_string_view(std::string_view& out, bool& escaped) noexcept {
  ++p_;  // opening quote
  const char* start = p_;
  escaped = false;
  while (p_ < end_) {
    const char c = *p_;
    if (c == '"') {
      out = std::string_view(start, static_cast<std::size_t>(p_ - start));
      ++p_;
      return true;
    }
    if (c == '\\') {
      escaped = true;
      p_ += 2;
      continue;
    }
    ++p_;
  }
  return false;
}

std::uint32_t Document::parse_value(std::string_view key) noexcept {
  skip_ws();
  if (p_ >= end_ || ++depth_ > kMaxDepth) return 0;
  const auto index = static_cast<std::uint32_t>(nodes_.size());
  nodes_.push_back(Node{});
  nodes_[index].key = key;
  const char c = *p_;
  if (c == '{' || c == '[') {
    const bool object = c == '{';
    nodes_[index].type = object ? Type::object : Type::array;
    ++p_;
    skip_ws();
    std::uint32_t last = 0;
    if (p_ < end_ && *p_ == (object ? '}' : ']')) {
      ++p_;
      --depth_;
      return index;
    }
    while (p_ < end_) {
      std::string_view member_key;
      if (object) {
        skip_ws();
        if (p_ >= end_ || *p_ != '"') return 0;
        bool escaped = false;
        if (!parse_string_view(member_key, escaped)) return 0;
        skip_ws();
        if (p_ >= end_ || *p_ != ':') return 0;
        ++p_;
      }
      const std::uint32_t child = parse_value(member_key);
      if (child == 0) return 0;
      if (last == 0) nodes_[index].first = child;
      else nodes_[last].next = child;
      last = child;
      skip_ws();
      if (p_ >= end_) return 0;
      if (*p_ == ',') {
        ++p_;
        continue;
      }
      if (*p_ == (object ? '}' : ']')) {
        ++p_;
        --depth_;
        return index;
      }
      return 0;
    }
    return 0;
  }
  if (c == '"') {
    nodes_[index].type = Type::string;
    bool escaped = false;
    std::string_view text;
    if (!parse_string_view(text, escaped)) return 0;
    nodes_[index].text = text;
    nodes_[index].escaped = escaped;
  } else if (c == 't' || c == 'f') {
    const std::string_view word = c == 't' ? "true" : "false";
    if (static_cast<std::size_t>(end_ - p_) < word.size() || std::string_view(p_, word.size()) != word) return 0;
    nodes_[index].type = Type::boolean;
    nodes_[index].truth = c == 't';
    p_ += word.size();
  } else if (c == 'n') {
    if (end_ - p_ < 4 || std::string_view(p_, 4) != "null") return 0;
    p_ += 4;
  } else if (c == '-' || (c >= '0' && c <= '9')) {
    const char* start = p_;
    ++p_;
    while (p_ < end_ && ((*p_ >= '0' && *p_ <= '9') || *p_ == '.' || *p_ == 'e' || *p_ == 'E' || *p_ == '+' || *p_ == '-')) ++p_;
    nodes_[index].type = Type::number;
    nodes_[index].text = std::string_view(start, static_cast<std::size_t>(p_ - start));
  } else {
    return 0;
  }
  --depth_;
  return index;
}

bool Document::parse(std::string_view text) noexcept {
  nodes_.clear();
  nodes_.push_back(Node{});  // index 0 means "none"
  p_ = text.data();
  end_ = text.data() + text.size();
  depth_ = 0;
  ok_ = parse_value({}) == 1;
  if (ok_) {
    skip_ws();
    ok_ = p_ == end_;
  }
  return ok_;
}

Type Value::type() const noexcept { return doc_->node(index_).type; }

Value Value::operator[](std::string_view key) const noexcept {
  if (!is_object()) return {};
  for (auto i = doc_->node(index_).first; i != 0; i = doc_->node(i).next)
    if (doc_->node(i).key == key) return {doc_, i};
  return {};
}

Value Value::at(std::size_t position) const noexcept {
  if (!is_array() && !is_object()) return {};
  std::size_t n = 0;
  for (auto i = doc_->node(index_).first; i != 0; i = doc_->node(i).next, ++n)
    if (n == position) return {doc_, i};
  return {};
}

std::size_t Value::size() const noexcept {
  if (!is_array() && !is_object()) return 0;
  std::size_t n = 0;
  for (auto i = doc_->node(index_).first; i != 0; i = doc_->node(i).next) ++n;
  return n;
}

std::string_view Value::key() const noexcept { return valid() ? doc_->node(index_).key : std::string_view{}; }

std::string_view Value::raw() const noexcept {
  if (!valid()) return {};
  const auto& n = doc_->node(index_);
  return (n.type == Type::string || n.type == Type::number) ? n.text : std::string_view{};
}

std::string Value::string() const {
  if (!valid()) return {};
  const auto& n = doc_->node(index_);
  if (n.type != Type::string) return std::string(n.text);
  return n.escaped ? unescape(n.text) : std::string(n.text);
}

bool Value::number(double& out) const noexcept {
  const auto text = raw();
  if (text.empty()) return false;
  const auto* first = text.data();
  const auto* last = text.data() + text.size();
  if (*first == '+') ++first;
  const auto result = std::from_chars(first, last, out);
  return result.ec == std::errc{} && result.ptr == last;
}

double Value::number_or(double fallback) const noexcept {
  double v = 0;
  return number(v) ? v : fallback;
}

bool Value::integer(std::int64_t& out) const noexcept {
  const auto text = raw();
  if (text.empty()) return false;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), out);
  if (result.ec == std::errc{} && result.ptr == text.data() + text.size()) return true;
  double d = 0;
  if (!number(d)) return false;
  out = static_cast<std::int64_t>(d);
  return true;
}

std::int64_t Value::integer_or(std::int64_t fallback) const noexcept {
  std::int64_t v = 0;
  return integer(v) ? v : fallback;
}

bool Value::boolean_or(bool fallback) const noexcept {
  if (!is_bool()) return fallback;
  return doc_->node(index_).truth;
}

Value::Iterator& Value::Iterator::operator++() noexcept {
  index_ = doc_->node(index_).next;
  return *this;
}

Value::Iterator Value::begin() const noexcept {
  if (!is_array() && !is_object()) return end();
  return {doc_, doc_->node(index_).first};
}

}  // namespace pkm::json
