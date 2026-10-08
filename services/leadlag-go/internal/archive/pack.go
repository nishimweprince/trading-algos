package archive

import (
	"archive/zip"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DayManifest describes one packed symbol-day. It is written next to the .llp
// and is what `llfetch verify` and the research tools read for integrity.
type DayManifest struct {
	Version      int        `json:"version"`
	Symbol       string     `json:"symbol"`
	Day          string     `json:"day"` // YYYY-MM-DD, UTC
	Source       string     `json:"source"`
	SourceSHA256 string     `json:"source_sha256"`
	Count        int        `json:"count"`
	FirstID      int64      `json:"first_id"`
	LastID       int64      `json:"last_id"`
	FirstMs      int64      `json:"first_ms"`
	LastMs       int64      `json:"last_ms"`
	MissingIDs   int64      `json:"missing_ids"`
	GapRanges    [][2]int64 `json:"gap_ranges,omitempty"` // first 20 [after, before) id gaps
	TimeBackward int        `json:"time_backward"`        // rows whose time < previous row's
	PackedAt     string     `json:"packed_at"`
}

const manifestVersion = 1

// Layout maps symbols and days to paths under the archive root.
type Layout struct{ Root string }

// LLPPath is root/llp/SYM/SYM-YYYY-MM-DD.llp.
func (l Layout) LLPPath(sym, day string) string {
	return filepath.Join(l.Root, "llp", sym, sym+"-"+day+".llp")
}

// ManifestPath is the .json next to the .llp.
func (l Layout) ManifestPath(sym, day string) string {
	return filepath.Join(l.Root, "llp", sym, sym+"-"+day+".json")
}

// RawPath is root/raw/SYM/<name>.
func (l Layout) RawPath(sym, name string) string {
	return filepath.Join(l.Root, "raw", sym, name)
}

// ReadManifest loads a day's manifest.
func (l Layout) ReadManifest(sym, day string) (*DayManifest, error) {
	b, err := os.ReadFile(l.ManifestPath(sym, day))
	if err != nil {
		return nil, err
	}
	var m DayManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("%s %s manifest: %w", sym, day, err)
	}
	return &m, nil
}

// HasDay reports whether a day is packed (manifest written last, so it implies the .llp).
func (l Layout) HasDay(sym, day string) bool {
	_, err := os.Stat(l.ManifestPath(sym, day))
	return err == nil
}

// PackZip splits an aggTrades zip (daily or monthly) into per-day .llp files,
// keeping only days for which keep returns true. Rows must be in non-decreasing
// UTC day order (they are id-ordered in Binance's files); a row for a day that was
// already flushed is an error rather than silently dropped.
func (l Layout) PackZip(sym, zipPath, sourceSHA string, keep func(day string) bool) ([]DayManifest, error) {
	zr, err := zip.OpenReader(zipPath)
	if err != nil {
		return nil, err
	}
	defer zr.Close()
	var csvFile *zip.File
	for _, f := range zr.File {
		if strings.HasSuffix(f.Name, ".csv") {
			if csvFile != nil {
				return nil, fmt.Errorf("%s: more than one csv", zipPath)
			}
			csvFile = f
		}
	}
	if csvFile == nil {
		return nil, fmt.Errorf("%s: no csv", zipPath)
	}
	rc, err := csvFile.Open()
	if err != nil {
		return nil, err
	}
	defer rc.Close()

	var (
		out     []DayManifest
		cur     string
		buf     []Trade
		flushed = map[string]bool{}
	)
	flush := func() error {
		if cur == "" {
			return nil
		}
		flushed[cur] = true
		if keep != nil && !keep(cur) {
			buf = buf[:0]
			return nil
		}
		m := summarize(sym, cur, buf)
		m.Source = filepath.Base(zipPath)
		m.SourceSHA256 = sourceSHA
		if err := WriteLLP(l.LLPPath(sym, cur), buf); err != nil {
			return err
		}
		b, _ := json.MarshalIndent(m, "", "  ")
		if err := writeAtomic(l.ManifestPath(sym, cur), b); err != nil {
			return err
		}
		out = append(out, m)
		buf = buf[:0]
		return nil
	}
	err = ReadCSV(rc, func(t Trade) error {
		day := time.UnixMilli(t.TradeMs).UTC().Format(time.DateOnly)
		if day != cur {
			if flushed[day] || (cur != "" && day < cur) {
				return fmt.Errorf("%s: row id %d for day %s after day %s", zipPath, t.AggID, day, cur)
			}
			if err := flush(); err != nil {
				return err
			}
			cur = day
		}
		buf = append(buf, t)
		return nil
	})
	if err != nil {
		return nil, err
	}
	if err := flush(); err != nil {
		return nil, err
	}
	return out, nil
}

func summarize(sym, day string, ts []Trade) DayManifest {
	m := DayManifest{Version: manifestVersion, Symbol: sym, Day: day, Count: len(ts),
		PackedAt: time.Now().UTC().Format(time.RFC3339)}
	if len(ts) == 0 {
		return m
	}
	m.FirstID, m.LastID = ts[0].AggID, ts[len(ts)-1].AggID
	m.FirstMs, m.LastMs = ts[0].TradeMs, ts[len(ts)-1].TradeMs
	for i := 1; i < len(ts); i++ {
		if d := ts[i].AggID - ts[i-1].AggID; d != 1 {
			if d > 1 {
				m.MissingIDs += d - 1
			}
			if len(m.GapRanges) < 20 {
				m.GapRanges = append(m.GapRanges, [2]int64{ts[i-1].AggID, ts[i].AggID})
			}
		}
		if ts[i].TradeMs < ts[i-1].TradeMs {
			m.TimeBackward++
		}
	}
	return m
}

func writeAtomic(path string, b []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
