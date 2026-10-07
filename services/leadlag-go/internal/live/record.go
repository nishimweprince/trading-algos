package live

import "github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"

// RecordEngine is the M2 engine: it only tracks the latest trade and quote per
// symbol for the status page. The policy engine replaces it in M5.
type RecordEngine struct {
	Last []SymState
}

// SymState is the latest market state of one symbol.
type SymState struct {
	Trades   uint64 `json:"trades"`
	Quotes   uint64 `json:"quotes"`
	LastPx   int64  `json:"last_px_e8"`
	Bid      int64  `json:"bid_e8"`
	Ask      int64  `json:"ask_e8"`
	LastExch int64  `json:"last_exch_ms"`
}

// NewRecordEngine sizes the state for n symbols.
func NewRecordEngine(n int) *RecordEngine { return &RecordEngine{Last: make([]SymState, n)} }

// OnEvent implements Engine.
func (r *RecordEngine) OnEvent(e *model.Event) {
	s := &r.Last[e.Sym]
	if e.Kind == model.KindTrade {
		s.Trades++
		s.LastPx = e.Px
	} else {
		s.Quotes++
		s.Bid, s.Ask = e.Bid, e.Ask
	}
	s.LastExch = e.ExchMs
}
