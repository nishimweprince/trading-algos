#include "feed/frames.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <cstring>
#include <ctime>

namespace arb::feed {

FrameWriter::~FrameWriter() { close(); }

void FrameWriter::close() {
  if (f_) std::fclose(f_);
  f_ = nullptr;
}

bool FrameWriter::open_path(const std::string& path) {
  close();
  f_ = std::fopen(path.c_str(), "wb");
  if (!f_) return false;
  fixed_ = true;
  path_ = path;
  return std::fwrite(FRAME_MAGIC, 1, 8, f_) == 8;
}

bool FrameWriter::write(const uint8_t* data, uint32_t len, uint64_t recv_ns, int64_t wall_ms) {
  if (!fixed_) {
    int64_t bucket = wall_ms / 1000 / (rotate_s_ > 0 ? rotate_s_ : 3600);
    if (!f_ || bucket != bucket_) {
      close();
      ::mkdir(dir_.c_str(), 0755);
      time_t t = time_t(wall_ms / 1000);
      tm g;
      gmtime_r(&t, &g);
      char name[64];
      std::strftime(name, sizeof name, "/frames-%Y%m%d-%H%M.bin", &g);
      path_ = dir_ + name;
      bool fresh = ::access(path_.c_str(), F_OK) != 0;
      f_ = std::fopen(path_.c_str(), "ab");
      if (!f_) return false;
      if (fresh && std::fwrite(FRAME_MAGIC, 1, 8, f_) != 8) return false;
      bucket_ = bucket;
    }
  }
  if (!f_) return false;
  uint8_t hdr[20];
  std::memcpy(hdr, &len, 4);
  std::memcpy(hdr + 4, &recv_ns, 8);
  std::memcpy(hdr + 12, &wall_ms, 8);
  if (std::fwrite(hdr, 1, 20, f_) != 20 || std::fwrite(data, 1, len, f_) != len) return false;
  bytes_ += 20 + len;
  return true;
}

FrameReader::~FrameReader() {
  if (f_) std::fclose(f_);
}

bool FrameReader::open(const std::string& path) {
  f_ = std::fopen(path.c_str(), "rb");
  if (!f_) return false;
  char magic[8];
  return std::fread(magic, 1, 8, f_) == 8 && std::memcmp(magic, FRAME_MAGIC, 8) == 0;
}

bool FrameReader::next(Frame& out) {
  if (!f_) return false;
  uint8_t hdr[20];
  size_t n = std::fread(hdr, 1, 20, f_);
  if (n == 0) return false;
  if (n != 20) { truncated_ = true; return false; }
  uint32_t len;
  std::memcpy(&len, hdr, 4);
  std::memcpy(&out.recv_ns, hdr + 4, 8);
  std::memcpy(&out.wall_ms, hdr + 12, 8);
  if (len > (64u << 20)) { truncated_ = true; return false; }
  out.data.resize(len);
  if (std::fread(out.data.data(), 1, len, f_) != len) { truncated_ = true; return false; }
  return true;
}

}  // namespace arb::feed
