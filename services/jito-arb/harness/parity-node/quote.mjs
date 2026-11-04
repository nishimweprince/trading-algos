// SDK parity harness (VM only; nothing here runs on the laptop). For every recorded snapshot in a
// parity file (tests/fixtures/parity_<dex>.json format: account bytes per batch + sizes), compute the
// official SDK's exact-in quote and write {pool, a_to_b, amount_in, sdk_out}; compare.mjs then checks them
// against the chain's simulated outputs (which the C++ quoter matches exactly): 100% exact required.
//
//   node quote.mjs ../../tests/fixtures/parity_whirlpool.json > sdk_whirlpool.json
//   node quote.mjs ../../tests/fixtures/parity_raydium_v4.json > sdk_raydium_v4.json
//
// The spec's 1,000 snapshots x 20 sizes comes from a sim-parity run on the VM with
// --min-samples 20000 (RUNBOOK-VM.md).
import fs from 'fs';
import BN from 'bn.js';
import { PublicKey } from '@solana/web3.js';

const file = process.argv[2];
const doc = JSON.parse(fs.readFileSync(file, 'utf8'));
const out = [];

async function whirlpoolQuotes(batch) {
  const { ParsableWhirlpool, ParsableTickArray, swapQuoteWithParams, SwapUtils, PDAUtil, ORCA_WHIRLPOOL_PROGRAM_ID, TickUtil } =
    await import('@orca-so/whirlpools-sdk');
  const acc = (k) => batch.accounts[k] && Buffer.from(batch.accounts[k].data, 'base64');
  const pool = new PublicKey(batch.pool);
  const wp = ParsableWhirlpool.parse(pool, { data: acc(batch.pool), owner: ORCA_WHIRLPOOL_PROGRAM_ID, lamports: 0, executable: false });
  for (const s of batch.samples) {
    const aToB = s.a_to_b;
    const starts = SwapUtils.getTickArrayPublicKeys(wp.tickCurrentIndex, wp.tickSpacing, aToB, ORCA_WHIRLPOOL_PROGRAM_ID, pool);
    const tickArrays = starts.map((pk) => {
      const raw = acc(pk.toBase58());
      return { address: pk, data: raw ? ParsableTickArray.parse(pk, { data: raw, owner: ORCA_WHIRLPOOL_PROGRAM_ID, lamports: 0, executable: false }) : null };
    });
    try {
      const q = swapQuoteWithParams({
        whirlpoolData: wp, tokenAmount: new BN(String(s.amount_in)), otherAmountThreshold: new BN(0),
        sqrtPriceLimit: SwapUtils.getDefaultSqrtPriceLimit(aToB), aToB, amountSpecifiedIsInput: true, tickArrays,
        tokenExtensionCtx: { currentEpoch: 0, tokenMintWithProgramA: null, tokenMintWithProgramB: null },
      }, { numerator: new BN(0), denominator: new BN(1) });
      out.push({ pool: batch.pool, a_to_b: aToB, amount_in: s.amount_in, sdk_out: q.estimatedAmountOut.toString() });
    } catch (e) {
      out.push({ pool: batch.pool, a_to_b: aToB, amount_in: s.amount_in, sdk_error: String(e.message || e) });
    }
  }
}

async function raydiumQuotes(batch) {
  const { liquidityStateV4Layout, Liquidity } = await import('@raydium-io/raydium-sdk-v2');
  const acc = (k) => batch.accounts[k] && Buffer.from(batch.accounts[k].data, 'base64');
  const st = liquidityStateV4Layout.decode(acc(batch.pool));
  const amount = (k) => new BN(acc(k.toBase58()).subarray(64, 72), 'le');
  const baseReserve = amount(st.baseVault).sub(st.baseNeedTakePnl);
  const quoteReserve = amount(st.quoteVault).sub(st.quoteNeedTakePnl);
  for (const s of batch.samples) {
    // a_to_b = coin (base) -> pc (quote), as the C++ quoter.
    const [rin, rout] = s.a_to_b ? [baseReserve, quoteReserve] : [quoteReserve, baseReserve];
    const amountIn = new BN(String(s.amount_in));
    const fee = amountIn.mul(st.swapFeeNumerator).add(st.swapFeeDenominator).subn(1).div(st.swapFeeDenominator);  // ceil
    const inAfter = amountIn.sub(fee);
    // The SDK's own constant-product helper where available; the explicit form otherwise (same as the program).
    const outBn = Liquidity && Liquidity.getAmountOut ? Liquidity.getAmountOut(inAfter, rin, rout) : rout.mul(inAfter).div(rin.add(inAfter));
    out.push({ pool: batch.pool, a_to_b: s.a_to_b, amount_in: s.amount_in, sdk_out: outBn.toString() });
  }
}

for (const b of doc.batches) {
  if (doc.dex === 'whirlpool') await whirlpoolQuotes(b);
  else if (doc.dex === 'raydium_v4') await raydiumQuotes(b);
}
process.stdout.write(JSON.stringify({ dex: doc.dex, source: file, quotes: out }, null, 1) + '\n');
