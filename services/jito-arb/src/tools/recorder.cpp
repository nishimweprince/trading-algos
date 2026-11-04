// Frame-file utility for the recorder/replay format (src/feed/frames.hpp).
//
//   recorder stat FILE                      count frames by kind, bytes, slot range
//   recorder from-fixtures OUT [ACCOUNTS]   write tests/fixtures/accounts.json as one frame per account
//                                           (a synthetic capture for replay tests)
//
// Live recording is `jito-arb --mode recorder` (needs ARB_WITH_GRPC, a VM step).
#include <simdjson.h>

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "feed/frames.hpp"
#include "feed/update.hpp"

using namespace arb;

int main(int argc, char** argv) {
  if (argc < 3) {
    std::fprintf(stderr, "usage: recorder stat FILE | recorder from-fixtures OUT [ACCOUNTS_JSON]\n");
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "stat") {
    feed::FrameReader r;
    if (!r.open(argv[2])) {
      std::fprintf(stderr, "cannot open %s\n", argv[2]);
      return 1;
    }
    feed::Frame f;
    feed::UpdateView v;
    uint64_t n = 0, bytes = 0, acc = 0, slots = 0, bad = 0, lo = UINT64_MAX, hi = 0;
    while (r.next(f)) {
      ++n;
      bytes += f.data.size();
      if (!feed::parse_update(f.data.data(), f.data.size(), v)) { ++bad; continue; }
      uint64_t s = v.kind == feed::UpdateView::Account ? v.account.slot : v.kind == feed::UpdateView::Slot ? v.slot.slot : 0;
      if (v.kind == feed::UpdateView::Account) ++acc;
      if (v.kind == feed::UpdateView::Slot) ++slots;
      if (s) { lo = std::min(lo, s); hi = std::max(hi, s); }
    }
    std::printf("frames=%llu bytes=%llu accounts=%llu slots=%llu bad=%llu slot_range=%llu..%llu truncated=%d\n",
                (unsigned long long)n, (unsigned long long)bytes, (unsigned long long)acc, (unsigned long long)slots,
                (unsigned long long)bad, (unsigned long long)(lo == UINT64_MAX ? 0 : lo), (unsigned long long)hi, int(r.truncated()));
    return 0;
  }
  if (cmd == "from-fixtures") {
    std::string in = argc > 3 ? argv[3] : "tests/fixtures/accounts.json";
    std::ifstream f(in);
    std::stringstream ss;
    ss << f.rdbuf();
    simdjson::dom::parser p;
    simdjson::padded_string ps(ss.str());
    simdjson::dom::object accs = p.parse(ps)["accounts"];
    feed::FrameWriter w("", 0);
    if (!w.open_path(argv[2])) return 1;
    uint64_t wv = 1, slot_max = 0;
    for (auto kv : accs) {
      if (kv.value.is_null()) continue;
      Pubkey key, owner;
      std::string_view s;
      std::vector<uint8_t> data;
      uint64_t slot = 0, lamports = 0;
      if (!b58_decode32(kv.key, key)) continue;
      if (kv.value["owner"].get(s) != simdjson::SUCCESS || !b58_decode32(s, owner)) continue;
      if (kv.value["data"].get(s) != simdjson::SUCCESS || !b64_decode(s, data)) continue;
      if (kv.value["slot"].get(slot) != simdjson::SUCCESS) slot = 0;
      if (kv.value["lamports"].get(lamports) != simdjson::SUCCESS) lamports = 0;
      slot_max = std::max(slot_max, slot);
      std::string fr = feed::encode_account_frame(key, owner, data.data(), data.size(), lamports, slot, wv++);
      w.write(reinterpret_cast<const uint8_t*>(fr.data()), uint32_t(fr.size()), wv * 1000, 0);
    }
    std::string sf = feed::encode_slot_frame(slot_max);
    w.write(reinterpret_cast<const uint8_t*>(sf.data()), uint32_t(sf.size()), wv * 1000 + 1, 0);
    std::printf("wrote %llu frames to %s\n", (unsigned long long)wv, argv[2]);
    return 0;
  }
  return 2;
}
