import crypto from 'node:crypto';
import { FastifyReply, FastifyRequest } from 'fastify';
import { AppConfig } from '../../config/env.js';
import { ApplicationError } from '../errors/application-error.js';

function timingSafeEqualString(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) return false;
  return crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

export function createAuthenticationHook(config: AppConfig) {
  return async (request: FastifyRequest, _reply: FastifyReply): Promise<void> => {
    if (request.url.startsWith('/health/')) return;
    const provided = request.headers['x-internal-api-key'];
    if (typeof provided !== 'string' || !timingSafeEqualString(provided, config.INTERNAL_API_KEY)) {
      throw new ApplicationError('AUTHENTICATION_FAILED', 'Invalid internal API key.', 401);
    }
  };
}
