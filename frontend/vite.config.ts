import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: mode === 'demo' ? {
    'import.meta.env.VITE_PUBLIC_DEMO': JSON.stringify('true'),
    'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
  } : {},
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
}))
