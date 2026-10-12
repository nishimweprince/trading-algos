#pragma once
// Clocks, core pinning, memory locking.
#include <cstdint>
#include <ctime>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#endif

namespace pd {

/** Monotonic nanoseconds (vDSO, ~20 ns). The latency clock for every hot-path stamp. */
inline uint64_t now_ns() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return uint64_t(ts.tv_sec) * 1'000'000'000ULL + uint64_t(ts.tv_nsec);
}

/** Wall clock, milliseconds since the epoch (records and reports). */
inline int64_t wall_ms() {
  timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return int64_t(ts.tv_sec) * 1000 + ts.tv_nsec / 1'000'000;
}

/** Pin the calling thread to one CPU. Returns false off Linux or on failure (logged by caller). */
inline bool pin_thread(int cpu) {
#if defined(__linux__)
  if (cpu < 0) return true;
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(cpu, &set);
  return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
#else
  (void)cpu;
  return false;
#endif
}

/** SCHED_FIFO for the calling thread (needs CAP_SYS_NICE). 0 = leave the default policy. */
inline bool set_fifo(int priority) {
#if defined(__linux__)
  if (priority <= 0) return true;
  sched_param p{};
  p.sched_priority = priority;
  return pthread_setschedparam(pthread_self(), SCHED_FIFO, &p) == 0;
#else
  (void)priority;
  return false;
#endif
}

inline void name_thread(const char* name) {
#if defined(__linux__)
  pthread_setname_np(pthread_self(), name);
#else
  (void)name;
#endif
}

/** Lock current and future pages so the hot path never page-faults. */
inline bool lock_memory() {
#if defined(__linux__)
  return mlockall(MCL_CURRENT | MCL_FUTURE) == 0;
#else
  return false;
#endif
}

}  // namespace pd
