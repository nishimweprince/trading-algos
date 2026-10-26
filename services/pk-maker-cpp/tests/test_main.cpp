#include <fstream>
#include <iostream>
#include <sstream>

#include "check.hpp"

namespace pkm::test {

std::string read_fixture(const std::string& name) {
  std::ifstream in(fixtures_dir() + "/" + name);
  if (!in) throw std::runtime_error("missing fixture " + name);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

std::map<std::string, std::string> read_vectors(const std::string& name) {
  std::map<std::string, std::string> out;
  std::istringstream in(read_fixture(name));
  std::string line;
  while (std::getline(in, line)) {
    const auto eq = line.find('=');
    if (eq != std::string::npos) out[line.substr(0, eq)] = line.substr(eq + 1);
  }
  return out;
}

}  // namespace pkm::test

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: pkm-tests <fixtures-dir> [test-name]\n";
    return 2;
  }
  pkm::test::fixtures_dir() = argv[1];
  const std::string only = argc > 2 ? argv[2] : "";
  int ran = 0;
  for (const auto& [name, fn] : pkm::test::registry()) {
    if (!only.empty() && name != only) continue;
    try {
      fn();
      ++ran;
    } catch (const std::exception& error) {
      std::cerr << "FAIL " << name << " after " << pkm::test::checks() << " checks: " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << ran << " tests, " << pkm::test::checks() << " checks passed\n";
  return 0;
}
