package source

import (
	"io"

	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
)

// Slice replays in-memory events (tests, synthetic studies).
type Slice struct {
	Events []model.Event
	i      int
}

// Next implements Source.
func (s *Slice) Next(e *model.Event) error {
	if s.i >= len(s.Events) {
		return io.EOF
	}
	*e = s.Events[s.i]
	s.i++
	return nil
}

// Close implements Source.
func (s *Slice) Close() error { return nil }
