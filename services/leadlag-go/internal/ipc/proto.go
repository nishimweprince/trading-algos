package ipc

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// Record types on the trader → observer stream. Every record is
// [u32 len LE][u8 type][body], len = 1 + len(body).
const (
	// TypeHello: body is JSON (Hello), sent by the writer on every connect.
	TypeHello byte = 1
	// TypeFrame: body is [i64 recvNs][u8 conn][raw websocket frame].
	TypeFrame byte = 2
	// TypeGap: body is [u64 records][u64 bytes][i64 atNs]: records the trader
	// dropped because the ring was full or the observer was not connected.
	TypeGap byte = 3
	// TypeConn: body is [i64 atNs][u8 conn][u8 state]: a feed connection changed state.
	TypeConn byte = 4
	// TypeJournal: body is one JSON journal record (a completed shadow trade);
	// the observer appends it to <journal dir>/<UTC day>.jsonl.
	TypeJournal byte = 5
)

// Connection ids carried in TypeFrame and TypeConn records.
const (
	ConnMarket byte = 'm' // /market: aggTrade
	ConnPublic byte = 'p' // /public: bookTicker
)

// Connection states in TypeConn records.
const (
	StateUp   byte = 1
	StateDown byte = 2
)

// Hello is sent first on every trader → observer connection.
type Hello struct {
	Version  int      `json:"version"`
	PID      int      `json:"pid"`
	Mode     string   `json:"mode"`
	Build    string   `json:"build"`
	Symbols  []string `json:"symbols"`
	StartNs  int64    `json:"start_ns"`
	Hostname string   `json:"hostname"`
	// ParamsSHA256 is the (first) shadow params file's sha256 (shadow mode
	// only), so a replay can tell which parameters a run used.
	ParamsSHA256 string `json:"params_sha256,omitempty"`
	// ParamsSHA256s lists every shadow params file's sha256 when the trader
	// runs several shadow engines (explore-2 next to explore-3).
	ParamsSHA256s []string `json:"params_sha256s,omitempty"`
	// Cumulative counters at connect time: bytes drained while no observer was
	// connected, and records dropped on a full ring. A rise since the previous
	// hello means a hole in the tape.
	DiscardedBytes uint64 `json:"discarded_bytes"`
	Dropped        uint64 `json:"dropped"`
}

// UsedParams reports whether the run used the params file with this sha256.
func (h *Hello) UsedParams(sha string) bool {
	if h.ParamsSHA256 == sha {
		return true
	}
	for _, s := range h.ParamsSHA256s {
		if s == sha {
			return true
		}
	}
	return false
}

// ProtoVersion is bumped on any incompatible change to the record layout.
const ProtoVersion = 1

// MaxRecord bounds a record body (a websocket frame is far smaller).
const MaxRecord = 1 << 20

// ErrRecord is a malformed record.
var ErrRecord = errors.New("ipc: malformed record")

// Reader decodes records from a stream.
type Reader struct {
	br  *bufio.Reader
	buf []byte
}

// NewReader wraps r.
func NewReader(r io.Reader) *Reader {
	return &Reader{br: bufio.NewReaderSize(r, 1<<20), buf: make([]byte, 64*1024)}
}

// Next returns the next record's type and body. The body is valid until the
// next call.
func (r *Reader) Next() (byte, []byte, error) {
	var hdr [5]byte
	if _, err := io.ReadFull(r.br, hdr[:]); err != nil {
		return 0, nil, err
	}
	n := int(binary.LittleEndian.Uint32(hdr[:4]))
	if n < 1 || n > MaxRecord {
		return 0, nil, fmt.Errorf("%w: length %d", ErrRecord, n)
	}
	if cap(r.buf) < n-1 {
		r.buf = make([]byte, n-1)
	}
	body := r.buf[:n-1]
	if _, err := io.ReadFull(r.br, body); err != nil {
		return 0, nil, err
	}
	return hdr[4], body, nil
}

// FrameBody splits a TypeFrame body.
func FrameBody(b []byte) (recvNs int64, conn byte, frame []byte, err error) {
	if len(b) < 9 {
		return 0, 0, nil, ErrRecord
	}
	return int64(binary.LittleEndian.Uint64(b)), b[8], b[9:], nil
}

// GapBody splits a TypeGap body.
func GapBody(b []byte) (records, bytes uint64, atNs int64, err error) {
	if len(b) != 24 {
		return 0, 0, 0, ErrRecord
	}
	return binary.LittleEndian.Uint64(b), binary.LittleEndian.Uint64(b[8:]), int64(binary.LittleEndian.Uint64(b[16:])), nil
}

// ConnBody splits a TypeConn body.
func ConnBody(b []byte) (atNs int64, conn, state byte, err error) {
	if len(b) != 10 {
		return 0, 0, 0, ErrRecord
	}
	return int64(binary.LittleEndian.Uint64(b)), b[8], b[9], nil
}
