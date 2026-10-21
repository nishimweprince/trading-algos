#include "hla/bridge_layout.hpp"

#include <chrono>
#include <string>

#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace hla::bridge {
namespace {

#ifndef _WIN32
std::string posix_name(const char* name) {
  std::string text(name);
  const auto slash = text.find_last_of('\\');
  if (slash != std::string::npos) text = text.substr(slash + 1);
  return "/" + text;
}
#endif

}  // namespace

std::uint64_t wall_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

SharedMemory::~SharedMemory() { close(); }

#ifdef _WIN32

bool SharedMemory::create(const char* name, std::size_t size) noexcept {
  close();
  auto* mapping = ::CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                       static_cast<DWORD>(static_cast<std::uint64_t>(size) >> 32),
                                       static_cast<DWORD>(size & 0xFFFFFFFFULL), name);
  if (mapping == nullptr) return false;
  auto* view = ::MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, size);
  if (view == nullptr) {
    ::CloseHandle(mapping);
    return false;
  }
  handle_ = mapping;
  data_ = view;
  size_ = size;
  return true;
}

bool SharedMemory::open(const char* name, std::size_t size) noexcept {
  close();
  auto* mapping = ::OpenFileMappingA(FILE_MAP_ALL_ACCESS, FALSE, name);
  if (mapping == nullptr) return false;
  auto* view = ::MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, size);
  if (view == nullptr) {
    ::CloseHandle(mapping);
    return false;
  }
  handle_ = mapping;
  data_ = view;
  size_ = size;
  return true;
}

void SharedMemory::remove(const char*) noexcept {}

void SharedMemory::close() noexcept {
  if (data_ != nullptr) ::UnmapViewOfFile(data_);
  if (handle_ != nullptr) ::CloseHandle(static_cast<HANDLE>(handle_));
  data_ = nullptr;
  handle_ = nullptr;
}

#else

bool SharedMemory::create(const char* name, std::size_t size) noexcept {
  close();
  const auto path = posix_name(name);
  const int fd = ::shm_open(path.c_str(), O_CREAT | O_RDWR, 0600);
  if (fd < 0) return false;
  struct stat info {};
  if (::fstat(fd, &info) != 0 || (static_cast<std::size_t>(info.st_size) < size &&
                                  ::ftruncate(fd, static_cast<off_t>(size)) != 0)) {
    ::close(fd);
    return false;
  }
  void* view = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  ::close(fd);
  if (view == MAP_FAILED) return false;
  data_ = view;
  size_ = size;
  return true;
}

bool SharedMemory::open(const char* name, std::size_t size) noexcept {
  close();
  const auto path = posix_name(name);
  const int fd = ::shm_open(path.c_str(), O_RDWR, 0600);
  if (fd < 0) return false;
  void* view = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  ::close(fd);
  if (view == MAP_FAILED) return false;
  data_ = view;
  size_ = size;
  return true;
}

void SharedMemory::remove(const char* name) noexcept { ::shm_unlink(posix_name(name).c_str()); }

void SharedMemory::close() noexcept {
  if (data_ != nullptr) ::munmap(data_, size_);
  data_ = nullptr;
}

#endif

}  // namespace hla::bridge
