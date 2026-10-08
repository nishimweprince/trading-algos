package research

import (
	"math"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/source"
)

// synth builds two days of a leader (sym 0) and a follower (sym 1) whose fair
// price copies the leader's with a lag of lagMs (negative: the follower leads).
// The leader has a one-sided burst of flow, moving it 10 bp, every ~30 s.
func synth(lagMs int64, seed uint64) ([]model.Event, time.Time) {
	r := rand.New(rand.NewPCG(seed, 9))
	from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	start := from.UnixMilli()
	const step = 100
	n := int64(2 * 86_400_000 / step)
	lead := make([]float64, n) // leader log-price path
	burstLeft, burstDir := 0, 0.0
	type tr struct {
		t       int64
		px, qty float64
		buy     bool
		sym     uint16
	}
	var trades []tr
	for i := int64(1); i < n; i++ {
		t := start + i*step
		lead[i] = lead[i-1] + r.NormFloat64()*0.00001
		if burstLeft == 0 && r.Float64() < step/30_000.0 {
			burstLeft = 3
			burstDir = float64(1 - 2*r.IntN(2))
		}
		if burstLeft > 0 {
			burstLeft--
			lead[i] += burstDir * 0.001 / 3 // 10 bp over the burst
			trades = append(trades, tr{t, 100 * math.Exp(lead[i]), 5, burstDir > 0, 0})
		} else {
			trades = append(trades, tr{t, 100 * math.Exp(lead[i]), 0.05 + r.Float64()*0.05, r.IntN(2) == 0, 0})
		}
		// follower: fair = leader path lagged; trades at fair ± 0.5 bp
		j := i - lagMs/step
		if j < 0 {
			j = 0
		}
		if j >= n {
			j = n - 1
		}
		fair := 10 * math.Exp(lead[j])
		if lagMs < 0 {
			// leading follower: needs the future leader path; generated below
			continue
		}
		buy := r.IntN(2) == 0
		px := fair * (1 - 0.00005)
		if buy {
			px = fair * (1 + 0.00005)
		}
		trades = append(trades, tr{t, px, 1, buy, 1})
	}
	if lagMs < 0 {
		for i := int64(1); i < n; i++ {
			j := i - lagMs/step
			if j >= n {
				j = n - 1
			}
			fair := 10 * math.Exp(lead[j])
			buy := r.IntN(2) == 0
			px := fair * (1 - 0.00005)
			if buy {
				px = fair * (1 + 0.00005)
			}
			trades = append(trades, tr{start + i*step, px, 1, buy, 1})
		}
		// keep time order (leader before follower within a ms)
		sortTrades := func() {
			ts := trades
			// stable merge: both halves are already time ordered
			out := make([]tr, 0, len(ts))
			a, b := ts[:n-1], ts[n-1:]
			for len(a) > 0 || len(b) > 0 {
				if len(b) == 0 || (len(a) > 0 && a[0].t <= b[0].t) {
					out = append(out, a[0])
					a = a[1:]
				} else {
					out = append(out, b[0])
					b = b[1:]
				}
			}
			trades = out
		}
		sortTrades()
	}
	evs := make([]model.Event, len(trades))
	for i, x := range trades {
		evs[i] = model.Event{TradeMs: x.t, ExchMs: x.t, ID: int64(i), Px: int64(x.px * model.Scale),
			Qty: int64(x.qty * model.Scale), Sym: x.sym, Kind: model.KindTrade, BuyerMaker: !x.buy}
	}
	return evs, from
}

func synthStudy(t *testing.T, lagMs int64) *Study {
	t.Helper()
	evs, from := synth(lagMs, 42)
	det := PrimaryDetector()
	det.HalfLifeMs, det.WarmupMs = 5*60_000, 5*60_000
	s := NewStudy(StudyCfg{
		Leaders: []string{"LEAD"}, Followers: []string{"FOLL"}, Detectors: []DetectorCfg{det},
		LatMs: []int64{0, 100, 1000}, HMs: []int64{1000}, DelayMs: map[string]int64{"LEAD": 0},
		ShiftsMs: []int64{0, 3_600_000}, From: from, Days: 2,
	})
	if err := s.Run(&source.Slice{Events: evs}); err != nil {
		t.Fatal(err)
	}
	if s.Events(0, 0) < 1000 {
		t.Fatalf("only %d events", s.Events(0, 0))
	}
	return s
}

// TestSyntheticLagRecovered: a follower lagging 300 ms is caught at L = 0 and
// 100 ms; at L = 1000 ms the move is over; the 1 h placebo is null.
func TestSyntheticLagRecovered(t *testing.T) {
	s := synthStudy(t, 300)
	at := func(sh, lat int) Summary { return s.Response(sh, 0, 0, 0, lat, 0, 500, 1) }
	l0, l100, l1000, placebo := at(0, 0), at(0, 1), at(0, 2), at(1, 0)
	t.Logf("L=0 %+v\nL=100 %+v\nL=1000 %+v\nplacebo %+v", l0, l100, l1000, placebo)
	if !(l0.Lo > 5 && l100.Lo > 5) {
		t.Errorf("lag not recovered: L=0 %+v L=100 %+v", l0, l100)
	}
	if math.Abs(l1000.Mean) > 1 {
		t.Errorf("response after the lag elapsed: %+v", l1000)
	}
	if math.Abs(placebo.Mean) > 1 || !(placebo.Lo < 0 && placebo.Hi > 0) {
		t.Errorf("placebo not null: %+v", placebo)
	}
}

// TestSyntheticNoLookahead: a follower that moves 300 ms BEFORE the leader
// shows no response at L ≥ 0 (guards against a stale-side mid estimator,
// which leaked +0.59 bp here).
func TestSyntheticNoLookahead(t *testing.T) {
	s := synthStudy(t, -300)
	r := s.Response(0, 0, 0, 0, 0, 0, 500, 1)
	pre := s.PreDrift(0, 0, 0, 0, 1, 500, 1) // L = 100 ms
	t.Logf("response %+v\npre-drift %+v", r, pre)
	if math.Abs(r.Mean) > 0.2 {
		t.Errorf("response to a move that already happened: %+v", r)
	}
	// The move finished before the event, so even the drift between the
	// event and entry is null: nothing about it can be traded.
	if math.Abs(pre.Mean) > 0.2 {
		t.Errorf("pre-drift %+v", pre)
	}
}

// TestObservabilityDelayCostsTheEdge: with δ larger than the lag, the edge is gone.
func TestObservabilityDelayCostsTheEdge(t *testing.T) {
	evs, from := synth(300, 7)
	det := PrimaryDetector()
	det.HalfLifeMs, det.WarmupMs = 5*60_000, 5*60_000
	s := NewStudy(StudyCfg{Leaders: []string{"LEAD"}, Followers: []string{"FOLL"}, Detectors: []DetectorCfg{det},
		LatMs: []int64{0}, HMs: []int64{1000}, DelayMs: map[string]int64{"LEAD": 600}, From: from, Days: 2})
	if err := s.Run(&source.Slice{Events: evs}); err != nil {
		t.Fatal(err)
	}
	if r := s.Response(0, 0, 0, 0, 0, 0, 0, 1); math.Abs(r.Mean) > 1 {
		t.Errorf("δ = 600 ms > lag 300 ms still shows %+v", r)
	}
}
