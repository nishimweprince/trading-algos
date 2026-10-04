import { describe, expect, it, beforeEach } from 'vitest';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Keypair, TransactionMessage, VersionedTransaction, SystemProgram } from '@solana/web3.js';
import { SignerGuard } from '../src/positions/signerGuard.ts';
import { WalletStream, type WalletTx } from '../src/positions/walletStream.ts';
import { TypedBus } from '../src/core/bus.ts';
import { isOwnSignature, recordOwnSignature, resetOwnSignatures } from '../src/core/ownSignatures.ts';
import { Wallet } from '../src/executor/wallet.ts';
import { base58Encode } from '../src/core/base58.ts';

const WALLET = 'Wa11et1111111111111111111111111111111111111';

const tx = (signature: string, feePayer = WALLET): WalletTx => ({
  signature, feePayer, err: null, solDeltaLamports: -5_000n, tokenDeltas: new Map(), seenAtMs: 0,
});

function harness(allowListPath = join(mkdtempSync(join(tmpdir(), 'guard-')), 'allow.txt')) {
  const bus = new TypedBus();
  const halts: string[] = [];
  const alerts: string[] = [];
  bus.on('alert', (a) => alerts.push(a.message));
  const guard = new SignerGuard({
    wallet: WALLET,
    watch: new WalletStream({ endpoint: '', wallet: WALLET }),
    bus,
    halt: (r) => halts.push(r),
    allowListPath,
    graceMs: 0,
  });
  return { guard, halts, alerts, allowListPath };
}

describe('foreign-signer guard', () => {
  beforeEach(() => resetOwnSignatures());

  it('ignores transactions this process signed', async () => {
    const { guard, halts } = harness();
    recordOwnSignature('MINE');
    await guard.check(tx('MINE'));
    expect(halts).toEqual([]);
  });

  it('halts entries and alerts on a wallet-paid tx nobody here signed', async () => {
    const { guard, halts, alerts } = harness();
    await guard.check(tx('THEIRS'));
    await guard.check(tx('THEIRS2'));
    expect(halts).toHaveLength(1); // halts once
    expect(alerts.filter((a) => a.includes('foreign signer'))).toHaveLength(2);
    expect(guard.foreign).toEqual(['THEIRS', 'THEIRS2']);
  });

  it('ignores transactions someone else paid for (transfers to us)', async () => {
    const { guard, halts } = harness();
    await guard.check(tx('AIRDROP', 'Someone1111111111111111111111111111111111'));
    expect(halts).toEqual([]);
  });

  it('accepts operator-CLI signatures from the allow-list file', async () => {
    const { guard, halts, allowListPath } = harness();
    writeFileSync(allowListPath, 'CLI_SIG\n');
    await guard.check(tx('CLI_SIG'));
    expect(halts).toEqual([]);
  });

  it('startup audit alerts on recent signatures the DB has never seen', async () => {
    const { guard, alerts, halts } = harness();
    const rpc = { getSignaturesForAddress: async () => [
      { signature: 'KNOWN', slot: 1, blockTime: null, err: null },
      { signature: 'STRANGER', slot: 2, blockTime: null, err: null },
    ] };
    const unknown = await guard.auditRecent(rpc, (s) => s === 'KNOWN', 50);
    expect(unknown).toEqual(['STRANGER']);
    expect(alerts.some((a) => a.includes('not in this bot'))).toBe(true);
    expect(halts).toEqual([]); // audit never halts
  });

  it('Wallet.sign records the signature as our own', () => {
    process.env.GUARD_TEST_KEY = base58Encode(Keypair.generate().secretKey);
    const wallet = Wallet.load('GUARD_TEST_KEY', 'live');
    const msg = new TransactionMessage({
      payerKey: wallet.keypair.publicKey,
      recentBlockhash: base58Encode(new Uint8Array(32).fill(7)),
      instructions: [SystemProgram.transfer({ fromPubkey: wallet.keypair.publicKey, toPubkey: wallet.keypair.publicKey, lamports: 1 })],
    }).compileToV0Message();
    const vtx = new VersionedTransaction(msg);
    wallet.sign(vtx);
    expect(isOwnSignature(base58Encode(vtx.signatures[0]!))).toBe(true);
    delete process.env.GUARD_TEST_KEY;
  });
});
