#pragma once

#include "bpmm/parser.hpp"

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

namespace bpmm {

class BinancePublicClient final {
 public:
  using FrameHandler = std::function<bool(std::uint64_t, std::string_view)>;
  using SnapshotHandler = std::function<bool(std::uint64_t, const Snapshot&)>;

  BinancePublicClient(std::string symbol, SnapshotHandler snapshot_handler, FrameHandler handler);
  ~BinancePublicClient();
  BinancePublicClient(const BinancePublicClient&) = delete;
  BinancePublicClient& operator=(const BinancePublicClient&) = delete;

  bool fetch_snapshot(Snapshot& snapshot, std::string& error);
  bool run(std::atomic<bool>& stop, std::string& error);

 private:
  std::string symbol_;
  std::string lower_symbol_;
  SnapshotHandler snapshot_handler_;
  FrameHandler handler_;
};

}  // namespace bpmm
