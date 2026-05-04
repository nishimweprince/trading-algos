import type { Request, Response } from 'express';

export type ChannelStatusRow = {
  channel: string;
  lastSeenMessageId: number;
  updatedAt: string;
};

export type StatusSnapshot = {
  startedAt: number;
  telegramConnected: boolean;
  lastTickAt: string | null;
  channels: ChannelStatusRow[];
  counters: {
    messagesPolledTotal: number;
    signalsDetectedTotal: number;
    smsAttemptsTotal: number;
    smsFailuresTotal: number;
  };
};

export function healthHandler(_req: Request, res: Response): void {
  res.json({ status: 'ok' });
}

export function statusHandler(snapshot: StatusSnapshot) {
  return (_req: Request, res: Response): void => {
    const uptimeSeconds = Math.floor((Date.now() - snapshot.startedAt) / 1000);
    res.json({
      uptimeSeconds,
      telegramConnected: snapshot.telegramConnected,
      lastTickAt: snapshot.lastTickAt,
      channels: snapshot.channels,
      counters: snapshot.counters,
    });
  };
}
