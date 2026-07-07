import type { IncomingHttpHeaders, IncomingMessage, ServerResponse } from 'node:http';
import { defineConfig, type Plugin } from 'vite';
import preact from '@preact/preset-vite';

const dashboardApi = 'http://127.0.0.1:8787';
type Next = (err?: unknown) => void;

export default defineConfig({
  plugins: [preact(), quietDashboardApi()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  preview: {
    allowedHosts: ['pumpdesk.nishimweprince.dev'],
  },
});

function quietDashboardApi(): Plugin {
  return {
    name: 'quiet-dashboard-api',
    configureServer(server) {
      server.middlewares.use(proxyDashboardApi);
    },
    configurePreviewServer(server) {
      server.middlewares.use(proxyDashboardApi);
    },
  };
}

async function proxyDashboardApi(req: IncomingMessage, res: ServerResponse, next: Next): Promise<void> {
  if (!req.url?.startsWith('/api')) {
    next();
    return;
  }

  const target = new URL(req.url, dashboardApi);

  try {
    const apiRes = await fetch(target, {
      method: req.method,
      headers: cleanProxyHeaders(req.headers),
    });

    res.statusCode = apiRes.status;
    apiRes.headers.forEach((value, key) => res.setHeader(key, value));

    if (apiRes.body) {
      for await (const chunk of apiRes.body) {
        res.write(chunk);
      }
    }
    res.end();
  } catch {
    if (req.url.startsWith('/api/stream')) {
      res.statusCode = 503;
      res.setHeader('content-type', 'text/event-stream');
      res.end('event: error\ndata: {"ok":false,"error":"dashboard_api_offline"}\n\n');
      return;
    }

    res.statusCode = 503;
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ ok: false, error: 'dashboard_api_offline' }));
  }
}

function cleanProxyHeaders(headers: IncomingHttpHeaders): Headers {
  const next = new Headers();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === 'host' || value === undefined) continue;
    if (Array.isArray(value)) {
      next.set(key, value.join(', '));
    } else {
      next.set(key, value);
    }
  }
  return next;
}
