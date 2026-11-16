import { createHmac } from 'crypto';

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

/** Decode an RFC 4648 base32 secret (authenticator apps show it grouped and lower-case). */
export function base32Decode(secret: string): Buffer {
  const clean = secret.replace(/[\s-]/g, '').replace(/=+$/, '').toUpperCase();
  if (!clean || /[^A-Z2-7]/.test(clean)) {
    throw new Error('TOTP secret is not valid base32');
  }
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of clean) {
    value = (value << 5) | BASE32.indexOf(char);
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238 TOTP (HMAC-SHA1, 30 s step, 6 digits by default). */
export function totp(
  secret: string | Buffer,
  nowMs: number = Date.now(),
  options: { stepSeconds?: number; digits?: number } = {},
): string {
  const step = options.stepSeconds ?? 30;
  const digits = options.digits ?? 6;
  const key = typeof secret === 'string' ? base32Decode(secret) : secret;
  const counter = Math.floor(nowMs / 1000 / step);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const hmac = createHmac('sha1', key).update(message).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const code =
    ((hmac[offset] & 0x7f) << 24) |
    (hmac[offset + 1] << 16) |
    (hmac[offset + 2] << 8) |
    hmac[offset + 3];
  return String(code % 10 ** digits).padStart(digits, '0');
}

/** Seconds left in the current TOTP window; callers wait for a fresh one when it is short. */
export function totpSecondsRemaining(nowMs: number = Date.now(), stepSeconds = 30): number {
  return stepSeconds - (Math.floor(nowMs / 1000) % stepSeconds);
}
