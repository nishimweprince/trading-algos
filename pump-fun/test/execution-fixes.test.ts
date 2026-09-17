import { describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { buySlippageAttempts } from '../src/executor/slippage.ts';
import { withTimeout, TimeoutError } from '../src/executor/timeout.ts';
import { describeBuyFailure } from '../src/positions/manager.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';
import { Executor } from '../src/executor/index.ts';
import type { RpcClient } from '../src/core/rpc.ts';

const base = (over: Partial<BroadcastResult>): BroadcastResult => ({
  mode: 'live',
  simulated: true,
  sent: false,
  confirmed: false,
  logs: [],
  attempts: [],
  ...over,
});

describe('buy slippage tiers are decoupled from the exit ladder', () => {
  it('config default retries once at 8%, never at the exit ladder 25%', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.entry.buyRetrySlippageTiers).toEqual([8]);
    expect(buySlippageAttempts(cfg.entry.maxSlippagePct, cfg.entry.buyRetrySlippageTiers)).toEqual([5, 8]);
    // The old behaviour, for contrast.
    expect(buySlippageAttempts(cfg.entry.maxSlippagePct, cfg.exits.ladderSlippageTiers)).toEqual([5, 10, 25]);
  });
  it('an empty tier list means one attempt at the configured max', () => {
    expect(buySlippageAttempts(5, [])).toEqual([5]);
  });
});

describe('describeBuyFailure names the stage that failed', () => {
  it('reports a failed pre-send simulate as such, with the program error', () => {
    const r = base({ sent: false, simErr: { InstructionError: [7, { Custom: 6004 }] } });
    expect(describeBuyFailure(r)).toBe('buy simulation failed: {"InstructionError":[7,{"Custom":6004}]}');
  });
  it('reports a real confirmation timeout as sent-but-unconfirmed', () => {
    const r = base({ sent: true, signature: 'sig', sendErr: 'confirmation timeout' });
    expect(describeBuyFailure(r)).toBe('buy sent but not confirmed: confirmation timeout');
  });
});

describe('withTimeout', () => {
  it('resolves fast promises untouched and rejects stalled ones', async () => {
    await expect(withTimeout(Promise.resolve(1), 50, 'x')).resolves.toBe(1);
    const never = new Promise<number>(() => {});
    await expect(withTimeout(never, 20, 'simulate')).rejects.toBeInstanceOf(TimeoutError);
  });
  it('0 disables the bound', async () => {
    await expect(withTimeout(Promise.resolve('ok'), 0, 'x')).resolves.toBe('ok');
  });
});

describe('Executor.reconcileTokenBalance retries the post-buy read', () => {
  function executorWith(reads: Array<bigint | null>) {
    const calls: Array<{ commitment: string | undefined }> = [];
    const rpc = {
      getTokenAccountBalance: async (_ata: string, commitment?: string) => {
        calls.push({ commitment });
        const v = reads.shift();
        return v === null || v === undefined ? null : { amount: v, decimals: 6 };
      },
      getSignatureStatuses: async () => [],
    } as unknown as RpcClient;
    const config = ConfigSchema.parse({
      mode: 'dry-run', // ephemeral wallet; nothing is sent
      rpc: { primaryHttp: 'http://127.0.0.1:1' },
      execution: { reconcileAttempts: 3, reconcileDelayMs: 1, stateCommitment: 'processed' },
    });
    return { exec: new Executor({ config, rpc, httpUrl: 'http://127.0.0.1:1' }), calls };
  }

  it('returns the balance once a later read sees it, at the state commitment', async () => {
    const { exec, calls } = executorWith([null, 0n, 12345n]);
    await expect(exec.reconcileTokenBalance('So11111111111111111111111111111111111111112')).resolves.toBe(12345n);
    expect(calls).toHaveLength(3);
    expect(calls.every((c) => c.commitment === 'processed')).toBe(true);
  });
  it('gives up after the configured attempts', async () => {
    const { exec, calls } = executorWith([null, null, null, 99n]);
    await expect(exec.reconcileTokenBalance('So11111111111111111111111111111111111111112')).resolves.toBe(0n);
    expect(calls).toHaveLength(3);
  });
});
