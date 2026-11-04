#include "obs/decision_log.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <cstring>
#include <ctime>

namespace arb::obs {

DecisionLog::~DecisionLog() { close(); }

void DecisionLog::close() {
  if (f_) std::fclose(f_);
  f_ = nullptr;
}

bool DecisionLog::open_path(const std::string& path) {
  close();
  f_ = std::fopen(path.c_str(), "wb");
  if (!f_) return false;
  fixed_ = true;
  path_ = path;
  return std::fwrite(DECISION_MAGIC, 1, 8, f_) == 8;
}

bool DecisionLog::write(const DecisionRecord& r) {
  if (!fixed_) {
    int64_t bucket = r.wall_ms / 1000 / (rotate_s_ > 0 ? rotate_s_ : 3600);
    if (!f_ || bucket != bucket_) {
      close();
      ::mkdir(dir_.c_str(), 0755);
      time_t t = time_t(r.wall_ms / 1000);
      tm g;
      gmtime_r(&t, &g);
      char name[64];
      std::strftime(name, sizeof name, "/decisions-%Y%m%d-%H.bin", &g);
      path_ = dir_ + name;
      bool fresh = ::access(path_.c_str(), F_OK) != 0;
      f_ = std::fopen(path_.c_str(), "ab");
      if (!f_) return false;
      if (fresh && std::fwrite(DECISION_MAGIC, 1, 8, f_) != 8) return false;
      bucket_ = bucket;
    }
  }
  if (!f_ || std::fwrite(&r, sizeof r, 1, f_) != 1) return false;
  ++records_;
  return true;
}

DecisionReader::~DecisionReader() {
  if (f_) std::fclose(f_);
}

bool DecisionReader::open(const std::string& path) {
  f_ = std::fopen(path.c_str(), "rb");
  if (!f_) return false;
  char magic[8];
  return std::fread(magic, 1, 8, f_) == 8 && std::memcmp(magic, DECISION_MAGIC, 8) == 0;
}

bool DecisionReader::next(DecisionRecord& r) { return f_ && std::fread(&r, sizeof r, 1, f_) == 1; }

}  // namespace arb::obs
