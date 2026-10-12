#pragma once
// Minimal test harness (no external test framework to install on the VM).
#include <cstdio>
#include <functional>
#include <string>
#include <vector>

namespace pdtest {

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

}  // namespace pdtest

#define PD_CAT2(a, b) a##b
#define PD_CAT(a, b) PD_CAT2(a, b)
#define TEST(name)                                                              \
  static void name();                                                           \
  static ::pdtest::Registrar PD_CAT(reg_, name)(#name, name);                   \
  static void name()

#define CHECK(cond)                                                                       \
  do {                                                                                    \
    if (!(cond)) {                                                                        \
      std::fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);              \
      ++::pdtest::failures();                                                             \
    }                                                                                     \
  } while (0)

#define CHECK_EQ(a, b)                                                                                     \
  do {                                                                                                     \
    auto _va = (a);                                                                                        \
    auto _vb = (b);                                                                                        \
    if (!(_va == _vb)) {                                                                                   \
      std::fprintf(stderr, "  FAIL %s:%d: %s == %s\n    lhs: %s\n    rhs: %s\n", __FILE__, __LINE__, #a, #b, \
                   ::pdtest::show(_va).c_str(), ::pdtest::show(_vb).c_str());                              \
      ++::pdtest::failures();                                                                              \
    }                                                                                                      \
  } while (0)

namespace pdtest {
inline std::string show(const std::string& s) { return s; }
inline std::string show(const char* s) { return s; }
inline std::string show(bool b) { return b ? "true" : "false"; }
template <typename T>
std::string show(const T& v) {
  if constexpr (std::is_arithmetic_v<T>) return std::to_string(v);
  else return "<value>";
}
}  // namespace pdtest
