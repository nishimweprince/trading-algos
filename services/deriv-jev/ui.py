"""Progress UI for deriv-jev (stdlib http.server, HTTP basic auth, binds 127.0.0.1)."""
import base64, json, os, sqlite3, threading, time, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>deriv-jev</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b6b6b;--faint:#9a9a9a;--line:#e8e8e6;--soft:#f6f6f4;--pos:#1a7f4b;--neg:#c2410c;--warn:#8a6100;
 --c1:#2f6fdf;--c2:#1a8f6a;--c3:#b7791f;--c4:#8a4fd6;--c5:#d1495b}
[data-theme=dark]{--bg:#111213;--fg:#e8e8e6;--mut:#9b9b98;--faint:#6f6f6c;--line:#262728;--soft:#18191a;--pos:#4cc38a;--neg:#f0845c;--warn:#d9a441;
 --c1:#6b9ef5;--c2:#4cc3a0;--c3:#d9a441;--c4:#b48af0;--c5:#ec7a89}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111213;--fg:#e8e8e6;--mut:#9b9b98;--faint:#6f6f6c;--line:#262728;--soft:#18191a;--pos:#4cc38a;--neg:#f0845c;--warn:#d9a441;
 --c1:#6b9ef5;--c2:#4cc3a0;--c3:#d9a441;--c4:#b48af0;--c5:#ec7a89}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:28px 24px 48px}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:18px;font-weight:600;margin:0}
.status{color:var(--mut);font-size:13px;flex:1}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px;background:var(--faint)}
.dot.ok{background:var(--pos)}.dot.bad{background:var(--neg)}
button.theme{background:none;border:1px solid var(--line);color:var(--mut);border-radius:6px;padding:3px 10px;font:inherit;font-size:12px;cursor:pointer}
.notice{margin:14px 0 0;font-size:13px;color:var(--warn)}
.summary{margin:24px 0 4px;max-width:760px}
table.cmp td{font-size:15px;padding:8px 10px}table.cmp td:first-child{font-size:13px;color:var(--mut)}table.cmp tr.diff td{border-bottom:none;font-weight:600}table.cmp tr.diff td:first-child{font-weight:400}
.stat .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.stat .l{font-size:12px;color:var(--mut)}
h2{font-size:13px;font-weight:600;margin:32px 0 10px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th{font-weight:500;color:var(--mut);font-size:12px;text-align:right;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
th:first-child,td:first-child{text-align:left;padding-left:0}
th:last-child,td:last-child{padding-right:0}
.sub{color:var(--mut);font-size:12px}.mut{color:var(--faint)}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.name{font-weight:500}.sw{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:7px}
.scroll{overflow-x:auto}
.lt td:nth-child(2),.lt th:nth-child(2){text-align:left}
.lt td,.lt th{padding-left:7px;padding-right:7px}.cols{grid-template-columns:5fr 6fr!important}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:40px}@media(max-width:960px){.cols{grid-template-columns:1fr}}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-top:6px}
.empty{color:var(--mut);font-size:13px;padding:18px 0}
svg text{fill:var(--faint);font-size:11px;font-family:inherit}

nav{display:flex;gap:14px;font-size:13px}nav a{color:var(--mut);text-decoration:none}nav a:hover{color:var(--fg)}
section.bt{margin-top:56px;padding-top:28px;border-top:1px solid var(--line)}
section.bt h2.big{font-size:16px;margin:0 0 4px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;margin:14px 0 6px}
.seg button{background:none;border:none;color:var(--mut);font:inherit;font-size:12px;padding:5px 12px;cursor:pointer}
.seg button.on{background:var(--soft);color:var(--fg);font-weight:600}
.caveat{color:var(--warn);font-size:12.5px;margin:10px 0 0;max-width:900px}
footer{margin-top:32px;color:var(--faint);font-size:12px;line-height:1.6}
</style></head><body><div class="wrap">
<header><h1>deriv-jev</h1><nav><a href="#live">Jev strategies</a><a href="#wr70">70%+ win rate</a><a href="#backtest">Backtest</a></nav><div class="status" id="statusline">Loading…</div><button class="theme" id="theme"></button></header>
<div class="notice" id="notice"></div><div id="live"></div>
<div class="summary"><table id="summary" class="cmp"></table></div>
<h2>Strategies</h2><div class="scroll"><table id="strat"></table></div>
<h2>Cumulative R, all strategies</h2><div id="chart"></div><div class="legend" id="legend"></div>
<div class="cols">
 <div><h2>Jev decisions</h2><div class="scroll"><table id="sig" class="lt"></table></div></div>
 <div><h2>Trades, every signal</h2><div class="scroll"><table id="trd" class="lt"></table></div></div>
</div>

<section class="bt" id="wr70">
 <h2 class="big">70%+ win-rate strategies</h2>
 <div class="sub">Paper trading at zero spread, every signal taken (no Jev). Rule, stop and target per strategy are the latest walk-forward pick from the 70%+ win-rate search; backtest columns are that search's out-of-sample year. Fills are simulated at the next M1 open, as in the backtest.</div>
 <div class="summary"><table id="wsum" class="cmp"></table></div>
 <h2>Strategies</h2><div class="scroll"><table id="wstrat"></table></div>
 <h2>Cumulative R, all 70%+ strategies</h2><div id="wchart"></div><div class="legend" id="wlegend"></div>
 <h2>Recent trades</h2><div class="scroll" style="max-width:900px"><table id="wtrd" class="lt"></table></div>
</section>

<section class="bt" id="backtest">
 <h2 class="big">Backtest</h2>
 <div class="sub" id="btperiod"></div>
 <div class="seg" id="riskseg"></div>
 <div class="summary"><table id="btsum" class="cmp"></table></div>
 <h2>Account equity, $10,000 start</h2><div id="btchart"></div><div class="legend" id="btlegend"></div>
 <h2>By strategy (Jev history v1)</h2><div class="scroll"><table id="btstrat"></table></div>
 <h2>By month (all strategies, Jev history v1)</h2><div class="scroll" style="max-width:640px"><table id="btmonth"></table></div>
 <div class="caveat" id="btcaveat"></div>
</section>
<footer>R = profit in multiples of the initial stop distance (2 ATR), after spread. "Ungated" is every entry the strategy made, whether Jev took it or skipped it; skipped trades are followed on paper only, so Jev can be judged against them. Jev earns its place only if its R per trade beats ungated. Jev sees the trade setup plus the strategy's last 10 closed trades and last 12 bar moves (history v1, since 23 Nov 14:00 Kigali; earlier signals used the shorter state). Backtest figures are out-of-sample, Nov 2025 to Nov 2026, same thresholds. Positions marked "pre-start" were opened by the history replay before the service started; they keep each strategy in step with the backtest and are never counted.</footer>
</div>
<script>
const E=id=>document.getElementById(id);
const COL=['--c1','--c2','--c3','--c4','--c5'].map(v=>`var(${v})`);
const f=(x,d=2)=>x==null||isNaN(x)?'–':(+x).toFixed(d);
const sg=(x,d=2)=>x==null||isNaN(x)?'–':(x>0?'+':'')+(+x).toFixed(d);
const cls=x=>x==null?'':x>0?'pos':x<0?'neg':'';
const pct=x=>x==null?'–':Math.round(x*100)+'%';
const hm=m=>{if(m==null)return'–';const d=new Date(m*60000);return d.toISOString().slice(5,16).replace('T',' ')};
const ago=s=>s==null?'–':s<90?Math.round(s)+'s ago':s<5400?Math.round(s/60)+'m ago':Math.round(s/3600)+'h ago';
const dur=s=>s<3600?Math.round(s/60)+'m':s<172800?Math.round(s/3600)+'h':Math.round(s/86400)+'d';
const short=t=>t.replace(/^JEV-/,'');
// theme
const T=['auto','light','dark'];let th='auto';try{th=localStorage.getItem('theme')||'auto'}catch(e){}
function applyTheme(){if(th==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',th);E('theme').textContent='Theme: '+th}
E('theme').onclick=()=>{th=T[(T.indexOf(th)+1)%3];try{localStorage.setItem('theme',th)}catch(e){}applyTheme();if(window.S)render(window.S)};applyTheme();
async function load(){try{const r=await fetch('api/state',{cache:'no-store'});if(r.ok){window.S=await r.json();render(window.S)}}catch(e){E('statusline').innerHTML='<span class="dot bad"></span>UI cannot reach the service'}}
function render(S){
 const st=S.status,now=Date.now()/1000,syms=Object.entries(st.symbols);
 const live=st.connected&&st.last_poll&&now-st.last_poll<150;
 E('statusline').innerHTML=`<span class="dot ${live?'ok':'bad'}"></span>${live?'Live':'Not receiving data'} · last bar ${ago(st.last_poll?now-st.last_poll:null)} · ${syms.map(([k,v])=>k+' '+f(v.last)).join(' · ')} · ${st.jev_model}${st.jev_state==='v1-history'?' (history v1)':''}${st.jev_asks>1?', median of '+st.jev_asks+' asks':''} · up ${dur(now-st.started)}${st.kill?' · <b>kill switch on</b>':''}${st.errors?' · '+st.errors+' errors':''}`;
 const closed=syms.filter(([k,v])=>v.trading_mode&&v.trading_mode!=='ENABLED').map(([k])=>k);
 E('notice').textContent=`Shadow mode on a Deriv cTrader demo account: no orders are sent, fills are simulated at the next M1 open plus spread.`+(closed.length?` ${closed.join(' and ')} ${closed.length>1?'are':'is'} close-only on Deriv cTrader (retired 19 Nov 2026).`:'');
 const t=S.totals,$$=x=>`${x<0?'−':''}$${Math.abs(Math.round(x*S.risk_usd)).toLocaleString()}`;
 const row=(l,n,w,m,s,c='')=>`<tr class="${c}"><td>${l}</td><td>${n}</td><td>${w}</td><td class="${cls(m)}">${m==null?'–':sg(m)}</td><td class="${cls(s)}">${n?sg(s,1):'–'}</td><td class="${cls(s)}">${n?$$(s):'–'}</td></tr>`;
 const dm=(t.n&&t.u_n)?t.meanR-t.u_meanR:null, ds=t.u_n?t.sumR-t.u_sumR:null;
 E('summary').innerHTML=`<tr><th></th><th>Closed trades</th><th>Win rate</th><th>R / trade</th><th>Total R</th><th>P&L at $${S.risk_usd}/R</th></tr>`+
  row('Jev-approved',t.n,t.n?pct(t.win):'–',t.n?t.meanR:null,t.sumR)+
  row('Ungated (every signal)',t.u_n,t.u_n?pct(t.u_win):'–',t.u_n?t.u_meanR:null,t.u_sumR)+
  `<tr class="diff"><td>Jev minus ungated</td><td class="sub">${t.taken} of ${t.signals} signals taken</td><td></td><td class="${cls(dm)}">${dm==null?'–':sg(dm)}</td><td class="${cls(ds)}">${ds==null?'–':sg(ds,1)}</td><td class="${cls(ds)}">${ds==null?'–':$$(ds)}</td></tr>`;
 strat.innerHTML=`<tr><th>Strategy</th><th>Position</th><th>Open R</th><th>Signals</th><th>Taken</th><th>Jev R / trade</th><th>Ungated R / trade</th><th>Jev − ungated<div class="sub">total R</div></th><th>Backtest R / trade<div class="sub">Jev / ungated</div></th><th>Jev trades / wk<div class="sub">live / backtest</div></th></tr>`+
 S.strategies.map((s,i)=>{const p=s.pos;let pos='<span class="mut">Flat</span>',or='';
  if(p){const side=p.side>0?'Long':'Short';const live=p.taken&&!p.warmup;
   const tag=p.warmup?'pre-start, not traded':p.taken?`Jev ${f(p.jev_p)} · taken`:`Jev ${f(p.jev_p)} · skipped, paper only`;
   pos=`<div${live?'':' class="mut"'}>${side} ${f(p.entry_px)}</div><div class="sub">${tag}</div>`;
   or=live?`<span class="${cls(p.open_r)}">${sg(p.open_r)}</span>`:`<span class="mut">${sg(p.open_r)}</span>`}
  else if(s.pending)pos='<span class="mut">Entering</span>';
  return `<tr><td><div class="name">${short(s.tag)}</div><div class="sub">${s.rule} · Jev threshold ${s.threshold}</div></td>
  <td>${pos}</td><td>${or}</td><td>${s.signals}</td><td>${s.signals?s.taken:'–'}</td>
  <td class="${cls(s.meanR)}">${s.n?sg(s.meanR)+` <span class="sub">(${s.n})</span>`:'–'}</td>
  <td class="${cls(s.u_meanR)}">${s.u_n?sg(s.u_meanR)+` <span class="sub">(${s.u_n})</span>`:'–'}</td>
  <td class="${cls(s.sumR-s.u_sumR)}">${s.u_n?sg(s.sumR-s.u_sumR,1):'–'}</td>
  <td>${sg(s.bt_meanR)} <span class="sub">/ ${sg(s.bt_u_meanR)}</span></td><td>${s.n?f(s.tpw,1):'–'} <span class="sub">/ ${f(s.bt_tpw,1)}</span></td></tr>`}).join('');
 chartDraw(S);
 renderWR(S.wr70,S);
 E('sig').innerHTML=S.signals.length?`<tr><th>Time UTC</th><th>Strategy</th><th>Side</th><th>Jev p<div class="sub">median of answers</div></th><th>Threshold</th><th>Decision</th></tr>`+S.signals.map(x=>`<tr><td>${hm(x.signal_t)}</td><td>${short(x.tag)}</td><td>${x.side>0?'Long':'Short'}</td><td>${f(x.jev_p)}${(()=>{try{const a=JSON.parse(x.jev_ps||'[]');return a.length>1?` <span class="sub">(${a.map(v=>f(v)).join(', ')})</span>`:''}catch(e){return ''}})()}</td><td class="mut">${f(x.threshold)}</td><td class="${x.decision==='take'?'pos':x.decision==='skip'?'mut':'neg'}">${x.decision==='take'?'Take':x.decision==='skip'?'Skip':x.decision}${x.error?' · '+x.error:''}</td></tr>`).join(''):'<div class="empty">No signals yet.</div>';
 E('trd').innerHTML=S.trades.length?`<tr><th>Entry UTC</th><th>Strategy</th><th>Side</th><th>Jev</th><th>Entry</th><th>Exit</th><th>R</th></tr>`+S.trades.map(x=>{const sk=!x.taken;return `<tr${sk?' class="mut"':''}><td>${hm(x.entry_t)}</td><td>${short(x.tag)}</td><td>${x.side>0?'Long':'Short'}</td><td>${x.taken?'Taken':'Skipped'} <span class="sub">${f(x.jev_p)}</span></td><td>${f(x.entry_px)}</td><td>${x.exit_px!=null?f(x.exit_px)+` <span class="sub">${x.reason||''}</span>`:'open'}</td><td class="${sk?'':cls(x.r)}">${x.r!=null?sg(x.r):''}</td></tr>`}).join(''):'<div class="empty">No trades yet.</div>';
}

function renderWR(W,S){
 if(!W)return;const t=W.totals,per=S.risk_usd,$$=x=>`${x<0?'−':''}$${Math.abs(Math.round(x*per)).toLocaleString()}`;
 E('wsum').innerHTML=`<tr><th></th><th>Closed trades</th><th>Win rate</th><th>R / trade</th><th>Total R</th><th>P&L at $${per}/R</th></tr><tr><td>All 70%+ strategies</td><td>${t.n.toLocaleString()}</td><td class="${t.n&&t.win>=0.7?'pos':''}">${t.n?pct(t.win):'–'}</td><td class="${cls(t.meanR)}">${t.n?sg(t.meanR):'–'}</td><td class="${cls(t.sumR)}">${t.n?sg(t.sumR,1):'–'}</td><td class="${cls(t.sumR)}">${t.n?$$(t.sumR):'–'}</td></tr>`;
 E('wstrat').innerHTML=`<tr><th>Strategy</th><th>Position</th><th>Open R</th><th>Trades</th><th>Win rate<div class="sub">live / backtest</div></th><th>R / trade<div class="sub">live / backtest</div></th><th>Total R</th><th>Trades / wk<div class="sub">live / backtest</div></th></tr>`+
 W.strategies.map(s=>{const p=s.pos;let pos='<span class="mut">Flat</span>',or='';
  if(p){const live=p.taken&&!p.warmup;pos=`<div${live?'':' class="mut"'}>${p.side>0?'Long':'Short'} ${f(p.entry_px)}</div><div class="sub">${p.warmup?'pre-start, not traded':'paper'}</div>`;or=`<span class="${live?cls(p.open_r):'mut'}">${sg(p.open_r)}</span>`}
  else if(s.pending)pos='<span class="mut">Entering</span>';
  return `<tr><td><div class="name">${s.code} M${s.tf} · ${s.rule}</div><div class="sub">${s.stop_atr} ATR stop · ${s.target_R}R target</div></td><td>${pos}</td><td>${or}</td><td>${s.n}</td>
  <td>${s.n?`<span class="${s.win>=0.7?'pos':'neg'}">${pct(s.win)}</span>`:'–'} <span class="sub">/ ${pct(s.bt.wr)}</span></td>
  <td class="${cls(s.meanR)}">${s.n?sg(s.meanR):'–'} <span class="sub">/ ${sg(s.bt.meanR)}</span></td><td class="${cls(s.sumR)}">${s.n?sg(s.sumR,1):'–'}</td>
  <td>${s.n?f(s.tpw,0):'–'} <span class="sub">/ ${f(s.bt.tpw,0)}</span></td></tr>`}).join('');
 const L=W.curve_jev;
 if(L.length){lineChart(E('wchart'),[{pts:L.map(p=>[p[0]/1440,p[1]]),color:'var(--c2)',width:2}],v=>v.toFixed(1)+'R');E('wlegend').innerHTML=`<span><span class="sw" style="background:var(--c2)"></span>Cumulative R ${sg(L[L.length-1][1],1)}</span>`}
 else{E('wchart').innerHTML='<div class="empty">The chart starts with the first closed trade.</div>';E('wlegend').innerHTML=''}
 E('wtrd').innerHTML=W.trades.length?`<tr><th>Entry UTC</th><th>Strategy</th><th>Side</th><th>Entry</th><th>Exit</th><th>R</th></tr>`+W.trades.map(x=>`<tr><td>${hm(x.entry_t)}</td><td>${short(x.tag).replace(/^WR70-/,'')}</td><td>${x.side>0?'Long':'Short'}</td><td>${f(x.entry_px)}</td><td>${x.exit_px!=null?f(x.exit_px)+` <span class="sub">${x.reason||''}</span>`:'<span class="mut">open</span>'}</td><td class="${cls(x.r)}">${x.r!=null?sg(x.r):''}</td></tr>`).join(''):'<div class="empty">No trades yet.</div>';
}
function chartDraw(S){
 const L1=S.curve_jev,L2=S.curve_ungated;E('legend').innerHTML='';
 if(!L2.length){E('chart').innerHTML='<div class="empty">The chart starts with the first closed trade.</div>';return}
 const W=Math.max(320,E('chart').clientWidth||900),H=240,L=40,B=22,R=8,Tp=8,all=L1.concat(L2);
 const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]).concat([0]);let x0=Math.min(...xs),x1=Math.max(...xs);if(x1-x0<60)x1=x0+60;
 let y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<2){y1+=1;y0-=1}
 const X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=y=>Tp+(y1-y)/(y1-y0)*(H-B-Tp);
 let g=`<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">`;
 for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end">${v.toFixed(1)}</text>`}
 g+=`<line x1="${L}" x2="${W-R}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--faint)"/><text x="${L}" y="${H-5}">${hm(x0)}</text><text x="${W-R}" y="${H-5}" text-anchor="end">${hm(x1)}</text>`;
 const step=c=>{if(!c.length)return'';let prev=0,pp=[[x0,0]];c.forEach(p=>{pp.push([p[0],prev]);pp.push(p);prev=p[1]});pp.push([x1,prev]);return pp.map(p=>X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join(' ')};
 if(L1.length)g+=`<polyline fill="none" stroke="var(--c1)" stroke-width="2" points="${step(L1)}"/>`;
 g+=`<polyline fill="none" stroke="var(--mut)" stroke-width="1.5" stroke-dasharray="4 4" points="${step(L2)}"/>`;
 E('chart').innerHTML=g+'</svg>';
 const last=c=>c.length?c[c.length-1][1]:0;
 E('legend').innerHTML=`<span><span class="sw" style="background:var(--c1)"></span>Jev-approved ${sg(last(L1),1)}</span><span><span class="sw" style="background:var(--mut)"></span>Ungated (dashed), every signal ${sg(last(L2),1)}</span>`}

function lineChart(el, series, yfmt){ // series: [{pts:[[x,y]],color,dash,width}]
 const all=series.flatMap(s=>s.pts);if(!all.length){el.innerHTML='<div class="empty">No data.</div>';return}
 const W=Math.max(320,el.clientWidth||900),H=260,L=58,B=22,R=8,Tp=8;
 const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]);let x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<1e-9){y1+=1;y0-=1}
 const X=x=>L+(x-x0)/(x1-x0||1)*(W-L-R),Y=y=>Tp+(y1-y)/(y1-y0)*(H-B-Tp);
 let g=`<svg width="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">`;
 for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end">${yfmt(v)}</text>`}
 const d0=new Date(x0*86400000),d1=new Date(x1*86400000),fd=d=>d.toISOString().slice(0,10);
 g+=`<text x="${L}" y="${H-5}">${fd(d0)}</text><text x="${W-R}" y="${H-5}" text-anchor="end">${fd(d1)}</text>`;
 series.forEach(s=>{g+=`<polyline fill="none" stroke="${s.color}" stroke-width="${s.width||1.8}" ${s.dash?`stroke-dasharray="${s.dash}"`:''} points="${s.pts.map(p=>X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join(' ')}"/>`});
 el.innerHTML=g+'</svg>'}
const START=10000;let RISK=0.005,BT=null;
const usd=x=>(x<0?'−':'')+'$'+Math.abs(Math.round(x)).toLocaleString();
const pctS=x=>(x>0?'+':x<0?'−':'')+Math.abs(x*100).toLocaleString(undefined,{maximumFractionDigits:x*100>=100?0:1})+'%';
function renderBT(){
 if(!BT||!BT.portfolio)return;const per=START*RISK,J=BT.portfolio.jev,U=BT.portfolio.ungated;
 const d=m=>new Date(m*60000).toISOString().slice(0,10);
 E('btperiod').textContent=`Out-of-sample, ${d(BT.period[0])} to ${d(BT.period[1])}. Four quarterly test periods; each strategy's rule and Jev threshold were fitted only on the 12 months before each quarter.`;
 E('riskseg').innerHTML=[0.0025,0.005,0.01].map(r=>`<button class="${r===RISK?'on':''}" data-r="${r}">Risk ${r*100}% ($${START*r}) per trade</button>`).join('');
 E('riskseg').querySelectorAll('button').forEach(b=>b.onclick=()=>{RISK=+b.dataset.r;renderBT()});
 const row=(l,p)=>{const fin=START+p.sumR*per;return `<tr><td>${l}</td><td>${usd(fin)}</td><td class="${cls(p.sumR)}">${pctS(p.sumR*per/START)}</td><td class="neg">${usd(p.maxddR*per)} <span class="sub">(${pctS(p.maxddR*per/START)})</span></td><td>${p.n.toLocaleString()}</td><td>${pct(p.win)}</td><td>${f(p.pf)}</td><td>${sg(p.meanR)}</td><td>${f(p.weekly_t,1)}</td></tr>`};
 E('btsum').innerHTML=`<tr><th></th><th>Final balance</th><th>Return</th><th>Max drawdown</th><th>Trades</th><th>Win rate</th><th>Profit factor</th><th>R / trade</th><th>Weekly t</th></tr>`+row('Jev history v1',J)+row('Ungated (every signal)',U);
 const pts=c=>c.map(p=>[p[0],START+p[1]*per]);
 lineChart(E('btchart'),[{pts:pts(U.curve),color:'var(--mut)',dash:'4 4',width:1.5},{pts:pts(J.curve),color:'var(--c1)',width:2}],v=>'$'+Math.round(v/1000).toLocaleString()+'k');
 E('btlegend').innerHTML=`<span><span class="sw" style="background:var(--c1)"></span>Jev history v1 ${usd(START+J.sumR*per)}</span><span><span class="sw" style="background:var(--mut)"></span>Ungated (dashed) ${usd(START+U.sumR*per)}</span>`;
 E('btstrat').innerHTML=`<tr><th>Strategy</th><th>Trades</th><th>Per week</th><th>Win rate</th><th>R / trade</th><th>Total R</th><th>P&L</th><th>Max drawdown</th><th>Profit factor</th><th>Profitable quarters</th><th>Ungated R / trade</th></tr>`+
  BT.strategies.map(s=>{const j=s.jev;return `<tr><td><div class="name">${short(s.tag)}</div><div class="sub">${s.rule} · Jev threshold ${s.threshold}</div></td><td>${j.n.toLocaleString()}</td><td>${f(j.tpw,1)}</td><td>${pct(j.win)}</td><td class="${cls(j.meanR)}">${sg(j.meanR)}</td><td class="${cls(j.sumR)}">${sg(j.sumR,0)}</td><td class="${cls(j.sumR)}">${usd(j.sumR*per)}</td><td class="neg">${usd(j.maxddR*per)}</td><td>${f(j.pf)}</td><td>${j.folds_pos} of 4</td><td class="mut">${sg(s.ungated.meanR)}</td></tr>`}).join('');
 E('btmonth').innerHTML=`<tr><th>Month</th><th>Trades</th><th>R</th><th>P&L</th><th>% of $10,000</th></tr>`+J.monthly.map(m=>`<tr><td>${m[0]}</td><td>${m[2].toLocaleString()}</td><td class="${cls(m[1])}">${sg(m[1],0)}</td><td class="${cls(m[1])}">${usd(m[1]*per)}</td><td class="${cls(m[1])}">${pctS(m[1]*per/START)}</td></tr>`).join('');
 E('btcaveat').textContent=`Fixed risk of ${(RISK*100)}% of the starting $10,000 per trade, no compounding; up to five positions can be open at once. These are backtest figures on 1-minute bars with measured cTrader spreads. They do not include slippage beyond the spread, broker volume limits, or spread differences on Deriv MT5, and the Drift Switch indices can no longer be opened on Deriv cTrader. Treat the live section above, not this one, as the real test.`;
}
fetch('api/backtest',{cache:'no-store'}).then(r=>r.json()).then(d=>{BT=d;renderBT()});
window.addEventListener('resize',()=>renderBT());

load();setInterval(load,15000);window.addEventListener('resize',()=>window.S&&chartDraw(window.S));
</script></body></html>"""


def build_state(STATUS, LOCK, dbpath, CFG):
    con = sqlite3.connect(dbpath, timeout=10)
    with LOCK:
        st = json.loads(json.dumps(STATUS, default=str))
    first = con.execute('select min(ts) from signals').fetchone()[0]
    weeks = max((time.time() - first) / 604800, 1 / 7) if first else None

    def stats(r):
        return dict(n=len(r), win=sum(x > 0 for x in r) / len(r) if r else None, sumR=sum(r), meanR=sum(r) / len(r) if r else None)

    def q(sql, tags, extra=()):
        ph = ','.join('?' * len(tags))
        return con.execute(sql.replace('TAGS', ph), (*tags, *extra)).fetchall() if tags else []

    groups = {}
    for gname in ('jev', 'wr70'):
        strat = [s for s in CFG['strategies'] if s.get('group', 'jev') == gname]; tags = [s['tag'] for s in strat]
        out = []; tot_sig = tot_take = 0
        for s in strat:
            tag = s['tag']
            sig = con.execute("select count(*), sum(decision='take') from signals where tag=?", (tag,)).fetchone()
            jr = [x[0] for x in con.execute('select r from trades where tag=? and taken=1 and r is not null order by exit_t', (tag,))]
            ur = [x[0] for x in con.execute('select r from trades where tag=? and signal_id is not null and r is not null order by exit_t', (tag,))]
            tot_sig += sig[0] or 0; tot_take += sig[1] or 0
            tr = st['tracks'].get(tag, {}); J, U = stats(jr), stats(ur)
            row = dict(tag=tag, code=s['code'], tf=s['tf'], rule=s['rule'], signals=sig[0] or 0, taken=sig[1] or 0,
                       n=J['n'], win=J['win'], meanR=J['meanR'], sumR=J['sumR'], u_n=U['n'], u_win=U['win'], u_meanR=U['meanR'], u_sumR=U['sumR'],
                       tpw=J['n'] / weeks if weeks else None, pos=tr.get('pos'), pending=tr.get('pending'))
            if gname == 'jev':
                row.update(threshold=s['jev_threshold'], bt_meanR=s['backtest']['jev']['meanR'], bt_tpw=s['backtest']['jev']['tpw'], bt_u_meanR=s['backtest']['ungated']['meanR'])
            else:
                row.update(stop_atr=s['stop_atr'], target_R=s['target_R'], bt=s['backtest'])
            out.append(row)
        curve_j, curve_u = [], []; aj = au = 0.0
        for r, t, taken in q('select r, exit_t, taken from trades where tag in (TAGS) and signal_id is not null and r is not null order by exit_t, id', tags):
            au += r; curve_u.append([t, round(au, 3)])
            if taken:
                aj += r; curve_j.append([t, round(aj, 3)])
        J = stats([x[0] for x in q('select r from trades where tag in (TAGS) and taken=1 and r is not null', tags)])
        U = stats([x[0] for x in q('select r from trades where tag in (TAGS) and signal_id is not null and r is not null', tags)])
        trs = [dict(zip(['entry_t', 'tag', 'side', 'entry_px', 'exit_px', 'reason', 'r', 'taken', 'jev_p'], x)) for x in
               q('select entry_t, tag, side, entry_px, exit_px, reason, r, taken, jev_p from trades where tag in (TAGS) and signal_id is not null order by id desc limit 50', tags)]
        groups[gname] = dict(strategies=out, trades=trs, curve_jev=curve_j, curve_ungated=curve_u,
                             totals=dict(n=J['n'], win=J['win'], sumR=J['sumR'], meanR=J['meanR'], u_n=U['n'], u_win=U['win'], u_sumR=U['sumR'], u_meanR=U['meanR'],
                                         signals=tot_sig, taken=tot_take))
    jt = [s['tag'] for s in CFG['strategies'] if s.get('group', 'jev') == 'jev']
    sigs = [dict(zip(['signal_t', 'tag', 'side', 'jev_p', 'threshold', 'decision', 'latency', 'error', 'jev_ps'], x)) for x in
            q('select signal_t, tag, side, jev_p, threshold, decision, latency, error, jev_ps from signals where tag in (TAGS) order by id desc limit 40', jt)]
    con.close()
    st.pop('tracks', None); st.pop('account', None)
    G = groups['jev']
    return dict(status=st, strategies=G['strategies'], signals=sigs, trades=G['trades'], totals=G['totals'], curve_jev=G['curve_jev'],
                curve_ungated=G['curve_ungated'], wr70=groups['wr70'], risk_usd=CFG['risk_usd_per_R'])


def start(ENV, STATUS, LOCK, dbpath, CFG):
    AUTH = ENV.get('UI_AUTH', 'off') == 'on'          # public read-only page unless UI_AUTH=on
    want = 'Basic ' + base64.b64encode(f"{ENV.get('UI_USER', '')}:{ENV.get('UI_PASS', '')}".encode()).decode()
    bt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest.json')
    BT = open(bt_path, 'rb').read() if os.path.exists(bt_path) else b'{}'

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if AUTH and not hmac.compare_digest(self.headers.get('Authorization', ''), want):
                self.send_response(401); self.send_header('WWW-Authenticate', 'Basic realm="deriv-jev"'); self.end_headers(); return
            if self.path.rstrip('/').endswith('api/backtest'):
                body = BT; ctype = 'application/json'
            elif self.path.rstrip('/').endswith('api/state'):
                body = json.dumps(build_state(STATUS, LOCK, dbpath, CFG), default=str).encode(); ctype = 'application/json'
            elif self.path in ('/', '/index.html'):
                body = PAGE.encode(); ctype = 'text/html; charset=utf-8'
            elif self.path == '/health':
                body = b'ok'; ctype = 'text/plain'
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(body)

    host = ENV.get('UI_HOST', '127.0.0.1'); port = int(ENV.get('UI_PORT', '8091'))
    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
