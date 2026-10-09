package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Commission is one symbol's rate from GET /fapi/v1/commissionRate, in bp.
type Commission struct {
	Symbol  string  `json:"symbol"`
	MakerBp float64 `json:"maker_bp"`
	TakerBp float64 `json:"taker_bp"`
	RPIBp   float64 `json:"rpi_bp,omitempty"`
}

// runFees prints the account's maker and taker commission for each symbol.
// explore-3 assumes 0 bp maker / 4 bp taker on USDC-M (a promotion that no
// primary Binance page confirms); PREREG Part 4 records what this returns.
// The key needs no trading permission (USER_DATA, read only).
func runFees(args []string) error {
	fs := flag.NewFlagSet("fees", flag.ExitOnError)
	list := fs.String("symbols", "SOLUSDC,SOLUSDT", "comma-separated symbols")
	rest := fs.String("rest", "https://fapi.binance.com", "REST base")
	out := fs.String("out", "", "also write the JSON here")
	fs.Parse(args)
	key, secret := os.Getenv("BINANCE_API_KEY"), os.Getenv("BINANCE_API_SECRET")
	if key == "" || secret == "" {
		return errors.New("BINANCE_API_KEY and BINANCE_API_SECRET must be set (read-only key)")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client := &http.Client{Timeout: 10 * time.Second}
	var res []Commission
	for _, s := range strings.Split(*list, ",") {
		if s = strings.ToUpper(strings.TrimSpace(s)); s == "" {
			continue
		}
		c, err := commissionRate(ctx, client, *rest, key, secret, s, time.Now())
		if err != nil {
			return fmt.Errorf("%s: %w", s, err)
		}
		fmt.Printf("%-14s maker %6.3f bp  taker %6.3f bp\n", c.Symbol, c.MakerBp, c.TakerBp)
		res = append(res, c)
	}
	if *out != "" {
		b, _ := json.MarshalIndent(map[string]any{"at": time.Now().UTC(), "rates": res}, "", "  ")
		return os.WriteFile(*out, b, 0o644)
	}
	return nil
}

// sign returns the query with its HMAC-SHA256 signature appended.
func sign(q url.Values, secret string) string {
	enc := q.Encode()
	m := hmac.New(sha256.New, []byte(secret))
	m.Write([]byte(enc))
	return enc + "&signature=" + hex.EncodeToString(m.Sum(nil))
}

func commissionRate(ctx context.Context, c *http.Client, base, key, secret, sym string, now time.Time) (Commission, error) {
	q := url.Values{}
	q.Set("symbol", sym)
	q.Set("recvWindow", "5000")
	q.Set("timestamp", strconv.FormatInt(now.UnixMilli(), 10))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/fapi/v1/commissionRate?"+sign(q, secret), nil)
	if err != nil {
		return Commission{}, err
	}
	req.Header.Set("X-MBX-APIKEY", key)
	resp, err := c.Do(req)
	if err != nil {
		return Commission{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK {
		return Commission{}, fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)
	}
	var raw struct {
		Symbol string `json:"symbol"`
		Maker  string `json:"makerCommissionRate"`
		Taker  string `json:"takerCommissionRate"`
		RPI    string `json:"rpiCommissionRate"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return Commission{}, err
	}
	bp := func(s string) (float64, error) {
		if s == "" {
			return 0, nil
		}
		v, err := strconv.ParseFloat(s, 64)
		return v * 1e4, err
	}
	out := Commission{Symbol: raw.Symbol}
	var e1, e2, e3 error
	out.MakerBp, e1 = bp(raw.Maker)
	out.TakerBp, e2 = bp(raw.Taker)
	out.RPIBp, e3 = bp(raw.RPI)
	if err := errors.Join(e1, e2, e3); err != nil {
		return Commission{}, fmt.Errorf("rates %q %q: %w", raw.Maker, raw.Taker, err)
	}
	return out, nil
}
