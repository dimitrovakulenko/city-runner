import { defineConfig, loadEnv } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxy = { '/api': { target: env.API_PROXY_TARGET ?? 'http://127.0.0.1:8001' } };
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
