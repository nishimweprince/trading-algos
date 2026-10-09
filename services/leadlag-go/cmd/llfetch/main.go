// Command llfetch downloads Binance USDⓈ-M aggTrades from data.binance.vision,
// verifies each zip against its .CHECKSUM, packs it into per-day .llp files and
// checks the packed archive.
//
//	llfetch plan       --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31
//	llfetch fetch      --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31
//	llfetch verify     --symbols BTCUSDT,ETHUSDT --from 2025-10-01 --to 2026-01-31
//	llfetch candidates --n 30
//	llfetch candidates --quote USDC --onboarded-before ""   (USDC-M followers, explore-3)
//	llfetch ticks      --symbols SOLUSDC,XRPUSDC            (tick sizes for a shadow params file)
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/archive"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/symbols"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var err error
	switch cmd {
	case "plan":
		err = runPlan(ctx, args)
	case "fetch":
		err = runFetch(ctx, args)
	case "verify":
		err = runVerify(args)
	case "candidates":
		err = runCandidates(ctx, args)
	case "ticks":
		err = runTicks(ctx, args)
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "llfetch:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: llfetch plan|fetch|verify|candidates|ticks [flags]  (-h for flags)")
	os.Exit(2)
}

type rangeFlags struct {
	root    string
	symbols string
	from    string
	to      string
}

func (r *rangeFlags) register(fs *flag.FlagSet) {
	fs.StringVar(&r.root, "root", envOr("LL_ARCHIVE_ROOT", "/data/leadlag/archive"), "archive root (env LL_ARCHIVE_ROOT)")
	fs.StringVar(&r.symbols, "symbols", "", "comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
	fs.StringVar(&r.from, "from", "", "first UTC day, YYYY-MM-DD")
	fs.StringVar(&r.to, "to", "", "last UTC day, YYYY-MM-DD (default: yesterday)")
}

func (r *rangeFlags) parse() ([]string, time.Time, time.Time, error) {
	var syms []string
	for _, s := range strings.Split(r.symbols, ",") {
		if s = strings.ToUpper(strings.TrimSpace(s)); s != "" {
			syms = append(syms, s)
		}
	}
	if len(syms) == 0 {
		return nil, time.Time{}, time.Time{}, errors.New("--symbols is required")
	}
	from, err := time.Parse(time.DateOnly, r.from)
	if err != nil {
		return nil, time.Time{}, time.Time{}, fmt.Errorf("--from: %w", err)
	}
	to := time.Now().UTC().AddDate(0, 0, -1)
	if r.to != "" {
		if to, err = time.Parse(time.DateOnly, r.to); err != nil {
			return nil, time.Time{}, time.Time{}, fmt.Errorf("--to: %w", err)
		}
	}
	if to.Before(from) {
		return nil, time.Time{}, time.Time{}, errors.New("--to is before --from")
	}
	return syms, from, to, nil
}

func runPlan(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("plan", flag.ExitOnError)
	var rf rangeFlags
	rf.register(fs)
	fs.Parse(args)
	syms, from, to, err := rf.parse()
	if err != nil {
		return err
	}
	f := &archive.Fetcher{Layout: archive.Layout{Root: rf.root}, Client: &http.Client{Timeout: time.Minute}}
	jobs := archive.Plan(f.Layout, syms, from, to, time.Now())
	perSym := map[string]int64{}
	var total int64
	var missing []string
	for _, j := range jobs {
		n, err := f.Size(ctx, j)
		if errors.Is(err, archive.ErrNotFound) {
			missing = append(missing, j.Name())
			continue
		} else if err != nil {
			return err
		}
		perSym[j.Symbol] += n
		total += n
	}
	fmt.Printf("%d files to fetch for %d symbols, %s → %s\n", len(jobs), len(syms), from.Format(time.DateOnly), to.Format(time.DateOnly))
	for _, s := range syms {
		fmt.Printf("  %-14s %8.1f GB zipped\n", s, float64(perSym[s])/1e9)
	}
	// Packed .llp files measured 0.72–0.84× their zips (SOL, ETH 2026-10-05); the zip is deleted after packing.
	fmt.Printf("total %.1f GB zipped download, ≈%.1f GB packed on disk\n", float64(total)/1e9, 0.85*float64(total)/1e9)
	if len(missing) > 0 {
		fmt.Printf("%d not published (monthly ones fall back to daily files): %s\n", len(missing), strings.Join(missing, " "))
	}
	return nil
}

func runFetch(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("fetch", flag.ExitOnError)
	var rf rangeFlags
	rf.register(fs)
	conc := fs.Int("concurrency", 4, "parallel downloads")
	keep := fs.Bool("keep-zip", false, "keep downloaded zips after packing")
	base := fs.String("base-url", archive.DefaultBaseURL, "archive base URL")
	fs.Parse(args)
	syms, from, to, err := rf.parse()
	if err != nil {
		return err
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	f := &archive.Fetcher{Layout: archive.Layout{Root: rf.root}, BaseURL: *base, KeepZip: *keep, Log: log}
	jobs := archive.Plan(f.Layout, syms, from, to, time.Now())
	log.Info("fetch", "files", len(jobs), "symbols", len(syms), "from", from.Format(time.DateOnly), "to", to.Format(time.DateOnly))
	start := time.Now()
	results := f.Run(ctx, jobs, *conc)
	var failed, notFound, days int
	var bytes int64
	for _, r := range results {
		switch {
		case errors.Is(r.Err, archive.ErrNotFound):
			notFound++
			log.Warn("not published", "file", r.Job.Name())
		case r.Err != nil:
			failed++
			log.Error("failed", "file", r.Job.Name(), "err", r.Err)
		default:
			days += len(r.Manifests)
			bytes += r.Bytes
		}
	}
	log.Info("done", "days_packed", days, "gb", fmt.Sprintf("%.2f", float64(bytes)/1e9),
		"not_published", notFound, "failed", failed, "elapsed", time.Since(start).Round(time.Second))
	if failed > 0 {
		return fmt.Errorf("%d files failed", failed)
	}
	return ctx.Err()
}

// verifyRow is one symbol's integrity summary.
type verifyRow struct {
	Symbol      string   `json:"symbol"`
	Days        int      `json:"days"`
	MissingDays []string `json:"missing_days,omitempty"`
	GapDays     []string `json:"gap_days,omitempty"`      // id gaps inside the day
	SeamDays    []string `json:"seam_days,omitempty"`     // first id != previous day's last id + 1
	BackDays    []string `json:"backward_days,omitempty"` // time went backwards inside the day
	BadFiles    []string `json:"bad_files,omitempty"`
	Trades      int64    `json:"trades"`
	MissingIDs  int64    `json:"missing_ids"`
	GapDayFrac  float64  `json:"gap_day_frac"`
}

func runVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	var rf rangeFlags
	rf.register(fs)
	asJSON := fs.Bool("json", false, "print JSON")
	fs.Parse(args)
	syms, from, to, err := rf.parse()
	if err != nil {
		return err
	}
	l := archive.Layout{Root: rf.root}
	var rows []verifyRow
	for _, sym := range syms {
		row := verifyRow{Symbol: sym}
		var prevLast int64 = -1
		for d := from; !d.After(to); d = d.AddDate(0, 0, 1) {
			day := d.Format(time.DateOnly)
			m, err := l.ReadManifest(sym, day)
			if err != nil {
				row.MissingDays = append(row.MissingDays, day)
				prevLast = -1
				continue
			}
			row.Days++
			r, err := archive.OpenLLP(l.LLPPath(sym, day))
			if err != nil || r.Count() != m.Count {
				row.BadFiles = append(row.BadFiles, day)
			}
			if r != nil {
				r.Close()
			}
			row.Trades += int64(m.Count)
			row.MissingIDs += m.MissingIDs
			if len(m.GapRanges) > 0 {
				row.GapDays = append(row.GapDays, day)
			}
			if m.TimeBackward > 0 {
				row.BackDays = append(row.BackDays, day)
			}
			if prevLast >= 0 && m.Count > 0 && m.FirstID != prevLast+1 {
				row.SeamDays = append(row.SeamDays, day)
			}
			if m.Count > 0 {
				prevLast = m.LastID
			}
		}
		if row.Days > 0 {
			row.GapDayFrac = float64(len(row.GapDays)) / float64(row.Days)
		}
		rows = append(rows, row)
	}
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(rows)
	}
	bad := false
	fmt.Printf("%-14s %5s %7s %6s %6s %6s %5s %14s %12s\n", "symbol", "days", "missing", "gaps", "seams", "back", "bad", "trades", "missing_ids")
	for _, r := range rows {
		fmt.Printf("%-14s %5d %7d %6d %6d %6d %5d %14d %12d\n", r.Symbol, r.Days, len(r.MissingDays),
			len(r.GapDays), len(r.SeamDays), len(r.BackDays), len(r.BadFiles), r.Trades, r.MissingIDs)
		if len(r.BadFiles) > 0 || len(r.MissingDays) > 0 {
			bad = true
		}
		for _, d := range r.GapDays {
			m, _ := l.ReadManifest(r.Symbol, d)
			fmt.Printf("    gap   %s missing_ids=%d first=%v\n", d, m.MissingIDs, m.GapRanges[0])
		}
	}
	if bad {
		return errors.New("missing days or unreadable files (see table)")
	}
	return nil
}

// runCandidates lists the most-traded USDT perpetuals by median daily quote
// volume over the last --days days (a median, so a one-day pump does not rank),
// for the screen's candidate universe. Ranking by today's volume is
// survivorship-biased toward coins that are big now; PREREG.md records that the
// list was drawn this way.
func runCandidates(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("candidates", flag.ExitOnError)
	n := fs.Int("n", 30, "number of candidates")
	days := fs.Int("days", 30, "volume lookback in days (median of daily quote volume)")
	onboardBefore := fs.String("onboarded-before", "2025-01-01", "only contracts listed before this day (empty: any)")
	quote := fs.String("quote", "USDT", "margin asset: USDT or USDC")
	exclude := fs.String("exclude", "BTCUSDT,ETHUSDT,USDCUSDT,BTCDOMUSDT", "symbols to leave out (leaders, stables, indices)")
	cache := fs.String("cache", envOr("LL_SYMBOLS_CACHE", filepath.Join(os.TempDir(), "leadlag-exchangeinfo.json")), "exchangeInfo cache")
	fs.Parse(args)
	var cutoff time.Time
	if *onboardBefore != "" {
		var err error
		if cutoff, err = time.Parse(time.DateOnly, *onboardBefore); err != nil {
			return err
		}
	}
	client := &http.Client{Timeout: 30 * time.Second}
	tbl, err := symbols.Load(ctx, client, *cache, 24*time.Hour)
	if err != nil && tbl == nil {
		return err
	}
	skip := map[string]bool{}
	for _, s := range strings.Split(*exclude, ",") {
		skip[strings.TrimSpace(s)] = true
	}
	var syms []string
	for _, i := range tbl.Perpetuals(strings.ToUpper(*quote), cutoff) {
		if !skip[i.Symbol] {
			syms = append(syms, i.Symbol)
		}
	}
	vols, err := medianDailyVolumes(ctx, client, syms, *days)
	if err != nil {
		return err
	}
	sort.Slice(syms, func(a, b int) bool { return vols[syms[a]] > vols[syms[b]] })
	if len(syms) > *n {
		syms = syms[:*n]
	}
	for i, s := range syms {
		fmt.Fprintf(os.Stderr, "%2d %-16s %8.0f M %s median daily (%d d)\n", i+1, s, vols[s]/1e6, strings.ToUpper(*quote), *days)
	}
	fmt.Println(strings.Join(syms, ","))
	return nil
}

// runTicks prints {"SYM": tick_e8, …} for the symbols, the "ticks" object of a
// quoter params file (explore-3). Ticks are fixed in the params file rather
// than read at start so that a replay makes the same decisions.
func runTicks(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("ticks", flag.ExitOnError)
	list := fs.String("symbols", "", "comma-separated symbols")
	cache := fs.String("cache", envOr("LL_SYMBOLS_CACHE", filepath.Join(os.TempDir(), "leadlag-exchangeinfo.json")), "exchangeInfo cache")
	fs.Parse(args)
	tbl, err := symbols.Load(ctx, &http.Client{Timeout: 30 * time.Second}, *cache, 24*time.Hour)
	if err != nil && tbl == nil {
		return err
	}
	ticks := map[string]int64{}
	for _, s := range strings.Split(*list, ",") {
		if s = strings.ToUpper(strings.TrimSpace(s)); s == "" {
			continue
		}
		i, ok := tbl.Get(s)
		if !ok || i.Tick <= 0 {
			return fmt.Errorf("%s: not in exchangeInfo (or no tick size)", s)
		}
		if i.Status != "TRADING" {
			fmt.Fprintf(os.Stderr, "warning: %s status %s\n", s, i.Status)
		}
		ticks[s] = i.Tick
	}
	b, _ := json.Marshal(ticks)
	fmt.Println(string(b))
	return nil
}

// medianDailyVolumes fetches daily klines (8 at a time) and returns each
// symbol's median daily quote volume over the last `days` completed days.
func medianDailyVolumes(ctx context.Context, client *http.Client, syms []string, days int) (map[string]float64, error) {
	type res struct {
		sym string
		v   float64
		err error
	}
	in := make(chan string)
	out := make(chan res)
	for range 8 {
		go func() {
			for s := range in {
				v, err := medianVolume(ctx, client, s, days)
				out <- res{s, v, err}
			}
		}()
	}
	go func() {
		for _, s := range syms {
			in <- s
		}
		close(in)
	}()
	vols := make(map[string]float64, len(syms))
	var firstErr error
	for range syms {
		r := <-out
		if r.err != nil && firstErr == nil {
			firstErr = r.err
		}
		vols[r.sym] = r.v
	}
	return vols, firstErr
}

func medianVolume(ctx context.Context, client *http.Client, sym string, days int) (float64, error) {
	// limit days+1 and drop the last (today's, still open) candle.
	url := fmt.Sprintf("https://fapi.binance.com/fapi/v1/klines?symbol=%s&interval=1d&limit=%d", sym, days+1)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("klines %s: HTTP %d", sym, resp.StatusCode)
	}
	var rows [][]any
	if err := json.NewDecoder(resp.Body).Decode(&rows); err != nil {
		return 0, fmt.Errorf("klines %s: %w", sym, err)
	}
	if len(rows) > 0 {
		rows = rows[:len(rows)-1]
	}
	var qv []float64
	for _, r := range rows {
		if len(r) > 7 {
			if s, ok := r[7].(string); ok {
				v, _ := strconv.ParseFloat(s, 64)
				qv = append(qv, v)
			}
		}
	}
	if len(qv) == 0 {
		return 0, nil
	}
	sort.Float64s(qv)
	return qv[len(qv)/2], nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
