import { ApplicationError } from './application-error.js';

export class ValidationError extends ApplicationError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('VALIDATION_FAILED', message, 400, details);
    this.name = 'ValidationError';
  }
}
