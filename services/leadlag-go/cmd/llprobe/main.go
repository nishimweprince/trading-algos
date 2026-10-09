// Command llprobe measures this host's latency to Binance USDⓈ-M, to pick the
// AWS Tokyo availability zone (run it for ~30 min on a small instance in each AZ)
// and to feed the measured latency into the backtest.
//
// It measures:
//
//   - warm REST round trip (keep-alive GET /fapi/v1/time, 1/s) and the clock
//     offset to Binance's server time;
//
//   - cold connection setup (DNS, TCP connect, TLS, first byte) to the REST,
//     market-stream and ws-fapi hosts, every 15 s;
//
//   - feed lag recv − E for btcusdt/ethusdt aggTrade (/market) and bookTicker
//     (/public), plus how far aggTrade publication trails the trade (E − T) and
//     whether E equals the next trade's T.
//
//     llprobe --duration 30m --out probe.json
//
// llprobe fees reads the account's commission rates (fees.go):
//
//	llprobe fees --symbols SOLUSDC,SOLUSDT
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"os"
	"os/signal"
	"sort"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/model"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/parse"
	"github.com/nishimweprince/trading-algos/services/leadlag-go/internal/stats"
)

type probe struct {
	mu        sync.Mutex
	restWarm  stats.Hist // µs
	offsets   []float64  // ms, server − local midpoint
	cold      map[string]map[string]*stats.Hist
	lag       map[string]*stats.Hist // stream → recv−E µs
	aggPubLag map[string]*stats.Hist // symbol → E−T ms
	aggNext   map[string]*[2]int     // symbol → [E == next T, total]
	lastAgg   map[string]int64       // symbol → previous E
	errors    map[string]int
	reconnect map[string]int
}

// Report is the JSON written to --out.
type Report struct {
	Host          string                              `json:"host"`
	AZID          string                              `json:"az_id"`
	AZ            string                              `json:"az"`
	InstanceType  string                              `json:"instance_type"`
	Start         time.Time                           `json:"start"`
	End           time.Time                           `json:"end"`
	RESTWarmUs    stats.Summary                       `json:"rest_warm_us"`
	ClockOffsetMs float64                             `json:"clock_offset_ms_median"`
	ColdUs        map[string]map[string]stats.Summary `json:"cold_us"`
	FeedLagUs     map[string]stats.Summary            `json:"feed_lag_us"`
	AggPublishMs  map[string]stats.Summary            `json:"aggtrade_publish_after_trade_ms"`
	AggEIsNextT   map[string]float64                  `json:"aggtrade_E_equals_next_T_frac"`
	Errors        map[string]int                      `json:"errors"`
	Reconnects    map[string]int                      `json:"reconnects"`
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "fees" {
		if err := runFees(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "llprobe fees:", err)
			os.Exit(1)
		}
		return
	}
	dur := flag.Duration("duration", 30*time.Minute, "how long to probe")
	out := flag.String("out", "", "write the JSON report here (default probe-<az>.json)")
	rest := flag.String("rest", "https://fapi.binance.com", "REST base")
	ws := flag.String("ws", "wss://fstream.binance.com", "market stream base")
	wsapi := flag.String("wsapi", "ws-fapi.binance.com:443", "ws-fapi host:port (TLS setup only)")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, *dur)
	defer cancel()

	p := &probe{
		cold: map[string]map[string]*stats.Hist{}, lag: map[string]*stats.Hist{},
		aggPubLag: map[string]*stats.Hist{}, aggNext: map[string]*[2]int{}, lastAgg: map[string]int64{},
		errors: map[string]int{}, reconnect: map[string]int{},
	}
	rep := Report{Start: time.Now().UTC()}
	rep.Host, _ = os.Hostname()
	rep.AZID, rep.AZ, rep.InstanceType = imds()
	fmt.Fprintf(os.Stderr, "llprobe: %s az=%s (%s) %s for %s\n", rep.Host, rep.AZID, rep.AZ, rep.InstanceType, *dur)

	var wg sync.WaitGroup
	run := func(f func()) { wg.Add(1); go func() { defer wg.Done(); f() }() }
	run(func() { p.warmREST(ctx, *rest) })
	run(func() { p.coldLoop(ctx, *rest, *ws, *wsapi) })
	run(func() {
		p.stream(ctx, "market", *ws+"/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade")
	})
	run(func() { p.stream(ctx, "public", *ws+"/public/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker") })
	run(func() {
		t := time.NewTicker(time.Minute)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				p.mu.Lock()
				w := p.restWarm.Summarize()
				l := p.lag["btcusdt@bookTicker"]
				var ls stats.Summary
				if l != nil {
					ls = l.Summarize()
				}
				p.mu.Unlock()
				fmt.Fprintf(os.Stderr, "  rest p50 %.1f ms p99 %.1f ms | bookTicker lag p50 %.1f ms p99 %.1f ms\n",
					ms(w.P50), ms(w.P99), ms(ls.P50), ms(ls.P99))
			}
		}
	})
	wg.Wait()

	rep.End = time.Now().UTC()
	p.mu.Lock()
	rep.RESTWarmUs = p.restWarm.Summarize()
	rep.ClockOffsetMs = median(p.offsets)
	rep.ColdUs = map[string]map[string]stats.Summary{}
	for host, phases := range p.cold {
		rep.ColdUs[host] = map[string]stats.Summary{}
		for ph, h := range phases {
			rep.ColdUs[host][ph] = h.Summarize()
		}
	}
	rep.FeedLagUs = map[string]stats.Summary{}
	for k, h := range p.lag {
		rep.FeedLagUs[k] = h.Summarize()
	}
	rep.AggPublishMs = map[string]stats.Summary{}
	for k, h := range p.aggPubLag {
		rep.AggPublishMs[k] = h.Summarize()
	}
	rep.AggEIsNextT = map[string]float64{}
	for k, c := range p.aggNext {
		if c[1] > 0 {
			rep.AggEIsNextT[k] = float64(c[0]) / float64(c[1])
		}
	}
	rep.Errors, rep.Reconnects = p.errors, p.reconnect
	p.mu.Unlock()

	printReport(&rep)
	path := *out
	if path == "" {
		path = fmt.Sprintf("probe-%s.json", orDefault(rep.AZID, "local"))
	}
	b, _ := json.MarshalIndent(rep, "", "  ")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, "llprobe:", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stderr, "wrote", path)
}

func (p *probe) warmREST(ctx context.Context, base string) {
	client := &http.Client{Timeout: 5 * time.Second}
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		t0 := time.Now()
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/fapi/v1/time", nil)
		resp, err := client.Do(req)
		if err == nil {
			var body struct {
				ServerTime int64 `json:"serverTime"`
			}
			err = json.NewDecoder(resp.Body).Decode(&body)
			resp.Body.Close()
			t1 := time.Now()
			if err == nil {
				mid := t0.Add(t1.Sub(t0) / 2)
				p.mu.Lock()
				p.restWarm.Record(t1.Sub(t0).Microseconds())
				p.offsets = append(p.offsets, float64(body.ServerTime)-float64(mid.UnixMicro())/1000)
				p.mu.Unlock()
			}
		}
		if err != nil && ctx.Err() == nil {
			p.countErr("rest_warm")
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// coldLoop measures fresh-connection setup to each host every 15 s.
func (p *probe) coldLoop(ctx context.Context, rest, ws, wsapi string) {
	restHost := hostOf(rest)
	wsHost := hostOf(ws)
	t := time.NewTicker(15 * time.Second)
	defer t.Stop()
	for {
		p.coldHTTP(ctx, rest+"/fapi/v1/ping", restHost)
		p.coldTLS(ctx, wsHost+":443", "fstream")
		p.coldTLS(ctx, wsapi, "ws-fapi")
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (p *probe) coldHTTP(ctx context.Context, url, label string) {
	// Trace callbacks run on the transport's goroutines, hence the lock.
	var (
		mu                                   sync.Mutex
		dnsStart, connStart, tlsStart, start time.Time
	)
	phases := map[string]int64{}
	mark := func(at *time.Time) { mu.Lock(); *at = time.Now(); mu.Unlock() }
	done := func(k string, since *time.Time) {
		mu.Lock()
		phases[k] = time.Since(*since).Microseconds()
		mu.Unlock()
	}
	trace := &httptrace.ClientTrace{
		DNSStart:             func(httptrace.DNSStartInfo) { mark(&dnsStart) },
		DNSDone:              func(httptrace.DNSDoneInfo) { done("dns", &dnsStart) },
		ConnectStart:         func(string, string) { mark(&connStart) },
		ConnectDone:          func(string, string, error) { done("tcp", &connStart) },
		TLSHandshakeStart:    func() { mark(&tlsStart) },
		TLSHandshakeDone:     func(tls.ConnectionState, error) { done("tls", &tlsStart) },
		GotFirstResponseByte: func() { done("ttfb_total", &start) },
	}
	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{DisableKeepAlives: true}}
	req, _ := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodGet, url, nil)
	mark(&start)
	resp, err := client.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			p.countErr("cold_" + label)
		}
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	mu.Lock()
	defer mu.Unlock()
	p.recordCold("rest", phases)
}

func (p *probe) coldTLS(ctx context.Context, addr, label string) {
	d := net.Dialer{Timeout: 10 * time.Second}
	t0 := time.Now()
	raw, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		if ctx.Err() == nil {
			p.countErr("cold_" + label)
		}
		return
	}
	tcp := time.Since(t0)
	host, _, _ := net.SplitHostPort(addr)
	c := tls.Client(raw, &tls.Config{ServerName: host})
	t1 := time.Now()
	err = c.HandshakeContext(ctx)
	c.Close()
	if err != nil {
		p.countErr("cold_" + label)
		return
	}
	p.recordCold(label, map[string]int64{"tcp_incl_dns": tcp.Microseconds(), "tls": time.Since(t1).Microseconds()})
}

func (p *probe) recordCold(host string, phases map[string]int64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.cold[host] == nil {
		p.cold[host] = map[string]*stats.Hist{}
	}
	for ph, v := range phases {
		if p.cold[host][ph] == nil {
			p.cold[host][ph] = &stats.Hist{}
		}
		p.cold[host][ph].Record(v)
	}
}

func (p *probe) stream(ctx context.Context, route, url string) {
	syms := parse.NewSymbols([]string{"BTCUSDT", "ETHUSDT"})
	names := []string{"btcusdt", "ethusdt"}
	backoff := time.Second
	for ctx.Err() == nil {
		c, _, err := websocket.DefaultDialer.DialContext(ctx, url, nil)
		if err != nil {
			p.countErr("ws_dial_" + route)
			sleep(ctx, backoff)
			backoff = min(backoff*2, 30*time.Second)
			continue
		}
		backoff = time.Second
		go func() { <-ctx.Done(); c.Close() }()
		var e model.Event
		for {
			c.SetReadDeadline(time.Now().Add(30 * time.Second))
			_, b, err := c.ReadMessage()
			recv := time.Now()
			if err != nil {
				break
			}
			if parse.Frame(b, syms, &e) != nil {
				continue
			}
			kind := "@aggTrade"
			if e.Kind == model.KindQuote {
				kind = "@bookTicker"
			}
			key := names[e.Sym] + kind
			p.mu.Lock()
			if p.lag[key] == nil {
				p.lag[key] = &stats.Hist{}
			}
			p.lag[key].Record(recv.UnixMicro() - e.ExchMs*1000)
			if e.Kind == model.KindTrade {
				sym := names[e.Sym]
				if p.aggPubLag[sym] == nil {
					p.aggPubLag[sym] = &stats.Hist{}
					p.aggNext[sym] = &[2]int{}
				}
				p.aggPubLag[sym].Record(e.ExchMs - e.TradeMs)
				if prevE, ok := p.lastAgg[sym]; ok {
					p.aggNext[sym][1]++
					if prevE == e.TradeMs {
						p.aggNext[sym][0]++
					}
				}
				p.lastAgg[sym] = e.ExchMs
			}
			p.mu.Unlock()
		}
		c.Close()
		if ctx.Err() == nil {
			p.mu.Lock()
			p.reconnect[route]++
			for _, s := range names {
				delete(p.lastAgg, s)
			}
			p.mu.Unlock()
		}
	}
}

func (p *probe) countErr(k string) {
	p.mu.Lock()
	p.errors[k]++
	p.mu.Unlock()
}

// imds reads the AZ id, AZ name and instance type from the cloud's metadata
// service: EC2's IMDSv2, else Azure's IMDS (azID "azure:<region>[-<zone>]").
// Empty when neither answers.
func imds() (azID, az, itype string) {
	return imdsAt("http://169.254.169.254", &http.Client{Timeout: 500 * time.Millisecond})
}

func imdsAt(base string, c *http.Client) (azID, az, itype string) {
	if azID, az, itype = awsIMDS(base, c); azID != "" || az != "" {
		return azID, az, itype
	}
	return azureIMDS(base, c)
}

// fetch returns the body of a 200 answer only: other clouds answer EC2's
// paths with error documents, which must never be read as values.
func fetch(c *http.Client, req *http.Request) (string, bool) {
	resp, err := c.Do(req)
	if err != nil {
		return "", false
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode != http.StatusOK {
		return "", false
	}
	return string(b), true
}

func awsIMDS(base string, c *http.Client) (azID, az, itype string) {
	req, _ := http.NewRequest(http.MethodPut, base+"/latest/api/token", nil)
	req.Header.Set("X-aws-ec2-metadata-token-ttl-seconds", "60")
	tok, ok := fetch(c, req)
	if !ok {
		return "", "", ""
	}
	get := func(path string) string {
		r, _ := http.NewRequest(http.MethodGet, base+"/latest/meta-data/"+path, nil)
		r.Header.Set("X-aws-ec2-metadata-token", tok)
		v, _ := fetch(c, r)
		return v
	}
	return get("placement/availability-zone-id"), get("placement/availability-zone"), get("instance-type")
}

func azureIMDS(base string, c *http.Client) (azID, az, itype string) {
	req, _ := http.NewRequest(http.MethodGet, base+"/metadata/instance/compute?api-version=2021-02-01", nil)
	req.Header.Set("Metadata", "true")
	body, ok := fetch(c, req)
	if !ok {
		return "", "", ""
	}
	var m struct {
		Location string `json:"location"`
		Zone     string `json:"zone"`
		VMSize   string `json:"vmSize"`
	}
	if json.Unmarshal([]byte(body), &m) != nil || m.Location == "" {
		return "", "", ""
	}
	azID = "azure:" + m.Location
	if m.Zone != "" {
		azID += "-" + m.Zone
	}
	return azID, m.Location, m.VMSize
}

func printReport(r *Report) {
	fmt.Printf("\n%s  az %s (%s)  %s  %s → %s\n", r.Host, orDefault(r.AZID, "?"), r.AZ, r.InstanceType,
		r.Start.Format(time.TimeOnly), r.End.Format(time.TimeOnly))
	fmt.Printf("REST warm RTT    p50 %7.2f  p90 %7.2f  p99 %7.2f  max %7.2f ms  (n=%d)\n",
		ms(r.RESTWarmUs.P50), ms(r.RESTWarmUs.P90), ms(r.RESTWarmUs.P99), ms(r.RESTWarmUs.Max), r.RESTWarmUs.N)
	fmt.Printf("clock offset     %+.2f ms (Binance − local, median; feed lag below is not adjusted)\n", r.ClockOffsetMs)
	hosts := keys(r.ColdUs)
	for _, h := range hosts {
		for _, ph := range keys(r.ColdUs[h]) {
			s := r.ColdUs[h][ph]
			fmt.Printf("cold %-8s %-12s p50 %7.2f  p99 %7.2f ms\n", h, ph, ms(s.P50), ms(s.P99))
		}
	}
	for _, k := range keys(r.FeedLagUs) {
		s := r.FeedLagUs[k]
		fmt.Printf("feed recv−E %-20s p50 %7.2f  p90 %7.2f  p99 %7.2f  max %8.2f ms (n=%d)\n", k, ms(s.P50), ms(s.P90), ms(s.P99), ms(s.Max), s.N)
	}
	for _, k := range keys(r.AggPublishMs) {
		s := r.AggPublishMs[k]
		fmt.Printf("aggTrade E−T %-8s p50 %5d  p90 %5d  p99 %5d ms; E == next T in %.1f%%\n", k, s.P50, s.P90, s.P99, 100*r.AggEIsNextT[k])
	}
	if len(r.Errors) > 0 || len(r.Reconnects) > 0 {
		fmt.Printf("errors %v reconnects %v\n", r.Errors, r.Reconnects)
	}
}

func keys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func median(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	return s[len(s)/2]
}

func hostOf(url string) string {
	for _, p := range []string{"https://", "wss://", "http://", "ws://"} {
		if len(url) > len(p) && url[:len(p)] == p {
			url = url[len(p):]
		}
	}
	for i := range url {
		if url[i] == '/' {
			return url[:i]
		}
	}
	return url
}

func ms(us int64) float64 { return float64(us) / 1000 }

func sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

func orDefault(s, d string) string {
	if s == "" {
		return d
	}
	return s
}
