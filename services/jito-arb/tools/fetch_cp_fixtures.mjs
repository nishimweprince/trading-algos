// Independent Anchor decoding of real RPC bytes captured by the existing parity
// harness. No C++ decoder, offsets, expected values, RPC writes or signing.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
const require = createRequire(import.meta.url);
const { BorshAccountsCoder } = require('@coral-xyz/anchor');
export function fetchCpFixtures(root) {
  const specs = [
    ['cpmm', 'raydium_cp_swap', ['PoolState', 'AmmConfig']],
    ['pumpswap', 'pump_amm', ['Pool', 'GlobalConfig']],
    ['pumpswap', 'pump_fees', ['FeeConfig']],
  ];
  const records = [];
  for (const [dex, file, names] of specs) {
    const idl = JSON.parse(readFileSync(resolve(root, `harness/idl/${file}.json`)));
    const coder = new BorshAccountsCoder(idl);
    const capture = JSON.parse(readFileSync(resolve(root, `tests/fixtures/parity_${dex}.json`)));
    const seen = new Set();
    for (const batch of capture.batches) for (const [key, a] of Object.entries(batch.accounts)) {
      if (!a || seen.has(key) || a.owner !== idl.address) continue;
      const bytes = Buffer.from(a.data, 'base64');
      const account = idl.accounts.find(x => names.includes(x.name) && bytes.subarray(0, 8).equals(Buffer.from(x.discriminator)));
      if (!account) continue;
      const decoded = coder.decode(account.name, bytes);
      function normalize(v) {
        if (v?.toBase58) return v.toBase58();
        if (v?.constructor?.name === 'BN') return v.toString(10);
        if (Array.isArray(v)) return v.map(normalize);
        if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k,x]) => [k, normalize(x)]));
        return v;
      }
      records.push({ dex, account: account.name, key, slot: batch.slot_before, owner: a.owner, data: a.data, expected: normalize(decoded) });
      seen.add(key);
    }
    for (const name of names) if (!records.some(x => x.dex === dex && x.account === name)) throw new Error(`missing real ${name}`);
  }
  writeFileSync(resolve(root, 'tests/fixtures/cp_decode.json'), JSON.stringify({ oracle: 'Anchor BorshAccountsCoder; pinned upstream IDLs; parity RPC snapshots', records }, null, 1) + '\n');
  console.error(`independent CP decodes: ${records.length}`);
}
