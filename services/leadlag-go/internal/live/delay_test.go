package live

import (
	"testing"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

type collect struct{ evs []model.Event }

func (c *collect) OnEvent(e *model.Event) { c.evs = append(c.evs, *e) }

func TestDelayShiftsTheMarkedSymbolsInOrder(t *testing.T) {
	c := &collect{}
	d := NewDelay(c, []bool{true, false}, 1000)
	in := []model.Event{
		{Sym: 0, RecvNs: 100, ExchMs: 0, TradeMs: 0},
		{Sym: 1, RecvNs: 200},
		{Sym: 0, RecvNs: 600},
		{Sym: 1, RecvNs: 1100}, // after the first shifted leader event (1100), before the second (1600)
		{Sym: 1, RecvNs: 1700},
	}
	for i := range in {
		d.OnEvent(&in[i])
	}
	var got []int64
	for _, e := range c.evs {
		got = append(got, int64(e.Sym)*1_000_000+e.RecvNs)
	}
	want := []int64{1_000_200, 1100, 1_001_100, 1600, 1_001_700}
	if len(got) != len(want) {
		t.Fatalf("got %v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v want %v", got, want)
		}
	}
	if d.Pending() != 0 {
		t.Fatalf("pending %d", d.Pending())
	}
}
