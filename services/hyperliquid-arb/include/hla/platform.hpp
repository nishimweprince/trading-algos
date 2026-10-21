#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

// The few OS calls that differ between the Windows box (MSYS2 CLANG64) and the
// macOS/Linux development hosts. Everything else is portable C++20 + libcurl.
namespace hla::platform {

// A curl_socket_t widened so it holds a Windows SOCKET as well as a POSIX fd.
using SocketHandle = std::intptr_t;

// Waits until any socket is readable or `timeout_ms` passes (poll / WSAPoll).
// Returns false on a poll error.
bool wait_readable(const SocketHandle* sockets, std::size_t count, int timeout_ms) noexcept;

// Ctrl-C / SIGTERM / console close set `stop`. SIGPIPE is ignored on POSIX.
void install_stop_handler(std::atomic<bool>& stop) noexcept;

// Pins the calling thread to one core. False where unsupported (macOS) or on failure.
bool pin_current_thread(unsigned core) noexcept;

// 1 ms scheduler and timer granularity on Windows (timeBeginPeriod); no-op elsewhere.
void raise_timer_resolution() noexcept;

}  // namespace hla::platform
