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

// ShadowReport is one run's verdict numbers (PREREG Part 4 §4.7 or Part 5 §5.6).
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
// criteria of PREREG Part 4 (explore-3) or, with --part 5, the verdict of Part 5
// (explore-4). --maker-fee/--taker-fee re-price every round trip from its gross
// (e.g. USDT-M 2/5 bp if the USDC-M promotion ends).
func runShadow(args []string) error {
	fs := flag.NewFlagSet("shadow", flag.ExitOnError)
	dir := fs.String("journal", "/data/leadlag/shadow", "journal directory")
	run := fs.String("run", "", "run label")
	maker := fs.Float64("maker-fee", math.NaN(), "re-price at this maker fee (bp); default: the journal's fees")
	taker := fs.Float64("taker-fee", math.NaN(), "re-price at this taker fee (bp)")
	quoter := fs.Bool("quoter", false, "Part 4: apply the quoter-only criterion (f > 0.4)")
	part := fs.Int("part", 4, "PREREG part whose verdict to apply: 4 (explore-3) or 5 (explore-4)")
	boot := fs.Int("boot", 2000, "bootstrap replicates")
	fs.Parse(args)
	if *run == "" {
		return fmt.Errorf("--run is required")
	}
	if *part != 4 && *part != 5 {
		return fmt.Errorf("--part must be 4 or 5")
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
	if *part == 5 {
		rep.Verdict = part5Verdict(rep)
	}
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

// part5Verdict is PREREG Part 5 §5.6: a kill on the taker share at any time
// from 200 round trips; otherwise the day-block 95% CI decides at >= 7 days
// and >= 400 round trips, with one 7-day extension when it spans 0.
func part5Verdict(rep ShadowReport) string {
	span := rep.LastExit.Sub(rep.FirstExit)
	const week = 7 * 24 * time.Hour
	switch {
	case rep.Trades >= 200 && rep.TakerFrac > 0.25:
		return "kill: taker-exit share > 25% at >= 200 round trips"
	case span < week || rep.Trades < 400:
		return "continue"
	case rep.Net.Hi < 0:
		return "kill: 95% CI upper < 0 bp"
	case rep.Net.Lo > 0:
		return "pass: 95% CI lower > 0 bp"
	case span >= 2*week:
		return "stop: inconclusive after the one 7-day extension"
	default:
		return "extend: the CI spans 0; one 7-day extension, then decide"
	}
}
