package research

import (
	"testing"
	"time"
)

func TestMergeConcatenatesDays(t *testing.T) {
	cfg := StudyCfg{Leaders: []string{"L"}, Followers: []string{"F"}, Detectors: []DetectorCfg{PrimaryDetector()},
		LatMs: []int64{0}, HMs: []int64{1000}, From: time.Unix(0, 0), Days: 2}
	a, b := NewStudy(cfg), NewStudy(cfg)
	b.cfg.Days = 3
	b.resp = make([]DayStat, 3)
	b.pre = make([]DayStat, 3)
	b.events = make([]int64, 3)
	a.resp[0].add(1)
	a.resp[1].add(3)
	b.resp[2].add(8)
	m := Merge([]*Study{a, b})
	s := m.Response(0, 0, 0, 0, 0, 0, 0, 1)
	if s.N != 3 || s.Days != 3 || s.Mean != 4 {
		t.Fatalf("merged %+v", s)
	}
}

func TestSummarizeClusteredT(t *testing.T) {
	days := []DayStat{{N: 2, Sum: 2, SumSq: 2}, {N: 2, Sum: 6, SumSq: 18}}
	s := Summarize(days, 200, 1)
	if s.Mean != 2 || s.N != 4 || s.Days != 2 || !(s.Lo >= 1 && s.Hi <= 3) {
		t.Fatalf("%+v", s)
	}
}
