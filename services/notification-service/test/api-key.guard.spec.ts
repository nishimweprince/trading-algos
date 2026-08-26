import { ExecutionContext, UnauthorizedException } from '@nestjs/common';
import { ApiKeyGuard } from '../src/auth/api-key.guard';
import { AppConfigService } from '../src/config/config.service';

function mockContext(headers: Record<string, string>): ExecutionContext {
  return {
    switchToHttp: () => ({
      getRequest: () => ({ headers }),
    }),
  } as ExecutionContext;
}

function mockConfig(apiKey?: string): AppConfigService {
  return { apiKey } as AppConfigService;
}

describe('ApiKeyGuard', () => {
  it('allows requests when no API key is configured', () => {
    const guard = new ApiKeyGuard(mockConfig(undefined));
    expect(guard.canActivate(mockContext({}))).toBe(true);
  });

  it('accepts valid Bearer token', () => {
    const guard = new ApiKeyGuard(mockConfig('secret'));
    expect(
      guard.canActivate(
        mockContext({ authorization: 'Bearer secret' }),
      ),
    ).toBe(true);
  });

  it('accepts valid X-API-Key header', () => {
    const guard = new ApiKeyGuard(mockConfig('secret'));
    expect(guard.canActivate(mockContext({ 'x-api-key': 'secret' }))).toBe(
      true,
    );
  });

  it('rejects invalid key', () => {
    const guard = new ApiKeyGuard(mockConfig('secret'));
    expect(() =>
      guard.canActivate(mockContext({ 'x-api-key': 'wrong' })),
    ).toThrow(UnauthorizedException);
  });
});
