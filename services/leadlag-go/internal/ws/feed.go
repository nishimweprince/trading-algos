// Package ws keeps one Binance combined-stream websocket connected and hands
// every frame, stamped with its local receive time, to a callback.
package ws

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

// Feed is one connection (e.g. /market or /public). Run blocks until ctx ends.
type Feed struct {
	Name string // "market" / "public", for logs
	URL  string
	// OnFrame gets each frame with its receive time (unix ns, taken when the
	// frame is complete). The slice is reused after OnFrame returns.
	OnFrame func(recvNs int64, frame []byte)
	// OnState is called with true after connecting and false after losing the
	// connection (both with the time of the change, unix ns).
	OnState func(atNs int64, up bool)
	// ReadTimeout reconnects a connection that delivers nothing for this long.
	ReadTimeout time.Duration
	// MaxAge reconnects proactively before Binance's 24 h limit.
	MaxAge time.Duration
	Log    *slog.Logger

	Connects    atomic.Uint64
	LastFrameNs atomic.Int64
	Up          atomic.Bool
	LastErr     atomic.Pointer[string]
}

func (f *Feed) defaults() {
	if f.ReadTimeout == 0 {
		f.ReadTimeout = 30 * time.Second
	}
	if f.MaxAge == 0 {
		f.MaxAge = 23 * time.Hour
	}
	if f.Log == nil {
		f.Log = slog.Default()
	}
}

// Run connects, reads, and reconnects with jittered exponential backoff
// (0.5 s → 30 s, reset after a connection that lasted a minute).
func (f *Feed) Run(ctx context.Context) {
	f.defaults()
	backoff := 500 * time.Millisecond
	for ctx.Err() == nil {
		start := time.Now()
		err := f.session(ctx)
		if ctx.Err() != nil {
			return
		}
		msg := err.Error()
		f.LastErr.Store(&msg)
		if time.Since(start) > time.Minute {
			backoff = 500 * time.Millisecond
		}
		wait := backoff/2 + time.Duration(rand.Int64N(int64(backoff)))
		f.Log.Warn("feed disconnected", "feed", f.Name, "err", err, "retry_in", wait.Round(time.Millisecond))
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		backoff = min(backoff*2, 30*time.Second)
	}
}

var errMaxAge = errors.New("connection reached max age; rotating")

func (f *Feed) session(ctx context.Context) error {
	d := websocket.Dialer{
		Proxy:            http.ProxyFromEnvironment,
		HandshakeTimeout: 10 * time.Second,
		ReadBufferSize:   64 * 1024,
		WriteBufferSize:  4 * 1024,
	}
	conn, _, err := d.DialContext(ctx, f.URL, nil)
	if err != nil {
		return err
	}
	defer conn.Close()
	connected := time.Now()
	f.Connects.Add(1)
	f.Up.Store(true)
	if f.OnState != nil {
		f.OnState(connected.UnixNano(), true)
	}
	f.Log.Info("feed connected", "feed", f.Name, "url", redact(f.URL))
	defer func() {
		f.Up.Store(false)
		if f.OnState != nil {
			f.OnState(time.Now().UnixNano(), false)
		}
	}()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()

	buf := make([]byte, 0, 16*1024)
	for {
		if time.Since(connected) > f.MaxAge {
			return errMaxAge
		}
		conn.SetReadDeadline(time.Now().Add(f.ReadTimeout))
		typ, r, err := conn.NextReader()
		if err != nil {
			return err
		}
		if typ != websocket.TextMessage {
			continue
		}
		buf, err = readAll(r, buf[:0])
		if err != nil {
			return err
		}
		// Stamped once the whole frame is in: the earliest we could act on it.
		recv := time.Now().UnixNano()
		f.LastFrameNs.Store(recv)
		f.OnFrame(recv, buf)
	}
}

// readAll reads r into buf, growing it only when a frame is larger than any before.
func readAll(r io.Reader, buf []byte) ([]byte, error) {
	for {
		if len(buf) == cap(buf) {
			buf = append(buf, 0)[:len(buf)]
		}
		n, err := r.Read(buf[len(buf):cap(buf)])
		buf = buf[:len(buf)+n]
		if err == io.EOF {
			return buf, nil
		}
		if err != nil {
			return buf, err
		}
	}
}

func redact(u string) string {
	if i := strings.Index(u, "?"); i >= 0 && len(u) > i+80 {
		return u[:i+80] + "…"
	}
	return u
}

// StreamURL builds base/route/stream?streams=a/b/c.
func StreamURL(base, route string, streams []string) string {
	return strings.TrimRight(base, "/") + "/" + route + "/stream?streams=" + strings.Join(streams, "/")
}
