#include <cstdio>

#include "core/crypto.hpp"
#include "test.hpp"

int main(int argc, char** argv) {
  arb::crypto_init();
  const char* only = argc > 1 ? argv[1] : nullptr;
  int ran = 0;
  for (auto& c : arbtest::registry()) {
    if (only && std::string(c.name).find(only) == std::string::npos) continue;
    int before = arbtest::failures();
    c.fn();
    ++ran;
    std::printf("%s %s\n", arbtest::failures() == before ? "ok  " : "FAIL", c.name);
  }
  std::printf("%d tests, %d failed checks\n", ran, arbtest::failures());
  return arbtest::failures() == 0 ? 0 : 1;
}
