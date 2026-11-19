// Stand-in GBX transport for builds without grpc++ (the laptop): every call fails UNAVAILABLE, so the client
// starts, reports itself unauthenticated, and sends nothing. The real transport is gbx_grpc.cpp.
#include "submit/gbx.hpp"

namespace arb::submit {

namespace {
class NoGbx final : public GbxTransport {
 public:
  int call(const std::string&, const std::string&, std::string&, const std::string&, std::string& err) override {
    err = "built without grpc++";
    return 14;
  }
};
}  // namespace

std::unique_ptr<GbxTransport> make_gbx_transport(const std::string&, int) { return std::make_unique<NoGbx>(); }

}  // namespace arb::submit
