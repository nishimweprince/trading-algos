/**
 * Endpoint latency probe for the Frankfurt VM (feat/fra-lean WS5).
 *
 *   node --experimental-strip-types deploy/fra/probe-latency.ts [rounds=20]
 *
 * For each candidate endpoint: TCP connect, TLS handshake and (where it is a
 * plain HTTPS endpoint) a warm request round trip — min / p50 / p90 over
 * `rounds`. Reads HELIUS_HTTP_URL from the environment only to time a real
 * getSlot on the trade key (the key is never printed). Pick the lowest p50
 * per role and put it in .env / config.yaml; record the table in the runbook.
 */
import { connect as tlsConnect } from 'node:tls';
import { connect as netConnect } from 'node:net';

interface Target {
  role: string;
  host: string;
  port?: number;
  /** Warm HTTP probe: method + path + optional JSON body. */
  http?: { method: 'GET' | 'POST'; url: string; body?: string };
}

const rpcUrl = process.env.HELIUS_HTTP_URL;
const targets: Target[] = [
  { role: 'rpc (trade key)', host: 'mainnet.helius-rpc.com', ...(rpcUrl ? { http: { method: 'POST', url: rpcUrl, body: '{"jsonrpc":"2.0","id":1,"method":"getSlot","params":[{"commitment":"processed"}]}' } } : {}) },
  // Regional Sender endpoints are plain HTTP (port 80, no TLS).
  { role: 'sender fra', host: 'fra-sender.helius-rpc.com', port: 80, http: { method: 'GET', url: 'http://fra-sender.helius-rpc.com/ping' } },
  { role: 'sender ams', host: 'ams-sender.helius-rpc.com', port: 80, http: { method: 'GET', url: 'http://ams-sender.helius-rpc.com/ping' } },
  { role: 'sender global', host: 'sender.helius-rpc.com', http: { method: 'GET', url: 'https://sender.helius-rpc.com/ping' } },
  { role: 'laserstream fra', host: 'laserstream-mainnet-fra.helius-rpc.com' },
  { role: 'laserstream ams', host: 'laserstream-mainnet-ams.helius-rpc.com' },
  { role: 'laserstream ewr', host: 'laserstream-mainnet-ewr.helius-rpc.com' },
  { role: 'jito fra', host: 'frankfurt.mainnet.block-engine.jito.wtf' },
  { role: 'jito ams', host: 'amsterdam.mainnet.block-engine.jito.wtf' },
];

function tcp(host: string, port: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    const s = netConnect({ host, port }, () => {
      const ms = performance.now() - t0;
      s.destroy();
      resolve(ms);
    });
    s.setTimeout(5_000, () => {
      s.destroy();
      reject(new Error('timeout'));
    });
    s.on('error', reject);
  });
}

function tls(host: string, port: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    const s = tlsConnect({ host, port, servername: host, ALPNProtocols: ['h2', 'http/1.1'] }, () => {
      const ms = performance.now() - t0;
      s.destroy();
      resolve(ms);
    });
    s.setTimeout(5_000, () => {
      s.destroy();
      reject(new Error('timeout'));
    });
    s.on('error', reject);
  });
}

async function warmHttp(h: NonNullable<Target['http']>): Promise<number> {
  const init: RequestInit = { method: h.method, headers: { 'content-type': 'application/json' }, ...(h.body ? { body: h.body } : {}) };
  await (await fetch(h.url, init)).text(); // open + warm the socket
  const t0 = performance.now();
  await (await fetch(h.url, init)).text();
  return performance.now() - t0;
}

const stats = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  const q = (p: number) => s[Math.min(s.length - 1, Math.floor(p * s.length))] ?? NaN;
  return { min: s[0] ?? NaN, p50: q(0.5), p90: q(0.9) };
};
const fmt = (n: number) => (Number.isFinite(n) ? n.toFixed(1).padStart(7) : '      –');

async function main(): Promise<void> {
  const rounds = Number(process.argv[2] ?? 20);
  console.log(`role                 host                                        tcp p50   tls p50  http p50  http p90`);
  for (const t of targets) {
    const port = t.port ?? 443;
    const tcpMs: number[] = [];
    const tlsMs: number[] = [];
    const httpMs: number[] = [];
    for (let i = 0; i < rounds; i++) {
      await tcp(t.host, port).then((ms) => tcpMs.push(ms), () => undefined);
      if (port === 443) await tls(t.host, port).then((ms) => tlsMs.push(ms), () => undefined);
      if (t.http) await warmHttp(t.http).then((ms) => httpMs.push(ms), () => undefined);
    }
    const a = stats(tcpMs);
    const b = stats(tlsMs);
    const c = stats(httpMs);
    console.log(`${t.role.padEnd(20)} ${t.host.padEnd(43)} ${fmt(a.p50)}  ${fmt(b.p50)}  ${fmt(c.p50)}  ${fmt(c.p90)}`);
  }
}

void main();
