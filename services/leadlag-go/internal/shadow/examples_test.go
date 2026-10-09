package shadow

import (
	"path/filepath"
	"testing"
)

// The deploy example params files must load (the trader refuses invalid ones at start).
func TestExampleParamsLoad(t *testing.T) {
	files, _ := filepath.Glob("../../deploy/env/shadow*.example.json")
	if len(files) < 6 {
		t.Fatalf("found %d example params files", len(files))
	}
	runs := map[string]bool{}
	for _, f := range files {
		p, _, err := LoadParams(f)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		if runs[p.Run] {
			t.Fatalf("%s: run label %s used twice", f, p.Run)
		}
		runs[p.Run] = true
	}
}
