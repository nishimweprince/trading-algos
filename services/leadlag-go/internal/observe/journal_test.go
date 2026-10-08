package observe

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/ipc"
)

func TestJournalRecordsGoToTheDaysFile(t *testing.T) {
	dir := t.TempDir()
	now := time.Date(2026, 10, 8, 23, 59, 59, 0, time.UTC)
	o := New(Config{TapeDir: dir, DisableTape: true, JournalDir: filepath.Join(dir, "shadow"), Now: func() time.Time { return now }})
	for _, body := range []string{`{"follower":"SOLUSDT","net_bp":-3.1}`, `{"follower":"XRPUSDT","net_bp":2.4}`} {
		if err := o.Handle(ipc.TypeJournal, []byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	now = now.Add(2 * time.Second) // past midnight: a new day's file
	if err := o.Handle(ipc.TypeJournal, []byte(`{"follower":"DOGEUSDT"}`)); err != nil {
		t.Fatal(err)
	}
	if err := o.Close(); err != nil {
		t.Fatal(err)
	}
	day1, _ := os.ReadFile(filepath.Join(dir, "shadow", "2026-10-08.jsonl"))
	day2, _ := os.ReadFile(filepath.Join(dir, "shadow", "2026-10-09.jsonl"))
	if string(day1) != "{\"follower\":\"SOLUSDT\",\"net_bp\":-3.1}\n{\"follower\":\"XRPUSDT\",\"net_bp\":2.4}\n" || string(day2) != "{\"follower\":\"DOGEUSDT\"}\n" {
		t.Fatalf("day1 %q day2 %q", day1, day2)
	}
	if st := o.Stats(); st.Journal.Records != 3 || st.Journal.Err != "" || st.Journal.Day != "2026-10-09" {
		t.Fatalf("stats %+v", st.Journal)
	}

	quiet := New(Config{TapeDir: dir, DisableTape: true})
	if err := quiet.Handle(ipc.TypeJournal, []byte(`{}`)); err != nil || quiet.Stats().Journal.Records != 1 {
		t.Fatal("a journal without a directory must still be counted")
	}
}
