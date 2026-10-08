package research

import (
	"container/heap"
	"fmt"
	"io"
	"math"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/source"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
)

// StudyCfg configures one pass over a merged trade stream whose symbol order is
// Leaders then Followers.
type StudyCfg struct {
	Leaders   []string
	Followers []string
	Detectors []DetectorCfg
	LatMs     []int64 // latency grid L
	HMs       []int64 // horizon grid h
	// DelayMs is each leader's observability delay δ (PREREG 1.2).
	DelayMs map[string]int64
	// ShiftsMs: 0 is the real study; non-zero values are placebos (t_sig moved).
	ShiftsMs []int64
	From     time.Time // first day (UTC) for day indexing
	Days     int
	// Tick sizes (price units) per follower, for the half-spread floor.
	Tick map[string]float64

	SpreadStaleMs int64 // both sides within this for a spread sample (1 s)
	GapMs         int64 // follower data gap that voids a window (10 min)
}

// Defaults fills unset tolerances.
func (c *StudyCfg) Defaults() {
	if c.SpreadStaleMs == 0 {
		c.SpreadStaleMs = 1_000
	}
	if c.GapMs == 0 {
		c.GapMs = 10 * 60_000
	}
	if len(c.ShiftsMs) == 0 {
		c.ShiftsMs = []int64{0}
	}
}

// DayStat accumulates one cell's responses on one day.
type DayStat struct {
	N     int64
	Sum   float64
	SumSq float64
}

func (d *DayStat) add(v float64) {
	d.N++
	d.Sum += v
	d.SumSq += v * v
}

// Study runs the pass and holds the accumulators.
type Study struct {
	cfg  StudyCfg
	nL   int
	nF   int
	nD   int
	nS   int
	nLat int
	nH   int

	resp   []DayStat // [s][d][l][f][lat][h][day]
	pre    []DayStat // [s][d][l][f][lat][day]
	events []int64   // [d][l][day]

	dets    [][]*Detector // [l][d]
	touches []Touch       // per symbol (leaders too)
	spread  []stats.Hist  // per follower, half-spread bp ×100
	pend    []pendHeap    // per follower
	live    map[int32]*evState
	nextID  int32
	lastT   int64

	Trades   int64
	Dropped  int64 // follower windows voided by a data gap or missing price
	Resolved int64
}

type evState struct {
	day       int32
	s, d, l   uint8
	dir       float64
	px        []float64 // [f][slot]
	remaining int
}

type pendItem struct {
	target int64
	ev     int32
	slot   uint16
}

type pendHeap []pendItem

func (h pendHeap) Len() int           { return len(h) }
func (h pendHeap) Less(i, j int) bool { return h[i].target < h[j].target }
func (h pendHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *pendHeap) Push(x any)        { *h = append(*h, x.(pendItem)) }
func (h *pendHeap) Pop() any {
	old := *h
	x := old[len(old)-1]
	*h = old[:len(old)-1]
	return x
}

// NewStudy allocates a study.
func NewStudy(cfg StudyCfg) *Study {
	cfg.Defaults()
	s := &Study{cfg: cfg, nL: len(cfg.Leaders), nF: len(cfg.Followers), nD: len(cfg.Detectors),
		nS: len(cfg.ShiftsMs), nLat: len(cfg.LatMs), nH: len(cfg.HMs), live: map[int32]*evState{}}
	cells := s.nS * s.nD * s.nL * s.nF * s.nLat
	s.resp = make([]DayStat, cells*s.nH*cfg.Days)
	s.pre = make([]DayStat, cells*cfg.Days)
	s.events = make([]int64, s.nD*s.nL*cfg.Days)
	s.dets = make([][]*Detector, s.nL)
	for l := range s.dets {
		for _, dc := range cfg.Detectors {
			s.dets[l] = append(s.dets[l], NewDetector(dc))
		}
	}
	s.touches = make([]Touch, s.nL+s.nF)
	s.spread = make([]stats.Hist, s.nF)
	s.pend = make([]pendHeap, s.nF)
	return s
}

func (s *Study) slots() int { return 1 + s.nLat + s.nLat*s.nH }

func (s *Study) cell(sh, d, l, f, lat int) int {
	return (((sh*s.nD+d)*s.nL+l)*s.nF+f)*s.nLat + lat
}

// Run consumes the source to the end.
func (s *Study) Run(src source.Source) error {
	var e model.Event
	for {
		err := src.Next(&e)
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		s.OnTrade(&e)
	}
	s.Finish()
	return nil
}

// OnTrade processes one archive trade (exported for tests and custom drivers).
func (s *Study) OnTrade(e *model.Event) {
	s.Trades++
	t := e.TradeMs
	s.lastT = t
	px := float64(e.Px) / model.Scale
	buy := !e.BuyerMaker
	sym := int(e.Sym)
	if sym >= s.nL {
		f := sym - s.nL
		s.resolve(f, t)
		s.touches[sym].Update(t, px, buy, s.cfg.SpreadStaleMs)
		if hs, ok := s.touches[sym].HalfSpreadBp(t, s.cfg.SpreadStaleMs); ok {
			if tick := s.cfg.Tick[s.cfg.Followers[f]]; tick > 0 {
				hs = math.Max(hs, tick/2/px*1e4)
			}
			s.spread[f].Record(int64(hs * 100))
		}
		return
	}
	s.touches[sym].Update(t, px, buy, s.cfg.SpreadStaleMs)
	signed := px * float64(e.Qty) / model.Scale
	if !buy {
		signed = -signed
	}
	for d, det := range s.dets[sym] {
		ev, ok := det.OnTrade(t, signed)
		if !ok {
			continue
		}
		day := int32((t - s.cfg.From.UnixMilli()) / 86_400_000)
		if day < 0 || int(day) >= s.cfg.Days {
			continue
		}
		s.events[(d*s.nL+sym)*s.cfg.Days+int(day)]++
		for sh := range s.cfg.ShiftsMs {
			s.schedule(ev, day, sh, d, sym)
		}
	}
}

func (s *Study) schedule(ev Event, day int32, sh, d, l int) {
	id := s.nextID
	s.nextID++
	shift := s.cfg.ShiftsMs[sh]
	tsig := ev.T + s.cfg.DelayMs[s.cfg.Leaders[l]] + shift
	st := &evState{day: day, s: uint8(sh), d: uint8(d), l: uint8(l), dir: float64(ev.Dir),
		px: make([]float64, s.nF*s.slots()), remaining: s.nF * s.slots()}
	s.live[id] = st
	for f := range s.nF {
		heap.Push(&s.pend[f], pendItem{target: ev.T + shift, ev: id, slot: 0})
		for li, L := range s.cfg.LatMs {
			t0 := tsig + L
			heap.Push(&s.pend[f], pendItem{target: t0, ev: id, slot: uint16(1 + li)})
			for hi, h := range s.cfg.HMs {
				heap.Push(&s.pend[f], pendItem{target: t0 + h, ev: id, slot: uint16(1 + s.nLat + li*s.nH + hi)})
			}
		}
	}
}

// resolve settles follower f's targets strictly before t, using the state
// after every follower trade with time ≤ target (PREREG 1.2).
func (s *Study) resolve(f int, t int64) {
	h := &s.pend[f]
	tc := &s.touches[s.nL+f]
	gap := tc.LastT > 0 && t-tc.LastT > s.cfg.GapMs
	for h.Len() > 0 && (*h)[0].target < t {
		it := heap.Pop(h).(pendItem)
		px := math.NaN()
		if !gap && tc.LastT > 0 {
			px = tc.Mid()
		}
		s.settle(it, f, px)
	}
}

func (s *Study) settle(it pendItem, f int, px float64) {
	st := s.live[it.ev]
	st.px[f*s.slots()+int(it.slot)] = px
	st.remaining--
	if st.remaining == 0 {
		s.finalize(st)
		delete(s.live, it.ev)
	}
}

func (s *Study) finalize(st *evState) {
	ns := s.slots()
	for f := range s.nF {
		p := st.px[f*ns : (f+1)*ns]
		pre := p[0]
		for li := range s.nLat {
			entry := p[1+li]
			c := s.cell(int(st.s), int(st.d), int(st.l), f, li)
			if !(entry > 0) {
				s.Dropped++
				continue
			}
			if pre > 0 {
				s.pre[c*s.cfg.Days+int(st.day)].add(st.dir * (entry - pre) / pre * 1e4)
			}
			for hi := range s.nH {
				exit := p[1+s.nLat+li*s.nH+hi]
				if !(exit > 0) {
					s.Dropped++
					continue
				}
				s.resp[(c*s.nH+hi)*s.cfg.Days+int(st.day)].add(st.dir * (exit - entry) / entry * 1e4)
				s.Resolved++
			}
		}
	}
}

// Finish settles what is left: targets at or before the last trade with the
// final state, later ones as missing.
func (s *Study) Finish() {
	for f := range s.nF {
		tc := &s.touches[s.nL+f]
		h := &s.pend[f]
		for h.Len() > 0 {
			it := heap.Pop(h).(pendItem)
			px := math.NaN()
			if it.target <= s.lastT && tc.LastT > 0 && s.lastT-tc.LastT <= s.cfg.GapMs {
				px = tc.Mid()
			}
			s.settle(it, f, px)
		}
	}
}

// Summary is one cell's pooled statistics.
type Summary struct {
	N     int64   `json:"n"`
	Days  int     `json:"days"`
	Mean  float64 `json:"mean_bp"`
	Lo    float64 `json:"ci_lo_bp"`
	Hi    float64 `json:"ci_hi_bp"`
	TStat float64 `json:"t_day_clustered"`
	SDbp  float64 `json:"sd_bp"`
}

// Response returns the pooled response summary for a cell.
func (s *Study) Response(sh, d, l, f, lat, h int, boot int, seed uint64) Summary {
	c := s.cell(sh, d, l, f, lat)
	base := (c*s.nH + h) * s.cfg.Days
	return Summarize(s.resp[base:base+s.cfg.Days], boot, seed)
}

// PreDrift returns the pre-entry drift summary for a cell.
func (s *Study) PreDrift(sh, d, l, f, lat int, boot int, seed uint64) Summary {
	base := s.cell(sh, d, l, f, lat) * s.cfg.Days
	return Summarize(s.pre[base:base+s.cfg.Days], boot, seed)
}

// Events returns event counts for detector d and leader l.
func (s *Study) Events(d, l int) int64 {
	var n int64
	for _, v := range s.events[(d*s.nL+l)*s.cfg.Days : (d*s.nL+l+1)*s.cfg.Days] {
		n += v
	}
	return n
}

// HalfSpreadBp is follower f's median half-spread estimate (bp).
func (s *Study) HalfSpreadBp(f int) float64 { return float64(s.spread[f].Quantile(0.5)) / 100 }

// Cfg returns the configuration (with defaults).
func (s *Study) Cfg() StudyCfg { return s.cfg }

// String is a one-line progress summary.
func (s *Study) String() string {
	return fmt.Sprintf("trades %d resolved %d dropped %d live events %d", s.Trades, s.Resolved, s.Dropped, len(s.live))
}

// Merge pools studies with identical grids over different date ranges by
// concatenating their per-day accumulators (days stay independent clusters).
func Merge(studies []*Study) *Study {
	if len(studies) == 1 {
		return studies[0]
	}
	a := studies[0]
	cfg := a.cfg
	cfg.Days = 0
	for _, s := range studies {
		cfg.Days += s.cfg.Days
	}
	m := NewStudy(cfg)
	cat := func(dst []DayStat, get func(*Study) []DayStat, cells int) {
		for c := range cells {
			off := c * cfg.Days
			for _, s := range studies {
				src := get(s)
				off += copy(dst[off:off+s.cfg.Days], src[c*s.cfg.Days:(c+1)*s.cfg.Days])
			}
		}
	}
	cells := a.nS * a.nD * a.nL * a.nF * a.nLat
	cat(m.resp, func(s *Study) []DayStat { return s.resp }, cells*a.nH)
	cat(m.pre, func(s *Study) []DayStat { return s.pre }, cells)
	for c := range a.nD * a.nL {
		off := c * cfg.Days
		for _, s := range studies {
			off += copy(m.events[off:off+s.cfg.Days], s.events[c*s.cfg.Days:(c+1)*s.cfg.Days])
		}
	}
	for _, s := range studies {
		for f := range m.spread {
			m.spread[f].Merge(&s.spread[f])
		}
		m.Trades += s.Trades
		m.Resolved += s.Resolved
		m.Dropped += s.Dropped
	}
	return m
}
