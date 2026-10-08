package archive

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/klauspost/compress/zstd"
)

// .llp: one symbol-day of aggregate trades.
//
//	magic "LLP1" | uint32 record count | zstd stream of records
//	record (32 bytes, little endian): aggId int64 | tradeMs int64 | px int64 | qty int64
//	the top bit of qty carries is_buyer_maker (quantities are never negative).
const (
	llpMagic   = "LLP1"
	recordSize = 32
	makerBit   = int64(-1 << 63)
)

// ErrBadFile is returned for a file that is not a complete .llp.
var ErrBadFile = errors.New("archive: not a valid .llp file")

// WriteLLP writes trades to path atomically (temp file + rename).
func WriteLLP(path string, trades []Trade) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".llp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	var hdr [8]byte
	copy(hdr[:4], llpMagic)
	binary.LittleEndian.PutUint32(hdr[4:], uint32(len(trades)))
	if _, err := tmp.Write(hdr[:]); err != nil {
		tmp.Close()
		return err
	}
	zw, err := zstd.NewWriter(tmp, zstd.WithEncoderLevel(zstd.SpeedBetterCompression))
	if err != nil {
		tmp.Close()
		return err
	}
	bw := bufio.NewWriterSize(zw, 1<<20)
	var rec [recordSize]byte
	for _, t := range trades {
		q := t.Qty
		if t.BuyerMaker {
			q |= makerBit
		}
		binary.LittleEndian.PutUint64(rec[0:], uint64(t.AggID))
		binary.LittleEndian.PutUint64(rec[8:], uint64(t.TradeMs))
		binary.LittleEndian.PutUint64(rec[16:], uint64(t.Px))
		binary.LittleEndian.PutUint64(rec[24:], uint64(q))
		if _, err := bw.Write(rec[:]); err != nil {
			zw.Close()
			tmp.Close()
			return err
		}
	}
	if err := bw.Flush(); err != nil {
		zw.Close()
		tmp.Close()
		return err
	}
	if err := zw.Close(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// LLPReader streams one .llp file.
type LLPReader struct {
	f     *os.File
	zr    *zstd.Decoder
	br    *bufio.Reader
	count uint32
	read  uint32
	rec   [recordSize]byte
}

// OpenLLP opens path for reading.
func OpenLLP(path string) (*LLPReader, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	var hdr [8]byte
	if _, err := io.ReadFull(f, hdr[:]); err != nil || string(hdr[:4]) != llpMagic {
		f.Close()
		return nil, fmt.Errorf("%s: %w", path, ErrBadFile)
	}
	zr, err := zstd.NewReader(f, zstd.WithDecoderConcurrency(1))
	if err != nil {
		f.Close()
		return nil, err
	}
	return &LLPReader{f: f, zr: zr, br: bufio.NewReaderSize(zr, 1<<20), count: binary.LittleEndian.Uint32(hdr[4:])}, nil
}

// Count is the number of records in the file.
func (r *LLPReader) Count() int { return int(r.count) }

// Next reads the next trade. It returns io.EOF after the last record, and
// ErrBadFile if the stream ends before the header's count.
func (r *LLPReader) Next(t *Trade) error {
	if r.read == r.count {
		return io.EOF
	}
	if _, err := io.ReadFull(r.br, r.rec[:]); err != nil {
		return fmt.Errorf("record %d of %d: %w", r.read, r.count, ErrBadFile)
	}
	r.read++
	t.AggID = int64(binary.LittleEndian.Uint64(r.rec[0:]))
	t.TradeMs = int64(binary.LittleEndian.Uint64(r.rec[8:]))
	t.Px = int64(binary.LittleEndian.Uint64(r.rec[16:]))
	q := int64(binary.LittleEndian.Uint64(r.rec[24:]))
	t.BuyerMaker = q&makerBit != 0
	t.Qty = q &^ makerBit
	return nil
}

// Close releases the file.
func (r *LLPReader) Close() error {
	r.zr.Close()
	return r.f.Close()
}

// ReadAllLLP reads a whole file (tests and small tools).
func ReadAllLLP(path string) ([]Trade, error) {
	r, err := OpenLLP(path)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	out := make([]Trade, 0, r.Count())
	var t Trade
	for {
		if err := r.Next(&t); err == io.EOF {
			return out, nil
		} else if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
}
