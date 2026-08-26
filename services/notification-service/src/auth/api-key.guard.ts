import {
  CanActivate,
  ExecutionContext,
  Injectable,
  UnauthorizedException,
} from '@nestjs/common';
import { Request } from 'express';
import { AppConfigService } from '../config/config.service';

@Injectable()
export class ApiKeyGuard implements CanActivate {
  constructor(private readonly config: AppConfigService) {}

  canActivate(context: ExecutionContext): boolean {
    const expected = this.config.apiKey;
    if (!expected) return true;

    const request = context.switchToHttp().getRequest<Request>();
    const provided = this.extractKey(request);
    if (!provided || provided !== expected) {
      throw new UnauthorizedException('Invalid or missing API key');
    }
    return true;
  }

  private extractKey(request: Request): string | undefined {
    const header = request.headers['x-api-key'];
    if (typeof header === 'string' && header.trim()) {
      return header.trim();
    }

    const auth = request.headers.authorization;
    if (typeof auth === 'string' && auth.toLowerCase().startsWith('bearer ')) {
      return auth.slice(7).trim();
    }

    return undefined;
  }
}
