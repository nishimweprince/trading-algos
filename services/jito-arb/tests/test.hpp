#pragma once
// Minimal test harness (no external test framework to install on the VM).
#include <cstdio>
#include <functional>
#include <string>
#include <type_traits>
#include <vector>

namespace arbtest {

struct Case {
  const char* name;
  std::function<void()> fn;
};

inline std::vector<Case>& registry() {
  static std::vector<Case> r;
  return r;
}
inline int& failures() {
  static int f = 0;
  return f;
}

struct Registrar {
  Registrar(const char* name, std::function<void()> fn) { registry().push_back({name, std::move(fn)}); }
};

}  // namespace arbtest

#define ARB_CAT2(a, b) a##b
#define ARB_CAT(a, b) ARB_CAT2(a, b)
#define TEST(name)                                                              \
  static void name();                                                           \
  static ::arbtest::Registrar ARB_CAT(reg_, name)(#name, name);                   \
  static void name()

#define CHECK(cond)                                                                       \
  do {                                                                                    \
    if (!(cond)) {                                                                        \
      std::fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);              \
      ++::arbtest::failures();                                                             \
    }                                                                                     \
  } while (0)

#define CHECK_EQ(a, b)                                                                                     \
  do {                                                                                                     \
    auto _va = (a);                                                                                        \
    auto _vb = (b);                                                                                        \
    if (!(_va == _vb)) {                                                                                   \
      std::fprintf(stderr, "  FAIL %s:%d: %s == %s\n    lhs: %s\n    rhs: %s\n", __FILE__, __LINE__, #a, #b, \
                   ::arbtest::show(_va).c_str(), ::arbtest::show(_vb).c_str());                              \
      ++::arbtest::failures();                                                                              \
    }                                                                                                      \
  } while (0)

namespace arbtest {
inline std::string show(const std::string& s) { return s; }
inline std::string show(const char* s) { return s; }
inline std::string show(bool b) { return b ? "true" : "false"; }
inline std::string show(unsigned __int128 v) {
  if (v == 0) return "0";
  std::string s;
  while (v) { s.insert(s.begin(), char('0' + int(v % 10))); v /= 10; }
  return s;
}
inline std::string show(__int128 v) { return v < 0 ? "-" + show((unsigned __int128)(-v)) : show((unsigned __int128)v); }
template <typename T>
std::string show(const T& v) {
  if constexpr (std::is_arithmetic_v<T>) return std::to_string(v);
  else return "<value>";
}
}  // namespace arbtest
