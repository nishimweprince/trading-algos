package archive

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// DefaultBaseURL is Binance's public data host for USDⓈ-M futures.
const DefaultBaseURL = "https://data.binance.vision/data/futures/um"

// ErrNotFound means the archive has no such file (symbol not listed then, or not published yet).
var ErrNotFound = errors.New("archive: file not published")

// Job is one archive file to fetch: a whole month or a single day.
type Job struct {
	Symbol string
	Month  string   // "YYYY-MM" for a monthly file, else ""
	Day    string   // "YYYY-MM-DD" for a daily file, else ""
	Days   []string // the days this job is expected to produce (within the requested range)
}

// Name is the zip file name.
func (j Job) Name() string {
	if j.Month != "" {
		return fmt.Sprintf("%s-aggTrades-%s.zip", j.Symbol, j.Month)
	}
	return fmt.Sprintf("%s-aggTrades-%s.zip", j.Symbol, j.Day)
}

// URL is the zip's URL under base.
func (j Job) URL(base string) string {
	if j.Month != "" {
		return fmt.Sprintf("%s/monthly/aggTrades/%s/%s", base, j.Symbol, j.Name())
	}
	return fmt.Sprintf("%s/daily/aggTrades/%s/%s", base, j.Symbol, j.Name())
}

// Plan lists the jobs for [from, to] (inclusive UTC days). Whole calendar months
// before now's month use the monthly file; everything else uses daily files.
// Days already packed under l are skipped; a month with every day packed is skipped.
func Plan(l Layout, symbols []string, from, to, now time.Time) []Job {
	from, to = truncDay(from), truncDay(to)
	curMonth := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
	var jobs []Job
	for _, sym := range symbols {
		for d := from; !d.After(to); {
			mStart := time.Date(d.Year(), d.Month(), 1, 0, 0, 0, 0, time.UTC)
			mEnd := mStart.AddDate(0, 1, -1)
			if d.Equal(mStart) && !mEnd.After(to) && mStart.Before(curMonth) {
				days := daysBetween(mStart, mEnd)
				if missing := missingDays(l, sym, days); len(missing) > 0 {
					jobs = append(jobs, Job{Symbol: sym, Month: mStart.Format("2006-01"), Days: days})
				}
				d = mEnd.AddDate(0, 0, 1)
				continue
			}
			day := d.Format(time.DateOnly)
			if !l.HasDay(sym, day) {
				jobs = append(jobs, Job{Symbol: sym, Day: day, Days: []string{day}})
			}
			d = d.AddDate(0, 0, 1)
		}
	}
	return jobs
}

func truncDay(t time.Time) time.Time {
	t = t.UTC()
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
}

func daysBetween(a, b time.Time) []string {
	var out []string
	for d := a; !d.After(b); d = d.AddDate(0, 0, 1) {
		out = append(out, d.Format(time.DateOnly))
	}
	return out
}

func missingDays(l Layout, sym string, days []string) []string {
	var out []string
	for _, d := range days {
		if !l.HasDay(sym, d) {
			out = append(out, d)
		}
	}
	return out
}

// Fetcher downloads, verifies and packs archive files.
type Fetcher struct {
	Layout  Layout
	BaseURL string
	Client  *http.Client
	KeepZip bool
	Retries int
	Log     *slog.Logger
}

// Result is the outcome of one job.
type Result struct {
	Job       Job
	Manifests []DayManifest
	Bytes     int64
	Err       error
}

// Run executes jobs with n workers. A monthly job that is not published falls
// back to its daily files.
func (f *Fetcher) Run(ctx context.Context, jobs []Job, n int) []Result {
	in := make(chan Job)
	var (
		mu  sync.Mutex
		out []Result
		wg  sync.WaitGroup
	)
	for range max(n, 1) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range in {
				rs := f.runJob(ctx, j)
				mu.Lock()
				out = append(out, rs...)
				mu.Unlock()
			}
		}()
	}
	for _, j := range jobs {
		select {
		case in <- j:
		case <-ctx.Done():
		}
	}
	close(in)
	wg.Wait()
	return out
}

func (f *Fetcher) runJob(ctx context.Context, j Job) []Result {
	r := f.fetchAndPack(ctx, j)
	if j.Month == "" || !errors.Is(r.Err, ErrNotFound) {
		return []Result{r}
	}
	f.log().Info("monthly file not published, falling back to daily", "symbol", j.Symbol, "month", j.Month)
	var out []Result
	for _, d := range j.Days {
		if f.Layout.HasDay(j.Symbol, d) {
			continue
		}
		out = append(out, f.fetchAndPack(ctx, Job{Symbol: j.Symbol, Day: d, Days: []string{d}}))
	}
	return out
}

func (f *Fetcher) fetchAndPack(ctx context.Context, j Job) Result {
	res := Result{Job: j}
	zipPath := f.Layout.RawPath(j.Symbol, j.Name())
	sum, n, err := f.download(ctx, j, zipPath)
	if err != nil {
		res.Err = err
		return res
	}
	res.Bytes = n
	want := map[string]bool{}
	for _, d := range j.Days {
		want[d] = true
	}
	ms, err := f.Layout.PackZip(j.Symbol, zipPath, sum, func(day string) bool { return want[day] })
	if err != nil {
		res.Err = fmt.Errorf("pack %s: %w", j.Name(), err)
		return res
	}
	res.Manifests = ms
	if !f.KeepZip {
		os.Remove(zipPath)
	}
	f.log().Info("packed", "file", j.Name(), "days", len(ms), "mb", n>>20)
	return res
}

// download fetches the zip and its .CHECKSUM, verifying sha256 while streaming.
// An already-downloaded zip whose hash matches is reused.
func (f *Fetcher) download(ctx context.Context, j Job, dst string) (string, int64, error) {
	url := j.URL(f.base())
	want, err := f.fetchChecksum(ctx, url+".CHECKSUM")
	if err != nil {
		return "", 0, err
	}
	if sum, n, err := hashFile(dst); err == nil && sum == want {
		return sum, n, nil
	}
	var lastErr error
	for attempt := 0; attempt <= f.retries(); attempt++ {
		if attempt > 0 {
			select {
			case <-time.After(time.Duration(attempt*attempt) * 2 * time.Second):
			case <-ctx.Done():
				return "", 0, ctx.Err()
			}
		}
		sum, n, err := f.downloadOnce(ctx, url, dst)
		if err != nil {
			if errors.Is(err, ErrNotFound) {
				return "", 0, err
			}
			lastErr = err
			continue
		}
		if sum != want {
			lastErr = fmt.Errorf("%s: sha256 %s, checksum file says %s", j.Name(), sum, want)
			os.Remove(dst)
			continue
		}
		return sum, n, nil
	}
	return "", 0, lastErr
}

func (f *Fetcher) downloadOnce(ctx context.Context, url, dst string) (string, int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", 0, err
	}
	resp, err := f.client().Do(req)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", 0, fmt.Errorf("%s: %w", url, ErrNotFound)
	}
	if resp.StatusCode != http.StatusOK {
		return "", 0, fmt.Errorf("%s: HTTP %d", url, resp.StatusCode)
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", 0, err
	}
	tmp := dst + ".part"
	out, err := os.Create(tmp)
	if err != nil {
		return "", 0, err
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(out, h), resp.Body)
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(tmp)
		return "", 0, err
	}
	if err := os.Rename(tmp, dst); err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

func (f *Fetcher) fetchChecksum(ctx context.Context, url string) (string, error) {
	var lastErr error
	for attempt := 0; attempt <= f.retries(); attempt++ {
		if attempt > 0 {
			time.Sleep(time.Duration(attempt) * time.Second)
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return "", err
		}
		resp, err := f.client().Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		b, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if resp.StatusCode == http.StatusNotFound {
			return "", fmt.Errorf("%s: %w", url, ErrNotFound)
		}
		if err != nil || resp.StatusCode != http.StatusOK {
			lastErr = fmt.Errorf("%s: HTTP %d %v", url, resp.StatusCode, err)
			continue
		}
		return ParseChecksum(string(b))
	}
	return "", lastErr
}

// ParseChecksum extracts the hex sha256 from a "<sha256>  <name>" line.
func ParseChecksum(s string) (string, error) {
	fields := strings.Fields(s)
	if len(fields) == 0 || len(fields[0]) != 64 {
		return "", fmt.Errorf("archive: bad checksum file %q", s)
	}
	if _, err := hex.DecodeString(fields[0]); err != nil {
		return "", fmt.Errorf("archive: bad checksum file %q", s)
	}
	return strings.ToLower(fields[0]), nil
}

// Size returns the Content-Length of a job's zip via HEAD (for --plan estimates).
func (f *Fetcher) Size(ctx context.Context, j Job) (int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, j.URL(f.base()), nil)
	if err != nil {
		return 0, err
	}
	resp, err := f.client().Do(req)
	if err != nil {
		return 0, err
	}
	resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return 0, ErrNotFound
	}
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("HEAD %s: HTTP %d", j.Name(), resp.StatusCode)
	}
	return resp.ContentLength, nil
}

func hashFile(path string) (string, int64, error) {
	fh, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer fh.Close()
	h := sha256.New()
	n, err := io.Copy(h, fh)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

func (f *Fetcher) base() string {
	if f.BaseURL != "" {
		return strings.TrimRight(f.BaseURL, "/")
	}
	return DefaultBaseURL
}

func (f *Fetcher) client() *http.Client {
	if f.Client != nil {
		return f.Client
	}
	return &http.Client{Timeout: 30 * time.Minute}
}

func (f *Fetcher) retries() int {
	if f.Retries > 0 {
		return f.Retries
	}
	return 3
}

func (f *Fetcher) log() *slog.Logger {
	if f.Log != nil {
		return f.Log
	}
	return slog.Default()
}
