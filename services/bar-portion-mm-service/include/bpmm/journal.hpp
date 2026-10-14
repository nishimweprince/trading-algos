#pragma once

#include "bpmm/parser.hpp"
#include "bpmm/shadow.hpp"

#include <fstream>
#include <string>
#include <string_view>

namespace bpmm {

class Engine;

class Journal final {
 public:
  explicit Journal(const std::string& path);
  bool snapshot(const Snapshot& snapshot, std::uint64_t receive_ns);
  bool raw(std::uint64_t receive_ns, std::string_view frame);
  bool depth(const DepthEvent& event);
  bool trade(const TradeEvent& event);
  bool candle(const CandleEvent& event);
  bool mark_price(const MarkPriceEvent& event);
  bool intent(const QuoteIntent& intent);
  bool fill(const ShadowFill& fill);
  void flush() { stream_.flush(); }
  [[nodiscard]] bool good() const { return stream_.good(); }

 private:
  std::ofstream stream_;
};

bool replay_normalized(const std::string& path, Engine& engine, std::uint64_t& final_hash,
                       std::string& error);
bool gapcheck(const std::string& path, std::string& error);

}  // namespace bpmm
