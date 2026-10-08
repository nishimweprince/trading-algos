package tape

import (
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
