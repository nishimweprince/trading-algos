package ipc

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"log/slog"
	"net"
	"sync/atomic"
	"time"
)

// Producer is the trader side: hot-path methods copy into a Ring and never
// block; Run drains the ring to the observer's unix socket.
//
// Tape frames are lossy: when the ring is full the frame is dropped and counted,
// and a TypeGap record goes out ahead of the next frame that fits. While the
// observer is not connected, Run discards what it drains and counts that as a
// gap too, so the ring never fills because the observer is down.
type Producer struct {
	ring  *Ring
	path  string
	hello Hello
	log   *slog.Logger

	// pending gap, touched only by the producer (under the caller's lock)
	gapRecs, gapBytes uint64
	gapAt             int64

	scratch [24]byte

	// counters, read by status snapshots
	Dropped     atomic.Uint64 // records dropped because the ring was full
	Discarded   atomic.Uint64 // bytes drained while the observer was down
	Connected   atomic.Bool
	Connects    atomic.Uint64
	BytesSent   atomic.Uint64
	MaxRingUsed atomic.Uint64
}

// NewProducer makes a producer with a ring of ringBytes.
func NewProducer(path string, ringBytes int, hello Hello, log *slog.Logger) *Producer {
	hello.Version = ProtoVersion
	return &Producer{ring: NewRing(ringBytes), path: path, hello: hello, log: log}
}

// Frame queues a raw websocket frame. Caller holds the hub lock.
func (p *Producer) Frame(recvNs int64, conn byte, frame []byte) {
	if p.gapRecs > 0 && !p.flushGap() {
		p.drop(len(frame), recvNs)
		return
	}
	binary.LittleEndian.PutUint64(p.scratch[:8], uint64(recvNs))
	p.scratch[8] = conn
	if !p.ring.Put(TypeFrame, p.scratch[:9], frame) {
		p.drop(len(frame), recvNs)
		return
	}
	p.noteUsed()
}

// Journal queues one JSON journal record (a shadow trade). Caller holds the
// hub lock. Like a frame, it never blocks: a full ring drops it and counts a gap.
func (p *Producer) Journal(atNs int64, body []byte) {
	if p.gapRecs > 0 && !p.flushGap() {
		p.drop(len(body), atNs)
		return
	}
	if !p.ring.Put(TypeJournal, nil, body) {
		p.drop(len(body), atNs)
		return
	}
	p.noteUsed()
}

// Conn queues a connection state change. Caller holds the hub lock.
func (p *Producer) Conn(atNs int64, conn, state byte) {
	var b [10]byte
	binary.LittleEndian.PutUint64(b[:8], uint64(atNs))
	b[8], b[9] = conn, state
	if !p.ring.Put(TypeConn, b[:], nil) {
		p.drop(10, atNs)
	}
}

func (p *Producer) drop(n int, atNs int64) {
	if p.gapRecs == 0 {
		p.gapAt = atNs
	}
	p.gapRecs++
	p.gapBytes += uint64(n)
	p.Dropped.Add(1)
}

func (p *Producer) flushGap() bool {
	binary.LittleEndian.PutUint64(p.scratch[0:], p.gapRecs)
	binary.LittleEndian.PutUint64(p.scratch[8:], p.gapBytes)
	binary.LittleEndian.PutUint64(p.scratch[16:], uint64(p.gapAt))
	if !p.ring.Put(TypeGap, p.scratch[:24], nil) {
		return false
	}
	p.gapRecs, p.gapBytes = 0, 0
	return true
}

func (p *Producer) noteUsed() {
	if u := uint64(p.ring.Used()); u > p.MaxRingUsed.Load() {
		p.MaxRingUsed.Store(u)
	}
}

// RingCap is the ring size in bytes.
func (p *Producer) RingCap() int { return p.ring.Cap() }

// Run drains the ring until ctx ends, (re)connecting to the observer once a second.
func (p *Producer) Run(ctx context.Context) {
	buf := make([]byte, MaxRecord+5)
	var conn net.Conn
	lastDial := time.Time{}
	tick := time.NewTicker(50 * time.Millisecond)
	defer tick.Stop()
	defer func() {
		if conn != nil {
			// drain what is left on shutdown
			for n := p.ring.ReadRecords(buf); n > 0; n = p.ring.ReadRecords(buf) {
				if _, err := conn.Write(buf[:n]); err != nil {
					break
				}
			}
			conn.Close()
		}
	}()
	for {
		if conn == nil && time.Since(lastDial) >= time.Second {
			lastDial = time.Now()
			c, err := net.DialTimeout("unix", p.path, time.Second)
			if err == nil && p.sendHello(c) == nil {
				conn = c
				p.Connected.Store(true)
				p.Connects.Add(1)
				p.log.Info("observer connected", "socket", p.path)
			} else if c != nil {
				c.Close()
			}
		}
		n := p.ring.ReadRecords(buf)
		if n > 0 {
			if conn == nil {
				// The observer is down: discard so the ring keeps room. The next
				// hello carries the discarded total, so the observer marks the hole.
				p.Discarded.Add(uint64(n))
			} else if _, err := conn.Write(buf[:n]); err != nil {
				p.log.Warn("observer write failed; reconnecting", "err", err)
				p.Discarded.Add(uint64(n))
				conn.Close()
				conn = nil
				p.Connected.Store(false)
			} else {
				p.BytesSent.Add(uint64(n))
			}
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-p.ring.Wake():
		case <-tick.C:
		}
	}
}

func (p *Producer) sendHello(c net.Conn) error {
	h := p.hello
	h.DiscardedBytes = p.Discarded.Load()
	h.Dropped = p.Dropped.Load()
	b, err := json.Marshal(h)
	if err != nil {
		return err
	}
	var hdr [5]byte
	binary.LittleEndian.PutUint32(hdr[:4], uint32(len(b)+1))
	hdr[4] = TypeHello
	c.SetWriteDeadline(time.Now().Add(time.Second))
	defer c.SetWriteDeadline(time.Time{})
	if _, err := c.Write(hdr[:]); err != nil {
		return err
	}
	_, err = c.Write(b)
	return err
}
