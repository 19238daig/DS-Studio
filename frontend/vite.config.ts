import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: process.env.DS_API_URL || 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (
            id.includes('node_modules') &&
            (id.includes('/antd/') || id.includes('/@ant-design/'))
          )
            return 'components';
          if (
            id.includes('node_modules') &&
            (id.includes('/react-markdown/') || id.includes('/remark-'))
          )
            return 'markdown';
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
  },
});
