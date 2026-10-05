/**
 * Compact racer-stream frames (record-cli.ts) and their expansion back into
 * the SubscribeUpdate shapes `routeUpdate` reads, so a recorded window can be
 * replayed through the real routing + tracker code in tests.
 */
import { base58Decode } from '../core/base58.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { extractTransaction } from '../detector/laserstream.ts';
import { hasCreateLog } from '../detector/migrateLog.ts';
import { extractMigrateTx } from './migrateTx.ts';

export type CompactFrame =
  | { t: 'a'; slot: number | null; key: string; data: string; sig: string | null }
  | {
      t: 'x';
      slot: number | null;
      sig: string;
      err: boolean;
      logs: string[];
      key0: string | null;
      pre: Array<{ mint: string; owner?: string; amount: string }>;
      post: Array<{ mint: string; owner?: string; amount: string }>;
    }
  | { t: 'c'; mints: string[] };

export function compactFrame(d: unknown, enc: (b: Uint8Array) => string): CompactFrame | null {
  const acct = (d as { account?: { slot?: unknown; account?: { pubkey?: Uint8Array; data?: Uint8Array; txnSignature?: Uint8Array } } } | null)?.account;
  if (acct?.account?.pubkey && acct.account.data) {
    const slot = Number(acct.slot);
    return {
      t: 'a',
      slot: Number.isFinite(slot) ? slot : null,
      key: enc(acct.account.pubkey),
      data: Buffer.from(acct.account.data).toString('base64'),
      sig: acct.account.txnSignature?.length ? enc(acct.account.txnSignature) : null,
    };
  }
  if (!(d as { transaction?: unknown } | null)?.transaction) return null;
  const mig = extractMigrateTx(d);
  if (mig) {
    const logs = ((d as { transaction: { transaction: { meta: { logMessages: string[] } } } }).transaction.transaction.meta.logMessages ?? []) as string[];
    const bal = (bs: typeof mig.pre) => bs.map((b) => ({ mint: b.mint, ...(b.owner ? { owner: b.owner } : {}), amount: b.amount.toString() }));
    return { t: 'x', slot: mig.slot, sig: mig.signature, err: !mig.ok, logs, key0: mig.caller, pre: bal(mig.pre), post: bal(mig.post) };
  }
  const tx = extractTransaction(d);
  if (tx && !tx.err && hasCreateLog(tx.logs)) {
    const pre = new Set(tx.preMints);
    const mints = [...new Set(tx.postBalances.map((b) => b.mint))].filter((m) => m !== WSOL_MINT && !pre.has(m));
    return mints.length ? { t: 'c', mints } : null;
  }
  return null;
}

/** Expand one compact frame into the update shape(s) the stream router reads. */
export function expandFrame(f: CompactFrame): unknown[] {
  if (f.t === 'a') {
    return [
      {
        account: {
          slot: f.slot === null ? undefined : String(f.slot),
          account: {
            pubkey: f.key,
            owner: PROGRAM_IDS.PUMP_FUN,
            data: Uint8Array.from(Buffer.from(f.data, 'base64')),
            ...(f.sig ? { txnSignature: base58Decode(f.sig) } : {}),
          },
        },
      },
    ];
  }
  if (f.t === 'x') {
    const bal = (bs: typeof f.pre) => bs.map((b) => ({ mint: b.mint, ...(b.owner ? { owner: b.owner } : {}), uiTokenAmount: { amount: b.amount } }));
    return [
      {
        transaction: {
          slot: f.slot === null ? undefined : String(f.slot),
          transaction: {
            signature: f.sig,
            transaction: { message: { accountKeys: f.key0 ? [f.key0] : [] } },
            meta: {
              err: f.err ? { InstructionError: [0, 'Custom'] } : null,
              logMessages: f.logs,
              preTokenBalances: bal(f.pre),
              postTokenBalances: bal(f.post),
            },
          },
        },
      },
    ];
  }
  // Creates: a minimal tx whose post balances name the new mints.
  return [
    {
      transaction: {
        transaction: {
          signature: 'create',
          transaction: { message: { accountKeys: [] } },
          meta: {
            err: null,
            logMessages: ['Program log: Instruction: Create'],
            preTokenBalances: [],
            postTokenBalances: f.mints.map((m) => ({ mint: m, uiTokenAmount: { amount: '1' } })),
          },
        },
      },
    },
  ];
}
