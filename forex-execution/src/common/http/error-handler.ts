import { FastifyError, FastifyReply, FastifyRequest } from 'fastify';
import { ApplicationError } from '../errors/application-error.js';

export function errorHandler(error: FastifyError | Error, request: FastifyRequest, reply: FastifyReply): void {
  if (error instanceof ApplicationError) {
    request.log.warn({ code: error.code, details: error.details }, error.message);
    void reply.status(error.statusCode).send({ error: { code: error.code, message: error.message, requestId: request.id, details: error.details } });
    return;
  }
  request.log.error({ err: error }, 'Unhandled error');
  void reply.status(500).send({ error: { code: 'INTERNAL_SERVER_ERROR', message: 'Unexpected server error.', requestId: request.id } });
}
