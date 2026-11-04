#pragma once
// Recorder file format: raw SubscribeUpdate frames with receive timestamps.
//   header: "ARBFRM01" (8 bytes)
//   record: u32 len | u64 recv_mono_ns | i64 wall_ms | len bytes of SubscribeUpdate
// Files rotate hourly: <dir>/frames-YYYYMMDD-HH.bin. Replay reads the same records
// and feeds them through the same StateStore path as the live feed.
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace arb::feed {

constexpr char FRAME_MAGIC[8] = {'A', 'R', 'B', 'F', 'R', 'M', '0', '1'};

class FrameWriter {
 public:
  FrameWriter(std::string dir, int rotate_s) : dir_(std::move(dir)), rotate_s_(rotate_s) {}
  ~FrameWriter();
  /** Append one frame; opens/rotates the file by wall time. False on an I/O error. */
  bool write(const uint8_t* data, uint32_t len, uint64_t recv_ns, int64_t wall_ms);
  /** Write to exactly this path (tests, fixture conversion); no rotation. */
  bool open_path(const std::string& path);
  void close();
  uint64_t bytes_written() const { return bytes_; }
  const std::string& current_path() const { return path_; }

 private:
  std::string dir_;
  int rotate_s_;
  FILE* f_ = nullptr;
  std::string path_;
  int64_t bucket_ = -1;
  bool fixed_ = false;
  uint64_t bytes_ = 0;
};

struct Frame {
  std::vector<uint8_t> data;
  uint64_t recv_ns = 0;
  int64_t wall_ms = 0;
};

class FrameReader {
 public:
  bool open(const std::string& path);
  /** Next record; false at end of file or on a truncated record (counted in truncated()). */
  bool next(Frame& out);
  bool truncated() const { return truncated_; }
  ~FrameReader();

 private:
  FILE* f_ = nullptr;
  bool truncated_ = false;
};

}  // namespace arb::feed
