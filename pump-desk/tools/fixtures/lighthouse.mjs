// Golden fixtures for pump-desk's hand-rolled Lighthouse assertions.
//
// Builds AssertTokenAccount(Amount) and AssertAccountInfo(Lamports) with the
// official lighthouse-sdk-legacy and writes the instruction bytes and account
// metas the C++ builders must reproduce (tests/test_routing.cpp). No RPC.
//
//   npm install --prefix <dir> --ignore-scripts lighthouse-sdk-legacy@2.0.1 rpc-websockets@7.11.0
//   node pump-desk/tools/fixtures/lighthouse.mjs <dir>
//
// rpc-websockets is pinned because the SDK's web3.js 1.91.7 needs its 7.x layout.
import { createRequire } from 'node:module';
import { writeFileSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const sdkDir = process.argv[2];
if (!sdkDir) throw new Error('usage: lighthouse.mjs <dir with node_modules/lighthouse-sdk-legacy>');
const require = createRequire(resolve(sdkDir, 'package.json'));
const lh = require('lighthouse-sdk-legacy');
const sdkVersion = JSON.parse(readFileSync(resolve(sdkDir, 'node_modules/lighthouse-sdk-legacy/package.json'), 'utf8')).version;

// The generated builders only ask the context for the program id.
const ctx = { programs: { getPublicKey: (_name, fallback) => fallback } };

// Fixed targets: the golden seed pool's quote vault and two arbitrary ATAs.
const QUOTE_VAULT = 'BWFkM4yH4mEjrjQ4Q8xJ4xnx2aTzTmZ3cY4TXo5FXR5z';
const RIVAL_ATA = '7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU';

const cases = [
  { name: 'token-amount-le', kind: 'token_amount', target: QUOTE_VAULT, op: 5, value: 67747050000n },
  { name: 'token-amount-lt-wide', kind: 'token_amount', target: QUOTE_VAULT, op: 3, value: 1234567890123456789n },
  { name: 'lamports-eq-zero', kind: 'lamports', target: RIVAL_ATA, op: 0, value: 0n },
  { name: 'lamports-ge', kind: 'lamports', target: RIVAL_ATA, op: 4, value: 2039280n },
];

const hex = (b) => Buffer.from(b).toString('hex');
const out = [];
for (const c of cases) {
  const builder =
    c.kind === 'token_amount'
      ? lh.assertTokenAccount(ctx, {
          targetAccount: c.target,
          assertion: lh.tokenAccountAssertion('Amount', { value: c.value, operator: c.op }),
        })
      : lh.assertAccountInfo(ctx, {
          targetAccount: c.target,
          assertion: lh.accountInfoAssertion('Lamports', { value: c.value, operator: c.op }),
        });
  const [ix] = builder.getInstructions();
  out.push({
    name: c.name,
    kind: c.kind,
    target: c.target,
    op: c.op,
    value: c.value.toString(),
    program: ix.programId.toString(),
    keys: ix.keys.map((k) => ({ pubkey: k.pubkey.toString(), signer: k.isSigner, writable: k.isWritable })),
    data: hex(ix.data),
  });
}

const path = resolve(here, '../../tests/fixtures/lighthouse.json');
writeFileSync(path, JSON.stringify({ generated: new Date().toISOString(), sdkVersion, cases: out }, null, 1));
console.log(`wrote ${path}: ${out.length} cases, program ${out[0].program}`);
