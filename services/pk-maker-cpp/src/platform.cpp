#include "pkm/platform.hpp"

#include <pthread.h>
#include <sched.h>
#include <unistd.h>

#include <csignal>
#include <cstdio>
#include <ctime>
#include <string>

namespace pkm::platform {
namespace {

std::atomic<bool>* g_stop = nullptr;

void on_signal(int) {
  if (g_stop != nullptr) g_stop->store(true);
}

}  // namespace

bool pin_current_thread(unsigned core) noexcept {
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(core, &set);
  return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
}

void install_stop_handler(std::atomic<bool>& stop) noexcept {
  g_stop = &stop;
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);
  std::signal(SIGPIPE, SIG_IGN);
}

void log(std::string_view text) noexcept {
  timespec ts{};
  clock_gettime(CLOCK_REALTIME, &ts);
  std::tm tm{};
  gmtime_r(&ts.tv_sec, &tm);
  char stamp[40];
  const auto n = std::strftime(stamp, sizeof(stamp), "%Y-%m-%dT%H:%M:%S", &tm);
  std::snprintf(stamp + n, sizeof(stamp) - n, ".%03ldZ", ts.tv_nsec / 1'000'000);
  std::string line = "[pkm ";
  line.append(stamp).append("] ").append(text).push_back('\n');
  if (::write(STDERR_FILENO, line.data(), line.size()) < 0) return;
}

}  // namespace pkm::platform
