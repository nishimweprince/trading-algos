#include "stream/ingest.hpp"

#include <grpcpp/generic/generic_stub.h>
#include <grpcpp/grpcpp.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <deque>
#include <thread>

#include "aux/writer.hpp"
#include "core/sys.hpp"
#include "stream/request.hpp"

namespace pd {

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

struct Ingest::Impl {
  const Config& cfg;
  Shared& sh;
  FrameRing& ring;
  Logger& log;
  SubscriptionSpec spec;
  std::shared_ptr<grpc::Channel> channel;
  std::unique_ptr<grpc::GenericStub> stub;
  std::vector<grpc::Slice> slices;
  int32_t ping_id = 0;

  Impl(const Config& c, Shared& s, FrameRing& r, const Pubkey& wallet, Logger& l) : cfg(c), sh(s), ring(r), log(l) {
    spec.band_values = c.stream.band_values;
    spec.wallet = wallet;
    slices.reserve(256);
    auto [target, tls] = grpc_target(c.stream.endpoint);
    grpc::ChannelArguments args;
    // Aggressive keepalive keeps the AWS NAT mapping and the LB connection hot.
    args.SetInt(GRPC_ARG_KEEPALIVE_TIME_MS, 10000);
    args.SetInt(GRPC_ARG_KEEPALIVE_TIMEOUT_MS, 5000);
    args.SetInt(GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS, 1);
    args.SetInt(GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA, 0);
    // No internal throttling of large frames.
    args.SetInt(GRPC_ARG_MAX_RECEIVE_MESSAGE_LENGTH, 16 * 1024 * 1024);
    args.SetInt(GRPC_ARG_HTTP2_BDP_PROBE, 1);
    auto creds = tls ? grpc::SslCredentials(grpc::SslCredentialsOptions()) : grpc::InsecureChannelCredentials();
    channel = grpc::CreateCustomChannel(target, creds, args);
    stub = std::make_unique<grpc::GenericStub>(channel);
  }

  /** Apply queued watch adds/removes. True when the subscription changed. */
  bool drain_watches() {
    bool changed = false;
    WatchCmd c;
    while (sh.watch_cmds.try_pop(c)) {
      auto it = std::find(spec.watched.begin(), spec.watched.end(), c.key);
      if (c.add && it == spec.watched.end()) { spec.watched.push_back(c.key); changed = true; }
      if (!c.add && it != spec.watched.end()) { *it = spec.watched.back(); spec.watched.pop_back(); changed = true; }
    }
    return changed;
  }

  void publish(grpc::ByteBuffer& buf) {
    uint64_t recv = now_ns();
    size_t len = buf.Length();
    sh.counters.frames.fetch_add(1, std::memory_order_relaxed);
    sh.counters.frame_bytes.fetch_add(len, std::memory_order_relaxed);
    if (len > ring.slot_bytes()) {
      sh.counters.oversize_frames.fetch_add(1, std::memory_order_relaxed);
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

  /** One stream lifetime. Returns when it ends for any reason. */
  void session() {
    grpc::ClientContext ctx;
    if (!cfg.stream.token.empty()) ctx.AddMetadata("x-token", cfg.stream.token);
    grpc::CompletionQueue cq;
    std::unique_ptr<grpc::GenericClientAsyncReaderWriter> call =
        stub->PrepareCall(&ctx, "/geyser.Geyser/Subscribe", &cq);
    call->StartCall(tag(START));

    grpc::ByteBuffer rbuf, wbuf;
    grpc::Status status;
    std::deque<std::string> outq;
    drain_watches();
    outq.push_back(encode_subscribe_request(spec));
    // started: START completed ok. start_done: START completed at all (Finish is legal after it).
    bool started = false, start_done = false, reading = false, writing = false;
    bool finishing = false, finish_called = false, finished = false;
    uint64_t last_frame = now_ns(), next_ping = now_ns() + uint64_t(cfg.stream.ping_interval_ms) * 1'000'000ULL;
    uint64_t next_watch_write = 0;

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
        log.warn(std::string("stream ending: ") + why);
        ctx.TryCancel();
      }
    };

    while (!finished) {
      void* t = nullptr;
      bool ok = false;
      auto deadline = std::chrono::system_clock::now() + std::chrono::microseconds(250);
      auto st = cq.AsyncNext(&t, &ok, deadline);
      if (st == grpc::CompletionQueue::SHUTDOWN) break;
      if (st == grpc::CompletionQueue::GOT_EVENT) {
        switch (reinterpret_cast<intptr_t>(t)) {
          case START:
            start_done = true;
            if (!ok) { begin_finish("start failed"); break; }
            started = true;
            sh.counters.stream_up.store(true);
            log.info("stream subscribed (" + std::to_string(spec.watched.size()) + " watched accounts)");
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
            log.warn("stream finished: " + std::to_string(int(status.error_code())) + " " + status.error_message());
            break;
        }
      }
      uint64_t now = now_ns();
      if (sh.stop.load(std::memory_order_relaxed)) begin_finish("shutdown");
      if (!finishing && started) {
        if (now >= next_watch_write && drain_watches()) {
          // Yellowstone replaces the whole subscription on each write; coalesce bursts to 1/ms.
          outq.push_back(encode_subscribe_request(spec));
          next_watch_write = now + 1'000'000ULL;
          start_write();
        }
        if (now >= next_ping) {
          next_ping = now + uint64_t(cfg.stream.ping_interval_ms) * 1'000'000ULL;
          outq.push_back(encode_ping_request(++ping_id));
          start_write();
        }
        if (now - last_frame > uint64_t(cfg.stream.silence_reconnect_ms) * 1'000'000ULL) begin_finish("silence");
      }
      // Finish exactly once, after START completed and no read/write is outstanding.
      if (finishing && start_done && !reading && !writing && !finish_called) {
        call->Finish(&status, tag(FINISH));
        finish_called = true;
      }
    }
    sh.counters.stream_up.store(false);
    cq.Shutdown();
    void* t;
    bool ok;
    while (cq.Next(&t, &ok)) {
    }
  }
};

Ingest::Ingest(const Config& cfg, Shared& sh, FrameRing& ring, const Pubkey& wallet, Logger& log)
    : impl_(std::make_unique<Impl>(cfg, sh, ring, wallet, log)) {}

Ingest::~Ingest() = default;

void Ingest::run() {
  int backoff = impl_->cfg.stream.reconnect_min_ms;
  while (!impl_->sh.stop.load()) {
    uint64_t started = now_ns();
    impl_->session();
    if (impl_->sh.stop.load()) break;
    impl_->sh.counters.reconnects.fetch_add(1);
    // A session that lived a while resets the backoff.
    if (now_ns() - started > 60'000'000'000ULL) backoff = impl_->cfg.stream.reconnect_min_ms;
    std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
    backoff = std::min(backoff * 2, impl_->cfg.stream.reconnect_max_ms);
  }
}

}  // namespace pd
