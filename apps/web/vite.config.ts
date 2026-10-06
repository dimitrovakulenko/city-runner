import { defineConfig, loadEnv } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxy = { ...(env.DISCOVERY_API_PROXY_TARGET ? Object.fromEntries(['/api/cities', '/api/streets'].map((path) => [path, { target: env.DISCOVERY_API_PROXY_TARGET }])) : {}), ...(env.ROUTES_API_PROXY_TARGET ? { '/api/routes': { target: env.ROUTES_API_PROXY_TARGET } } : {}), '/api': { target: env.API_PROXY_TARGET ?? 'http://127.0.0.1:8001' } };
  return {
    tsconfig: 'tsconfig.json',
    optimizeDeps: { rolldownOptions: { tsconfig: fileURLToPath(new URL('./tsconfig.json', import.meta.url)) } },
    server: {
      port: 5173, strictPort: true,
      fs: { allow: [fileURLToPath(new URL('../', import.meta.url))] },
      proxy,
    },
    preview: { port: 5173, strictPort: true, proxy },
  };
});
