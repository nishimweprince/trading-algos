#include "cxa/status.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <string_view>
#include <vector>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

namespace cxa {
namespace {

std::string_view request_path(std::string_view line) {
  constexpr std::string_view prefix = "GET ";
  if (!line.starts_with(prefix)) return {};
  line.remove_prefix(prefix.size());
  const auto end = line.find(' ');
  if (end != std::string_view::npos) line = line.substr(0, end);
  const auto query = line.find('?');
  if (query != std::string_view::npos) line = line.substr(0, query);
  return line;
}

}  // namespace

StatusServer::StatusServer(std::uint16_t port, Snapshot snapshot, std::string journal_path)
    : port_(port), snapshot_(std::move(snapshot)), journal_path_(std::move(journal_path)) {}
StatusServer::~StatusServer() { stop(); }

bool StatusServer::start(std::string& error) {
  socket_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (socket_ < 0) { error = std::strerror(errno); return false; }
  int enabled = 1;
  setsockopt(socket_, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port_);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (::bind(socket_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 || ::listen(socket_, 8) < 0) {
    error = std::strerror(errno);
    ::close(socket_);
    socket_ = -1;
    return false;
  }
  thread_ = std::thread([this] { serve(); });
  return true;
}

void StatusServer::stop() {
  stop_.store(true, std::memory_order_relaxed);
  if (socket_ >= 0) {
    ::shutdown(socket_, SHUT_RDWR);
    ::close(socket_);
    socket_ = -1;
  }
  if (thread_.joinable()) thread_.join();
}

void StatusServer::serve() {
  while (!stop_.load(std::memory_order_relaxed)) {
    const auto client = ::accept(socket_, nullptr, nullptr);
    if (client < 0) continue;
    std::array<char, 2048> request{};
    const auto count = ::recv(client, request.data(), request.size() - 1, 0);
    if (count > 0) {
      const auto path = request_path({request.data(), static_cast<std::size_t>(count)});
      std::string body;
      std::string_view content_type = "application/json";
      std::string_view status = "200 OK";
      if (path == "/") {
        body = dashboard_page();
        content_type = "text/html; charset=utf-8";
      } else if (path == "/api/activity") {
        body = activity_json(journal_path_);
      } else if (path == "/api/summary" || path == "/health/live" || path == "/health/ready" || path == "/metrics") {
        body = snapshot_();
      } else {
        body = "{\"error\":\"not_found\"}\n";
        status = "404 Not Found";
      }
      const auto response = std::string{"HTTP/1.1 "} + std::string(status) + "\r\nContent-Type: " +
                            std::string(content_type) + "\r\nCache-Control: no-store\r\nConnection: close\r\n" +
                            "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
      ::send(client, response.data(), response.size(), MSG_NOSIGNAL);
    }
    ::close(client);
  }
}

std::string activity_json(const std::string& journal_path) {
  constexpr std::streamoff kTail = 512 * 1024;
  constexpr std::size_t kMaxRows = 200;
  std::ifstream input(journal_path, std::ios::binary | std::ios::ate);
  if (!input) return "{\"rows\":[]}\n";
  const auto size = static_cast<std::streamoff>(input.tellg());
  input.seekg(std::max<std::streamoff>(0, size - kTail));
  std::string line;
  if (size > kTail) std::getline(input, line);  // drop the partial first line
  std::vector<std::string> rows;
  while (std::getline(input, line)) {
    if (line.size() > 2 && (line[0] == 'P' || line[0] == 'X' || line[0] == 'H' || line[0] == 'G')) rows.push_back(line);
  }
  if (rows.size() > kMaxRows) rows.erase(rows.begin(), rows.end() - static_cast<std::ptrdiff_t>(kMaxRows));
  std::string out = "{\"rows\":[";
  for (std::size_t i = 0; i < rows.size(); ++i) {
    if (i > 0) out += ',';
    out += '"';
    for (const char c : rows[i]) {
      if (c == '"' || c == '\\') out += '\\';
      out += c;
    }
    out += '"';
  }
  out += "]}\n";
  return out;
}

std::string dashboard_page() {
  return R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crossex Shadow</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--good:#1f7a4d;--bad:#b3261e;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9a9993;--line:#2c2c2a;--good:#5fbf8a;--bad:#ef7a70;--card:#1c1c1a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}.muted{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.tile b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}.wrap{overflow-x:auto;margin-bottom:16px}
.good{color:var(--good)}.bad{color:var(--bad)}pre{white-space:pre-wrap;font-size:12px}
</style></head><body><main>
<h1 id="title">Binance ↔ peer shadow</h1><div class="muted" id="sub">loading…</div>
<div class="tiles" id="tiles"></div>
<div class="wrap"><table id="symbols"></table></div>
<h2 style="font-size:15px">Recent decisions and cycles</h2><pre id="activity" class="muted"></pre>
</main><script>
const usd=r=>(r/1e8).toFixed(2), bp=p=>(p/100).toFixed(1);
function tile(k,v,c=''){return `<div class="tile"><span class="muted">${k}</span><b class="${c}">${v}</b></div>`}
async function refresh(){
 try{
  const s=await (await fetch('/api/summary')).json();
  const peer=(s.peer||'bybit').replace(/^./,c=>c.toUpperCase());
  document.getElementById('title').textContent=`Binance ↔ ${peer} shadow`;
  document.getElementById('sub').textContent=`${s.mode} · ${s.exec_mode||'taker_taker'} · latency ${s.latency_mode} · ${s.halted?'HALTED':'running'} · updated ${new Date().toLocaleTimeString()}`;
  document.getElementById('tiles').innerHTML=[
   tile('Net P&L (USD)',usd(s.net_raw),s.net_raw>=0?'good':'bad'),tile('Cycles',s.cycles),tile('Missed',s.missed),
   tile('Repairs',s.repairs),tile('Open pairs',s.open_pairs),tile('Census episodes',s.census_episodes),
   tile('Binance one-way p50/p95 ms',`${(s.latency[0].p50_ns/1e6).toFixed(1)} / ${(s.latency[0].p95_ns/1e6).toFixed(1)}`),
   tile(`${peer} one-way p50/p95 ms`,`${(s.latency[1].p50_ns/1e6).toFixed(1)} / ${(s.latency[1].p95_ns/1e6).toFixed(1)}`),
   tile('Gaps B / P',`${s.gaps[0]} / ${s.gaps[1]}`)].join('');
  document.getElementById('symbols').innerHTML='<tr><th>Symbol</th><th>Books</th><th>Basis bp</th><th>Mean bp</th><th>Warm</th><th>Fees B/P ppm</th><th>Funding B/P ppm</th><th>Phase</th><th>Dir</th></tr>'+
   s.symbols.map(x=>`<tr><td>${x.name}</td><td>${x.books}</td><td>${bp(x.basis_ppm)}</td><td>${bp(x.mean_ppm)}</td><td>${x.warm?'yes':'no'}</td><td>${x.fee_ppm[0]}/${x.fee_ppm[1]}${x.maker_ppm?` m${x.maker_ppm[0]}/${x.maker_ppm[1]}`:''}</td><td>${x.funding_ppm[0]}/${x.funding_ppm[1]}</td><td>${x.phase}</td><td>${x.dir}</td></tr>`).join('');
  const a=await (await fetch('/api/activity')).json();
  document.getElementById('activity').textContent=a.rows.slice(-40).reverse().join('\n')||'no decisions yet';
 }catch(e){document.getElementById('sub').textContent='status unavailable: '+e}
}
refresh();setInterval(refresh,2000);
</script></body></html>
)HTML";
}

}  // namespace cxa
