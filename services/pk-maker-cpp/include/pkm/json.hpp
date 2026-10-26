#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

// A small DOM over a caller-owned buffer. Nodes live in one vector that a
// reparse reuses, so a warm Document parses feed frames without allocating.
// Strings and numbers are views into the buffer; `string()` unescapes.
namespace pkm::json {

enum class Type : std::uint8_t { null, boolean, number, string, array, object };

struct Node {
  Type type = Type::null;
  bool escaped = false;  // string contains a backslash escape
  bool truth = false;
  std::uint32_t first = 0;  // first child (array/object), 0 = none
  std::uint32_t next = 0;   // next sibling, 0 = none
  std::string_view key;     // member name inside an object (raw)
  std::string_view text;    // string contents (raw) or number text
};

class Document;

class Value final {
 public:
  Value() = default;
  Value(const Document* doc, std::uint32_t index) : doc_(doc), index_(index) {}

  [[nodiscard]] bool valid() const noexcept { return doc_ != nullptr; }
  explicit operator bool() const noexcept { return valid(); }
  [[nodiscard]] Type type() const noexcept;
  [[nodiscard]] bool is_null() const noexcept { return !valid() || type() == Type::null; }
  [[nodiscard]] bool is_string() const noexcept { return valid() && type() == Type::string; }
  [[nodiscard]] bool is_number() const noexcept { return valid() && type() == Type::number; }
  [[nodiscard]] bool is_array() const noexcept { return valid() && type() == Type::array; }
  [[nodiscard]] bool is_object() const noexcept { return valid() && type() == Type::object; }
  [[nodiscard]] bool is_bool() const noexcept { return valid() && type() == Type::boolean; }

  // Member by name (objects), else invalid.
  [[nodiscard]] Value operator[](std::string_view key) const noexcept;
  [[nodiscard]] Value at(std::size_t position) const noexcept;
  [[nodiscard]] std::size_t size() const noexcept;
  [[nodiscard]] std::string_view key() const noexcept;

  // Raw view of a string's contents or a number's text; empty otherwise.
  [[nodiscard]] std::string_view raw() const noexcept;
  [[nodiscard]] std::string string() const;
  // A number, or a string holding one (the venues send both).
  [[nodiscard]] bool number(double& out) const noexcept;
  [[nodiscard]] double number_or(double fallback) const noexcept;
  [[nodiscard]] bool integer(std::int64_t& out) const noexcept;
  [[nodiscard]] std::int64_t integer_or(std::int64_t fallback) const noexcept;
  [[nodiscard]] bool boolean_or(bool fallback) const noexcept;

  class Iterator final {
   public:
    Iterator(const Document* doc, std::uint32_t index) : doc_(doc), index_(index) {}
    Value operator*() const noexcept { return {doc_, index_}; }
    Iterator& operator++() noexcept;
    bool operator!=(const Iterator& other) const noexcept { return index_ != other.index_; }

   private:
    const Document* doc_;
    std::uint32_t index_;
  };
  [[nodiscard]] Iterator begin() const noexcept;
  [[nodiscard]] Iterator end() const noexcept { return {doc_, 0}; }

 private:
  const Document* doc_ = nullptr;
  std::uint32_t index_ = 0;
};

class Document final {
 public:
  Document() { nodes_.reserve(256); }
  // The buffer must outlive every Value read from it.
  bool parse(std::string_view text) noexcept;
  [[nodiscard]] Value root() const noexcept { return ok_ ? Value(this, 1) : Value(); }
  [[nodiscard]] const Node& node(std::uint32_t index) const noexcept { return nodes_[index]; }

 private:
  std::uint32_t parse_value(std::string_view key) noexcept;
  void skip_ws() noexcept;
  bool parse_string_view(std::string_view& out, bool& escaped) noexcept;

  std::vector<Node> nodes_;
  const char* p_ = nullptr;
  const char* end_ = nullptr;
  bool ok_ = false;
  int depth_ = 0;
};

// Appends `text` as a JSON string literal.
void append_string(std::string& out, std::string_view text);
std::string unescape(std::string_view raw);

}  // namespace pkm::json
