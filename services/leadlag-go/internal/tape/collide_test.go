package tape

import (
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Two writers opening the same hour in the same second (an observer restart)
// must both get a file: the second takes the next free name.
func TestASecondWriterInTheSameSecondGetsItsOwnFile(t *testing.T) {
	dir := t.TempDir()
	at := time.Now().UnixNano()
	a, b := &Writer{Dir: dir}, &Writer{Dir: dir}
	if err := a.Frame(at, 'm', []byte(`{"a":1}`)); err != nil {
		t.Fatal(err)
	}
	if err := b.Frame(at, 'm', []byte(`{"b":1}`)); err != nil {
		t.Fatalf("second writer: %v", err)
	}
	if a.Path() == b.Path() || filepath.Dir(a.Path()) != filepath.Dir(b.Path()) {
		t.Fatalf("paths %s %s", a.Path(), b.Path())
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
	if err := b.Close(); err != nil {
		t.Fatal(err)
	}
	files, err := Files(dir)
	if err != nil || len(files) != 2 || files[0] != a.Path() {
		t.Fatalf("files %v err %v (the first writer's file must sort first)", files, err)
	}
}

// The newest file is normally still being written: a replay must read it up to
// the cut and stop there. The same cut in an earlier file is an error.
func TestACutLastFileEndsTheTapeButACutEarlierFileFails(t *testing.T) {
	dir := t.TempDir()
	w := &Writer{Dir: dir}
	at := time.Date(2026, 10, 8, 15, 0, 0, 0, time.UTC).UnixNano()
	for i := 0; i < 2000; i++ {
		if err := w.Frame(at+int64(i), 'm', []byte(`{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","a":1}}`)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	whole, _ := os.ReadFile(w.Path())
	cut := filepath.Join(dir, "cut.jsonl.zst")
	if err := os.WriteFile(cut, whole[:len(whole)-40], 0o644); err != nil {
		t.Fatal(err)
	}

	r := NewReader([]string{cut})
	var l Line
	n := 0
	for {
		err := r.Next(&l)
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("after %d lines: %v", n, err)
		}
		n++
	}
	r.Close()
	if !r.Truncated || n == 0 || n >= 2000 {
		t.Fatalf("read %d lines, truncated %v", n, r.Truncated)
	}

	r = NewReader([]string{cut, w.Path()})
	defer r.Close()
	for {
		err := r.Next(&l)
		if err == io.EOF {
			t.Fatal("a cut in an earlier file must not end the tape")
		}
		if err != nil {
			break
		}
	}
}
