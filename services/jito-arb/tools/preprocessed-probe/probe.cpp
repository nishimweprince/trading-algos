// Standalone availability gate. No service, recorder, state store or dispatcher linkage.
#include <grpcpp/grpcpp.h>
#include "geyser.grpc.pb.h"
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <thread>
#include <fstream>
#include <vector>
#include <algorithm>

int main() {
  const char* endpoint = std::getenv("PREPROC_ENDPOINT");
  const char* accounts = std::getenv("PREPROC_ACCOUNTS_FILE");
  const char* token = std::getenv("PREPROC_TOKEN");
  if (!endpoint || !token || !*token || !accounts) return 2;
  std::ifstream filter_file(accounts);
  std::vector<std::string> filter;
  for (std::string line; std::getline(filter_file, line);) if (!line.empty()) filter.push_back(line);
  if (filter.empty()) return 2;
  std::string target(endpoint);
  if (target.rfind("https://", 0) == 0) target.erase(0, 8);
  while (!target.empty() && target.back() == '/') target.pop_back();
  if (target.find(':') == std::string::npos) target += ":443";
  auto stub = geyser::Geyser::NewStub(grpc::CreateChannel(target, grpc::SslCredentials({})));
  grpc::ClientContext ctx;
  ctx.AddMetadata("x-token", token);
  ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(60));
  auto stream = stub->SubscribePreprocessed(&ctx);
  geyser::SubscribePreprocessedRequest req;
  auto& f = (*req.mutable_transactions())["probe"];
  f.set_vote(false);
  for (const auto& key : filter) f.add_account_include(key);
  std::vector<int64_t> lag_us;
  const auto start = std::chrono::steady_clock::now();
  bool wrote = stream->Write(req);
  uint64_t transactions = 0, malformed = 0, pings = 0;
  geyser::SubscribePreprocessedUpdate update;
  while (wrote && stream->Read(&update)) {
    if (update.has_transaction()) {
      if (update.has_created_at()) {
        auto now = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        lag_us.push_back(now - (update.created_at().seconds() * 1000000LL + update.created_at().nanos() / 1000));
      }
      const auto& t = update.transaction();
      if (t.transaction().signature().size() == 64 && t.transaction().has_transaction()
          && t.transaction().transaction().has_message()) ++transactions;
      else ++malformed;
    }
    if (update.has_ping()) {
      ++pings;
      geyser::SubscribePreprocessedRequest pong;
      pong.mutable_ping()->set_id(static_cast<int32_t>(pings));
      if (!stream->Write(pong)) break;
    }
  }
  stream->WritesDone();
  const auto status = stream->Finish();
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - start).count();
  // Do not print endpoint, credentials or server error text (may contain metadata).
  const bool success = elapsed >= 59000 && transactions > 0 && malformed == 0
      && status.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED;
  std::sort(lag_us.begin(), lag_us.end());
  auto pct = [&](double p) -> int64_t { return lag_us.empty() ? -1 : lag_us[size_t(p * (lag_us.size() - 1))]; };
  std::cout << "filter_accounts=" << filter.size() << " rate_per_s=" << (elapsed ? transactions * 1000.0 / elapsed : 0)
            << " created_at_lag_us_p50=" << pct(.5) << " created_at_lag_us_p99=" << pct(.99) << ' ';
  std::cout << "probe_success=" << success << " elapsed_ms=" << elapsed
            << " grpc_code=" << status.error_code() << " transactions=" << transactions
            << " malformed=" << malformed << " pings=" << pings << '\n';
  return success ? 0 : 1;
}
