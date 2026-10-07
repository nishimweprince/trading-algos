// Package ipc moves records from the trader's hot path to the observer process
// without ever blocking the hot path: the producer copies into a preallocated
// byte ring and returns; a writer goroutine drains the ring onto a unix socket.
package ipc

import (
	"encoding/binary"
	"sync/atomic"
)

// Ring is a single-producer, single-consumer byte ring of length-prefixed
// records. Producers must be serialized by the caller (the trader holds its
// hub lock); the consumer is one goroutine.
type Ring struct {
	buf  []byte
	mask uint64
	head atomic.Uint64 // next write position (producer)
	tail atomic.Uint64 // next read position (consumer)
	wake chan struct{}
}

// NewRing makes a ring of size bytes, rounded up to a power of two.
func NewRing(size int) *Ring {
	n := uint64(1)
	for n < uint64(size) {
		n <<= 1
	}
	return &Ring{buf: make([]byte, n), mask: n - 1, wake: make(chan struct{}, 1)}
}

// Cap is the ring's capacity in bytes.
func (r *Ring) Cap() int { return len(r.buf) }

// Used is the number of unread bytes.
func (r *Ring) Used() int { return int(r.head.Load() - r.tail.Load()) }

// Put writes one record: [u32 len][u8 typ][a][b], len = 1+len(a)+len(b).
// It returns false, writing nothing, when the record does not fit.
func (r *Ring) Put(typ byte, a, b []byte) bool {
	n := uint64(4 + 1 + len(a) + len(b))
	head := r.head.Load()
	if head+n-r.tail.Load() > uint64(len(r.buf)) {
		return false
	}
	var hdr [5]byte
	binary.LittleEndian.PutUint32(hdr[:4], uint32(n-4))
	hdr[4] = typ
	p := r.copyIn(head, hdr[:])
	p = r.copyIn(p, a)
	r.copyIn(p, b)
	r.head.Store(head + n)
	select {
	case r.wake <- struct{}{}:
	default:
	}
	return true
}

func (r *Ring) copyIn(pos uint64, src []byte) uint64 {
	for len(src) > 0 {
		i := pos & r.mask
		c := copy(r.buf[i:], src)
		src = src[c:]
		pos += uint64(c)
	}
	return pos
}

// ReadRecords copies whole records only (as many as fit in dst) and consumes
// them, so every write the consumer makes starts and ends on a record boundary.
// dst must hold at least one maximum-size record.
func (r *Ring) ReadRecords(dst []byte) int {
	tail := r.tail.Load()
	avail := r.head.Load() - tail
	var n uint64
	var hdr [4]byte
	for avail-n >= 4 {
		r.peek(tail+n, hdr[:])
		rec := 4 + uint64(binary.LittleEndian.Uint32(hdr[:]))
		if rec > avail-n || n+rec > uint64(len(dst)) {
			break
		}
		r.peek(tail+n, dst[n:n+rec])
		n += rec
	}
	r.tail.Store(tail + n)
	return int(n)
}

func (r *Ring) peek(pos uint64, dst []byte) {
	got := 0
	for got < len(dst) {
		i := (pos + uint64(got)) & r.mask
		got += copy(dst[got:], r.buf[i:])
	}
}

// Wake is signalled after each Put (coalesced); the consumer waits on it.
func (r *Ring) Wake() <-chan struct{} { return r.wake }
