// Package live is the trader's hot path: every websocket frame enters through
// Hub.OnFrame, which tapes it, parses it and runs the engine under one lock.
// Replay (cmd/leadlag --replay) drives the same Hub from tape files, so live
// and replayed runs process identical event sequences (checked by Digest).
package live

import (
	"hash/fnv"
	"sync"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/parse"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
)

// Engine consumes parsed events. It runs under the hub lock and must not block.
type Engine interface {
	OnEvent(e *model.Event)
}

// Tape receives raw frames and connection changes (ipc.Producer in production).
type Tape interface {
	Frame(recvNs int64, conn byte, frame []byte)
	Conn(atNs int64, conn, state byte)
}

// Hub serializes the two feed connections onto one engine.
type Hub struct {
	mu   sync.Mutex
	syms parse.Symbols
	eng  Engine
	tape Tape
	now  func() int64 // monotonic-backed wall clock, unix ns
	ev   model.Event

	lastTrade []int64 // per symbol: last aggregate trade id dispatched
	lastQuote []int64 // per symbol: last bookTicker update id dispatched

	st Stats
	dg uint64 // running digest of dispatched events
}

// Stats are the hub's counters and latency histograms (all under the lock).
type Stats struct {
	Frames      [2]uint64 // [market, public]
	Events      uint64
	ParseErrors uint64
	UnknownSym  uint64
	Other       uint64 // control replies, unsubscribed streams
	DupTrades   uint64 // aggTrade id ≤ last seen (reconnect overlap)
	DupQuotes   uint64
	TradeGaps   uint64 // aggTrade id jumped by more than 1
	// EngineNs: parse + engine time per frame. LoopNs: receive stamp → engine done.
	EngineNs stats.Hist
	LoopNs   stats.Hist
	Digest   uint64
}

// NewHub builds a hub for symbols (index = model.Event.Sym). tape may be nil.
func NewHub(symbols []string, eng Engine, tape Tape) *Hub {
	return &Hub{
		syms: parse.NewSymbols(symbols), eng: eng, tape: tape,
		now:       func() int64 { return time.Now().UnixNano() },
		lastTrade: make([]int64, len(symbols)),
		lastQuote: make([]int64, len(symbols)),
		dg:        fnv.New64a().Sum64(),
	}
}

// SetClock replaces the clock (replay uses the recorded receive time).
func (h *Hub) SetClock(now func() int64) { h.now = now }

func connIndex(conn byte) int {
	if conn == ipc.ConnPublic {
		return 1
	}
	return 0
}

// OnFrame handles one frame from connection conn received at recvNs.
func (h *Hub) OnFrame(conn byte, recvNs int64, frame []byte) {
	h.mu.Lock()
	start := h.now()
	if h.tape != nil {
		h.tape.Frame(recvNs, conn, frame)
	}
	h.st.Frames[connIndex(conn)]++
	e := &h.ev
	switch err := parse.Frame(frame, h.syms, e); err {
	case nil:
		e.RecvNs = recvNs
		if h.fresh(e) {
			h.st.Events++
			h.digest(e)
			h.eng.OnEvent(e)
		}
	case parse.ErrUnknownSymbol:
		h.st.UnknownSym++
	case parse.ErrUnknownStream:
		h.st.Other++
	default:
		h.st.ParseErrors++
	}
	end := h.now()
	h.st.EngineNs.Record(end - start)
	h.st.LoopNs.Record(end - recvNs)
	h.mu.Unlock()
}

// fresh drops events already seen (overlap after a reconnect) and counts id jumps.
func (h *Hub) fresh(e *model.Event) bool {
	if e.Kind == model.KindLiq {
		return true // no id; Binance does not resend them on reconnect
	}
	if e.Kind == model.KindTrade {
		last := h.lastTrade[e.Sym]
		if last != 0 && e.ID <= last {
			h.st.DupTrades++
			return false
		}
		if last != 0 && e.ID > last+1 {
			h.st.TradeGaps++
		}
		h.lastTrade[e.Sym] = e.ID
		return true
	}
	if last := h.lastQuote[e.Sym]; last != 0 && e.ID <= last {
		h.st.DupQuotes++
		return false
	}
	h.lastQuote[e.Sym] = e.ID
	return true
}

// digest folds the event's fields into a running FNV-1a-style hash.
func (h *Hub) digest(e *model.Event) {
	flags := int64(e.Sym)<<16 | int64(e.Kind)<<8
	if e.BuyerMaker {
		flags |= 1
	}
	for _, v := range [...]int64{e.RecvNs, e.ExchMs, e.TradeMs, e.ID, e.Px, e.Qty, e.Bid, e.Ask, e.BidQty, e.AskQty, flags} {
		h.dg = (h.dg ^ uint64(v)) * 1099511628211
	}
}

// OnConn records a feed connection change on the tape.
func (h *Hub) OnConn(conn byte, atNs int64, up bool) {
	state := ipc.StateDown
	if up {
		state = ipc.StateUp
	}
	h.mu.Lock()
	if h.tape != nil {
		h.tape.Conn(atNs, conn, state)
	}
	h.mu.Unlock()
}

// Snapshot copies the stats (the histograms are ~8 KB; this takes microseconds).
func (h *Hub) Snapshot() Stats {
	h.mu.Lock()
	s := h.st
	s.Digest = h.dg
	h.mu.Unlock()
	return s
}

// WithLock runs f under the hub lock (status snapshots of engine state).
func (h *Hub) WithLock(f func()) {
	h.mu.Lock()
	f()
	h.mu.Unlock()
}
