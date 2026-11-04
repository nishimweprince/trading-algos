// Compare the SDK's quotes (quote.mjs output) with the chain's (sim_out in the parity file, which the C++
// quoter already matches exactly: arb_tests parity_*). Exit 1 on any difference.
//   node compare.mjs ../../tests/fixtures/parity_whirlpool.json sdk_whirlpool.json
import fs from 'fs';
const parity = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sdk = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const want = new Map();
for (const b of parity.batches)
  for (const s of b.samples) if (s.sim_ok) want.set(`${b.pool}|${s.a_to_b}|${s.amount_in}`, String(s.sim_out));
let n = 0, bad = 0, errors = 0;
for (const q of sdk.quotes) {
  const k = `${q.pool}|${q.a_to_b}|${q.amount_in}`;
  if (!want.has(k)) continue;
  ++n;
  if (q.sdk_error) { ++errors; continue; }
  if (q.sdk_out !== want.get(k)) {
    ++bad;
    console.error(`mismatch ${k}: chain ${want.get(k)} sdk ${q.sdk_out}`);
  }
}
console.log(`${parity.dex}: ${n} compared, ${bad} mismatches, ${errors} SDK errors`);
process.exit(bad || errors ? 1 : 0);
