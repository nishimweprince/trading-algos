#pragma once

#include <functional>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

// The hand-rolled harness the other C++ services use: `require` throws, a
// counter tracks checks, and each file registers its tests at static init.
namespace pkm::test {

inline int& checks() {
  static int n = 0;
  return n;
}

inline std::string& fixtures_dir() {
  static std::string dir;
  return dir;
}

inline void require(bool condition, std::string_view message) {
  ++checks();
  if (!condition) throw std::runtime_error(std::string(message));
}

template <typename A, typename B>
void require_eq(const A& got, const B& want, std::string_view message) {
  ++checks();
  if (!(got == want)) {
    std::string text(message);
    if constexpr (requires(std::string& s, const A& a, const B& b) { s += a; s += b; }) {
      text += "\n  got:  ";
      text += got;
      text += "\n  want: ";
      text += want;
    } else if constexpr (requires { std::to_string(got); std::to_string(want); }) {
      text += "\n  got:  " + std::to_string(got) + "\n  want: " + std::to_string(want);
    }
    throw std::runtime_error(text);
  }
}

using TestFn = void (*)();
inline std::vector<std::pair<std::string, TestFn>>& registry() {
  static std::vector<std::pair<std::string, TestFn>> tests;
  return tests;
}

struct Register {
  Register(const char* name, TestFn fn) { registry().emplace_back(name, fn); }
};

// Key=value lines (tests/fixtures/clob_vectors.txt).
std::map<std::string, std::string> read_vectors(const std::string& name);
std::string read_fixture(const std::string& name);

}  // namespace pkm::test

#define PKM_TEST(name)                                                  \
  static void name();                                                   \
  static const ::pkm::test::Register name##_registered(#name, &name);   \
  static void name()
