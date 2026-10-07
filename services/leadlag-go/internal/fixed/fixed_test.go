package fixed

import "testing"

func TestParseE8(t *testing.T) {
	cases := []struct {
		in   string
		want int64
		err  bool
	}{
		{"0", 0, false},
		{"7", 700_000_000, false},
		{"142.35", 14_235_000_000, false},
		{"142.3500", 14_235_000_000, false},
		{"0.00001234", 1234, false},
		{"0.000012340000", 1234, false},
		{"0.000000001", 0, true},
		{"123456.7", 12_345_670_000_000, false},
		{".5", 50_000_000, false},
		{"", 0, true},
		{".", 0, true},
		{"1.2.3", 0, true},
		{"-1", 0, true},
		{"99999999999999999999", 0, true},
		// Binance archive CSVs: large quantities in scientific notation.
		{"1.6711908E7", 1_671_190_800_000_000, false},
		{"2.0007964E7", 2_000_796_400_000_000, false},
		{"1e3", 100_000_000_000, false},
		{"1.5e-3", 150_000, false},
		{"1.5E+2", 15_000_000_000, false},
		{"1.5e-9", 0, true},
		{"1e", 0, true},
		{"1e+", 0, true},
		{"1e99", 0, true},
		{"e5", 0, true},
	}
	for _, c := range cases {
		got, err := ParseE8String(c.in)
		if (err != nil) != c.err || got != c.want {
			t.Errorf("ParseE8(%q) = %d, %v; want %d, err=%v", c.in, got, err, c.want, c.err)
		}
	}
}

func TestFormatRoundTrip(t *testing.T) {
	for _, s := range []string{"0", "7", "142.35", "0.00001234", "123456.7"} {
		v, err := ParseE8String(s)
		if err != nil {
			t.Fatal(err)
		}
		if got := Format(v); got != s && !(s == ".5") {
			t.Errorf("Format(ParseE8(%q)) = %q", s, got)
		}
	}
}

func TestParseE8NoAlloc(t *testing.T) {
	b := []byte("64123.45")
	if n := testing.AllocsPerRun(1000, func() { _, _ = ParseE8(b) }); n != 0 {
		t.Fatalf("ParseE8 allocates %v per run", n)
	}
}
