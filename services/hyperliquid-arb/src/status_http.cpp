#include "hla/status_http.hpp"

#include <array>
#include <cstring>
#include <string_view>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
using Socket = SOCKET;
constexpr Socket kInvalid = INVALID_SOCKET;
static int close_sock(Socket s) { return ::closesocket(s); }
static bool net_init() {
  static bool ready = false;
  if (!ready) {
    WSADATA data{};
    if (::WSAStartup(MAKEWORD(2, 2), &data) != 0) return false;
    ready = true;
  }
  return true;
}
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
using Socket = int;
constexpr Socket kInvalid = -1;
static int close_sock(Socket s) { return ::close(s); }
static bool net_init() { return true; }
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#endif

namespace hla {
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

StatusHttp::StatusHttp(std::uint16_t port) : port_(port) {}
StatusHttp::~StatusHttp() { stop(); }

void StatusHttp::publish(std::string json) {
  std::lock_guard lock(mutex_);
  json_ = std::move(json);
}

std::string StatusHttp::snapshot() const {
  std::lock_guard lock(mutex_);
  return json_;
}

bool StatusHttp::start(std::string& error) {
  if (port_ == 0) return true;
  if (!net_init()) {
    error = "status http: winsock init failed";
    return false;
  }
  const Socket sock = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (sock == kInvalid) {
    error = "status http: socket failed";
    return false;
  }
  int enabled = 1;
#ifdef _WIN32
  ::setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&enabled), sizeof(enabled));
#else
  ::setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
#endif
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port_);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (::bind(sock, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 || ::listen(sock, 8) < 0) {
    error = "status http: bind/listen on 127.0.0.1:" + std::to_string(port_) + " failed";
    close_sock(sock);
    return false;
  }
  socket_ = static_cast<std::intptr_t>(sock);
  thread_ = std::thread([this] { serve(); });
  return true;
}

void StatusHttp::stop() {
  stop_.store(true);
  if (socket_ != -1) {
    const auto sock = static_cast<Socket>(socket_);
#ifdef _WIN32
    ::shutdown(sock, SD_BOTH);
#else
    ::shutdown(sock, SHUT_RDWR);
#endif
    close_sock(sock);
    socket_ = -1;
  }
  if (thread_.joinable()) thread_.join();
}

void StatusHttp::serve() {
  const auto listen = static_cast<Socket>(socket_);
  while (!stop_.load()) {
    const Socket client = ::accept(listen, nullptr, nullptr);
    if (client == kInvalid) continue;
    std::array<char, 2048> request{};
#ifdef _WIN32
    const int count = ::recv(client, request.data(), static_cast<int>(request.size() - 1), 0);
#else
    const auto count = ::recv(client, request.data(), request.size() - 1, 0);
#endif
    if (count > 0) {
      const auto path = request_path({request.data(), static_cast<std::size_t>(count)});
      std::string body;
      std::string_view type = "application/json";
      std::string_view status = "200 OK";
      if (path == "/" || path == "/index.html") {
        body = dashboard();
        type = "text/html; charset=utf-8";
      } else if (path == "/api/summary" || path == "/health" || path == "/health/live" || path == "/health/ready") {
        body = snapshot();
        if (!body.empty() && body.back() != '\n') body.push_back('\n');
      } else {
        body = "{\"error\":\"not_found\"}\n";
        status = "404 Not Found";
      }
      const auto response = std::string{"HTTP/1.1 "} + std::string(status) + "\r\nContent-Type: " +
                            std::string(type) + "\r\nCache-Control: no-store\r\nConnection: close\r\n" +
                            "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
#ifdef _WIN32
      ::send(client, response.data(), static_cast<int>(response.size()), 0);
#else
      ::send(client, response.data(), response.size(), MSG_NOSIGNAL);
#endif
    }
    close_sock(client);
  }
}

std::string StatusHttp::dashboard() const {
  return R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>hlarb — HL ↔ MT5</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--good:#1f7a4d;--bad:#b3261e;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9a9993;--line:#2c2c2a;--good:#5fbf8a;--bad:#ef7a70;--card:#1c1c1a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}.muted{color:var(--muted);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.tile b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-variant-numeric:tabular-nums;font-size:12.5px}
th,td{text-align:right;padding:4px 7px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}.wrap{overflow-x:auto;margin-top:8px}
.good{color:var(--good)}.bad{color:var(--bad)}
h2{font-size:14px;margin:18px 0 6px;font-weight:600}
</style></head><body><main>
<h1>Hyperliquid ↔ MT5</h1>
<div class="muted" id="sub">loading…</div>
<div class="tiles" id="tiles"></div>
<h2>Flow</h2>
<div class="tiles" id="flow"></div>
<h2>Pairs</h2>
<div class="wrap"><table id="pairs"></table></div>
</main><script>
const tile=(k,v,c='')=>`<div class="tile"><span class="muted">${k}</span><b class="${c}">${v}</b></div>`;
const n=x=>(x??0);
const f=(x,d=2)=>Number(x??0).toFixed(d);
const stateName=s=>['idle','placing','resting','modifying','canceling'][s]||String(s);
async function refresh(){
 try{
  const s=await (await fetch('/api/summary')).json();
  const live=!!s.live;
  const soft=s.soft_halt||'none';
  document.getElementById('sub').textContent=
    `${live?'LIVE':'not live'} · halt ${s.halt||'none'} · soft ${soft} · killed ${!!s.killed} · ${new Date().toLocaleTimeString()}`;
  document.getElementById('tiles').innerHTML=[
    tile('Mode', live?'live':'offline', live?'good':'bad'),
    tile('Soft halt', soft, soft==='none'?'good':'bad'),
    tile('Gateway', s.gateway_up?'up':'down', s.gateway_up?'good':'bad'),
    tile('HL $', f(s.hl_account_value)),
    tile('HL withdraw', f(s.hl_min_withdrawable)),
    tile('MT5 equity', f(s.mt5_equity)),
    tile('MT5 free', f(s.mt5_margin_free)),
    tile('Day start $', f(s.day_start_equity)),
  ].join('');
  document.getElementById('flow').innerHTML=[
    tile('Requests', n(s.requests_sent)),
    tile('Budget left', n(s.requests_remaining)),
    tile('Places OK', n(s.places_ok), 'good'),
    tile('Places reject', n(s.places_reject), n(s.places_reject)?'bad':''),
    tile('Modifies', n(s.modifies)),
    tile('Cancels', n(s.cancels)),
    tile('Fills', n(s.fills), n(s.fills)?'good':''),
    tile('Fill notional $', f(s.fill_notional_usd)),
    tile('Fill fees $', f(s.fill_fees_usd,4)),
    tile('Soft-halt events', n(s.soft_halt_events)),
    tile('Pulls', n(s.pulls)),
  ].join('');
  const rows=s.symbols||[];
  document.getElementById('pairs').innerHTML=
    '<tr><th>Coin</th><th>Block</th><th>Fair</th><th>Our bid/ask</th><th>State</th><th>Touch ppm</th><th>Half ppm</th><th>Age ms</th><th>HL book</th><th>MT5</th><th>Pos HL/MT5</th><th>Fills</th><th>Rejects</th></tr>'+
    rows.map(x=>{
      const resting=x.bid_state===2||x.ask_state===2;
      const touch=`${f(x.bid_touch_ppm,0)}/${f(x.ask_touch_ppm,0)}`;
      const age=`${n(x.bid_age_ms)}/${n(x.ask_age_ms)}`;
      return `<tr>
        <td>${x.coin}</td>
        <td class="${x.block&&x.block!=='none'?'bad':''}">${x.block}</td>
        <td>${f(x.fair,4)}</td>
        <td class="${resting?'good':''}">${f(x.bid,4)} / ${f(x.ask,4)}</td>
        <td>${stateName(x.bid_state)}/${stateName(x.ask_state)}</td>
        <td>${touch}</td>
        <td>${f(x.half_spread_ppm,0)}</td>
        <td>${age}</td>
        <td>${f(x.hl_bid,4)}/${f(x.hl_ask,4)}</td>
        <td>${f(x.mt5_bid,4)}/${f(x.mt5_ask,4)}</td>
        <td>${f(x.hl_pos,4)} / ${f(x.mt5_lots,4)}</td>
        <td class="${n(x.fills)?'good':''}">${n(x.fills)}</td>
        <td class="${n(x.rejects)?'bad':''}">${n(x.rejects)}</td>
      </tr>`;
    }).join('');
 }catch(e){document.getElementById('sub').textContent='status unavailable: '+e}
}
refresh(); setInterval(refresh,1000);
</script></body></html>
)HTML";
}

}  // namespace hla
