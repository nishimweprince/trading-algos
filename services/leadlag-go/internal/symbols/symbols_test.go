package symbols

import (
	"strings"
	"testing"
	"time"
)

const body = `{"symbols":[
 {"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT","onboardDate":1569398400000,
  "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.10"},{"filterType":"LOT_SIZE","stepSize":"0.001"}]},
 {"symbol":"BTCUSDT_261225","status":"TRADING","contractType":"CURRENT_QUARTER","quoteAsset":"USDT","onboardDate":1569398400000,
  "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.10"},{"filterType":"LOT_SIZE","stepSize":"0.001"}]},
 {"symbol":"1000PEPEUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT","onboardDate":1683000000000,
  "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.0000001"},{"filterType":"LOT_SIZE","stepSize":"1"}]},
 {"symbol":"NEWUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT","onboardDate":1790000000000,
  "filters":[]},
 {"symbol":"SOLUSDC","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDC","onboardDate":1705000000000,
  "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.01"},{"filterType":"LOT_SIZE","stepSize":"0.01"}]}
]}`

func TestParseAndPerpetuals(t *testing.T) {
	tbl, err := Parse(strings.NewReader(body), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	btc, _ := tbl.Get("BTCUSDT")
	if btc.Tick != 10_000_000 || btc.Step != 100_000 {
		t.Fatalf("BTC: %+v", btc)
	}
	pepe, _ := tbl.Get("1000PEPEUSDT")
	if pepe.Tick != 10 || pepe.Step != 100_000_000 {
		t.Fatalf("PEPE: %+v", pepe)
	}
	perps := tbl.Perpetuals("USDT", time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC))
	if len(perps) != 2 || perps[0].Symbol != "1000PEPEUSDT" || perps[1].Symbol != "BTCUSDT" {
		t.Fatalf("perpetuals: %+v", perps)
	}
	usdc := tbl.Perpetuals("USDC", time.Time{})
	if len(usdc) != 1 || usdc[0].Symbol != "SOLUSDC" || usdc[0].Tick != 1_000_000 {
		t.Fatalf("USDC perpetuals: %+v", usdc)
	}
}
