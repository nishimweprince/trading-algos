package observe

import (
	"encoding/json"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
)

// StatsDoc is the observer's /v1/stats document. Latencies are µs unless named _ms.
type StatsDoc struct {
	NowNs     int64                 `json:"now_ns"`
	StartedNs int64                 `json:"started_ns"`
	Tape      TapeDoc               `json:"tape"`
	Trader    TraderDoc             `json:"trader"`
	Conns     map[string]*connState `json:"conns"`
	Symbols   []SymDoc              `json:"symbols"`
	Frames    uint64                `json:"frames"`
	ParseErr  uint64                `json:"parse_errors"`
	Journal   JournalDoc            `json:"journal"`
}

// JournalDoc describes the shadow-trade journal.
type JournalDoc struct {
	Dir     string `json:"dir"`
	Day     string `json:"day"`
	Records uint64 `json:"records"`
	Err     string `json:"err,omitempty"`
}

// TapeDoc describes the recorder.
type TapeDoc struct {
	Enabled      bool    `json:"enabled"`
	PausedReason string  `json:"paused_reason,omitempty"`
	Dir          string  `json:"dir"`
	File         string  `json:"file"`
	FileLines    int64   `json:"file_lines"`
	FilesClosed  int64   `json:"files_closed"`
	DiskUsedPct  float64 `json:"disk_used_pct"`
	DiskFreeGB   float64 `json:"disk_free_gb"`
	LastError    string  `json:"last_error,omitempty"`
}

// TraderDoc describes the trader connection.
type TraderDoc struct {
	Connected  bool            `json:"connected"`
	Hello      json.RawMessage `json:"hello,omitempty"`
	Hellos     int             `json:"hellos"`
	Gaps       int64           `json:"gaps"`
	GapRecords uint64          `json:"gap_records"`
	Holes      []Hole          `json:"holes,omitempty"`
}

// SymDoc is one symbol's feed statistics.
type SymDoc struct {
	Symbol        string        `json:"symbol"`
	Trades        uint64        `json:"trades"`
	Quotes        uint64        `json:"quotes"`
	TradesPerSec  float64       `json:"trades_per_s_hour"`
	QuotesPerSec  float64       `json:"quotes_per_s_hour"`
	TradeLag      stats.Summary `json:"trade_lag_us"`
	QuoteLag      stats.Summary `json:"quote_lag_us"`
	TradeLagHour  stats.Summary `json:"trade_lag_us_hour"`
	QuoteLagHour  stats.Summary `json:"quote_lag_us_hour"`
	PublishMs     stats.Summary `json:"agg_publish_after_trade_ms"`
	ENextTFrac    float64       `json:"agg_E_is_next_T_frac"`
	IDGaps        uint64        `json:"agg_id_gaps"`
	ReconnectGaps uint64        `json:"agg_id_gaps_after_reconnect"`
	LastTradeNs   int64         `json:"last_trade_recv_ns"`
	LastQuoteNs   int64         `json:"last_quote_recv_ns"`
}

// Stats builds the document (on request; the observer is off the hot path).
func (o *Observer) Stats() StatsDoc {
	o.mu.Lock()
	defer o.mu.Unlock()
	now := o.cfg.Now()
	d := StatsDoc{
		NowNs: now.UnixNano(), StartedNs: o.started.UnixNano(), Conns: map[string]*connState{},
		Frames: o.frames, ParseErr: o.parseErr,
		Journal: JournalDoc{Dir: o.cfg.JournalDir, Day: o.jday, Records: o.journals, Err: o.journalErr},
		Tape: TapeDoc{Enabled: o.tapeOn, PausedReason: o.pausedReason, Dir: o.cfg.TapeDir,
			DiskUsedPct: o.diskUsedPct, DiskFreeGB: o.diskFreeGB, LastError: o.tapeErr},
		Trader: TraderDoc{Connected: o.traderUp, Hello: o.helloRaw, Hellos: o.hellos, Gaps: o.gaps,
			GapRecords: o.gapRecords, Holes: append([]Hole(nil), o.holes...)},
	}
	for k, c := range o.conns {
		cp := *c
		d.Conns[k] = &cp
	}
	if o.w != nil {
		d.Tape.File = o.w.Path()
		d.Tape.FileLines = o.w.Current().Lines
		d.Tape.FilesClosed = o.w.Files
	}
	hourStart := time.Unix(0, (now.UnixNano()/int64(time.Hour))*int64(time.Hour))
	if o.started.After(hourStart) {
		hourStart = o.started
	}
	secs := now.Sub(hourStart).Seconds()
	for i, a := range o.per {
		s := SymDoc{
			Symbol: o.names[i], Trades: a.trades, Quotes: a.quotes,
			TradeLag: a.tradeLag.Summarize(), QuoteLag: a.quoteLag.Summarize(),
			TradeLagHour: a.hTradeLag.Summarize(), QuoteLagHour: a.hQuoteLag.Summarize(),
			PublishMs: a.pubMs.Summarize(), IDGaps: a.idGaps, ReconnectGaps: a.reconnectGaps,
			LastTradeNs: a.lastTradeRecv, LastQuoteNs: a.lastQuoteRx,
		}
		if secs > 1 {
			s.TradesPerSec = float64(a.hTrades) / secs
			s.QuotesPerSec = float64(a.hQuotes) / secs
		}
		if a.nextTotal > 0 {
			s.ENextTFrac = float64(a.nextT) / float64(a.nextTotal)
		}
		d.Symbols = append(d.Symbols, s)
	}
	return d
}
