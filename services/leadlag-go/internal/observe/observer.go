// Package observe is the observer process's core: it receives the trader's
// records over the unix socket, writes the tape, and keeps the statistics the
// status page shows. None of this runs in the trader process.
package observe

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/parse"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/tape"
)

// Config for an Observer.
type Config struct {
	TapeDir     string
	DiskMaxPct  float64 // stop taping at this /data usage; resume 5 points below
	Log         *slog.Logger
	Now         func() time.Time
	DisableTape bool
	// JournalDir receives the trader's journal records (shadow trades), one
	// JSON-lines file per UTC day. Empty: journal records are counted, not kept.
	JournalDir string
}

// Observer owns the tape writer and the aggregates.
type Observer struct {
	cfg Config
	mu  sync.Mutex

	w            *tape.Writer
	tapeOn       bool
	pausedReason string
	diskUsedPct  float64
	diskFreeGB   float64
	tapeErr      string

	started    time.Time
	traderUp   bool
	hello      ipc.Hello
	helloRaw   json.RawMessage
	hellos     int
	gaps       int64
	gapRecords uint64
	holes      []Hole // hellos whose counters rose (data lost while disconnected)

	names []string
	syms  parse.Symbols
	per   []*symAgg
	conns map[string]*connState
	hour  int64
	ev    model.Event

	frames, parseErr uint64

	closed     bool     // Close ran: late records must not open new files
	jf         *os.File // today's journal file
	jday       string
	journals   uint64
	journalErr string
}

// Hole is a span the trader could not deliver (observer down or ring full).
type Hole struct {
	AtNs           int64  `json:"at_ns"`
	DiscardedBytes uint64 `json:"discarded_bytes"`
	Dropped        uint64 `json:"dropped"`
}

type connState struct {
	Up      bool  `json:"up"`
	SinceNs int64 `json:"since_ns"`
	Ups     int   `json:"ups"`
	Downs   int   `json:"downs"`
}

type symAgg struct {
	trades, quotes             uint64
	hTrades, hQuotes           uint64
	tradeLag, quoteLag         stats.Hist // µs, since start
	hTradeLag, hQuoteLag       stats.Hist // µs, this hour
	pubMs                      stats.Hist // aggTrade E − T, ms, since start
	nextT, nextTotal           uint64     // aggTrade E == next T
	lastE, lastID, lastU       int64
	idGaps, reconnectGaps      uint64
	lastTradeRecv, lastQuoteRx int64
	afterReconnect             bool
}

// New builds an observer.
func New(cfg Config) *Observer {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	if cfg.DiskMaxPct == 0 {
		cfg.DiskMaxPct = 90
	}
	o := &Observer{cfg: cfg, started: cfg.Now(), conns: map[string]*connState{}, tapeOn: !cfg.DisableTape}
	if !cfg.DisableTape {
		o.w = &tape.Writer{Dir: cfg.TapeDir}
	}
	return o
}

// Serve accepts trader connections on ln until ctx ends. Only one trader is
// read at a time; a new connection replaces the old one.
func (o *Observer) Serve(ctx context.Context, ln net.Listener) {
	go func() { <-ctx.Done(); ln.Close() }()
	var cur net.Conn
	var curMu sync.Mutex
	for {
		c, err := ln.Accept()
		if err != nil {
			if ctx.Err() == nil {
				o.cfg.Log.Error("accept", "err", err)
			}
			return
		}
		curMu.Lock()
		if cur != nil {
			cur.Close()
		}
		cur = c
		curMu.Unlock()
		go func() {
			err := o.read(c)
			if err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
				o.cfg.Log.Warn("trader stream ended", "err", err)
			}
			o.mu.Lock()
			o.traderUp = false
			o.mu.Unlock()
		}()
	}
}

func (o *Observer) read(c net.Conn) error {
	defer c.Close()
	rd := ipc.NewReader(c)
	for {
		typ, body, err := rd.Next()
		if err != nil {
			return err
		}
		if err := o.Handle(typ, body); err != nil {
			return err
		}
	}
}

// Handle processes one record.
func (o *Observer) Handle(typ byte, body []byte) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	now := o.cfg.Now().UnixNano()
	switch typ {
	case ipc.TypeHello:
		var h ipc.Hello
		if err := json.Unmarshal(body, &h); err != nil {
			return err
		}
		if o.hellos > 0 && (h.DiscardedBytes > o.hello.DiscardedBytes || h.Dropped > o.hello.Dropped) && h.PID == o.hello.PID {
			o.holes = append(o.holes, Hole{AtNs: now, DiscardedBytes: h.DiscardedBytes - o.hello.DiscardedBytes, Dropped: h.Dropped - o.hello.Dropped})
		}
		o.hello, o.helloRaw = h, append(json.RawMessage(nil), body...)
		o.hellos++
		o.traderUp = true
		o.setSymbols(h.Symbols)
		for _, a := range o.per {
			a.afterReconnect = true // ids may jump across a trader restart or hole
		}
		o.cfg.Log.Info("trader hello", "pid", h.PID, "mode", h.Mode, "build", h.Build, "symbols", len(h.Symbols))
		return o.tape(func(w *tape.Writer) error { return w.Hello(now, body) })
	case ipc.TypeFrame:
		recv, conn, frame, err := ipc.FrameBody(body)
		if err != nil {
			return err
		}
		o.frames++
		o.aggregate(recv, frame)
		return o.tape(func(w *tape.Writer) error { return w.Frame(recv, conn, frame) })
	case ipc.TypeGap:
		recs, n, at, err := ipc.GapBody(body)
		if err != nil {
			return err
		}
		o.gaps++
		o.gapRecords += recs
		for _, a := range o.per {
			a.afterReconnect = true
		}
		return o.tape(func(w *tape.Writer) error { return w.Gap(at, recs, n) })
	case ipc.TypeConn:
		at, conn, state, err := ipc.ConnBody(body)
		if err != nil {
			return err
		}
		cs := o.conns[string(conn)]
		if cs == nil {
			cs = &connState{}
			o.conns[string(conn)] = cs
		}
		cs.Up, cs.SinceNs = state == ipc.StateUp, at
		if cs.Up {
			cs.Ups++
		} else {
			cs.Downs++
			for _, a := range o.per {
				a.afterReconnect = true
			}
		}
		return o.tape(func(w *tape.Writer) error { return w.Conn(at, conn, state == ipc.StateUp) })
	case ipc.TypeJournal:
		o.journals++
		o.journal(now, body)
	}
	return nil
}

// journal appends one record to the day's file. A failure is reported in the
// stats and never stops the observer (the tape still lets a replay rebuild it).
func (o *Observer) journal(now int64, body []byte) {
	if o.cfg.JournalDir == "" || o.closed {
		return
	}
	day := time.Unix(0, now).UTC().Format(time.DateOnly)
	if o.jf == nil || day != o.jday {
		if o.jf != nil {
			o.jf.Close()
			o.jf = nil
		}
		if err := os.MkdirAll(o.cfg.JournalDir, 0o755); err != nil {
			o.journalErr = err.Error()
			return
		}
		f, err := os.OpenFile(filepath.Join(o.cfg.JournalDir, day+".jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			o.journalErr = err.Error()
			return
		}
		o.jf, o.jday = f, day
	}
	line := append(append(make([]byte, 0, len(body)+1), body...), '\n')
	if _, err := o.jf.Write(line); err != nil {
		o.journalErr = err.Error()
	}
}

func (o *Observer) setSymbols(names []string) {
	same := len(names) == len(o.names)
	for i := 0; same && i < len(names); i++ {
		same = names[i] == o.names[i]
	}
	if same {
		return
	}
	o.names = append([]string(nil), names...)
	o.syms = parse.NewSymbols(names)
	o.per = make([]*symAgg, len(names))
	for i := range o.per {
		o.per[i] = &symAgg{}
	}
}

func (o *Observer) aggregate(recv int64, frame []byte) {
	if o.syms == nil {
		return
	}
	e := &o.ev
	if err := parse.Frame(frame, o.syms, e); err != nil {
		if err != parse.ErrUnknownStream {
			o.parseErr++
		}
		return
	}
	if h := recv / int64(time.Hour); h != o.hour {
		o.hour = h
		for _, a := range o.per {
			a.hTrades, a.hQuotes = 0, 0
			a.hTradeLag.Reset()
			a.hQuoteLag.Reset()
		}
	}
	if e.Kind == model.KindLiq {
		return // liquidations are taped; they have no feed statistics
	}
	a := o.per[e.Sym]
	lagUs := recv/1000 - e.ExchMs*1000
	if e.Kind == model.KindTrade {
		a.trades++
		a.hTrades++
		a.tradeLag.Record(lagUs)
		a.hTradeLag.Record(lagUs)
		a.pubMs.Record(e.ExchMs - e.TradeMs)
		if a.lastID != 0 && !a.afterReconnect {
			a.nextTotal++
			if a.lastE == e.TradeMs {
				a.nextT++
			}
			if e.ID > a.lastID+1 {
				a.idGaps++
			}
		} else if a.lastID != 0 && e.ID > a.lastID+1 {
			a.reconnectGaps++
		}
		a.afterReconnect = false
		a.lastE, a.lastID, a.lastTradeRecv = e.ExchMs, e.ID, recv
		return
	}
	a.quotes++
	a.hQuotes++
	a.quoteLag.Record(lagUs)
	a.hQuoteLag.Record(lagUs)
	a.lastU, a.lastQuoteRx = e.ID, recv
}

// tape runs f on the writer if taping is on; a write error pauses taping
// rather than killing the observer (statistics keep flowing).
func (o *Observer) tape(f func(*tape.Writer) error) error {
	if o.closed || !o.tapeOn || o.w == nil {
		return nil
	}
	if err := f(o.w); err != nil {
		o.tapeErr = err.Error()
		o.tapeOn = false
		o.pausedReason = "write error: " + err.Error()
		o.cfg.Log.Error("tape write failed; taping paused", "err", err)
		o.w.Close()
	}
	return nil
}

// Housekeep flushes the tape and applies the disk guard; call it every few seconds.
func (o *Observer) Housekeep() {
	used, free, err := diskUsage(o.cfg.TapeDir)
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.w != nil && o.tapeOn {
		if err := o.w.Flush(); err != nil {
			o.tapeErr = err.Error()
		}
	}
	if err != nil {
		return
	}
	o.diskUsedPct, o.diskFreeGB = used, free
	switch {
	case o.tapeOn && used >= o.cfg.DiskMaxPct:
		o.tapeOn = false
		o.pausedReason = "disk guard"
		o.cfg.Log.Error("disk guard: taping paused", "used_pct", used)
		if o.w != nil {
			o.w.Close()
		}
	case !o.tapeOn && o.pausedReason == "disk guard" && used < o.cfg.DiskMaxPct-5:
		o.tapeOn, o.pausedReason = true, ""
		o.cfg.Log.Info("disk guard: taping resumed", "used_pct", used)
	}
}

// Close finishes the open tape file (writes its manifest).
func (o *Observer) Close() error {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.closed = true
	if o.jf != nil {
		o.jf.Close()
		o.jf = nil
	}
	if o.w != nil {
		return o.w.Close()
	}
	return nil
}

func diskUsage(dir string) (usedPct, freeGB float64, err error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, 0, err
	}
	total := float64(st.Blocks) * float64(st.Bsize)
	avail := float64(st.Bavail) * float64(st.Bsize)
	if total == 0 {
		return 0, 0, errors.New("statfs: zero size")
	}
	return 100 * (1 - avail/total), avail / 1e9, nil
}
