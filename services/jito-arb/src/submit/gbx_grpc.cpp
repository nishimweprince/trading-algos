// GBX transport over grpc++ (the VM build, ARB_WITH_GRPC=ON): one unary call at a time through the generic stub,
// raw protobuf bytes in and out (encoded by submit/gbx.cpp), as feed/laserstream_client.cpp does for streams.
#include <grpcpp/generic/generic_stub.h>
#include <grpcpp/grpcpp.h>

#include <chrono>

#include "submit/gbx.hpp"

namespace arb::submit {

namespace {

class GrpcGbx final : public GbxTransport {
 public:
  GrpcGbx(const std::string& url, int timeout_ms) : timeout_ms_(timeout_ms) {
    std::string e = url;
    bool tls = true;
    if (e.rfind("https://", 0) == 0) e = e.substr(8);
    else if (e.rfind("http://", 0) == 0) { e = e.substr(7); tls = false; }
    while (!e.empty() && e.back() == '/') e.pop_back();
    if (e.find(':') == std::string::npos) e += tls ? ":443" : ":80";
    grpc::ChannelArguments args;
    args.SetInt(GRPC_ARG_KEEPALIVE_TIME_MS, 10000);
    args.SetInt(GRPC_ARG_KEEPALIVE_TIMEOUT_MS, 5000);
    args.SetInt(GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS, 1);
    args.SetInt(GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA, 0);
    auto creds = tls ? grpc::SslCredentials(grpc::SslCredentialsOptions()) : grpc::InsecureChannelCredentials();
    channel_ = grpc::CreateCustomChannel(e, creds, args);
    stub_ = std::make_unique<grpc::GenericStub>(channel_);
  }

  int call(const std::string& method, const std::string& req, std::string& resp, const std::string& bearer, std::string& err) override {
    grpc::ClientContext ctx;
    ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::milliseconds(timeout_ms_));
    if (!bearer.empty()) ctx.AddMetadata("authorization", "Bearer " + bearer);
    grpc::CompletionQueue cq;
    grpc::Slice s(req.data(), req.size());
    grpc::ByteBuffer in(&s, 1), out;
    auto rpc = stub_->PrepareUnaryCall(&ctx, method, in, &cq);
    rpc->StartCall();
    grpc::Status status;
    rpc->Finish(&out, &status, reinterpret_cast<void*>(1));
    void* tag = nullptr;
    bool ok = false;
    if (!cq.Next(&tag, &ok) || !ok) {
      err = "completion queue failed";
      cq.Shutdown();
      while (cq.Next(&tag, &ok)) {
      }
      return 14;  // UNAVAILABLE
    }
    cq.Shutdown();
    while (cq.Next(&tag, &ok)) {
    }
    if (!status.ok()) {
      err = status.error_message();
      return int(status.error_code());
    }
    resp.clear();
    std::vector<grpc::Slice> slices;
    if (out.Dump(&slices).ok())
      for (const auto& sl : slices) resp.append(reinterpret_cast<const char*>(sl.begin()), sl.size());
    return 0;
  }

 private:
  int timeout_ms_;
  std::shared_ptr<grpc::Channel> channel_;
  std::unique_ptr<grpc::GenericStub> stub_;
};

}  // namespace

std::unique_ptr<GbxTransport> make_gbx_transport(const std::string& url, int timeout_ms) {
  return std::make_unique<GrpcGbx>(url, timeout_ms);
}

}  // namespace arb::submit
