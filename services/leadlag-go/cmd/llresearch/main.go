// Command llresearch runs the M3 signal study (research/PREREG.md part 1).
//
//	llresearch delays     --tape /data/leadlag/tape --hours 24 --out research/delays.json
//	llresearch screen     --from 2025-10-01 --to 2026-01-31 --followers @screen-candidates.txt --out reports/screen
//	llresearch freeze     --screen reports/screen/screen.json --out research/prereg.json
//	llresearch eventstudy --prereg research/prereg.json --probe probe.json --out reports/g1
//	llresearch shadow     --journal /data/leadlag/shadow --run explore-3a   (PREREG Part 4 kill criteria)
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/bits"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/archive"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/parse"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/research"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/source"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/symbols"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/tape"
)

var log = slog.New(slog.NewTextHandler(os.Stderr, nil))

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "delays":
		err = runDelays(os.Args[2:])
	case "screen":
		err = runScreen(os.Args[2:])
	case "freeze":
		err = runFreeze(os.Args[2:])
	case "eventstudy":
		err = runEventStudy(os.Args[2:])
	case "shadow":
		err = runShadow(os.Args[2:])
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "llresearch:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: llresearch delays|screen|freeze|eventstudy|shadow [flags]  (-h for flags)")
	os.Exit(2)
}

// ---------- delays ----------

// DelayDoc is delays.json: each symbol's aggTrade publish delay E − T (ms) and
// the observability δ = p90 rounded up to 10 ms (PREREG 1.2).
type DelayDoc struct {
	FromNs  int64                `json:"from_ns"`
	ToNs    int64                `json:"to_ns"`
	Files   int                  `json:"files"`
	Symbols map[string]DelayStat `json:"symbols"`
}

// DelayStat is one symbol's delay distribution.
type DelayStat struct {
	N       uint64 `json:"n"`
	P50     int64  `json:"p50_ms"`
	P90     int64  `json:"p90_ms"`
	P99     int64  `json:"p99_ms"`
	DeltaMs int64  `json:"delta_ms"`
}

func runDelays(args []string) error {
	fs := flag.NewFlagSet("delays", flag.ExitOnError)
	dir := fs.String("tape", "/data/leadlag/tape", "tape directory")
	hours := fs.Float64("hours", 24, "use the first N hours of tape")
	out := fs.String("out", "delays.json", "output JSON")
	fs.Parse(args)
	files, err := tape.Files(*dir)
	if err != nil {
		return err
	}
	r := tape.NewReader(files)
	defer r.Close()
	hist := map[string]*stats.Hist{}
	var syms parse.Symbols
	var names []string
	var first, last int64
	var l tape.Line
	var e model.Event
	for {
		err := r.Next(&l)
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		switch l.Kind {
		case "hello":
			var w struct {
				Hello struct {
					Symbols []string `json:"symbols"`
				} `json:"hello"`
			}
			if json.Unmarshal(l.Raw, &w) == nil {
				names = w.Hello.Symbols
				syms = parse.NewSymbols(names)
			}
		case "frame":
			if syms == nil {
				continue
			}
			if first == 0 {
				first = l.RecvNs
			}
			if float64(l.RecvNs-first) > *hours*float64(time.Hour) {
				goto done
			}
			last = l.RecvNs
			if parse.Frame(l.Frame, syms, &e) != nil || e.Kind != model.KindTrade {
				continue
			}
			name := names[e.Sym]
			if hist[name] == nil {
				hist[name] = &stats.Hist{}
			}
			hist[name].Record(e.ExchMs - e.TradeMs)
		}
	}
done:
	doc := DelayDoc{FromNs: first, ToNs: last, Files: len(files), Symbols: map[string]DelayStat{}}
	for name, h := range hist {
		// Exact percentiles are not needed: δ rounds up to 10 ms and the histogram's
		// bucket upper edge is used, which only errs late (conservative).
		p90 := upper(h, 0.9)
		doc.Symbols[name] = DelayStat{N: h.N, P50: upper(h, 0.5), P90: p90, P99: upper(h, 0.99), DeltaMs: (p90 + 9) / 10 * 10}
	}
	hrs := float64(last-first) / float64(time.Hour)
	if hrs < *hours*0.95 {
		log.Warn("less tape than requested; δ is provisional", "hours", fmt.Sprintf("%.1f", hrs), "want", *hours)
	}
	return writeJSON(*out, doc)
}

// upper returns the upper edge of the histogram bucket holding quantile q
// (Quantile reports the midpoint); erring late keeps δ conservative.
func upper(h *stats.Hist, q float64) int64 {
	v := h.Quantile(q)
	if v < 8 {
		return v
	}
	width := int64(1) << (bits.Len64(uint64(v)) - 1 - 3)
	return min(v-v%width+width, h.Max)
}

// ---------- common study flags ----------

type studyFlags struct {
	root, from, to, leaders, followers string
	delays                             string
	delayMs                            int64
	out                                string
	cache                              string
	boot                               int
}

func (f *studyFlags) register(fs *flag.FlagSet) {
	fs.StringVar(&f.root, "root", envOr("LL_ARCHIVE_ROOT", "/data/leadlag/archive"), "archive root")
	fs.StringVar(&f.from, "from", "", "first day YYYY-MM-DD")
	fs.StringVar(&f.to, "to", "", "last day YYYY-MM-DD")
	fs.StringVar(&f.leaders, "leaders", "BTCUSDT,ETHUSDT", "leaders (first is primary)")
	fs.StringVar(&f.followers, "followers", "", "followers: comma list or @file")
	fs.StringVar(&f.delays, "delays", "", "delays.json from `llresearch delays` (else --delay-ms)")
	fs.Int64Var(&f.delayMs, "delay-ms", 150, "observability δ for every leader when --delays is not given (provisional)")
	fs.StringVar(&f.out, "out", "reports", "output directory")
	fs.StringVar(&f.cache, "symbols-cache", envOr("LL_SYMBOLS_CACHE", filepath.Join(os.TempDir(), "leadlag-exchangeinfo.json")), "exchangeInfo cache")
	fs.IntVar(&f.boot, "boot", 2000, "bootstrap replicates")
}

type window struct {
	from, to time.Time
	days     int
}

func parseWindow(from, to string) (window, error) {
	a, err := time.Parse(time.DateOnly, from)
	if err != nil {
		return window{}, fmt.Errorf("--from: %w", err)
	}
	b, err := time.Parse(time.DateOnly, to)
	if err != nil {
		return window{}, fmt.Errorf("--to: %w", err)
	}
	if b.Before(a) {
		return window{}, errors.New("--to before --from")
	}
	return window{a, b, int(b.Sub(a)/(24*time.Hour)) + 1}, nil
}

func list(s string) ([]string, error) {
	if strings.HasPrefix(s, "@") {
		b, err := os.ReadFile(s[1:])
		if err != nil {
			return nil, err
		}
		s = string(b)
	}
	var out []string
	for _, p := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == '\n' || r == ' ' }) {
		if p = strings.ToUpper(strings.TrimSpace(p)); p != "" {
			out = append(out, p)
		}
	}
	return out, nil
}

// delayMap returns δ per leader and where it came from.
func (f *studyFlags) delayMap(leaders []string) (map[string]int64, string, error) {
	m := map[string]int64{}
	if f.delays == "" {
		for _, l := range leaders {
			m[l] = f.delayMs
		}
		return m, fmt.Sprintf("provisional %d ms", f.delayMs), nil
	}
	var doc DelayDoc
	if err := readJSON(f.delays, &doc); err != nil {
		return nil, "", err
	}
	for _, l := range leaders {
		d, ok := doc.Symbols[l]
		if !ok {
			return nil, "", fmt.Errorf("%s has no delay in %s", l, f.delays)
		}
		m[l] = d.DeltaMs
	}
	return m, "frozen " + f.delays, nil
}

// skipDays lists days where any symbol's manifest shows id gaps (gate G0), and
// days a leader is missing.
func skipDays(l archive.Layout, w window, leaders, followers []string) (map[string]bool, map[string][]string) {
	skip := map[string]bool{}
	missing := map[string][]string{}
	for d := w.from; !d.After(w.to); d = d.AddDate(0, 0, 1) {
		day := d.Format(time.DateOnly)
		for i, sym := range append(append([]string(nil), leaders...), followers...) {
			m, err := l.ReadManifest(sym, day)
			if err != nil {
				missing[sym] = append(missing[sym], day)
				if i < len(leaders) {
					skip[day] = true
				}
				continue
			}
			if len(m.GapRanges) > 0 || m.TimeBackward > 0 {
				skip[day] = true
			}
		}
	}
	return skip, missing
}

func ticks(cache string, syms []string) map[string]float64 {
	out := map[string]float64{}
	tbl, err := symbols.Load(context.Background(), &http.Client{Timeout: 20 * time.Second}, cache, 7*24*time.Hour)
	if tbl == nil {
		log.Warn("no exchangeInfo; spread floor disabled", "err", err)
		return out
	}
	for _, s := range syms {
		if i, ok := tbl.Get(s); ok {
			out[s] = float64(i.Tick) / model.Scale
		}
	}
	return out
}

func runStudy(f *studyFlags, w window, leaders, followers []string, dets []research.DetectorCfg, lat, hs, shifts []int64) (*research.Study, map[string][]string, map[string]bool, string, error) {
	layout := archive.Layout{Root: f.root}
	skip, missing := skipDays(layout, w, leaders, followers)
	delays, delaySrc, err := f.delayMap(leaders)
	if err != nil {
		return nil, nil, nil, "", err
	}
	all := append(append([]string(nil), leaders...), followers...)
	src, err := source.OpenArchive(source.ArchiveConfig{Layout: layout, Symbols: all, From: w.from, To: w.to, SkipDays: skip, AllowMissing: true})
	if err != nil {
		return nil, nil, nil, "", err
	}
	defer src.Close()
	st := research.NewStudy(research.StudyCfg{
		Leaders: leaders, Followers: followers, Detectors: dets, LatMs: lat, HMs: hs,
		DelayMs: delays, ShiftsMs: shifts, From: w.from, Days: w.days, Tick: ticks(f.cache, followers),
	})
	log.Info("study", "from", w.from.Format(time.DateOnly), "to", w.to.Format(time.DateOnly), "days", w.days,
		"skipped_days", len(skip), "leaders", leaders, "followers", len(followers), "delay", delaySrc)
	start := time.Now()
	var e model.Event
	var n int64
	for {
		err := src.Next(&e)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, nil, nil, "", err
		}
		st.OnTrade(&e)
		if n++; n%50_000_000 == 0 {
			log.Info("progress", "day", time.UnixMilli(e.TradeMs).UTC().Format(time.DateOnly), "state", st.String(),
				"rate_m_per_s", fmt.Sprintf("%.1f", float64(n)/time.Since(start).Seconds()/1e6))
		}
	}
	st.Finish()
	log.Info("study done", "state", st.String(), "elapsed", time.Since(start).Round(time.Second))
	return st, missing, skip, delaySrc, nil
}

// ---------- screen ----------

// ScreenRow is one candidate's screen result (PREREG 1.6).
type ScreenRow struct {
	Symbol       string                      `json:"symbol"`
	HalfSpreadBp float64                     `json:"half_spread_bp"`
	HurdleBp     float64                     `json:"hurdle_bp"`
	Score        float64                     `json:"score"`
	Primary      research.Summary            `json:"primary_btc_l20_h1000"`
	Cells        map[string]research.Summary `json:"cells"`
	MissingDays  int                         `json:"missing_days"`
}

// ScreenDoc is screen.json.
type ScreenDoc struct {
	Window      string               `json:"window"`
	Leaders     []string             `json:"leaders"`
	Delay       string               `json:"delay"`
	TakerFeeBp  float64              `json:"taker_fee_bp"`
	Detector    research.DetectorCfg `json:"detector"`
	Events      map[string]int64     `json:"events"`
	SkippedDays []string             `json:"skipped_days"`
	Rows        []ScreenRow          `json:"rows"`
	PreregSHA   string               `json:"prereg_sha256"`
	CreatedAt   string               `json:"created_at"`
}

func runScreen(args []string) error {
	fs := flag.NewFlagSet("screen", flag.ExitOnError)
	var f studyFlags
	f.register(fs)
	fee := fs.Float64("taker-fee-bp", 5, "taker fee per side (bp), VIP 0 until M4 freezes the tier")
	prereg := fs.String("prereg-md", "research/PREREG.md", "PREREG.md (its sha256 is recorded)")
	fs.Parse(args)
	if f.from == "" {
		f.from, f.to = "2025-10-01", "2026-01-31"
	}
	w, err := parseWindow(f.from, f.to)
	if err != nil {
		return err
	}
	leaders, _ := list(f.leaders)
	followers, err := list(f.followers)
	if err != nil || len(followers) == 0 {
		return fmt.Errorf("--followers: %v", err)
	}
	lat := []int64{0, 20}
	hs := []int64{250, 1000, 5000}
	st, missing, skip, delaySrc, err := runStudy(&f, w, leaders, followers, []research.DetectorCfg{research.PrimaryDetector()}, lat, hs, nil)
	if err != nil {
		return err
	}
	doc := ScreenDoc{Window: f.from + " → " + f.to, Leaders: leaders, Delay: delaySrc, TakerFeeBp: *fee,
		Detector: research.PrimaryDetector(), Events: map[string]int64{}, PreregSHA: sha(*prereg), CreatedAt: now()}
	for l, name := range leaders {
		doc.Events[name] = st.Events(0, l)
	}
	for d := range skip {
		doc.SkippedDays = append(doc.SkippedDays, d)
	}
	sort.Strings(doc.SkippedDays)
	for fi, sym := range followers {
		row := ScreenRow{Symbol: sym, HalfSpreadBp: st.HalfSpreadBp(fi), Cells: map[string]research.Summary{}, MissingDays: len(missing[sym])}
		row.HurdleBp = 2**fee + 2*row.HalfSpreadBp
		for l, lname := range leaders {
			for li, L := range lat {
				for hi, h := range hs {
					s := st.Response(0, 0, l, fi, li, hi, f.boot, 1)
					row.Cells[fmt.Sprintf("%s/L%d/h%d", lname, L, h)] = s
				}
				row.Cells[fmt.Sprintf("%s/L%d/pre", lname, L)] = st.PreDrift(0, 0, l, fi, li, f.boot, 1)
			}
		}
		row.Primary = row.Cells[fmt.Sprintf("%s/L20/h1000", leaders[0])]
		if row.HurdleBp > 0 && !math.IsNaN(row.Primary.Lo) {
			row.Score = row.Primary.Lo / row.HurdleBp
		}
		doc.Rows = append(doc.Rows, row)
	}
	sort.Slice(doc.Rows, func(a, b int) bool { return doc.Rows[a].Score > doc.Rows[b].Score })
	if err := os.MkdirAll(f.out, 0o755); err != nil {
		return err
	}
	if err := writeJSON(filepath.Join(f.out, "screen.json"), doc); err != nil {
		return err
	}
	if err := writeScreenCSV(filepath.Join(f.out, "screen.csv"), doc); err != nil {
		return err
	}
	printScreen(doc)
	return nil
}

func printScreen(doc ScreenDoc) {
	fmt.Printf("screen %s  δ: %s  events %v  skipped days %d\n", doc.Window, doc.Delay, doc.Events, len(doc.SkippedDays))
	fmt.Printf("%-14s %8s %8s %8s %8s %8s %7s %6s %9s\n", "symbol", "mean", "ci_lo", "ci_hi", "t", "hs_bp", "hurdle", "score", "n")
	for _, r := range doc.Rows {
		p := r.Primary
		fmt.Printf("%-14s %8.3f %8.3f %8.3f %8.2f %8.3f %7.2f %6.3f %9d\n", r.Symbol, p.Mean, p.Lo, p.Hi, p.TStat, r.HalfSpreadBp, r.HurdleBp, r.Score, p.N)
	}
}

func writeScreenCSV(path string, doc ScreenDoc) error {
	fh, err := os.Create(path)
	if err != nil {
		return err
	}
	defer fh.Close()
	w := csv.NewWriter(fh)
	w.Write([]string{"symbol", "cell", "n", "days", "mean_bp", "ci_lo_bp", "ci_hi_bp", "t", "sd_bp", "half_spread_bp", "hurdle_bp", "score"})
	for _, r := range doc.Rows {
		keys := make([]string, 0, len(r.Cells))
		for k := range r.Cells {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			s := r.Cells[k]
			w.Write([]string{r.Symbol, k, itoa(s.N), strconv.Itoa(s.Days), ff(s.Mean), ff(s.Lo), ff(s.Hi), ff(s.TStat), ff(s.SDbp), ff(r.HalfSpreadBp), ff(r.HurdleBp), ff(r.Score)})
		}
	}
	w.Flush()
	return w.Error()
}

// ---------- freeze ----------

// Prereg is research/prereg.json: the frozen M3 universe.
type Prereg struct {
	Version     int      `json:"version"`
	Leaders     []string `json:"leaders"`
	Followers   []string `json:"followers"`
	ScreenFile  string   `json:"screen_file"`
	ScreenSHA   string   `json:"screen_sha256"`
	PreregMDSHA string   `json:"prereg_md_sha256"`
	FrozenAt    string   `json:"frozen_at"`
	Note        string   `json:"note,omitempty"`
}

func runFreeze(args []string) error {
	fs := flag.NewFlagSet("freeze", flag.ExitOnError)
	screen := fs.String("screen", "reports/screen/screen.json", "screen.json")
	out := fs.String("out", "research/prereg.json", "prereg.json to write")
	md := fs.String("prereg-md", "research/PREREG.md", "PREREG.md")
	top := fs.Int("top", 8, "followers to freeze")
	fs.Parse(args)
	if _, err := os.Stat(*out); err == nil {
		return fmt.Errorf("%s exists: the universe is frozen once (PREREG 1.6)", *out)
	}
	var doc ScreenDoc
	if err := readJSON(*screen, &doc); err != nil {
		return err
	}
	p := Prereg{Version: 1, Leaders: doc.Leaders, ScreenFile: *screen, ScreenSHA: sha(*screen), PreregMDSHA: sha(*md), FrozenAt: now()}
	for _, r := range doc.Rows {
		if len(p.Followers) == *top {
			break
		}
		if r.Primary.Lo > 0 {
			p.Followers = append(p.Followers, r.Symbol)
		}
	}
	if len(p.Followers) < 3 {
		p.Note = fmt.Sprintf("only %d candidates have a CI lower bound > 0: G1 cannot pass (PREREG 1.6)", len(p.Followers))
	}
	fmt.Printf("frozen followers (%d): %s\n", len(p.Followers), strings.Join(p.Followers, ","))
	if p.Note != "" {
		fmt.Println(p.Note)
	}
	return writeJSON(*out, p)
}

// ---------- event study + G1 ----------

// G1Doc is the gate decision (PREREG 1.7).
type G1Doc struct {
	Window      string           `json:"window"`
	LStarMs     int64            `json:"l_star_ms"`
	LStarSource string           `json:"l_star_source"`
	Delay       string           `json:"delay"`
	Followers   []G1Row          `json:"followers"`
	Passing     []string         `json:"passing"`
	Synthetic   string           `json:"synthetic"`
	Pass        bool             `json:"pass"`
	Events      map[string]int64 `json:"events_primary"`
	SkippedDays int              `json:"skipped_days"`
	CreatedAt   string           `json:"created_at"`
}

// G1Row is one frozen follower at L*, h = 1000 ms, BTC events.
type G1Row struct {
	Symbol    string           `json:"symbol"`
	Real      research.Summary `json:"real"`
	Placebo1h research.Summary `json:"placebo_1h"`
	Placebo2h research.Summary `json:"placebo_2h"`
	PreDrift  research.Summary `json:"pre_drift"`
	Signal    bool             `json:"signal"`
	PlaceboOK bool             `json:"placebo_ok"`
}

func runEventStudy(args []string) error {
	fs := flag.NewFlagSet("eventstudy", flag.ExitOnError)
	var f studyFlags
	f.register(fs)
	preregPath := fs.String("prereg", "research/prereg.json", "frozen universe")
	probe := fs.String("probe", "/home/ll/leadlag/probe.json", "llprobe report for L*")
	lstar := fs.Int64("lstar-ms", -1, "override L* (ms) instead of reading --probe")
	window2 := fs.String("also", "2026-02-01:2026-03-31", "second G1 window (from:to), empty for none")
	fs.Parse(args)
	if f.from == "" {
		f.from, f.to = "2025-01-01", "2025-09-30"
	}
	var p Prereg
	if err := readJSON(*preregPath, &p); err != nil {
		return err
	}
	leaders, followers := p.Leaders, p.Followers
	if len(followers) == 0 {
		return errors.New("prereg has no followers")
	}
	lat := []int64{0, 5, 10, 20, 50, 100, 200}
	hs := []int64{100, 250, 500, 1000, 2000, 5000}
	var dets []research.DetectorCfg
	for _, W := range []int64{500, 100, 250, 1000} { // primary first
		for _, th := range []float64{3, 2, 4} {
			d := research.PrimaryDetector()
			d.WMs, d.Theta = W, th
			dets = append(dets, d)
		}
	}
	ls, lsrc, err := lStar(*probe, *lstar, lat)
	if err != nil {
		return err
	}
	shifts := []int64{0, 3_600_000, 7_200_000}
	windows := [][2]string{{f.from, f.to}}
	if *window2 != "" {
		a, b, ok := strings.Cut(*window2, ":")
		if !ok {
			return errors.New("--also wants from:to")
		}
		windows = append(windows, [2]string{a, b})
	}
	// The G1 window is two date ranges; each runs as its own pass and the per-day
	// accumulators are pooled by summing cell summaries' day stats via a merge.
	var studies []*research.Study
	var delaySrc string
	skipped := 0
	for _, wn := range windows {
		w, err := parseWindow(wn[0], wn[1])
		if err != nil {
			return err
		}
		st, _, skip, ds, err := runStudy(&f, w, leaders, followers, dets, lat, hs, shifts)
		if err != nil {
			return err
		}
		studies = append(studies, st)
		delaySrc = ds
		skipped += len(skip)
	}
	merged := research.Merge(studies)
	if err := os.MkdirAll(f.out, 0o755); err != nil {
		return err
	}
	if err := writeGridCSV(filepath.Join(f.out, "eventstudy.csv"), merged, leaders, followers, dets, lat, hs, shifts, f.boot); err != nil {
		return err
	}
	li := indexOf(lat, ls)
	hi := indexOf(hs, 1000)
	g := G1Doc{Window: strings.Join(func() []string {
		var s []string
		for _, w := range windows {
			s = append(s, w[0]+" → "+w[1])
		}
		return s
	}(), " + "), LStarMs: ls, LStarSource: lsrc, Delay: delaySrc, Events: map[string]int64{}, SkippedDays: skipped,
		Synthetic: "go test ./internal/research -run Synthetic (CI)", CreatedAt: now()}
	for l, name := range leaders {
		g.Events[name] = merged.Events(0, l)
	}
	for fi, sym := range followers {
		r := G1Row{Symbol: sym,
			Real:      merged.Response(0, 0, 0, fi, li, hi, f.boot, 1),
			Placebo1h: merged.Response(1, 0, 0, fi, li, hi, f.boot, 1),
			Placebo2h: merged.Response(2, 0, 0, fi, li, hi, f.boot, 1),
			PreDrift:  merged.PreDrift(0, 0, 0, fi, li, f.boot, 1)}
		r.Signal = r.Real.Lo > 0
		null := func(s research.Summary) bool {
			return (s.Lo <= 0 && s.Hi >= 0) || math.Abs(s.Mean) < math.Abs(r.Real.Mean)/3
		}
		r.PlaceboOK = null(r.Placebo1h) && null(r.Placebo2h)
		if r.Signal && r.PlaceboOK {
			g.Passing = append(g.Passing, sym)
		}
		g.Followers = append(g.Followers, r)
	}
	g.Pass = len(g.Passing) >= 3
	if err := writeJSON(filepath.Join(f.out, "g1.json"), g); err != nil {
		return err
	}
	fmt.Printf("G1 %s  L*=%d ms (%s)  δ: %s  BTC events %d\n", g.Window, g.LStarMs, g.LStarSource, g.Delay, g.Events[leaders[0]])
	fmt.Printf("%-14s %9s %9s %9s %8s %10s %10s %9s %s\n", "follower", "mean", "ci_lo", "ci_hi", "t", "plc1h", "plc2h", "pre", "pass")
	for _, r := range g.Followers {
		fmt.Printf("%-14s %9.3f %9.3f %9.3f %8.2f %10.3f %10.3f %9.3f %v\n", r.Symbol, r.Real.Mean, r.Real.Lo, r.Real.Hi, r.Real.TStat,
			r.Placebo1h.Mean, r.Placebo2h.Mean, r.PreDrift.Mean, r.Signal && r.PlaceboOK)
	}
	fmt.Printf("G1 %s: %d of %d frozen followers pass (need 3)\n", map[bool]string{true: "PASSES", false: "FAILS"}[g.Pass], len(g.Passing), len(followers))
	return nil
}

// lStar is the smallest grid L ≥ probe warm REST p50 + 2 ms (PREREG 1.7).
func lStar(probe string, override int64, grid []int64) (int64, string, error) {
	need := override
	src := "override"
	if override < 0 {
		var doc struct {
			RESTWarmUs stats.Summary `json:"rest_warm_us"`
			AZID       string        `json:"az_id"`
		}
		if err := readJSON(probe, &doc); err != nil {
			return 0, "", fmt.Errorf("--probe: %w (or pass --lstar-ms)", err)
		}
		need = (doc.RESTWarmUs.P50+999)/1000 + 2
		src = fmt.Sprintf("probe %s REST p50 %.2f ms + 2", doc.AZID, float64(doc.RESTWarmUs.P50)/1000)
	}
	for _, L := range grid {
		if L >= need {
			return L, src, nil
		}
	}
	return 0, "", fmt.Errorf("no grid latency reaches %d ms", need)
}

func writeGridCSV(path string, st *research.Study, leaders, followers []string, dets []research.DetectorCfg, lat, hs, shifts []int64, boot int) error {
	fh, err := os.Create(path)
	if err != nil {
		return err
	}
	defer fh.Close()
	bw := bufio.NewWriter(fh)
	w := csv.NewWriter(bw)
	w.Write([]string{"shift_ms", "w_ms", "theta", "leader", "follower", "lat_ms", "h_ms", "n", "days", "mean_bp", "ci_lo_bp", "ci_hi_bp", "t", "sd_bp"})
	for si, sh := range shifts {
		for di, d := range dets {
			for l, ln := range leaders {
				for fi, fn := range followers {
					for li, L := range lat {
						for hi, h := range hs {
							b := boot
							if si > 0 || di > 0 {
								b = 400 // descriptive cells: fewer replicates
							}
							s := st.Response(si, di, l, fi, li, hi, b, 1)
							w.Write([]string{itoa(sh), itoa(d.WMs), ff(d.Theta), ln, fn, itoa(L), itoa(h), itoa(s.N), strconv.Itoa(s.Days), ff(s.Mean), ff(s.Lo), ff(s.Hi), ff(s.TStat), ff(s.SDbp)})
						}
					}
				}
			}
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return err
	}
	return bw.Flush()
}

// ---------- helpers ----------

func indexOf(xs []int64, v int64) int {
	for i, x := range xs {
		if x == v {
			return i
		}
	}
	return -1
}

func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o644)
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

func sha(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func now() string { return time.Now().UTC().Format(time.RFC3339) }

func ff(v float64) string {
	if math.IsNaN(v) {
		return ""
	}
	return strconv.FormatFloat(v, 'f', 4, 64)
}

func itoa(v int64) string { return strconv.FormatInt(v, 10) }

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
