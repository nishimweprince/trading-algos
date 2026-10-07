package parse

import (
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// Captured from wss://fstream.binance.com on 2026-10-07 (note "nq" and "st",
// which are not in the documented schema; unknown keys must be skipped).
const (
	aggFrame  = `{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1791393218902,"a":3478049687,"s":"BTCUSDT","p":"83501.20","q":"0.004","nq":"0.004","f":8154129572,"l":8154129573,"T":1791393218779,"m":true,"st":1}}`
	aggFrame2 = `{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1791393219073,"a":3478049689,"s":"BTCUSDT","p":"83501.30","q":"0.002","nq":"0.002","f":8154129575,"l":8154129575,"T":1791393218923,"m":false,"st":1}}`
	bookFrame = `{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","u":11758329992677,"s":"BTCUSDT","ps":"BTCUSDT","b":"83501.20","B":"4.711","a":"83501.30","A":"7.432","T":1791393220362,"E":1791393220362,"st":1}}`
)

var syms = NewSymbols([]string{"ETHUSDT", "BTCUSDT"})

func TestAggTrade(t *testing.T) {
	var e model.Event
	if err := Frame([]byte(aggFrame), syms, &e); err != nil {
		t.Fatal(err)
	}
	want := model.Event{ExchMs: 1791393218902, TradeMs: 1791393218779, ID: 3478049687, Px: 8_350_120_000_000,
		Qty: 400_000, Sym: 1, Kind: model.KindTrade, BuyerMaker: true}
	if e != want {
		t.Fatalf("got  %+v\nwant %+v", e, want)
	}
	if err := Frame([]byte(aggFrame2), syms, &e); err != nil || e.BuyerMaker || e.TakerSign() != 1 {
		t.Fatalf("frame 2: %+v %v", e, err)
	}
}

func TestBookTicker(t *testing.T) {
	var e model.Event
	if err := Frame([]byte(bookFrame), syms, &e); err != nil {
		t.Fatal(err)
	}
	want := model.Event{ExchMs: 1791393220362, TradeMs: 1791393220362, ID: 11758329992677, Bid: 8_350_120_000_000,
		BidQty: 471_100_000, Ask: 8_350_130_000_000, AskQty: 743_200_000, Sym: 1, Kind: model.KindQuote}
	if e != want {
		t.Fatalf("got  %+v\nwant %+v", e, want)
	}
}

func TestRejects(t *testing.T) {
	var e model.Event
	cases := map[string]error{
		`{"result":null,"id":1}`:                                    ErrUnknownStream,
		`{"stream":"btcusdt@depth","data":{}}`:                      ErrUnknownStream,
		`not json`:                                                  ErrFrame,
		`{"stream":"x@aggTrade","data":{"s":"DOGEUSDT"}}`:           ErrUnknownSymbol,
		`{"stream":"x@aggTrade","data":{"s":"BTCUSDT","p":"1"}}`:    ErrField,
		`{"stream":"x@aggTrade","data":{"s":"BTCUSDT","p":"1e3",`:   ErrFrame,
		`{"stream":"x@bookTicker","data":{"s":"BTCUSDT","b":"x1"}}`: ErrField,
	}
	for in, want := range cases {
		if err := Frame([]byte(in), syms, &e); err != want {
			t.Errorf("%s: err %v, want %v", in, err, want)
		}
	}
}

func TestEnvelopeWhitespaceAndNesting(t *testing.T) {
	in := ` { "stream" : "btcusdt@aggTrade" , "extra": {"x":[1,{"y":"}"}]}, "data" : {"e":"aggTrade","E":1,"a":2,"s":"BTCUSDT","p":"1","q":"2","T":1,"m":false} } `
	var e model.Event
	if err := Frame([]byte(in), syms, &e); err != nil || e.ID != 2 {
		t.Fatalf("%+v %v", e, err)
	}
}

func TestFrameNoAlloc(t *testing.T) {
	a, b := []byte(aggFrame), []byte(bookFrame)
	var e model.Event
	n := testing.AllocsPerRun(1000, func() {
		_ = Frame(a, syms, &e)
		_ = Frame(b, syms, &e)
	})
	if n != 0 {
		t.Fatalf("Frame allocates %v per run", n)
	}
}

func BenchmarkAggTrade(b *testing.B) {
	f := []byte(aggFrame)
	var e model.Event
	b.ReportAllocs()
	for b.Loop() {
		_ = Frame(f, syms, &e)
	}
}

func BenchmarkBookTicker(b *testing.B) {
	f := []byte(bookFrame)
	var e model.Event
	b.ReportAllocs()
	for b.Loop() {
		_ = Frame(f, syms, &e)
	}
}
