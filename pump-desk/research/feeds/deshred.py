#!/usr/bin/env python3
# deshred.py CAPTURE.bin PROCESSED.txt OUT.txt
# CAPTURE: shredcap.py records [u64 unix_ns][u16 len][raw shred]
# Rebuilds data-shred batches (DATA_COMPLETE boundaries) per slot, parses Vec<Entry>, and for every tx whose
# signature is in PROCESSED (Helius pump.fun sigs) writes "prefix_us batch_us slot sig8hex".
#   prefix_us: latest arrival among the batch's shreds from its first up to the one holding the tx's last byte
#              (what a streaming entry parser could achieve)
#   batch_us:  latest arrival among all shreds of the batch (what a standard deshredder achieves)
# No Reed-Solomon recovery: a batch with a missing data shred is decoded only up to the gap.
import struct, sys, collections

A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"; IDX = {c: i for i, c in enumerate(A)}
def b58(s):
    n = 0
    for c in s: n = n * 58 + IDX[c]
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return (b"\0" * (len(s) - len(s.lstrip("1"))) + b).rjust(64, b"\0")

want = set()
for l in open(sys.argv[2]):
    p = l.split()
    if len(p) == 3: want.add(b58(p[2])[:8])
print(f"helius pump sigs {len(want)}", file=sys.stderr)

class R:
    __slots__ = ("b", "p")
    def __init__(s, b): s.b, s.p = b, 0
    def take(s, n):
        if s.p + n > len(s.b): raise EOFError
        x = s.b[s.p:s.p + n]; s.p += n; return x
    def skip(s, n):
        if s.p + n > len(s.b): raise EOFError
        s.p += n
    def u8(s):
        if s.p >= len(s.b): raise EOFError
        x = s.b[s.p]; s.p += 1; return x
    def u16(s): return struct.unpack("<H", s.take(2))[0]
    def u32(s): return struct.unpack("<I", s.take(4))[0]
    def u64(s): return struct.unpack("<Q", s.take(8))[0]
    def sv(s):
        v = 0
        for i in range(3):
            b = s.u8(); v |= (b & 0x7f) << (7 * i)
            if not b & 0x80: return v
        raise ValueError

def tx_sig(r):
    if r.b[r.p] == 0x81:  # v1: signatures at the end
        r.skip(1); ns = r.u8(); r.skip(2); m = r.u32(); r.skip(32); ni = r.u8(); nk = r.u8()
        r.skip(32 * nk); r.skip(4 * bin(m).count("1"))
        tot = 0
        for _ in range(ni): r.u8(); na = r.u8(); nd = r.u16(); tot += na + nd
        r.skip(tot)
        return r.take(64 * ns)[:8]
    ns = r.sv(); sig = r.take(64 * ns)[:8]
    v0 = r.b[r.p] & 0x80
    if v0: r.skip(1)
    r.skip(3); r.skip(32 * r.sv()); r.skip(32)
    for _ in range(r.sv()): r.u8(); r.skip(r.sv()); r.skip(r.sv())
    if v0:
        for _ in range(r.sv()): r.skip(32); r.skip(r.sv()); r.skip(r.sv())
    return sig

out = open(sys.argv[3], "w")
st = collections.Counter()
slots = {}      # slot -> {index: (t_us, payload, flags)}
done = {}       # slot -> next undecoded batch start index
last_seen = {}  # slot -> last packet time

def decode_slot(slot, final):
    shreds = slots[slot]
    start = done.get(slot, 0)
    ends = sorted(i for i, (_, _, fl) in shreds.items() if fl & 0x40 and i >= start)
    for e in ends:
        # contiguous prefix of the batch [start, e]
        idxs = []
        for i in range(start, e + 1):
            if i not in shreds: break
            idxs.append(i)
        complete = len(idxs) == e - start + 1
        if not complete and not final:
            return  # wait for more shreds
        st["batches"] += 1
        if not complete: st["batches_gap"] += 1
        buf = bytearray(); bounds = []; tmax = 0; tpre = []
        for i in idxs:
            t, pl, _ = shreds[i]
            buf += pl; bounds.append(len(buf)); tmax = max(tmax, t); tpre.append(tmax)
        batch_t = tmax if complete else None
        r = R(bytes(buf)); bi = 0
        try:
            ne = r.u64()
            for _ in range(ne):
                r.skip(8 + 32); nt = r.u64(); st["entries"] += 1
                for _ in range(nt):
                    sig = tx_sig(r); st["txs"] += 1
                    while bounds[bi] < r.p: bi += 1
                    if sig in want:
                        st["pump"] += 1
                        out.write(f"{tpre[bi]} {batch_t if batch_t else -1} {slot} {sig.hex()}\n")
        except (EOFError, IndexError, ValueError):
            st["cut"] += 1
        start = e + 1
        done[slot] = start

f = open(sys.argv[1], "rb", buffering=8 << 20)
npk = 0
while True:
    h = f.read(10)
    if len(h) < 10: break
    ts, ln = struct.unpack("<QH", h); d = f.read(ln); npk += 1
    if ln < 88: continue
    var = d[64] >> 4
    if var not in (0x8, 0x9, 0xb) and d[64] != 0xa5: st["code"] += 1; continue
    slot = struct.unpack_from("<Q", d, 65)[0]; idx = struct.unpack_from("<I", d, 73)[0]
    flags = d[85]; size = struct.unpack_from("<H", d, 86)[0]
    if size < 88 or size > ln: st["badsize"] += 1; continue
    t_us = ts // 1000
    s = slots.setdefault(slot, {})
    if idx in s: st["dup"] += 1; continue
    s[idx] = (t_us, d[88:size], flags)
    st["data"] += 1
    last_seen[slot] = t_us
    if flags & 0x40: decode_slot(slot, False)
    if npk % 200000 == 0:
        old = [x for x, t in last_seen.items() if t < t_us - 5_000_000]
        for x in old:
            decode_slot(x, True); del slots[x]; del last_seen[x]; done.pop(x, None)
        print(f"{npk} pkts {dict(st)}", file=sys.stderr, flush=True)
for x in list(slots):
    decode_slot(x, True)
out.close()
print(f"done {npk} pkts {dict(st)}", file=sys.stderr)
