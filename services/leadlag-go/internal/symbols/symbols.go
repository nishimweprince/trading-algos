// Package symbols loads USDⓈ-M contract metadata (tick size, step size, status)
// from /fapi/v1/exchangeInfo and caches it as JSON.
package symbols

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/fixed"
)

// DefaultURL is the mainnet exchangeInfo endpoint.
const DefaultURL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

// Info is one contract. Tick and Step are E8.
type Info struct {
	Symbol       string `json:"symbol"`
	Status       string `json:"status"`
	ContractType string `json:"contract_type"`
	QuoteAsset   string `json:"quote_asset"`
	OnboardMs    int64  `json:"onboard_ms"`
	Tick         int64  `json:"tick_e8"`
	Step         int64  `json:"step_e8"`
}

// Table is a fetched snapshot.
type Table struct {
	FetchedAt time.Time       `json:"fetched_at"`
	Symbols   map[string]Info `json:"symbols"`
}

// Get returns a symbol's info.
func (t *Table) Get(sym string) (Info, bool) {
	i, ok := t.Symbols[sym]
	return i, ok
}

// Perpetuals lists trading USDT perpetuals onboarded before cutoff, sorted.
func (t *Table) Perpetuals(cutoff time.Time) []Info {
	var out []Info
	for _, i := range t.Symbols {
		if i.Status == "TRADING" && i.ContractType == "PERPETUAL" && i.QuoteAsset == "USDT" &&
			(cutoff.IsZero() || i.OnboardMs < cutoff.UnixMilli()) {
			out = append(out, i)
		}
	}
	sort.Slice(out, func(a, b int) bool { return out[a].Symbol < out[b].Symbol })
	return out
}

type rawInfo struct {
	Symbols []struct {
		Symbol       string `json:"symbol"`
		Status       string `json:"status"`
		ContractType string `json:"contractType"`
		QuoteAsset   string `json:"quoteAsset"`
		OnboardDate  int64  `json:"onboardDate"`
		Filters      []struct {
			FilterType string `json:"filterType"`
			TickSize   string `json:"tickSize"`
			StepSize   string `json:"stepSize"`
		} `json:"filters"`
	} `json:"symbols"`
}

// Parse decodes an exchangeInfo body.
func Parse(r io.Reader, now time.Time) (*Table, error) {
	var raw rawInfo
	if err := json.NewDecoder(r).Decode(&raw); err != nil {
		return nil, fmt.Errorf("symbols: %w", err)
	}
	t := &Table{FetchedAt: now.UTC(), Symbols: map[string]Info{}}
	for _, s := range raw.Symbols {
		i := Info{Symbol: s.Symbol, Status: s.Status, ContractType: s.ContractType,
			QuoteAsset: s.QuoteAsset, OnboardMs: s.OnboardDate}
		for _, f := range s.Filters {
			var err error
			switch f.FilterType {
			case "PRICE_FILTER":
				i.Tick, err = fixed.ParseE8String(f.TickSize)
			case "LOT_SIZE":
				i.Step, err = fixed.ParseE8String(f.StepSize)
			}
			if err != nil {
				return nil, fmt.Errorf("symbols: %s %s: %w", s.Symbol, f.FilterType, err)
			}
		}
		t.Symbols[s.Symbol] = i
	}
	if len(t.Symbols) == 0 {
		return nil, fmt.Errorf("symbols: empty exchangeInfo")
	}
	return t, nil
}

// Fetch downloads exchangeInfo.
func Fetch(ctx context.Context, client *http.Client, url string) (*Table, error) {
	if url == "" {
		url = DefaultURL
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("symbols: exchangeInfo HTTP %d", resp.StatusCode)
	}
	return Parse(resp.Body, time.Now())
}

// Load returns the cached table at path if younger than maxAge, else fetches
// and rewrites the cache. On fetch failure a stale cache is returned with the error.
func Load(ctx context.Context, client *http.Client, path string, maxAge time.Duration) (*Table, error) {
	cached, cerr := readCache(path)
	if cerr == nil && time.Since(cached.FetchedAt) < maxAge {
		return cached, nil
	}
	t, err := Fetch(ctx, client, "")
	if err != nil {
		if cerr == nil {
			return cached, fmt.Errorf("symbols: using stale cache from %s: %w", cached.FetchedAt.Format(time.RFC3339), err)
		}
		return nil, err
	}
	b, _ := json.MarshalIndent(t, "", "  ")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err == nil {
		_ = os.WriteFile(path, b, 0o644)
	}
	return t, nil
}

func readCache(path string) (*Table, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var t Table
	if err := json.Unmarshal(b, &t); err != nil {
		return nil, err
	}
	return &t, nil
}
