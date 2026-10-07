package ipc

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestRingWrapAndWholeRecords(t *testing.T) {
	r := NewRing(64)
	dst := make([]byte, 64)
	var got [][]byte
	for i := range 40 {
		payload := bytes.Repeat([]byte{byte('a' + i%26)}, 1+i%9)
		for !r.Put(TypeFrame, payload, nil) {
			got = append(got, splitRecords(t, dst[:r.ReadRecords(dst)])...)
		}
	}
	got = append(got, splitRecords(t, dst[:r.ReadRecords(dst)])...)
	if len(got) != 40 {
		t.Fatalf("records: %d", len(got))
	}
	for i, g := range got {
		want := bytes.Repeat([]byte{byte('a' + i%26)}, 1+i%9)
		if !bytes.Equal(g, want) {
			t.Fatalf("record %d = %q, want %q", i, g, want)
		}
	}
	if r.Used() != 0 {
		t.Fatalf("used %d", r.Used())
	}
}

func TestRingFullRejects(t *testing.T) {
	r := NewRing(16)
	if !r.Put(TypeFrame, make([]byte, 11), nil) { // 4+1+11 = 16
		t.Fatal("exact fit rejected")
	}
	if r.Put(TypeFrame, nil, nil) {
		t.Fatal("put into a full ring")
	}
	// ReadRecords with a small dst consumes nothing rather than a partial record.
	if n := r.ReadRecords(make([]byte, 10)); n != 0 {
		t.Fatalf("partial read %d", n)
	}
}

func TestPutNoAlloc(t *testing.T) {
	r := NewRing(1 << 20)
	a, b := make([]byte, 9), make([]byte, 200)
	dst := make([]byte, 1<<20)
	n := testing.AllocsPerRun(1000, func() {
		r.Put(TypeFrame, a, b)
		r.ReadRecords(dst)
	})
	if n != 0 {
		t.Fatalf("Put allocates %v", n)
	}
}

func splitRecords(t *testing.T, b []byte) [][]byte {
	t.Helper()
	var out [][]byte
	for len(b) > 0 {
		n := int(binary.LittleEndian.Uint32(b))
		out = append(out, append([]byte(nil), b[5:4+n]...))
		b = b[4+n:]
	}
	return out
}

// TestProducerToSocket: frames reach a listener in order, a full ring produces a
// gap record ahead of the next frame, and a reconnect starts with a hello.
func TestProducerToSocket(t *testing.T) {
	dir, err := os.MkdirTemp("", "ipc")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	sock := filepath.Join(dir, "t.sock") // short path: unix socket names are limited
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	p := NewProducer(sock, 256, Hello{Mode: "test"}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	// Fill the ring before the writer runs: 256-byte ring, 9+40+5 = 54 bytes a frame.
	frame := bytes.Repeat([]byte("x"), 40)
	for i := range 6 {
		p.Frame(int64(i), ConnMarket, frame)
	}
	if p.Dropped.Load() != 2 {
		t.Fatalf("dropped %d, want 2", p.Dropped.Load())
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { p.Run(ctx); close(done) }()

	c, err := ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	rd := NewReader(c)
	typ, body, err := rd.Next()
	if err != nil || typ != TypeHello {
		t.Fatalf("first record %d %v", typ, err)
	}
	var h Hello
	json.Unmarshal(body, &h)
	if h.Version != ProtoVersion || h.Mode != "test" || h.Dropped != 2 {
		t.Fatalf("hello %+v", h)
	}
	for i := range 4 {
		typ, body, err := rd.Next()
		if err != nil || typ != TypeFrame {
			t.Fatalf("record %d: %d %v", i, typ, err)
		}
		recv, conn, f, _ := FrameBody(body)
		if recv != int64(i) || conn != ConnMarket || !bytes.Equal(f, frame) {
			t.Fatalf("frame %d: %d %c %q", i, recv, conn, f)
		}
	}
	// Frame 4 was dropped; frame 5's call flushed that gap (29 bytes fit in the
	// 40 left) and was itself dropped, leaving a second pending gap.
	expectGap := func(wantAt int64) {
		t.Helper()
		typ, body, _ := rd.Next()
		recs, nbytes, at, err := GapBody(body)
		if typ != TypeGap || err != nil || recs != 1 || nbytes != 40 || at != wantAt {
			t.Fatalf("gap: type %d recs %d bytes %d at %d err %v", typ, recs, nbytes, at, err)
		}
	}
	expectGap(4)
	// The ring has room again: the pending gap goes out before the next frame.
	p.Frame(99, ConnPublic, frame)
	expectGap(5)
	typ, body, _ = rd.Next()
	if recv, conn, _, _ := FrameBody(body); typ != TypeFrame || recv != 99 || conn != ConnPublic {
		t.Fatalf("frame after gap: %d %d %c", typ, recv, conn)
	}
	cancel()
	<-done
	c.Close()
}
