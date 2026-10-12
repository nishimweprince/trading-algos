#include <cstdio>
#include <cstring>

#include "sol/constants.hpp"
#include "sol/crypto.hpp"
#include "test.hpp"

int main(int argc, char** argv) {
  pd::crypto_init();
  pd::init_derived();
  const char* only = argc > 1 ? argv[1] : nullptr;
  int ran = 0;
  for (auto& c : pdtest::registry()) {
    if (only && std::strstr(c.name, only) == nullptr) continue;
    int before = pdtest::failures();
    c.fn();
    ++ran;
    std::printf("%s %s\n", pdtest::failures() == before ? "ok  " : "FAIL", c.name);
  }
  std::printf("%d tests, %d failures\n", ran, pdtest::failures());
  return pdtest::failures() == 0 ? 0 : 1;
}
