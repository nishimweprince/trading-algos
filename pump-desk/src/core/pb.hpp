#pragma once
// Protobuf wire format, by hand. The reader is zero-copy (string_view slices of
// the frame) and never allocates, so the logic thread decodes Yellowstone
// updates straight out of the ingest ring. The writer builds the (rare)
// SubscribeRequest and may allocate.
#include <cstdint>
#include <string>
#include <string_view>

namespace pd::pb {

enum WireType : uint32_t { VARINT = 0, I64 = 1, LEN = 2, I32 = 5 };

struct Reader {
  const uint8_t* p;
  const uint8_t* end;
  bool ok = true;

  Reader(const uint8_t* data, size_t len) : p(data), end(data + len) {}
  explicit Reader(std::string_view s)
      : p(reinterpret_cast<const uint8_t*>(s.data())), end(reinterpret_cast<const uint8_t*>(s.data()) + s.size()) {}

  bool more() const { return ok && p < end; }

  uint64_t varint() {
    uint64_t v = 0;
    for (int shift = 0; shift < 64; shift += 7) {
      if (p >= end) { ok = false; return 0; }
      uint8_t b = *p++;
      v |= uint64_t(b & 0x7f) << shift;
      if (!(b & 0x80)) return v;
    }
    ok = false;
    return 0;
  }

  /** Next tag. False at end or on malformed input. */
  bool next(uint32_t& field, uint32_t& wt) {
    if (!more()) return false;
    uint64_t tag = varint();
    if (!ok) return false;
    field = static_cast<uint32_t>(tag >> 3);
    wt = static_cast<uint32_t>(tag & 7);
    return field != 0;
  }

  std::string_view bytes() {
    uint64_t n = varint();
    if (!ok || n > static_cast<uint64_t>(end - p)) { ok = false; return {}; }
    std::string_view s(reinterpret_cast<const char*>(p), n);
    p += n;
    return s;
  }

  uint64_t fixed64() {
    if (end - p < 8) { ok = false; return 0; }
    uint64_t v;
    __builtin_memcpy(&v, p, 8);
    p += 8;
    return v;
  }

  void skip(uint32_t wt) {
    switch (wt) {
      case VARINT: varint(); break;
      case I64: if (end - p < 8) ok = false; else p += 8; break;
      case LEN: bytes(); break;
      case I32: if (end - p < 4) ok = false; else p += 4; break;
      default: ok = false;
    }
  }
};

inline const uint8_t* u8(std::string_view s) { return reinterpret_cast<const uint8_t*>(s.data()); }

/** Iterate packed or unpacked repeated varints (both encodings are legal). */
template <typename F>
inline void each_varint(Reader& r, uint32_t wt, F&& f) {
  if (wt == LEN) {
    std::string_view s = r.bytes();
    Reader in(s);
    while (in.more()) f(in.varint());
  } else {
    f(r.varint());
  }
}

struct Writer {
  std::string out;

  void varint(uint64_t v) {
    while (v >= 0x80) { out.push_back(static_cast<char>((v & 0x7f) | 0x80)); v >>= 7; }
    out.push_back(static_cast<char>(v));
  }
  void tag(uint32_t field, uint32_t wt) { varint((uint64_t(field) << 3) | wt); }
  void u64(uint32_t field, uint64_t v) { tag(field, VARINT); varint(v); }
  void boolean(uint32_t field, bool v) { tag(field, VARINT); varint(v ? 1 : 0); }
  void bytes(uint32_t field, std::string_view s) { tag(field, LEN); varint(s.size()); out.append(s); }
  void message(uint32_t field, const Writer& w) { bytes(field, w.out); }
  /** map<string, Message> entry: key = 1, value = 2. */
  void map_entry(uint32_t field, std::string_view key, const Writer& value) {
    Writer e;
    e.bytes(1, key);
    e.message(2, value);
    message(field, e);
  }
};

}  // namespace pd::pb
