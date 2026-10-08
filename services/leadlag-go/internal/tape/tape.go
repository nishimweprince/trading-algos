// Package tape writes and reads the recorder's hourly files: zstd-compressed
// JSON lines holding every raw websocket frame with its local receive time, plus
// hello, gap and connection-state lines. A manifest is written next to each file
// when it is closed.
//
//	<dir>/<YYYY-MM-DD>/<HH>-<openUnix>.jsonl.zst
//	{"r":1791393218950123456,"c":"m","d":{"stream":"btcusdt@aggTrade","data":{…}}}
//	{"hello":{…}}  {"gap":{"records":2,"bytes":400,"at":…}}  {"conn":{"at":…,"c":"p","s":"down"}}
package tape

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
)

// Manifest summarizes one closed tape file.
type Manifest struct {
	File        string            `json:"file"`
	Lines       int64             `json:"lines"`
	Frames      map[string]int64  `json:"frames"` // by conn: "m", "p"
	FirstRecvNs int64             `json:"first_recv_ns"`
	LastRecvNs  int64             `json:"last_recv_ns"`
	Gaps        int64             `json:"gaps"`
	GapRecords  uint64            `json:"gap_records"`
	Hellos      int64             `json:"hellos"`
	ConnChanges map[string]int64  `json:"conn_changes,omitempty"`
	Bytes       int64             `json:"bytes"`
	SHA256      string            `json:"sha256"`
	OpenedAt    string            `json:"opened_at"`
	ClosedAt    string            `json:"closed_at"`
	Extra       map[string]string `json:"extra,omitempty"`
}

// Writer appends lines to the current hour's file, rotating on the UTC hour of
// each line's timestamp.
type Writer struct {
	Dir string

	hour  int64 // unix hour of the open file
	f     *os.File
	cw    *countWriter
	zw    *zstd.Encoder
	bw    *bufio.Writer
	man   Manifest
	path  string
	line  []byte
	Files int64 // files closed so far
}

type countWriter struct {
	w io.Writer
	h hash.Hash
	n int64
}

func (c *countWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.h.Write(p[:n])
	c.n += int64(n)
	return n, err
}

// Frame writes a frame line. frame must be one JSON value (Binance frames are objects).
func (w *Writer) Frame(recvNs int64, conn byte, frame []byte) error {
	if err := w.rotate(recvNs); err != nil {
		return err
	}
	b := append(w.line[:0], `{"r":`...)
	b = strconv.AppendInt(b, recvNs, 10)
	b = append(b, `,"c":"`...)
	b = append(b, conn)
	b = append(b, `","d":`...)
	b = append(b, frame...)
	b = append(b, "}\n"...)
	w.line = b
	if w.man.FirstRecvNs == 0 {
		w.man.FirstRecvNs = recvNs
	}
	w.man.LastRecvNs = recvNs
	w.man.Frames[string(conn)]++
	return w.write(b)
}

// Hello writes the trader's hello (raw JSON).
func (w *Writer) Hello(atNs int64, hello []byte) error {
	if err := w.rotate(atNs); err != nil {
		return err
	}
	w.man.Hellos++
	return w.writeObj("hello", json.RawMessage(hello))
}

// Gap records dropped records.
func (w *Writer) Gap(atNs int64, records, bytes uint64) error {
	if err := w.rotate(atNs); err != nil {
		return err
	}
	w.man.Gaps++
	w.man.GapRecords += records
	return w.writeObj("gap", map[string]any{"records": records, "bytes": bytes, "at": atNs})
}

// Conn records a feed connection state change.
func (w *Writer) Conn(atNs int64, conn byte, up bool) error {
	if err := w.rotate(atNs); err != nil {
		return err
	}
	s := "down"
	if up {
		s = "up"
	}
	if w.man.ConnChanges == nil {
		w.man.ConnChanges = map[string]int64{}
	}
	w.man.ConnChanges[string(conn)+"_"+s]++
	return w.writeObj("conn", map[string]any{"at": atNs, "c": string(conn), "s": s})
}

func (w *Writer) writeObj(key string, v any) error {
	b, err := json.Marshal(map[string]any{key: v})
	if err != nil {
		return err
	}
	return w.write(append(b, '\n'))
}

func (w *Writer) write(b []byte) error {
	w.man.Lines++
	_, err := w.bw.Write(b)
	return err
}

func (w *Writer) rotate(atNs int64) error {
	hour := atNs / int64(time.Hour)
	if w.f != nil && hour == w.hour {
		return nil
	}
	if w.f != nil && hour < w.hour {
		return nil // a late line stays in the current file
	}
	if err := w.Close(); err != nil {
		return err
	}
	t := time.Unix(0, hour*int64(time.Hour)).UTC()
	dir := filepath.Join(w.Dir, t.Format(time.DateOnly))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	// The name carries the opening second. Two writers can open the same hour
	// within one second (an observer restart), so a taken name moves to the
	// next free number instead of failing: O_EXCL never overwrites a file.
	var f *os.File
	var err error
	for n := time.Now().Unix(); ; n++ {
		w.path = filepath.Join(dir, fmt.Sprintf("%02d-%d.jsonl.zst", t.Hour(), n))
		f, err = os.OpenFile(w.path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
		if !errors.Is(err, fs.ErrExist) {
			break
		}
	}
	if err != nil {
		return err
	}
	w.f, w.hour = f, hour
	w.cw = &countWriter{w: f, h: sha256.New()}
	w.zw, err = zstd.NewWriter(w.cw, zstd.WithEncoderLevel(zstd.SpeedDefault), zstd.WithEncoderConcurrency(1))
	if err != nil {
		return err
	}
	w.bw = bufio.NewWriterSize(w.zw, 1<<20)
	w.man = Manifest{File: filepath.Base(w.path), Frames: map[string]int64{}, OpenedAt: time.Now().UTC().Format(time.RFC3339)}
	return nil
}

// Flush pushes buffered lines into the compressor (not a durability point;
// Close is).
func (w *Writer) Flush() error {
	if w.bw == nil {
		return nil
	}
	return w.bw.Flush()
}

// Path is the open file, if any.
func (w *Writer) Path() string { return w.path }

// Current is the open file's running manifest.
func (w *Writer) Current() Manifest { return w.man }

// Close finishes the open file and writes its manifest.
func (w *Writer) Close() error {
	if w.f == nil {
		return nil
	}
	err := w.bw.Flush()
	if e := w.zw.Close(); err == nil {
		err = e
	}
	if e := w.f.Sync(); err == nil {
		err = e
	}
	if e := w.f.Close(); err == nil {
		err = e
	}
	w.man.Bytes = w.cw.n
	w.man.SHA256 = hex.EncodeToString(w.cw.h.Sum(nil))
	w.man.ClosedAt = time.Now().UTC().Format(time.RFC3339)
	b, _ := json.MarshalIndent(w.man, "", "  ")
	mpath := strings.TrimSuffix(w.path, ".jsonl.zst") + ".json"
	if e := os.WriteFile(mpath, b, 0o644); err == nil {
		err = e
	}
	w.f = nil
	w.Files++
	return err
}

// Line is one decoded tape line. Frame and Raw alias the reader's buffer.
type Line struct {
	Kind   string // "frame", "hello", "gap", "conn"
	RecvNs int64
	Conn   byte
	Frame  []byte
	Raw    []byte
}

// Reader reads tape files in order.
type Reader struct {
	files []string
	f     *os.File
	zr    *zstd.Decoder
	br    *bufio.Reader
}

// Files lists tape files under dir (recursively), sorted by name, which is
// chronological (day directory, hour, open time).
func Files(dir string) ([]string, error) {
	var out []string
	err := filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(p, ".jsonl.zst") {
			out = append(out, p)
		}
		return nil
	})
	sort.Slice(out, func(i, j int) bool { return fileKey(out[i]) < fileKey(out[j]) })
	return out, err
}

func fileKey(p string) string {
	day := filepath.Base(filepath.Dir(p))
	name := filepath.Base(p)
	hh, open, _ := strings.Cut(strings.TrimSuffix(name, ".jsonl.zst"), "-")
	return fmt.Sprintf("%s/%s/%020s", day, hh, open)
}

// NewReader reads the given files in order.
func NewReader(files []string) *Reader { return &Reader{files: files} }

var framePrefix = []byte(`{"r":`)

// Next returns the next line, or io.EOF after the last file.
func (r *Reader) Next(l *Line) error {
	for {
		if r.br == nil {
			if len(r.files) == 0 {
				return io.EOF
			}
			if err := r.open(r.files[0]); err != nil {
				return err
			}
			r.files = r.files[1:]
		}
		b, err := r.br.ReadSlice('\n')
		if err == io.EOF && len(b) == 0 {
			r.closeFile()
			continue
		}
		if err == bufio.ErrBufferFull {
			return fmt.Errorf("tape: line longer than %d bytes", r.br.Size())
		}
		if err != nil && err != io.EOF {
			return err
		}
		return decodeLine(b, l)
	}
}

func decodeLine(b []byte, l *Line) error {
	*l = Line{Raw: b}
	if bytes.HasPrefix(b, framePrefix) {
		rest := b[len(framePrefix):]
		i := 0
		for i < len(rest) && rest[i] >= '0' && rest[i] <= '9' {
			i++
		}
		v, err := strconv.ParseInt(string(rest[:i]), 10, 64)
		const mid = `,"c":"`
		if err != nil || len(rest) < i+len(mid)+1+len(`","d":`) || string(rest[i:i+len(mid)]) != mid {
			return fmt.Errorf("tape: bad frame line %.80q", b)
		}
		l.Kind, l.RecvNs, l.Conn = "frame", v, rest[i+len(mid)]
		d := rest[i+len(mid)+1+len(`","d":`):]
		d = bytes.TrimRight(d, "\n")
		if len(d) == 0 || d[len(d)-1] != '}' {
			return fmt.Errorf("tape: bad frame line %.80q", b)
		}
		l.Frame = d[:len(d)-1]
		return nil
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		return fmt.Errorf("tape: %w", err)
	}
	for k := range m {
		l.Kind = k
	}
	return nil
}

func (r *Reader) open(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	zr, err := zstd.NewReader(f, zstd.WithDecoderConcurrency(1))
	if err != nil {
		f.Close()
		return err
	}
	r.f, r.zr, r.br = f, zr, bufio.NewReaderSize(zr, 1<<20)
	return nil
}

func (r *Reader) closeFile() {
	if r.zr != nil {
		r.zr.Close()
	}
	if r.f != nil {
		r.f.Close()
	}
	r.f, r.zr, r.br = nil, nil, nil
}

// Close releases the open file.
func (r *Reader) Close() error {
	r.closeFile()
	return nil
}
