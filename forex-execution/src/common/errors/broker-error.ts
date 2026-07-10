import { ApplicationError } from './application-error.js';

export class BrokerError extends ApplicationError {
  constructor(
    code: string,
    message: string,
    statusCode: number,
    public readonly brokerRequestId?: string,
    details?: Record<string, unknown>,
  ) {
    super(code, message, statusCode, details);
    this.name = 'BrokerError';
  }
}
