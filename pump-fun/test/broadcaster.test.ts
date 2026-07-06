import { describe, it, expect, vi } from 'vitest';
import { Broadcaster, BroadcastError, type TxSender } from '../src/executor/broadcaster.ts';

function sender(name: string, simErr: unknown = null): TxSender & { simulate: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> } {
  return {
    name,
    simulate: vi.fn(async () => ({ err: simErr, logs: ['log'] })),
    send: vi.fn(async () => `sig-${name}`),
  };
}

const TX = new Uint8Array([1, 2, 3]);

describe('Broadcaster mode gating (safety keystone)', () => {
  it('paper mode throws and NEVER touches a sender', async () => {
    const s = sender('primary');
    const b = new Broadcaster('paper', [s]);
    await expect(b.broadcast(TX, 'buy')).rejects.toBeInstanceOf(BroadcastError);
    expect(s.simulate).not.toHaveBeenCalled();
    expect(s.send).not.toHaveBeenCalled();
  });

  it('dry-run simulates but NEVER sends', async () => {
    const s = sender('primary');
    const b = new Broadcaster('dry-run', [s]);
    const r = await b.broadcast(TX, 'buy');
    expect(s.simulate).toHaveBeenCalledOnce();
    expect(s.send).not.toHaveBeenCalled();
    expect(r).toMatchObject({ simulated: true, sent: false });
  });

  it('live sends on all paths after a passing simulation', async () => {
    const a = sender('jito');
    const b2 = sender('rpc');
    const b = new Broadcaster('live', [a, b2]);
    const r = await b.broadcast(TX, 'buy');
    expect(a.send).toHaveBeenCalledOnce();
    expect(b2.send).toHaveBeenCalledOnce();
    expect(r.sent).toBe(true);
    expect(r.signature).toMatch(/^sig-/);
  });

  it('live refuses to send when simulation fails', async () => {
    const s = sender('primary', { InstructionError: [0, 'Custom'] });
    const b = new Broadcaster('live', [s]);
    const r = await b.broadcast(TX, 'buy');
    expect(s.send).not.toHaveBeenCalled();
    expect(r).toMatchObject({ sent: false, simErr: { InstructionError: [0, 'Custom'] } });
  });

  it('throws when no send paths are configured', async () => {
    const b = new Broadcaster('live', []);
    await expect(b.broadcast(TX, 'buy')).rejects.toBeInstanceOf(BroadcastError);
  });
});
