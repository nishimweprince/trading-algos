// Stand-in LaserstreamClient for builds without grpc++ (the laptop): it accepts a subscription and yields
// no frames, so live and recorder modes start, serve metrics and stop cleanly. The real client is
// laserstream_client.cpp (ARB_WITH_GRPC=ON on the VM).
#include <chrono>
#include <thread>

#include "feed/laserstream_client.hpp"

namespace arb::feed {

struct LaserstreamClient::Impl {
  std::atomic<bool>& stop;
  StreamCounters counters;
  explicit Impl(std::atomic<bool>& s) : stop(s) {}
};

LaserstreamClient::LaserstreamClient(const Config&, std::string, FrameRing&, std::atomic<bool>& stop)
    : impl_(std::make_unique<Impl>(stop)) {}
LaserstreamClient::~LaserstreamClient() = default;
void LaserstreamClient::set_spec(const ShardSpec&) {}
void LaserstreamClient::run() {
  while (!impl_->stop) std::this_thread::sleep_for(std::chrono::milliseconds(50));
}
StreamCounters& LaserstreamClient::counters() { return impl_->counters; }

}  // namespace arb::feed
