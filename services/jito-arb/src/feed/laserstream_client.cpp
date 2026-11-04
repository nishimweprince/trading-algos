// Compiled only with ARB_WITH_GRPC=ON (VM). Not built or exercised on the laptop.
#include "feed/laserstream_client.hpp"

#include <grpcpp/generic/generic_stub.h>
#include <grpcpp/grpcpp.h>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <thread>

#include "core/sys.hpp"
#include "obs/log.hpp"

namespace arb::feed {

namespace {

enum Tag : intptr_t { START = 1, READ = 2, WRITE = 3, FINISH = 4 };
void* tag(Tag t) { return reinterpret_cast<void*>(t); }

/** "https://host[:port]" -> ("host:port", tls). */
std::pair<std::string, bool> grpc_target(const std::string& endpoint) {
  std::string e = endpoint;
  bool tls = true;
  if (e.rfind("https://", 0) == 0) e = e.substr(8);
  else if (e.rfind("http://", 0) == 0) { e = e.substr(7); tls = false; }
  while (!e.empty() && e.back() == '/') e.pop_back();
  if (e.find(':') == std::string::npos) e += tls ? ":443" : ":80";
  return {e, tls};
}

}  // namespace

struct LaserstreamClient::Impl {
  const Config& cfg;
  std::string name;
  FrameRing& ring;
  std::atomic<bool>& stop;
  StreamCounters counters;
  std::mutex mu;
  ShardSpec spec;
  bool spec_dirty = true;
  std::string token;
  std::shared_ptr<grpc::Channel> channel;
  std::unique_ptr<grpc::GenericStub> stub;
  std::vector<grpc::Slice> slices;
  int32_t ping_id = 0;

  Impl(const Config& c, std::string n, FrameRing& r, std::atomic<bool>& s) : cfg(c), name(std::move(n)), ring(r), stop(s) {
    if (const char* k = std::getenv(c.helius.api_key_env.c_str())) token = k;
    auto [target, tls] = grpc_target(c.helius.laserstream_endpoint);
    grpc::ChannelArguments args;
    args.SetInt(GRPC_ARG_KEEPALIVE_TIME_MS, 10000);
    args.SetInt(GRPC_ARG_KEEPALIVE_TIMEOUT_MS, 5000);
    args.SetInt(GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS, 1);
    args.SetInt(GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA, 0);
    args.SetInt(GRPC_ARG_MAX_RECEIVE_MESSAGE_LENGTH, 64 * 1024 * 1024);
    args.SetInt(GRPC_ARG_HTTP2_BDP_PROBE, 1);
    auto creds = tls ? grpc::SslCredentials(grpc::SslCredentialsOptions()) : grpc::InsecureChannelCredentials();
    channel = grpc::CreateCustomChannel(target, creds, args);
    stub = std::make_unique<grpc::GenericStub>(channel);
  }

  std::string request(bool reconnect) {
    std::lock_guard<std::mutex> g(mu);
    ShardSpec s = spec;
    if (reconnect && cfg.helius.replay_from_slot && counters.last_slot.load()) s.from_slot = counters.last_slot.load();
    spec_dirty = false;
    return encode_subscribe_request(s);
  }

  void publish(grpc::ByteBuffer& buf) {
    uint64_t recv = now_ns();
    size_t len = buf.Length();
    counters.frames.fetch_add(1, std::memory_order_relaxed);
    counters.bytes.fetch_add(len, std::memory_order_relaxed);
    if (len > ring.slot_bytes()) {
      counters.oversize.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    uint64_t seq;
    uint8_t* dst = ring.claim(seq);
    grpc::Slice single;
    if (buf.TrySingleSlice(&single).ok()) {
      std::memcpy(dst, single.begin(), single.size());
    } else {
      buf.Dump(&slices);
      size_t off = 0;
      for (auto& s : slices) {
        std::memcpy(dst + off, s.begin(), s.size());
        off += s.size();
      }
    }
    ring.publish(seq, uint32_t(len), recv);
  }

  void session(bool reconnect) {
    grpc::ClientContext ctx;
    if (!token.empty()) ctx.AddMetadata("x-token", token);
    grpc::CompletionQueue cq;
    auto call = stub->PrepareCall(&ctx, "/geyser.Geyser/Subscribe", &cq);
    call->StartCall(tag(START));
    grpc::ByteBuffer rbuf, wbuf;
    grpc::Status status;
    std::deque<std::string> outq;
    outq.push_back(request(reconnect));
    bool started = false, start_done = false, reading = false, writing = false;
    bool finishing = false, finish_called = false, finished = false;
    uint64_t last_frame = now_ns(), next_ping = now_ns() + uint64_t(cfg.helius.ping_interval_ms) * 1'000'000ULL;
    auto start_write = [&]() {
      if (writing || outq.empty() || !started || finishing) return;
      grpc::Slice s(outq.front().data(), outq.front().size());
      wbuf = grpc::ByteBuffer(&s, 1);
      call->Write(wbuf, tag(WRITE));
      writing = true;
    };
    auto start_read = [&]() {
      if (reading || finishing) return;
      call->Read(&rbuf, tag(READ));
      reading = true;
    };
    auto begin_finish = [&](const char* why) {
      if (!finishing) {
        finishing = true;
        LOG_WARN("laserstream %s ending: %s", name.c_str(), why);
        ctx.TryCancel();
      }
    };
    while (!finished) {
      void* t = nullptr;
      bool ok = false;
      auto st = cq.AsyncNext(&t, &ok, std::chrono::system_clock::now() + std::chrono::microseconds(250));
      if (st == grpc::CompletionQueue::SHUTDOWN) break;
      if (st == grpc::CompletionQueue::GOT_EVENT) {
        switch (reinterpret_cast<intptr_t>(t)) {
          case START:
            start_done = true;
            if (!ok) { begin_finish("start failed"); break; }
            started = true;
            counters.up.store(true);
            LOG_INFO("laserstream %s subscribed", name.c_str());
            start_write();
            start_read();
            break;
          case READ:
            reading = false;
            if (!ok) { begin_finish("read failed"); break; }
            last_frame = now_ns();
            publish(rbuf);
            start_read();
            break;
          case WRITE:
            writing = false;
            if (!ok) { begin_finish("write failed"); break; }
            outq.pop_front();
            start_write();
            break;
          case FINISH:
            finished = true;
            LOG_WARN("laserstream %s finished: %d %s", name.c_str(), int(status.error_code()), status.error_message().c_str());
            break;
        }
      }
      uint64_t now = now_ns();
      if (stop.load(std::memory_order_relaxed)) begin_finish("shutdown");
      if (!finishing && started) {
        bool dirty;
        {
          std::lock_guard<std::mutex> g(mu);
          dirty = spec_dirty;
        }
        if (dirty) {
          outq.push_back(request(false));
          start_write();
        }
        if (now >= next_ping) {
          next_ping = now + uint64_t(cfg.helius.ping_interval_ms) * 1'000'000ULL;
          outq.push_back(encode_ping_request(++ping_id));
          start_write();
        }
        if (now - last_frame > 30'000'000'000ULL) begin_finish("silence");
      }
      if (finishing && start_done && !reading && !writing && !finish_called) {
        call->Finish(&status, tag(FINISH));
        finish_called = true;
      }
    }
    counters.up.store(false);
    cq.Shutdown();
    void* t;
    bool ok;
    while (cq.Next(&t, &ok)) {
    }
  }
};

LaserstreamClient::LaserstreamClient(const Config& cfg, std::string name, FrameRing& ring, std::atomic<bool>& stop)
    : impl_(std::make_unique<Impl>(cfg, std::move(name), ring, stop)) {}
LaserstreamClient::~LaserstreamClient() = default;

void LaserstreamClient::set_spec(const ShardSpec& spec) {
  std::lock_guard<std::mutex> g(impl_->mu);
  impl_->spec = spec;
  impl_->spec_dirty = true;
}

StreamCounters& LaserstreamClient::counters() { return impl_->counters; }

void LaserstreamClient::run() {
  int backoff = impl_->cfg.helius.reconnect_min_ms;
  bool reconnect = false;
  while (!impl_->stop.load()) {
    uint64_t started = now_ns();
    impl_->session(reconnect);
    if (impl_->stop.load()) break;
    reconnect = true;
    impl_->counters.reconnects.fetch_add(1);
    if (now_ns() - started > 60'000'000'000ULL) backoff = impl_->cfg.helius.reconnect_min_ms;
    std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
    backoff = std::min(backoff * 2, impl_->cfg.helius.reconnect_max_ms);
  }
}

}  // namespace arb::feed
