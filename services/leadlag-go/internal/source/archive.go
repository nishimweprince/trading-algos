// Package source produces time-ordered model.Events from the archive, our own
// tapes, or live feeds, behind one interface so research and the live loop run
// the same engine code.
package source

import (
	"container/heap"
	"fmt"
	"io"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/archive"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// Source yields events in processing order; io.EOF ends the stream.
type Source interface {
	Next(e *model.Event) error
	Close() error
}

// ArchiveConfig selects symbols and an inclusive UTC day range.
type ArchiveConfig struct {
	Layout  archive.Layout
	Symbols []string // index in this slice becomes Event.Sym
	From    time.Time
	To      time.Time
	// SkipDays are excluded for every symbol (e.g. days with id gaps), so no
	// symbol is seen while another is silent.
	SkipDays map[string]bool
	// AllowMissing skips a symbol-day that is not packed instead of failing.
	AllowMissing bool
}

// Archive merges per-symbol packed days into one stream ordered by
// (TradeMs, Sym, AggID). Sym order breaks same-millisecond ties deterministically;
// consumers must not treat a same-ms print as observable after another (see fill).
type Archive struct {
	h    eventHeap
	syms []*symStream
}

// OpenArchive validates that every requested day is packed and opens the streams.
func OpenArchive(cfg ArchiveConfig) (*Archive, error) {
	var days []string
	for d := cfg.From.UTC(); !d.After(cfg.To.UTC()); d = d.AddDate(0, 0, 1) {
		day := d.Format(time.DateOnly)
		if !cfg.SkipDays[day] {
			days = append(days, day)
		}
	}
	a := &Archive{}
	for i, sym := range cfg.Symbols {
		var symDays []string
		for _, day := range days {
			if cfg.Layout.HasDay(sym, day) {
				symDays = append(symDays, day)
			} else if !cfg.AllowMissing {
				return nil, fmt.Errorf("source: %s %s is not packed (run llfetch)", sym, day)
			}
		}
		s := &symStream{layout: cfg.Layout, sym: sym, idx: uint16(i), days: symDays}
		a.syms = append(a.syms, s)
		ok, err := s.advance()
		if err != nil {
			a.Close()
			return nil, err
		}
		if ok {
			a.h = append(a.h, s)
		}
	}
	heap.Init(&a.h)
	return a, nil
}

// Next fills e with the next event.
func (a *Archive) Next(e *model.Event) error {
	if len(a.h) == 0 {
		return io.EOF
	}
	s := a.h[0]
	t := &s.cur
	*e = model.Event{
		ExchMs: t.TradeMs, TradeMs: t.TradeMs, ID: t.AggID, Px: t.Px, Qty: t.Qty,
		Sym: s.idx, Kind: model.KindTrade, BuyerMaker: t.BuyerMaker,
	}
	ok, err := s.advance()
	if err != nil {
		return err
	}
	if ok {
		heap.Fix(&a.h, 0)
	} else {
		heap.Pop(&a.h)
	}
	return nil
}

// Close closes any open day files.
func (a *Archive) Close() error {
	for _, s := range a.syms {
		if s.r != nil {
			s.r.Close()
			s.r = nil
		}
	}
	return nil
}

type symStream struct {
	layout archive.Layout
	sym    string
	idx    uint16
	days   []string
	r      *archive.LLPReader
	cur    archive.Trade
}

// advance loads the next trade into cur, opening the next day as needed.
func (s *symStream) advance() (bool, error) {
	for {
		if s.r == nil {
			if len(s.days) == 0 {
				return false, nil
			}
			r, err := archive.OpenLLP(s.layout.LLPPath(s.sym, s.days[0]))
			if err != nil {
				return false, err
			}
			s.r, s.days = r, s.days[1:]
		}
		err := s.r.Next(&s.cur)
		if err == nil {
			return true, nil
		}
		s.r.Close()
		s.r = nil
		if err != io.EOF {
			return false, fmt.Errorf("source: %s: %w", s.sym, err)
		}
	}
}

type eventHeap []*symStream

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	a, b := &h[i].cur, &h[j].cur
	if a.TradeMs != b.TradeMs {
		return a.TradeMs < b.TradeMs
	}
	if h[i].idx != h[j].idx {
		return h[i].idx < h[j].idx
	}
	return a.AggID < b.AggID
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(*symStream)) }
func (h *eventHeap) Pop() any {
	old := *h
	x := old[len(old)-1]
	*h = old[:len(old)-1]
	return x
}
