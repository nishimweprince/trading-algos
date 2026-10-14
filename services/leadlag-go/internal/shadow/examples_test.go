package shadow

import (
	"path/filepath"
	"testing"
)

// The deploy example params file must load (the trader refuses an invalid one at start).
func TestExampleParamsLoad(t *testing.T) {
	files, _ := filepath.Glob("../../deploy/env/shadow*.example.json")
	if len(files) != 1 {
		t.Fatalf("found %d example params files, want the one trader's", len(files))
	}
	if _, _, err := LoadParams(files[0]); err != nil {
		t.Fatalf("%s: %v", files[0], err)
	}
}
