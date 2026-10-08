/**
 * Record the racer stream to compact JSONL for replay tests / offline checks.
 *
 *   node --experimental-strip-types src/racer/record-cli.ts <out.jsonl> [minutes=20] [bandValues=12]
 *
 * Needs HELIUS_GRPC_URL + HELIUS_GRPC_TOKEN in the environment. Frames are
 * normalized to the fields the racer reads (see replayFrame in replay.ts);
 * creates keep only the new mint.
 */
import { createWriteStream } from 'node:fs';
import { base58Encode } from '../core/base58.ts';
import { buildRacerRequest } from './stream.ts';
import { compactFrame } from './replay.ts';

const [out, minutesArg, bandArg] = process.argv.slice(2);
if (!out) {
  console.error('usage: record-cli.ts <out.jsonl> [minutes] [bandValues]');
  process.exit(2);
}
const minutes = Number(minutesArg ?? 20);
const endpoint = process.env.HELIUS_GRPC_URL ?? '';
const token = process.env.HELIUS_GRPC_TOKEN ?? '';
const file = createWriteStream(out);
const mod = (await import('helius-laserstream')) as unknown as {
  subscribe: (c: unknown, r: unknown, onData: (d: unknown) => void, onErr: (e: unknown) => void) => Promise<{ cancel(): void }>;
};
let n = 0;
const handle = await mod.subscribe(
  { apiKey: token, endpoint, replay: false },
  buildRacerRequest(Number(bandArg ?? 12)),
  (d) => {
    const f = compactFrame(d, base58Encode);
    if (!f) return;
    n++;
    file.write(`${JSON.stringify(f)}\n`);
  },
  (e) => console.error('stream error', (e as Error)?.message ?? e),
);
setTimeout(() => {
  handle.cancel();
  file.end(() => {
    console.log(JSON.stringify({ frames: n, out }));
    process.exit(0);
  });
}, minutes * 60_000);
