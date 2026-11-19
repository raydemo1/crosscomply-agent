import { createServer, type ViteDevServer } from 'vite';

/**
 * Vite server for loading TSX modules from node:test.
 *
 * Dependency discovery is off: the client-side optimizer scan is useless for
 * SSR loading and keeps the test process alive for tens of seconds.
 */
export function createSsrServer(mode?: string): Promise<ViteDevServer> {
  return createServer({
    mode,
    appType: 'custom',
    server: { middlewareMode: true, hmr: false, ws: false },
    optimizeDeps: { noDiscovery: true, include: [] },
  });
}
