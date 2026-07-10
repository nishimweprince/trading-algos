import { ApplicationError } from './application-error.js';

export class RiskRejectionError extends ApplicationError {
  constructor(code: string, message: string, details?: Record<string, unknown>) {
    super(code, message, 422, details);
    this.name = 'RiskRejectionError';
  }
}
