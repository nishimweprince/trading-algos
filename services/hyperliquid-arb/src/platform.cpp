#include "hla/platform.hpp"

#include <array>

#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#include <timeapi.h>
#else
#include <poll.h>
#include <pthread.h>
#include <csignal>
#endif

namespace hla::platform {
namespace {

constexpr std::size_t kMaxWait = 8;
std::atomic<std::atomic<bool>*> stop_flag{nullptr};

void request_stop() noexcept {
  if (auto* flag = stop_flag.load()) flag->store(true);
}

#ifdef _WIN32
BOOL WINAPI console_handler(DWORD) {
  request_stop();
  return TRUE;
}
#else
void signal_handler(int) { request_stop(); }
#endif

}  // namespace

bool wait_readable(const SocketHandle* sockets, std::size_t count, int timeout_ms) noexcept {
  if (count > kMaxWait) count = kMaxWait;
  // Zero sockets: timed sleep only. WSAPoll rejects nfds=0 without an active Winsock
  // session; POSIX poll(nfds=0) already sleeps, but keep both paths explicit.
  if (count == 0) {
#ifdef _WIN32
    if (timeout_ms > 0) ::Sleep(static_cast<DWORD>(timeout_ms));
#else
    if (timeout_ms > 0) {
      pollfd unused{};
      ::poll(&unused, 0, timeout_ms);
    }
#endif
    return true;
  }
#ifdef _WIN32
  std::array<WSAPOLLFD, kMaxWait> descriptors{};
  for (std::size_t i = 0; i < count; ++i) descriptors[i] = {static_cast<SOCKET>(sockets[i]), POLLRDNORM, 0};
  return ::WSAPoll(descriptors.data(), static_cast<ULONG>(count), timeout_ms) != SOCKET_ERROR;
#else
  std::array<pollfd, kMaxWait> descriptors{};
  for (std::size_t i = 0; i < count; ++i) descriptors[i] = {static_cast<int>(sockets[i]), POLLIN, 0};
  return ::poll(descriptors.data(), static_cast<nfds_t>(count), timeout_ms) >= 0;
#endif
}

void install_stop_handler(std::atomic<bool>& stop) noexcept {
  stop_flag.store(&stop);
#ifdef _WIN32
  ::SetConsoleCtrlHandler(console_handler, TRUE);
#else
  std::signal(SIGINT, signal_handler);
  std::signal(SIGTERM, signal_handler);
  std::signal(SIGPIPE, SIG_IGN);
#endif
}

bool pin_current_thread(unsigned core) noexcept {
#if defined(_WIN32)
  if (core >= 64) return false;
  return ::SetThreadAffinityMask(::GetCurrentThread(), DWORD_PTR{1} << core) != 0;
#elif defined(__linux__)
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(core, &set);
  return ::pthread_setaffinity_np(::pthread_self(), sizeof(set), &set) == 0;
#else
  (void)core;
  return false;
#endif
}

void raise_timer_resolution() noexcept {
#ifdef _WIN32
  ::timeBeginPeriod(1);
#endif
}

}  // namespace hla::platform
