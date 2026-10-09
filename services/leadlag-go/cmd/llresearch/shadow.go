package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/research"
)

// shadowTrade is the part of a journal record the report needs.
type shadowTrade struct {
	Kind      string  `json:"kind"`
	Run       string  `json:"run"`
	ExitNs    int64   `json:"exit_recv_ns"`
	GrossBp   float64 `json:"gross_bp"`
	NetBp     float64 `json:"net_bp"`
	ExitKind  string  `json:"exit_kind"`
	EntryKind string  `json:"entry_kind"`
}

// ShadowReport is one run's PREREG Part 4 §4.7 numbers.
type ShadowReport struct {
	Run        string           `json:"run"`
	Trades     int              `json:"round_trips"`
	Days       int              `json:"days"`
	Net        research.Summary `json:"net_bp"`
	TakerFrac  float64          `json:"taker_exit_frac"`
	FeeBasis   string           `json:"fee_basis"`
	Verdict    string           `json:"verdict"`
	FirstExit  time.Time        `json:"first_exit"`
	LastExit   time.Time        `json:"last_exit"`
	ByExitKind map[string]int   `json:"by_exit_kind"`
}

// runShadow reads a run's journal (live <day>.jsonl files and the archived
// <run>/ ones), pools net bp by UTC day of the exit, and applies the kill
// criteria. --maker-fee/--taker-fee re-price every round trip from its gross
// (e.g. USDT-M 2/5 bp if the USDC-M promotion ends).
func runShadow(args []string) error {
	fs := flag.NewFlagSet("shadow", flag.ExitOnError)
	dir := fs.String("journal", "/data/leadlag/shadow", "journal directory")
	run := fs.String("run", "", "run label")
	maker := fs.Float64("maker-fee", math.NaN(), "re-price at this maker fee (bp); default: the journal's fees")
	taker := fs.Float64("taker-fee", math.NaN(), "re-price at this taker fee (bp)")
	quoter := fs.Bool("quoter", false, "apply the quoter-only criterion (f > 0.4)")
	boot := fs.Int("boot", 2000, "bootstrap replicates")
	fs.Parse(args)
	if *run == "" {
		return fmt.Errorf("--run is required")
	}
	files, _ := filepath.Glob(filepath.Join(*dir, "[0-9]*.jsonl"))
	more, _ := filepath.Glob(filepath.Join(*dir, *run, "[0-9]*.jsonl"))
	files = append(files, more...)
	sort.Strings(files)
	var trades []shadowTrade
	for _, f := range files {
		ts, err := readShadow(f, *run)
		if err != nil {
			return err
		}
		trades = append(trades, ts...)
	}
	rep := shadowReport(*run, trades, *maker, *taker, *quoter, *boot)
	b, _ := json.MarshalIndent(rep, "", "  ")
	fmt.Println(string(b))
	return nil
}

func readShadow(path, run string) ([]shadowTrade, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []shadowTrade
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var t shadowTrade
		if err := json.Unmarshal(sc.Bytes(), &t); err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		if t.Run == run && (t.Kind == "" || t.Kind == "trade") {
			out = append(out, t)
		}
	}
	return out, sc.Err()
}

func shadowReport(run string, trades []shadowTrade, makerFee, takerFee float64, quoter bool, boot int) ShadowReport {
	rep := ShadowReport{Run: run, Trades: len(trades), FeeBasis: "journal", ByExitKind: map[string]int{}}
	reprice := !math.IsNaN(makerFee) || !math.IsNaN(takerFee)
	if reprice {
		rep.FeeBasis = fmt.Sprintf("re-priced: maker %g bp, taker %g bp", makerFee, takerFee)
	}
	days := map[string]*research.DayStat{}
	takers := 0
	for _, t := range trades {
		net := t.NetBp
		if reprice {
			fee := func(kind string) float64 {
				if kind == "maker" {
					return makerFee
				}
				return takerFee
			}
			net = t.GrossBp - fee(t.EntryKind) - fee(t.ExitKind)
		}
		at := time.Unix(0, t.ExitNs).UTC()
		if rep.FirstExit.IsZero() || at.Before(rep.FirstExit) {
			rep.FirstExit = at
		}
		if at.After(rep.LastExit) {
			rep.LastExit = at
		}
		day := at.Format(time.DateOnly)
		d := days[day]
		if d == nil {
			d = &research.DayStat{}
			days[day] = d
		}
		d.N++
		d.Sum += net
		d.SumSq += net * net
		rep.ByExitKind[t.ExitKind]++
		if t.ExitKind != "maker" {
			takers++
		}
	}
	keys := make([]string, 0, len(days))
	for k := range days {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	ds := make([]research.DayStat, 0, len(keys))
	for _, k := range keys {
		ds = append(ds, *days[k])
	}
	rep.Days = len(ds)
	rep.Net = research.Summarize(ds, boot, 1)
	if len(trades) > 0 {
		rep.TakerFrac = float64(takers) / float64(len(trades))
	}
	span := rep.LastExit.Sub(rep.FirstExit)
	switch {
	case len(trades) >= 500 && rep.Net.Mean < 0 && rep.Net.Hi < 0.5:
		rep.Verdict = "stop: mean net < 0 and CI upper < +0.5 bp after >= 500 round trips"
	case quoter && len(trades) >= 500 && rep.TakerFrac > 0.4:
		rep.Verdict = "stop: taker-exit fraction f > 0.4"
	case span >= 14*24*time.Hour && len(trades) < 100:
		rep.Verdict = "stop: fewer than 100 round trips in 2 weeks"
	case span >= 14*24*time.Hour && len(trades) >= 500:
		rep.Verdict = "complete: write up (descriptive; no gate)"
	default:
		rep.Verdict = "continue"
	}
	return rep
}
