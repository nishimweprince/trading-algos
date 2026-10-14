package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestMetricsDownloadHasTheMetricsButNotTheEvents(t *testing.T) {
	c := &cache{
		trader: json.RawMessage(`{"mode":"shadow","hub":{"events":5184189,"engine_ns_60s":{"p99":27648}},
			"shadow":{"run":"explore-4","signals":12,"total":{"n":3,"mean_net_bp":-4.1},
			"by_follower":{"SOLUSDC":{"n":2}},"recent":[{"follower":"SOLUSDC"},{"follower":"XRPUSDC"},{"follower":"SOLUSDC"}]}}`),
		observer: json.RawMessage(`{"tape":{"enabled":true},"symbols":[{"symbol":"BTCUSDT"}]}`),
		probe:    json.RawMessage(`{"rest_warm_us":{"p50":10750}}`),
		traderAt: time.Now(), obsAt: time.Now(),
	}
	rec := httptest.NewRecorder()
	newMux(c).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if cd := rec.Header().Get("Content-Disposition"); !strings.HasPrefix(cd, `attachment; filename="leadlag-metrics-`) || !strings.HasSuffix(cd, `.json"`) {
		t.Fatalf("Content-Disposition %q", cd)
	}
	var doc struct {
		Trader struct {
			Hub    map[string]any `json:"hub"`
			Shadow map[string]any `json:"shadow"`
		} `json:"trader"`
		Observer map[string]any `json:"observer"`
		Probe    map[string]any `json:"probe"`
		Age      map[string]any `json:"source_age_s"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	sh := doc.Trader.Shadow
	if _, ok := sh["recent"]; ok {
		t.Fatal("the recent trades must not be in the download")
	}
	if sh["recent_trades_omitted"] != float64(3) || sh["signals"] != float64(12) || sh["by_follower"] == nil || sh["total"] == nil {
		t.Fatalf("shadow %v", sh)
	}
	if doc.Trader.Hub["events"] != float64(5184189) || doc.Observer["tape"] == nil || doc.Probe["rest_warm_us"] == nil || doc.Age["trader"] == nil {
		t.Fatalf("metrics missing: %+v", doc)
	}
	// The cached trader document itself is untouched (the page still shows recent trades).
	if !strings.Contains(string(c.trader), `"recent"`) {
		t.Fatal("cache modified")
	}
}

func TestMetricsBeforeAnyPoll(t *testing.T) {
	rec := httptest.NewRecorder()
	newMux(&cache{}).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/metrics", nil))
	var doc map[string]any
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &doc) != nil || doc["trader"] != nil {
		t.Fatalf("code %d body %s", rec.Code, rec.Body.String())
	}
}
