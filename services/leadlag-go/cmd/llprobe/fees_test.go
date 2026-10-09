package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestCommissionRateSignsAndParses(t *testing.T) {
	const key, secret = "k", "s3cret"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/fapi/v1/commissionRate" || r.Header.Get("X-MBX-APIKEY") != key {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		raw := r.URL.RawQuery
		i := strings.LastIndex(raw, "&signature=")
		m := hmac.New(sha256.New, []byte(secret))
		m.Write([]byte(raw[:i]))
		if raw[i+len("&signature="):] != hex.EncodeToString(m.Sum(nil)) {
			http.Error(w, "bad signature", http.StatusUnauthorized)
			return
		}
		w.Write([]byte(`{"symbol":"SOLUSDC","makerCommissionRate":"0.000000","takerCommissionRate":"0.000400","rpiCommissionRate":"0"}`))
	}))
	defer srv.Close()
	c, err := commissionRate(context.Background(), srv.Client(), srv.URL, key, secret, "SOLUSDC", time.Unix(1, 0))
	if err != nil {
		t.Fatal(err)
	}
	if c.Symbol != "SOLUSDC" || c.MakerBp != 0 || math.Abs(c.TakerBp-4) > 1e-9 {
		t.Fatalf("%+v", c)
	}
	if _, err := commissionRate(context.Background(), srv.Client(), srv.URL, key, "wrong", "SOLUSDC", time.Unix(1, 0)); err == nil {
		t.Fatal("a bad signature must fail")
	}
}
