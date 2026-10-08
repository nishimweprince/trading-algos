import { Agent, setGlobalDispatcher } from 'undici';

/**
 * One process-wide keep-alive HTTP pool for every `fetch` (RPC, Helius
 * Sender, web3.js Connections).
 *
 * Node's default dispatcher drops an idle socket after 4 s. Graduations
 * arrive minutes apart, so the buy's send and confirm reads used to start
 * with a fresh TCP + TLS handshake (one to two extra round trips) on the
 * hot path. Sockets now stay open for a minute idle; the executor's
 * keep-warm loop (blockhash, fee plan, Sender ping) touches them well
 * inside that.
 */
let installed = false;

export function installKeepAliveDispatcher(opts: { keepAliveMs?: number; connections?: number } = {}): void {
  if (installed) return;
  installed = true;
  setGlobalDispatcher(
    new Agent({
      keepAliveTimeout: opts.keepAliveMs ?? 60_000,
      keepAliveMaxTimeout: 10 * 60_000,
      connections: opts.connections ?? 64,
      connect: { timeout: 5_000 },
    }),
  );
}
